"""Regression: the page-role rollup must agree with the in-memory classifier.

2026-08-14 throughput investigation. The fleet sat at 48.6% lane utilisation
with ZERO queued jobs while crawl intake capped at ~4.2 jobs/s. The submitter's
48 parallel POST slots were all busy: ``POST /jobs`` averaged ~11s because
resolving ``download_video`` called ``_page_role.get_host_roles()``, whose
10-minute-TTL cache miss either pulled 2000 rows of ``host_url_history`` or --
for a host with no history yet -- ran ``url LIKE '%host%'`` over the 1.5M-row
jobs table (COUNT(*) + paged SELECT = two full scans, 12.4s measured). Seven
hubs each kept their own copy with no single-flight guard.

The fix precomputes the verdict into ``host_template_roles`` so the hot path is
a primary-key lookup. That only holds if the rollup writes the SAME verdict the
in-memory path would have produced, so this file pins:

  1. ``classify_template`` is behaviour-identical to the old inline classifier
     (the extraction must not have changed any rule);
  2. the rollup's (n, nv, has_page_sibling) triple reproduces
     ``HostPageRoles.role_for_url`` exactly;
  3. the pagination co-occurrence bit -- the one input that needs a host's
     WHOLE template set -- matches the incremental accumulation;
  4. a rollup MISS still classifies keyword-driven roles correctly, because
     that is the fallback the hot path takes for a template the rollup hasn't
     seen yet.
"""

import random

import pytest

from server.hub._page_role import (
    _QUERY_CATEGORY,
    _QUERY_DETAIL,
    _QUERY_LISTING,
    _QUERY_TAG,
    _STATIC_CATEGORY,
    _STATIC_ERROR,
    _STATIC_LISTING,
    _STATIC_TAG,
    HostPageRoles,
    classify_template,
    has_page_sibling,
    page_sibling_prefixes,
    templatize,
)
from server.hub._role_rollup import _build_rows

# A spread of the shapes the crawl actually submits: top / tag / category /
# pagination / WP query routing / JAV product codes / camgirl handles that
# templatize leaves verbatim / non-ASCII paths.
CORPUS = [
    "https://h.tld/",
    "https://h.tld/tag/big-tits/",
    "https://h.tld/tag/big/page/3/",
    "https://h.tld/category/jav/",
    "https://h.tld/v/Xy7Az",
    "https://h.tld/2024/03/abc-123.html",
    "https://h.tld/?page=4&category=foo",
    "https://h.tld/?p=123",
    "https://h.tld/404",
    "https://h.tld/contact",
    "https://h.tld/search/keyword/",
    "https://h.tld/list/page/12/",
    "https://h.tld/access/sakuralive/airiQx",
    "https://h.tld/sex/Chaturbate_x578748",
    "https://h.tld/live/colombia-porno-chat/12",
    "https://h.tld/products/detail/29469",
    "https://h.tld/models/victoria-sweet/",
    "https://h.tld/ja/main/video?id=226437",
    "https://h.tld/vodshow/6-----L------20",
    "https://h.tld/まとめ/ギャラリー/",
]


def _legacy_role_for_url(roles: HostPageRoles, url: str):
    """The classifier exactly as it was inlined in ``HostPageRoles`` before
    ``classify_template`` was extracted. Kept verbatim so an accidental rule
    change during the split (or any later edit) shows up as a diff."""
    tpl = templatize(url)
    if not tpl:
        return "unknown", 0.0, "empty url"
    if tpl == "/":
        return "top", 0.99, "top path"
    path_tpl, _sep, query_tpl = tpl.partition("?")
    qkeys = set()
    if query_tpl:
        qkeys = {kv.split("=", 1)[0] for kv in query_tpl.split("&") if kv}
    segs = [s for s in path_tpl.strip("/").split("/") if s]
    statics = [s for s in segs if not s.startswith("{")]
    if any(s in _STATIC_ERROR for s in statics):
        return "error", 0.95, "static error keyword"
    if any(s in _STATIC_CATEGORY for s in statics):
        return "category", 0.9, "category keyword"
    if qkeys & _QUERY_CATEGORY:
        return "category", 0.9, "category query key"
    if any(s in _STATIC_TAG for s in statics):
        return "tag", 0.9, "tag keyword"
    if qkeys & _QUERY_TAG:
        return "tag", 0.9, "tag query key"
    if roles._page_co_occurs(path_tpl):
        return "listing", 0.95, "pagination"
    if qkeys & _QUERY_LISTING:
        return "listing", 0.9, "listing query key"
    if any(s in _STATIC_LISTING for s in statics):
        return "listing", 0.85, "listing keyword"
    if (qkeys & _QUERY_DETAIL) and len(segs) <= 1:
        return "detail", 0.85, "detail query key"
    n = int(roles.templates.get(tpl, 0))
    nv = int(roles.video_seen.get(tpl, 0))
    if nv >= 2 or (n >= 3 and nv >= max(1, int(n * 0.3))):
        return "detail", 0.95, f"video evidence ({nv}/{n})"
    var_segs = sum(1 for s in segs if s.startswith("{"))
    if var_segs >= 1 and n >= 3:
        return "detail", 0.6, f"variable segs ({n} obs)"
    if var_segs >= 1:
        return "detail", 0.4, "variable segs (few obs)"
    return "unknown", 0.3, "no signal"


def _random_host(rng: random.Random) -> HostPageRoles:
    roles = HostPageRoles()
    for url in rng.sample(CORPUS, rng.randint(0, len(CORPUS))):
        for _ in range(rng.randint(1, 5)):
            roles.observe(url, has_video_evidence=rng.random() < 0.4)
    return roles


def test_extraction_did_not_change_any_rule():
    rng = random.Random(1234)
    for _ in range(200):
        roles = _random_host(rng)
        for url in CORPUS:
            assert roles.role_for_url(url) == _legacy_role_for_url(roles, url), url


def test_rollup_rows_match_in_memory_verdict():
    """The three numbers the rollup stores must be enough to reproduce the
    bulk-loaded verdict. If they aren't, POST /jobs and the host edit modal
    would disagree about the same URL."""
    rng = random.Random(99)
    for _ in range(200):
        roles = _random_host(rng)
        counts = [
            (tpl, int(n), int(roles.video_seen.get(tpl, 0)))
            for tpl, n in roles.templates.items()
        ]
        rows = _build_rows("h.tld", counts, list(roles.templates.keys()))
        by_tpl = {r["template"]: r for r in rows}
        for url in CORPUS:
            tpl = templatize(url)
            if tpl not in by_tpl:
                continue  # not observed on this host -> miss path, covered below
            row = by_tpl[tpl]
            assert (row["role"], row["confidence"], row["reason"]) == \
                roles.role_for_url(url), url


def test_page_only_template_list_is_sufficient():
    """The incremental pass feeds ``_build_rows`` only the host's PAGINATING
    templates (``LIKE '%/page/%'``) instead of its whole list -- 5.2s / 9.8k
    rows vs 9.9s / 494k rows across a 15-minute window's hosts. That is only
    valid if a template without a ``page`` segment can never contribute a
    prefix, so pin the two inputs to the same result."""
    rng = random.Random(4242)
    for _ in range(200):
        roles = _random_host(rng)
        all_tpls = list(roles.templates.keys())
        page_tpls = [t for t in all_tpls if "/page/" in t.partition("?")[0]]
        counts = [
            (tpl, int(n), int(roles.video_seen.get(tpl, 0)))
            for tpl, n in roles.templates.items()
        ]
        assert _build_rows("h.tld", counts, page_tpls) == \
            _build_rows("h.tld", counts, all_tpls)


def test_pagination_bit_matches_incremental_accumulation():
    """``has_page_sibling`` is the only input that needs the host's WHOLE
    template list, so the rollup recomputes it from scratch each pass while
    the in-memory path accumulates it per observation. They must agree."""
    rng = random.Random(7)
    for _ in range(200):
        roles = _random_host(rng)
        prefixes = page_sibling_prefixes(roles.templates.keys())
        assert prefixes == roles.pagination_prefixes
        for tpl in roles.templates:
            assert has_page_sibling(tpl, prefixes) == roles._page_co_occurs(tpl)


@pytest.mark.parametrize(
    "url,role",
    [
        ("https://h.tld/", "top"),
        ("https://h.tld/contact", "error"),
        ("https://h.tld/category/jav/", "category"),
        ("https://h.tld/tag/big-tits/", "tag"),
        # `tag` wins over pagination: the tag keyword is rule 2, the
        # co-occurrence bit is rule 3. So this verdict does NOT depend on
        # host history -- which is exactly why a rollup miss is safe here.
        ("https://h.tld/tag/big/page/3/", "tag"),
        ("https://h.tld/?page=4&category=foo", "category"),
        ("https://h.tld/search/keyword/", "tag"),
        ("https://h.tld/list/page/12/", "listing"),
    ],
)
def test_rollup_miss_still_classifies_keyword_roles(url, role):
    """A template the rollup hasn't written yet falls back to
    ``classify_template(tpl)`` with zero observations. Everything that is
    decided by keywords must survive that, so a miss only ever costs the
    observation-driven *detail* verdict -- never a wrong listing/error/top.

    NB ``/404`` is deliberately absent: ``templatize`` turns a bare numeric
    segment into ``{int}`` before the keyword check runs, so the ``404`` and
    ``2257`` entries in ``_STATIC_ERROR`` are unreachable. Pre-existing, and
    orthogonal to the rollup -- pinning it here would freeze the quirk."""
    verdict, conf, _why = classify_template(templatize(url))
    assert verdict == role
    assert conf >= 0.85


def test_verdict_saturates_so_exact_counts_are_not_required():
    """The rollup stores exact n/nv for operator visibility, but the verdict
    only depends on min(n, 7) and min(nv, 2). Pinning that keeps a future
    "let's cap the count for speed" change honest -- and it is why counting
    over all time rather than a recent window is safe.

    Compares (role, confidence) only: the reason string embeds the raw n for
    the operator, so it legitimately keeps moving after the verdict stops."""
    def verdict(tpl, n, nv):
        role, conf, _why = classify_template(tpl, n=n, nv=nv)
        return role, conf

    for tpl in ("/v/{id}/", "/access/sakuralive/airiqx/"):
        for nv in (0, 1, 2, 3):
            ref = verdict(tpl, 7, min(nv, 2))
            for n in (7, 8, 20, 5000):
                assert verdict(tpl, n, min(nv, 2)) == ref, (tpl, n, nv)
        for big_nv in (2, 3, 50):
            assert verdict(tpl, 12, big_nv) == verdict(tpl, 12, 2), (tpl, big_nv)
