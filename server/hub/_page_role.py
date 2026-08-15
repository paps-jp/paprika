"""Per-host URL-template page-role inference.

Classify a URL into one of ``detail`` / ``listing`` / ``top`` / ``error`` /
``unknown`` by normalising the path into a TEMPLATE (each variable segment
becomes ``{int}/{slug}/{code}/...``) and looking it up against the host's
recent observations.

Why: ~28% of escalated codegen-loop jobs in the recent window were NOT
detail pages (tag/category/search listings, error/about, soft-404). They
have nothing for the AI to "recover" -- escalating them is wasted lane
time. A per-host template lookup catches these cheaply and lets the
escalator skip them.

This module is *observational*: it groups the host's job history by
template + tracks a video-evidence count per template + lets the caller
ask "what's the role of this URL on this host?". The hub keeps a small
in-process cache; first lookup for a host pulls the recent history.
"""
from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from typing import Iterable
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Template normalisation -- pure, no I/O
# ---------------------------------------------------------------------------

_TOK_INT = re.compile(r"^\d+$")
_TOK_YR = re.compile(r"^(19|20)\d{2}$")
_TOK_MO = re.compile(r"^(0?[1-9]|1[0-2])$")
_TOK_UUID = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.I
)
_TOK_HEX = re.compile(r"^[a-f0-9]{6,}$", re.I)
# JAV-style "abc-123" / "atid00326" product codes.
_TOK_CODE = re.compile(r"^[A-Za-z]{2,6}[-_]?\d{2,6}[A-Za-z]?$")

# Static listing keywords. A non-variable segment matching one of these means
# the path is a category / tag / search / archive page.
# Category-segment keywords: a path like /category/foo or /genre/bar is a
# CATEGORY page (a specific category's listing). Stronger semantic than the
# generic 'listing' label so operators / fetch_recipes can target it.
_STATIC_CATEGORY = frozenset({
    "category", "categories", "cat", "genre", "genres", "section", "sections",
    "channel", "channels", "department",
})

# Tag-segment keywords: a path like /tag/foo or /tags/bar is a TAG page
# (a specific tag's listing). Distinguished from category so the operator
# can see at a glance whether something is broad-topic vs narrow-keyword.
_STATIC_TAG = frozenset({
    "tag", "tags", "topic", "topics", "kw", "keyword", "keywords",
    "hashtag", "hashtags",
})

_STATIC_LISTING = frozenset({
    "cast", "actress", "actresses", "maker", "label", "studio", "series",
    "model", "models", "star", "stars", "actor", "actors", "director",
    "search", "page", "paged",
    # NOTE: 'archive(s)' is intentionally NOT in this set -- many CMSs use it
    # as the parent path for detail items (/archives/{int}/), so it's only a
    # listing in combination with pagination, which the page-co-occurrence
    # rule catches separately.
    "list", "ranking", "popular", "recent", "author", "authors",
    "following", "followers", "friends", "profile", "members", "user", "users",
})

# Static error / non-content keywords. A path matching these is informational
# or navigational, not a content page.
_STATIC_ERROR = frozenset({
    "404", "error", "contact", "about", "privacy", "terms", "tos", "dmca",
    "2257", "help", "faq", "sitemap", "feed", "rss", "login", "signup",
    "register", "cart", "checkout",
})

# Query-key keywords. Many CMSes (especially WordPress) route navigation
# through query parameters instead of clean URL segments. Without these,
# `/?category=foo&page=2` looks like the top page (path is just `/`) and
# gets misclassified as `top` with high confidence.
# Role precedence mirrors the path-side: category > tag > listing > detail.
_QUERY_CATEGORY = frozenset({
    "category", "category_name", "categories", "cat", "cat_id",
    "genre", "section", "channel", "department",
})
_QUERY_TAG = frozenset({
    "tag", "tag_name", "tags", "topic", "topics", "hashtag",
    "keyword", "keywords", "kw",
})
_QUERY_LISTING = frozenset({
    "page", "paged", "pg", "author", "author_name", "user_id",
    "s", "q", "query", "search", "k",
})
_QUERY_DETAIL = frozenset({
    # WordPress-style ?p=123 / ?post=123 detail keys. Only treated as
    # detail when the path is at or near the root (a deeper path with
    # ?post=... is usually a listing carrying a referrer).
    "p", "post", "post_id", "post_name", "item", "item_id",
})

# Keys that templatize() folds into the template signature. Excludes
# opaque/tracking keys (utm_*, ref, fbclid, gclid, ...) that would
# explode the template space without helping classification.
_NAV_QUERY_KEYS = (
    _QUERY_CATEGORY | _QUERY_TAG | _QUERY_LISTING | _QUERY_DETAIL
)


def _classify_token(t: str) -> str:
    if not t:
        return ""
    # Order matters: 4-digit years and 1-2 digit months would otherwise be
    # caught by the bare-integer rule before their more-specific ones fire.
    if _TOK_YR.match(t):
        return "{year}"
    if _TOK_MO.match(t):
        return "{month}"
    if _TOK_INT.match(t):
        return "{int}"
    if _TOK_UUID.match(t):
        return "{uuid}"
    if _TOK_HEX.match(t):
        return "{hex}"
    if _TOK_CODE.match(t):
        return "{code}"
    if any(ch.isdigit() for ch in t) and len(t) >= 5:
        return "{id}"
    # Multi-word slugs (kebab-case) AND non-ASCII (Japanese in path).
    if len(t) >= 2 and "-" in t and t.replace("-", "").isalnum():
        return "{slug}"
    if len(t) >= 2 and not t.isascii():
        return "{slug}"
    return t  # keep static keywords (tag/category/page/...) verbatim


def templatize(url: str) -> str:
    """Normalise a URL's path (and known nav query keys) into a template.

    Variable segments collapse to ``{int}/{slug}/{code}/{id}/{uuid}/{hex}``;
    static keywords (``tag``, ``page``, ...) stay verbatim. Returns ``/`` for
    the bare top URL. Trailing slash is normalised so ``/foo`` and ``/foo/``
    share a template.

    Known navigation query keys (``page``, ``category``, ``tag``, ``s``,
    WordPress ``p``, ...) are folded into the template (sorted by key,
    values tokenised) so CMS sites that route through query parameters
    rather than path segments don't all collapse to ``/``.

    Examples::

        /tag/itsuki-kitagawa/      -> /tag/{slug}/
        /tag/big/page/3/           -> /tag/big/page/{int}/
        /2024/03/abc-123.html      -> /{year}/{month}/abc-123.html/
        /v/Xy7Az                   -> /v/{id}/
        /                          -> /
        /?page=4&category=foo      -> /?category={slug}&page={int}
        /?p=123                    -> /?p={int}
    """
    try:
        p = urlparse(url or "")
    except Exception:
        return ""
    path = (p.path or "/").rstrip("/") or "/"
    if path == "/":
        path_tpl = "/"
    else:
        segs = [s for s in path.split("/") if s]
        path_tpl = "/" + "/".join(_classify_token(s.lower()) for s in segs) + "/"
    if not p.query:
        return path_tpl
    try:
        from urllib.parse import parse_qsl
        qs = parse_qsl(p.query, keep_blank_values=True)
    except Exception:
        return path_tpl
    parts = []
    for k, v in sorted(qs, key=lambda kv: kv[0].lower()):
        kl = (k or "").lower()
        if kl not in _NAV_QUERY_KEYS:
            continue
        parts.append(f"{kl}={_classify_token((v or '').lower())}")
    if not parts:
        return path_tpl
    return path_tpl + "?" + "&".join(parts)


# ---------------------------------------------------------------------------
# Per-host stats + role inference
# ---------------------------------------------------------------------------

# Confidence thresholds. ``role_for_url`` returns ``("unknown", 0.0)`` below
# the low threshold so the escalator can still try; above the high threshold
# the role is trusted enough to skip escalation.
ROLE_TRUST_THRESHOLD = 0.85


def classify_template(
    tpl: str, *, n: int = 0, nv: int = 0, has_page_sibling: bool = False
) -> tuple[str, float, str]:
    """``(role, confidence, reason)`` for one URL template.

    Pure -- no I/O, no host state. Everything the classifier needs from a
    host's history is these three numbers:

    * ``n``  -- how many distinct URLs on this host templated to ``tpl``
    * ``nv`` -- how many of those actually yielded a video
    * ``has_page_sibling`` -- whether a template under the same path prefix
      carries a literal ``page`` segment (i.e. ``tpl`` is part of a
      paginated set)

    Rules 1-4b are pure keyword matching and need NO history at all, which
    is what lets the hot path fall back to ``classify_template(tpl)`` with
    zeroes when the rollup has no row yet: listing / category / tag / error
    / top still classify correctly, and only the observation-driven detail
    rules (5, 6) degrade to a weaker verdict until the next rollup pass.

    ``role`` is one of ``detail`` / ``listing`` / ``category`` / ``tag`` /
    ``top`` / ``error`` / ``unknown``. ``confidence`` in [0, 1]; the caller
    should compare it against ``ROLE_TRUST_THRESHOLD`` before acting
    (low confidence == treat as unknown, let normal escalation run).

    NOTE on saturation: the thresholds are ``n >= 3`` and ``nv >= 2``, and
    the ratio branch degenerates to ``nv == 1 and 3 <= n <= 6``. So the
    verdict is fully determined by ``min(n, 7)`` and ``min(nv, 2)`` -- the
    rollup does not need exact counts for correctness, it keeps them for
    operator visibility.
    """
    if not tpl:
        return "unknown", 0.0, "empty url"
    # Bare top: path is root AND no nav query keys folded in.
    if tpl == "/":
        return "top", 0.99, "top path"

    # Split path/query halves of the template so both sides feed the
    # keyword checks. ``?`` is only present when templatize() folded
    # in at least one nav query key.
    path_tpl, _sep, query_tpl = tpl.partition("?")
    qkeys: set[str] = set()
    if query_tpl:
        qkeys = {kv.split("=", 1)[0] for kv in query_tpl.split("&") if kv}

    segs = [s for s in path_tpl.strip("/").split("/") if s]
    statics = [s for s in segs if not s.startswith("{")]

    # 1) static error keyword (path-only; error pages don't use nav query)
    if any(s in _STATIC_ERROR for s in statics):
        return "error", 0.95, "static error keyword"
    # 2) static category / tag (path OR query) -- more specific than listing
    if any(s in _STATIC_CATEGORY for s in statics):
        return "category", 0.9, "category keyword"
    if qkeys & _QUERY_CATEGORY:
        return "category", 0.9, "category query key"
    if any(s in _STATIC_TAG for s in statics):
        return "tag", 0.9, "tag keyword"
    if qkeys & _QUERY_TAG:
        return "tag", 0.9, "tag query key"
    # 3) explicit pagination OR pagination co-occurrence (strongest listing)
    if has_page_sibling:
        return "listing", 0.95, "pagination"
    # 3b) query-side pagination / search / author -- strong listing too
    if qkeys & _QUERY_LISTING:
        return "listing", 0.9, "listing query key"
    # 4) static listing keyword
    if any(s in _STATIC_LISTING for s in statics):
        return "listing", 0.85, "listing keyword"
    # 4b) WP-style detail query key on a top-ish path (?p=123)
    if (qkeys & _QUERY_DETAIL) and len(segs) <= 1:
        return "detail", 0.85, "detail query key"
    # 5) host-observed video evidence on this template -> detail (strong)
    if nv >= 2 or (n >= 3 and nv >= max(1, int(n * 0.3))):
        return "detail", 0.95, f"video evidence ({nv}/{n})"
    # 6) variable segments + multiple observations -> probable detail
    var_segs = sum(1 for s in segs if s.startswith("{"))
    if var_segs >= 1 and n >= 3:
        return "detail", 0.6, f"variable segs ({n} obs)"
    if var_segs >= 1:
        return "detail", 0.4, "variable segs (few obs)"
    return "unknown", 0.3, "no signal"


def page_sibling_prefixes(templates: Iterable[str]) -> set[str]:
    """Prefix set for the pagination co-occurrence rule, built from a host's
    full template list. Mirrors ``HostPageRoles.observe``'s accumulation so
    the rollup pass and the in-memory path agree."""
    out: set[str] = set()
    for tpl in templates:
        segs = (tpl or "").partition("?")[0].strip("/").split("/")
        if "page" in segs:
            out.add("/".join(segs[: segs.index("page")]))
    return out


def has_page_sibling(tpl: str, prefixes: set[str]) -> bool:
    """Whether ``tpl`` is part of a paginated set, given a host's prefix set
    from :func:`page_sibling_prefixes`. Same logic as
    ``HostPageRoles._page_co_occurs``."""
    segs = (tpl or "").partition("?")[0].strip("/").split("/")
    if "page" in segs:
        return True
    for prefix in prefixes:
        psegs = prefix.split("/") if prefix else []
        if psegs == segs[: len(psegs)]:
            return True
    return False


class HostPageRoles:
    """Per-host template observations.

    Build once with ``observe(url, has_video_evidence=...)`` for each known
    URL on a host. Then ``role_for_url(url)`` returns (role, confidence)
    using the host's own statistics + the static-keyword heuristics.
    """

    __slots__ = ("templates", "video_seen", "pagination_prefixes")

    def __init__(self) -> None:
        # template -> count of URLs observed
        self.templates: Counter = Counter()
        # template -> count with positive video evidence
        self.video_seen: Counter = Counter()
        # set of "/{seg}/{seg}" prefixes that have a pagination sibling (a
        # template that includes a literal ``page`` segment under the same
        # prefix) -- used to mark *non-paginated* templates under the same
        # prefix as listings too.
        self.pagination_prefixes: set[str] = set()

    def observe(self, url: str, *, has_video_evidence: bool = False) -> None:
        t = templatize(url)
        if not t:
            return
        self.templates[t] += 1
        if has_video_evidence:
            self.video_seen[t] += 1
        # Track pagination prefixes (everything before a literal ``page`` seg).
        segs = t.strip("/").split("/")
        if "page" in segs:
            i = segs.index("page")
            self.pagination_prefixes.add("/".join(segs[:i]))

    def _page_co_occurs(self, tpl: str) -> bool:
        """True iff some *other* template under the same path prefix has a
        ``page`` segment -- i.e. this template is part of a paginated set.
        Delegates to the shared helper so this path and the rollup pass
        can't drift apart."""
        return has_page_sibling(tpl, self.pagination_prefixes)

    def role_for_url(self, url: str) -> tuple[str, float, str]:
        """Return ``(role, confidence, reason)`` for ``url``.

        Thin wrapper over :func:`classify_template`: pulls this host's
        observations for the URL's template out of the in-memory counters
        and hands them to the shared classifier. Used by the bulk-loaded
        path (host edit modal / nightly review); the job-submit hot path
        goes through ``role_for_url()`` at module level, which reads the
        same three numbers from the pre-computed ``host_template_roles``
        rollup instead of building the whole table.
        """
        tpl = templatize(url)
        return classify_template(
            tpl,
            n=int(self.templates.get(tpl, 0)),
            nv=int(self.video_seen.get(tpl, 0)),
            has_page_sibling=self._page_co_occurs(tpl.partition("?")[0]),
        )


# ---------------------------------------------------------------------------
# Hub-side cache + role lookup
# ---------------------------------------------------------------------------

# host -> (built_at, HostPageRoles). TTL keeps the table fresh as new URLs
# come in without rebuilding on every call. Process-local; under nginx
# round-robin each hub builds its own copy from the SAME job history, so
# they converge.
_CACHE_TTL_S = 600.0  # 10 min
_cache: dict[str, tuple[float, HostPageRoles]] = {}


def _normalise_host_str(host: str) -> str:
    h = (host or "").strip().lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def host_from_url(url: str) -> str:
    try:
        h = urlparse(url or "").hostname or ""
    except Exception:
        return ""
    return _normalise_host_str(h)


async def get_host_roles(host: str) -> HostPageRoles:
    """Return a (cached) HostPageRoles for ``host``, building from the
    durable ``host_url_history`` MariaDB table on first call / after TTL
    expiry. Falls back to the rolling jobs window when MariaDB is empty for
    this host (cold-start). Any error returns an empty object so the caller
    behaves like "unknown role"."""
    h = _normalise_host_str(host)
    now = time.time()
    c = _cache.get(h)
    if c and (now - c[0]) < _CACHE_TTL_S:
        return c[1]
    roles = HostPageRoles()
    try:
        from server.hub._state import state
        # Primary source: durable host_url_history (survives jobs-table purge).
        if getattr(state, "mariadb_pool", None) is not None:
            try:
                from server.hub.mariadb import fetch_host_url_history
                rows = await fetch_host_url_history(state.mariadb_pool, h, limit=2000)
                for (url, _tpl, vid, _hit) in rows:
                    roles.observe(url, has_video_evidence=bool(vid))
            except Exception:
                pass
        # Cold start (or MariaDB down): seed from the rolling jobs window so
        # the host has SOME signal until completions populate the durable table.
        if not roles.templates and state.store is not None:
            jobs, _ = await state.store.list_job_infos(
                url_substr=h, limit=400,
            )
            for j in jobs:
                u = getattr(j, "url", "") or ""
                if not u or host_from_url(u) != h:
                    continue
                vid = False
                try:
                    r = getattr(j, "result", None)
                    if r is not None:
                        vid = bool(getattr(r, "video_detection", None)) or bool(
                            getattr(r, "video_urls_seen", None)
                        )
                except Exception:
                    vid = False
                roles.observe(u, has_video_evidence=vid)
    except Exception:
        pass
    _cache[h] = (now, roles)
    return roles


def observe_url(host: str, url: str, *, has_video_evidence: bool = False) -> None:
    """Live update the host's role table without rebuilding from the store.
    Called from the job-completion hook so a freshly-finished URL is part of
    the next role decision. No-op when the host hasn't been cached yet."""
    h = _normalise_host_str(host)
    c = _cache.get(h)
    if c is not None:
        c[1].observe(url, has_video_evidence=has_video_evidence)


def record_url(url: str, *, has_video_evidence: bool = False) -> None:
    """Fire-and-forget: persist ``url`` to ``host_url_history`` and update
    the in-process cache. Called from the job-completion hook so the per-host
    URL set accumulates durably (survives the jobs-table purge that bounds
    ``get_host_roles``' fallback). Never raises; failures are silent so a
    transient MariaDB hiccup can't break completion handling.
    """
    import asyncio
    h = host_from_url(url)
    if not h or not url:
        return
    # 1) In-process cache: surface immediately on the next role lookup.
    observe_url(h, url, has_video_evidence=has_video_evidence)
    # 2) Durable write-through (best-effort, off the completion fast path).
    try:
        from server.hub._state import state
        pool = getattr(state, "mariadb_pool", None)
        if pool is None:
            return
        tpl = templatize(url)
        from server.hub.mariadb import record_host_url_row
        asyncio.create_task(
            record_host_url_row(
                pool, host=h, url=url, template=tpl,
                has_video_evidence=has_video_evidence,
            )
        )
    except Exception:
        pass


async def role_for_url(url: str) -> tuple[str, float, str]:
    """Convenience: classify ``url`` using its host's role table.

    Operator-set per-host-template overrides (table
    ``host_url_role_overrides``) win over the URL heuristic: a single edit
    in the Live job panel / host edit modal corrects EVERY future job whose
    URL templates to the same value. Best-effort: a transient DB hiccup
    falls through to the heuristic.

    Hot path (2026-08-14). This runs inside ``POST /jobs`` for every crawl
    submission, so it must be a point lookup -- it reads the pre-computed
    ``host_template_roles`` rollup (PK ``(host, template_hash)``) instead of
    building the host's whole template table.

    It used to call ``get_host_roles()``, which on a cache miss pulled 2000
    rows of ``host_url_history`` (or, for a host with no history yet, ran
    ``url LIKE '%host%'`` across the 1.5M-row jobs table -- two full scans,
    ~12s measured). With a 10-minute per-process TTL, seven hubs and no
    single-flight guard, that put a 10-45s cold path in front of job
    creation and pinned the submitter's 48 POST slots: the fleet sat at 48%
    utilisation with zero queued jobs while intake capped at ~4 jobs/s.
    Measured cold POST /jobs latencies before this change: 1.7 / 2.8 / 6.8 /
    7.3 / 9.2 / 10.5 / 14.3 / 30.6 / 45(timeout) / 45(timeout) seconds;
    the same URL resubmitted warm took 0.04s.

    Rollup miss (a template nobody has fetched since the last pass) falls
    back to the keyword-only rules -- no I/O, and listing / category / tag /
    error / top still classify correctly. Only the observation-driven detail
    verdict waits for the next rollup pass. ``get_host_roles`` stays for the
    host edit modal + nightly review, which genuinely want the whole table.
    """
    h = host_from_url(url)
    if not h:
        return "unknown", 0.0, "no host"
    tpl = templatize(url)
    try:
        ov = await _host_overrides_cached(h)
        if ov and tpl in ov:
            return ov[tpl], 1.0, "operator override"
    except Exception:
        pass
    stats = await _template_stats(h, tpl)
    if stats is None:
        # No rollup row (yet). Keyword-only verdict; zero observations.
        return classify_template(tpl)
    n, nv, sib = stats
    return classify_template(tpl, n=n, nv=nv, has_page_sibling=sib)


# Rollup point-lookup memo. Tiny TTL: the rollup pass itself refreshes every
# few minutes, so this only collapses the burst of submits that arrive for the
# same template within seconds of each other (the crawl hits one template many
# times in a row). Bounded so a long tail of one-off templates can't grow it
# without limit.
_STATS_TTL_S = 60.0
_STATS_CACHE_MAX = 4096
_stats_cache: dict[tuple[str, str], tuple[float, tuple[int, int, bool] | None]] = {}


def invalidate_template_stats(host: str | None = None) -> None:
    """Drop memoised rollup lookups (all, or just one host's)."""
    if host is None:
        _stats_cache.clear()
        return
    h = _normalise_host_str(host)
    for k in [k for k in _stats_cache if k[0] == h]:
        _stats_cache.pop(k, None)


async def _template_stats(host: str, tpl: str) -> tuple[int, int, bool] | None:
    """``(n, nv, has_page_sibling)`` from the rollup, or None when absent."""
    if not tpl:
        return None
    key = (host, tpl)
    now = time.time()
    hit = _stats_cache.get(key)
    if hit is not None and (now - hit[0]) < _STATS_TTL_S:
        return hit[1]
    out: tuple[int, int, bool] | None = None
    try:
        from server.hub._state import state
        pool = getattr(state, "mariadb_pool", None)
        if pool is not None:
            from server.hub.mariadb import host_template_role_get
            row = await host_template_role_get(pool, host, tpl)
            if row is not None:
                out = (
                    int(row.get("n") or 0),
                    int(row.get("nv") or 0),
                    bool(row.get("has_page_sibling")),
                )
    except Exception:
        out = None
    if len(_stats_cache) >= _STATS_CACHE_MAX:
        _stats_cache.clear()
    _stats_cache[key] = (now, out)
    return out


# Per-host override cache (MariaDB read), TTL'd so a fresh override visible
# within ~30s. Bust manually via ``invalidate_host_overrides(host)`` when an
# operator change just landed.
_OV_TTL_S = 30.0
_ov_cache: dict[str, tuple[float, dict]] = {}


def invalidate_host_overrides(host: str) -> None:
    """Drop the per-host override cache entry so the next ``role_for_url``
    call refetches from MariaDB. Called by the PUT/DELETE override routes."""
    try:
        _ov_cache.pop(_normalise_host_str(host), None)
    except Exception:
        pass


async def _host_overrides_cached(host: str) -> dict:
    h = _normalise_host_str(host)
    import time as _t
    now = _t.time()
    cached = _ov_cache.get(h)
    if cached is not None and (now - cached[0]) < _OV_TTL_S:
        return cached[1]
    try:
        from server.hub._state import state
        pool = getattr(state, "mariadb_pool", None)
        if pool is None:
            _ov_cache[h] = (now, {})
            return {}
        from server.hub.mariadb import host_url_role_overrides_get
        ov = await host_url_role_overrides_get(pool, h)
    except Exception:
        ov = {}
    _ov_cache[h] = (now, ov)
    return ov
