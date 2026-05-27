"""HostKnowledge — the per-site unified knowledge store (v2 architecture).

Phase 2 of the v2 architecture (see ``internal/v2-architecture.html``).

Replaces the three fragmented stores of v1:

  * ``data/hosts/{host}.json``           -- cookies, recipes, popup behaviour
  * ``data/conventions/{tier}/*.json``   -- coding rules (site-specific ones)
  * ``data/skills/{tier}/*.json``        -- reusable scripts (site-specific ones)

Cookies remain on HostRecord (operator-edited, frequent updates).  Generic
patterns (kind=skill/rule) stay in their own store; HostKnowledge only
absorbs SITE-SPECIFIC distilled knowledge.

Each section is independently readable/writable so the four producers
(distiller, migration script, operator UI, perception observation) don't
fight each other.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums -- align with PerceptionResult's enums where they overlap so the
# brain can route directly from a PerceptionResult.barriers[].kind to a
# HostKnowledge.per_page.barriers[kind] lookup without translation.
# ---------------------------------------------------------------------------

BarrierKind = Literal[
    "cloudflare_challenge",
    "age_gate",
    "login_wall",
    "cookie_banner",
    "captcha",
    "paywall",
    "region_block",
    "popup_overlay",
]

PageKindHint = Literal[
    "video_page",
    "gallery",
    "login",
    "age_gate",
    "cloudflare",
    "image_post",
    "list_page",
    "detail_page",
    "unknown",
]

# How a barrier or content type is handled.
StrategyKind = Literal["click", "tool", "sequence", "manual", "passive_capture"]

# Site-level navigation style. Hints the crawler how to walk pages.
NavigationKind = Literal[
    "pagination",       # numbered page links / "next" button
    "infinite_scroll",  # JS appends new items on scroll
    "sitemap",          # /sitemap.xml or robots.txt sitemap
    "unknown",
]

# URL-pattern role within site structure.
UrlRole = Literal["landing", "list_page", "detail_page", "navigation", "other"]

PopupPolicy = Literal["kill", "follow", "ignore"]

ConfidenceTier = Literal["low", "medium", "high", "stale"]


# ---------------------------------------------------------------------------
# Barrier subtypes -- finer-grained classification within a BarrierKind.
#
# Used by R1 Distiller to record "this host's cloudflare_challenge is
# specifically a Turnstile checkbox" or "this host IP-bans us; only a
# proxied fetch works". Drives the pre-flight plugin auto-invocation
# at job dispatch (server/hub/routes/jobs.py::_consult_host_knowledge).
#
# Open string (not Literal) on purpose -- subtype taxonomy will grow as
# R1 discovers new patterns; we don't want a schema migration each time.
# Conventional values we expect today:
#
#   cloudflare_challenge:
#     - "js_challenge"        -- 5-second auto-clear via real Chrome
#     - "turnstile"           -- needs visible checkbox click (vision agent)
#     - "managed_challenge"   -- variable; try Chrome first, vision fallback
#     - "ip_banned"           -- 1020/1015 page; needs proxied egress
#
#   age_gate / login_wall / cookie_banner: kind-specific freeform tags
# ---------------------------------------------------------------------------
BarrierSubtype = str


# ---------------------------------------------------------------------------
# Verification tracking (per-field)
# ---------------------------------------------------------------------------


class Verified(BaseModel):
    """Per-field confidence tracking.

    Bumped whenever a job successfully exercises the field's strategy.
    Used by the maturity evaluator (see ``evaluate_maturity``) to decide
    if a piece of knowledge is fresh enough to trust without R1 review.
    """

    count: int = 0
    last_at: datetime | None = None
    success_rate: float | None = Field(default=None, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Strategies -- discriminated by ``kind``.
# ---------------------------------------------------------------------------


class StrategyStep(BaseModel):
    """One atomic action inside a sequence strategy."""

    action: str  # "click" | "wait" | "scroll" | "navigate" | ...
    selector: str | None = None
    ms: int | None = None
    url: str | None = None
    text: str | None = None


class Strategy(BaseModel):
    """How to handle a barrier or extract content.

    ``kind`` discriminates the union; the other fields are populated
    according to kind. Pydantic does not enforce the discrimination at
    validation time -- we keep it relaxed because the brain (R1) writes
    these structures and we want forwards-compat.
    """

    kind: StrategyKind

    # kind == "click"
    selector: str | None = None

    # kind == "tool"
    tool: str | None = None
    params: dict = Field(default_factory=dict)

    # kind == "sequence"
    steps: list[StrategyStep] = Field(default_factory=list)

    # kind == "manual"
    instruction: str | None = None

    # kind == "passive_capture"
    wait_ms: int | None = None
    scroll_to_load: bool | None = None


# ---------------------------------------------------------------------------
# per_page section
# ---------------------------------------------------------------------------


class BarrierKnowledge(BaseModel):
    """What we know about a single barrier on this host.

    ``present: false`` is meaningful -- it records "we *looked* for this
    barrier and didn't find one", which is different from "we don't
    know". Used by R1 to skip pre-emptive checks for confirmed-absent
    barriers.

    ``subtype`` and ``suggested_tool`` are populated by R1 Distiller
    after observing how previous jobs cleared (or failed to clear) this
    barrier. They drive the pre-flight plugin auto-invocation at job
    dispatch: e.g. cloudflare_challenge + subtype=ip_banned + suggested_tool=
    paprika-proxy-fetch tells the dispatcher to fetch cookies through the
    proxy plugin before the Worker even loads the page.

    ``tool_params`` carries plugin-specific overrides (wait_s, profile,
    proxy URL, ...) so the dispatcher can reuse them verbatim.
    """

    present: bool = True
    strategy: Strategy | None = None
    verified: Verified = Field(default_factory=Verified)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    notes: str | None = None

    # v2 Phase 7: barrier-subtype + suggested plugin (auto-invoked pre-flight).
    subtype: BarrierSubtype | None = None
    suggested_tool: str | None = None
    tool_params: dict = Field(default_factory=dict)


class ContentExtraction(BaseModel):
    """How to extract content from a URL pattern.

    Direct successor of ``HostRecipe.fetch_recipes[*]`` -- URL pattern
    determines which content type the page is, and the strategy says
    how to grab it.
    """

    url_pattern: str  # fnmatch-style glob, evaluated in order
    page_kind: PageKindHint = "unknown"
    strategy: Strategy
    verified: Verified = Field(default_factory=Verified)
    notes: str | None = None


class NavigationHints(BaseModel):
    """Per-page behavioural quirks of this host."""

    lazy_load_trigger_needed: bool | None = None
    wait_after_load_ms: int | None = None
    popup_policy: PopupPolicy | None = None
    common_observations: list[str] = Field(default_factory=list)


class PerPage(BaseModel):
    """Page-level knowledge: what's *on* a single page and how to handle it.

    Keyed-by-barrier-kind dict so absence/presence/history of each
    barrier type is queryable without scanning a list.
    """

    barriers: dict[BarrierKind, BarrierKnowledge] = Field(default_factory=dict)
    content_extraction: list[ContentExtraction] = Field(default_factory=list)
    navigation_hints: NavigationHints = Field(default_factory=NavigationHints)


# ---------------------------------------------------------------------------
# site_structure section
# ---------------------------------------------------------------------------


class UrlPattern(BaseModel):
    """URL-pattern → site-role mapping. Order matters (priority)."""

    pattern: str
    role: UrlRole


class SiteNavigation(BaseModel):
    """How crawlers move between pages on this site."""

    kind: NavigationKind = "unknown"
    next_button_selector: str | None = None
    page_url_template: str | None = None  # e.g. "/category?page={N}"
    max_observed_pages: int | None = None


class SiteStructure(BaseModel):
    """Site-level structure: entry points, patterns, navigation style."""

    entry_urls: list[str] = Field(default_factory=list)
    url_patterns: list[UrlPattern] = Field(default_factory=list)
    navigation: SiteNavigation = Field(default_factory=SiteNavigation)
    frontier_patterns: list[str] = Field(default_factory=list)
    leaf_patterns: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# crawl_strategies section
# ---------------------------------------------------------------------------


class CrawlLimits(BaseModel):
    """Bounded resource constraints for a crawl strategy."""

    max_pages: int | None = None
    max_minutes: int | None = None
    max_assets: int | None = None


class CrawlStrategy(BaseModel):
    """A named crawl plan -- 'how to systematically explore this site'.

    Stored as ``crawl_strategies[name] = CrawlStrategy(...)`` so multiple
    strategies can coexist (all_videos / recent_only / specific_category).
    """

    description: str = ""
    start_from: str  # URL or path
    follow_patterns: list[str] = Field(default_factory=list)
    extract_from: list[str] = Field(default_factory=list)
    limits: CrawlLimits = Field(default_factory=CrawlLimits)


# ---------------------------------------------------------------------------
# auth section -- LIGHT reference. Cookies stay on HostRecord.
# ---------------------------------------------------------------------------


class LoginCheck(BaseModel):
    """How to tell whether the current session is still logged in."""

    url: str | None = None
    expect_text: str | None = None


class AuthInfo(BaseModel):
    """Authentication metadata. NOT the cookies -- those stay on HostRecord."""

    profile_slug: str | None = None
    requires_login: bool = False
    login_check: LoginCheck | None = None


# ---------------------------------------------------------------------------
# tools section
# ---------------------------------------------------------------------------


class ToolRequirement(BaseModel):
    """A reference to a Tool in the ToolRegistry, plus optional version constraint."""

    name: str
    version: str | None = None  # semver range, e.g. ">=2026.1.0"


class ToolsConfig(BaseModel):
    """What tools this host needs / prefers.

    Three tiers:
      * required  -- job fails if not available
      * preferred -- used when available, fallback otherwise
      * optional  -- used opportunistically
    """

    required: list[ToolRequirement] = Field(default_factory=list)
    preferred: list[ToolRequirement] = Field(default_factory=list)
    optional: list[ToolRequirement] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# stats section
# ---------------------------------------------------------------------------


class Stats(BaseModel):
    """Aggregate success/failure history for this host.

    Driven by job completions. The maturity evaluator reads from here to
    decide tier (low/medium/high/stale).
    """

    total_jobs: int = 0
    successful_jobs: int = 0
    success_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_reason: str | None = None
    overall_confidence: ConfidenceTier = "low"


# ---------------------------------------------------------------------------
# Provenance (lightweight; full history goes to history.jsonl)
# ---------------------------------------------------------------------------


class Provenance(BaseModel):
    """Lightweight pointer to the last updater. Full audit in history.jsonl."""

    last_updated_by: str | None = None
    last_updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Top-level HostKnowledge
# ---------------------------------------------------------------------------


class HostKnowledge(BaseModel):
    """The unified per-host knowledge object.

    One per host. Stored at ``data/host_knowledge/{host}.json``.  All
    sections start empty; the distiller fills them over time as jobs
    accumulate observation.
    """

    host: str
    schema_version: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    per_page: PerPage = Field(default_factory=PerPage)
    site_structure: SiteStructure = Field(default_factory=SiteStructure)
    crawl_strategies: dict[str, CrawlStrategy] = Field(default_factory=dict)
    auth: AuthInfo = Field(default_factory=AuthInfo)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    stats: Stats = Field(default_factory=Stats)
    provenance: Provenance = Field(default_factory=Provenance)


# ---------------------------------------------------------------------------
# Maturity evaluation
# ---------------------------------------------------------------------------


_STALE_DAYS = 30
_RECENT_FAILURE_WINDOW_H = 24
_HIGH_JOBS_MIN = 10
_HIGH_SUCCESS_MIN = 0.9
_HIGH_FRESH_DAYS = 7
_MEDIUM_JOBS_MIN = 3
_MEDIUM_SUCCESS_MIN = 0.7


def evaluate_maturity(knowledge: HostKnowledge, *, now: datetime | None = None) -> ConfidenceTier:
    """Decide how much R1 supervision a job on this host needs.

    Tier semantics:
      * low      -- new or unreliable: R1 verifies each step
      * medium   -- building: Playbook runs, R1 only on mismatch
      * high     -- trusted: Playbook flies, R1 only for final Judge
      * stale    -- old or recently failing: re-verify before trusting

    See ``internal/v2-architecture.html § confidence`` for the thresholds
    and reasoning. These are v0 numbers; tune via env overrides later.
    """
    s = knowledge.stats
    now = now or datetime.utcnow()

    last = s.last_success_at
    if last is None:
        days_since_verify = 1e9  # never succeeded
    else:
        days_since_verify = (now - last).total_seconds() / 86400.0

    # Stale: time-based decay first.
    if days_since_verify > _STALE_DAYS:
        return "stale"

    # High: lots of evidence, fresh, high success rate.
    if (
        s.total_jobs >= _HIGH_JOBS_MIN
        and s.success_rate >= _HIGH_SUCCESS_MIN
        and days_since_verify <= _HIGH_FRESH_DAYS
    ):
        return "high"

    # Medium: some evidence, OK success rate.
    if s.total_jobs >= _MEDIUM_JOBS_MIN and s.success_rate >= _MEDIUM_SUCCESS_MIN:
        return "medium"

    # Low: anything else.
    return "low"
