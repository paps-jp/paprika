"""PerceptionResult — the eye's structured report on a page.

Phase 1 of the v2 architecture (see internal/v2-architecture.html).

The eye (Qwen3 or a vision LLM) generates this; the brain (DeepSeek-R1)
consumes it. The schema is the API contract between the two: by fixing it
here, we force perception to be pure observation (no judgment) and force
reasoning to be grounded in structured facts (no raw HTML/screenshot
inspection).

Design notes:
  * All fields are optional or have safe defaults so a partial / failed
    LLM call can still produce a valid object (with low confidence).
  * ``free_observation`` and ``anomalies`` are the schema's safety valves:
    when the eye observes something it can't classify, it puts it there
    rather than forcing it into a wrong enum slot.
  * Enums are intentionally narrow at v0. Add values as real sites demand
    them; do NOT preemptively widen the enums or the eye will hallucinate
    classifications.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums -- keep narrow at v0; widen only when a real site forces it.
# ---------------------------------------------------------------------------

PageKind = Literal[
    "video_page",   # primary purpose is playing a video
    "gallery",      # list / thumbnail grid of images or videos
    "login",        # login form is the primary content
    "age_gate",     # an age-confirmation page (nothing else visible)
    "cloudflare",   # Cloudflare interstitial / JS challenge
    "error",        # 404 / 503 / blank
    "unknown",      # eye couldn't classify -- check free_observation
]


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


VideoSrcKind = Literal[
    "blob_mse",      # MSE blob URL (HLS/DASH via JS player)
    "direct_mp4",    # plain <video src="...mp4">
    "iframe_embed",  # video inside an iframe (YouTube embed etc.)
]


# ---------------------------------------------------------------------------
# Sub-objects
# ---------------------------------------------------------------------------


class PageClassification(BaseModel):
    """The eye's best guess at the page's type.

    Single value (not multi-label) to keep Qwen3's output stable. If the
    eye is torn between two kinds, it should pick the more likely one
    and lower confidence, OR set value="unknown" with high confidence
    and explain in free_observation.
    """

    value: PageKind = "unknown"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    why: list[str] = Field(
        default_factory=list,
        description="Short evidence strings (1-3 items). E.g. ['<video> element present', 'play button visible'].",
    )


class BarrierActionable(BaseModel):
    """How to interact with a barrier, if the eye could spot a way."""

    selector: str | None = None
    text: str | None = None


class Barrier(BaseModel):
    """A single thing in the way of the goal (age-gate, cloudflare, etc.).

    Multiple barriers can coexist on one page; the eye reports them all and
    the brain decides which to handle first.
    """

    kind: BarrierKind
    evidence: str = Field(
        default="",
        description="What the eye saw that made it call this a barrier.",
    )
    actionable: BarrierActionable | None = None


class Video(BaseModel):
    """A video element / stream the eye spotted on the page."""

    kind: VideoSrcKind
    hls_url: str | None = None
    selector: str | None = None


class Links(BaseModel):
    """Aggregate link counts (not enumerated -- crawl context cue)."""

    to_same_host_count: int = 0
    external_count: int = 0


class Content(BaseModel):
    """What's *on* the page, regardless of barriers in the way."""

    videos: list[Video] = Field(default_factory=list)
    images_count: int = 0
    links: Links = Field(default_factory=Links)
    has_pagination: bool = False


class ProgressSignals(BaseModel):
    """Objective facts useful for job-progress reasoning.

    These are NOT the eye's judgment; they are observations that happen
    to be expressible as bools / ints. The brain uses them to decide
    whether a Playbook step succeeded.
    """

    url_changed_from_previous: bool = False
    page_loaded: bool = True
    new_assets_since_last: int = 0
    stderr_has_error: bool = False


class Anomaly(BaseModel):
    """Something the eye observed but couldn't classify.

    Semi-structured (kind + description) so the system can group similar
    anomalies across hosts later, but the kind label is free-form (Qwen3
    invents it).
    """

    kind: str = Field(
        ...,
        description="Short label the eye invented (e.g. 'unknown_overlay').",
    )
    description: str = ""


# ---------------------------------------------------------------------------
# Top-level PerceptionResult
# ---------------------------------------------------------------------------


class PerceptionResult(BaseModel):
    """A single structured observation of a page.

    Produced by the eye (Qwen3 vision LLM) at one moment in time. The brain
    (R1) reads this -- and only this -- to decide what to do next; it does
    NOT see the raw screenshot or HTML.

    A job typically produces multiple PerceptionResults (one per step in a
    Playbook). They are saved to ``data/jobs/{job_id}/perceptions/`` for
    distillation later.
    """

    # ---- where / when ----------------------------------------------------
    perceived_at: datetime = Field(default_factory=datetime.utcnow)
    step_index: int = Field(
        default=0,
        description="0 = end-of-job snapshot. 1+ = mid-job Playbook steps.",
    )
    url: str
    host: str

    # ---- what the eye saw ------------------------------------------------
    page_kind: PageClassification = Field(default_factory=PageClassification)
    barriers: list[Barrier] = Field(default_factory=list)
    content: Content = Field(default_factory=Content)
    progress_signals: ProgressSignals = Field(default_factory=ProgressSignals)

    # ---- safety valves ---------------------------------------------------
    anomalies: list[Anomaly] = Field(default_factory=list)
    free_observation: str = Field(
        default="",
        description="Anything the eye observed but couldn't fit into the schema above.",
    )

    # ---- provenance ------------------------------------------------------
    model: str | None = Field(
        default=None,
        description="Identifier of the LLM that produced this (e.g. 'qwen2.5-vl-72b').",
    )
    duration_ms: int | None = Field(
        default=None,
        description="Wall time the perception call took.",
    )
