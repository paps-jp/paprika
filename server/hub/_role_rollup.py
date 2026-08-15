"""Per-(host, template) page-role rollup refresher.

Keeps the ``host_template_roles`` table in sync with ``host_url_history`` so
``_page_role.role_for_url`` -- which runs inside ``POST /jobs`` for every crawl
submission -- can settle a URL's role with one primary-key lookup.

Background (2026-08-14 throughput investigation)
------------------------------------------------
The fleet was sitting at 48.6% lane utilisation with ZERO queued jobs while
the crawl submitter capped out at ~4.2 jobs/s. The submitter's 48 parallel
POST slots were all occupied: mean ``POST /jobs`` latency was ~11s, because
resolving ``download_video`` called ``get_host_roles()``, which on a
10-minute-TTL cache miss either

  * pulled 2000 rows of host_url_history for the host and counted them
    in-process, or
  * (host with no history yet) ran ``url LIKE '%host%'`` over the 1.5M-row
    jobs table -- COUNT(*) plus the paged SELECT, two full scans, 12.4s.

Seven hubs each kept their own copy of that cache with no single-flight
guard, so a popular host expiring meant many simultaneous rebuilds.

Computing the answer ahead of time removes all of it: the read side becomes
a point lookup and the aggregation happens here, off the request path.

Cost (measured on prod, 7.7M rows / 532 hosts / 552k pairs)
-----------------------------------------------------------
  initial full build      ~29s, one time
  incremental (15 min)    ~2.3s to find changed pairs (~910 across ~287
                          hosts) + ~7.4s to recount them

Recounting every template of every changed host instead would return 400k+
rows and take ~23s -- template-exploded hosts carry up to 18k templates
each -- so we recount only the pairs that actually moved.

One hub at a time via a Redis lease, same pattern as the nightly review.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import time

log = logging.getLogger("paprika.role_rollup")

# How often to refresh. The verdict for a template only changes when its
# observation count crosses 3 (or its video-evidence count crosses 2), so
# minute-scale freshness is plenty -- a template that isn't in the rollup yet
# still classifies correctly on keywords, it just doesn't get the
# observation-driven detail verdict until the next pass.
_INTERVAL_S = float(os.environ.get("PAPRIKA_ROLE_ROLLUP_INTERVAL_S") or 300.0)

# Look back further than the interval so a slow pass (or a hub restart
# between passes) can't leave a gap of unprocessed rows.
_LOOKBACK_MIN = int(os.environ.get("PAPRIKA_ROLE_ROLLUP_LOOKBACK_MIN") or 30)

# Rows per UPSERT batch. Keeps a full rebuild off one giant statement.
_BATCH = 2000

_LEASE_KEY = "paprika:role_rollup:lock"
# Must outlive a pass (~15s measured) so two hubs can't overlap, but expire
# BEFORE the next tick -- a TTL longer than the interval makes every hub lose
# the NX race on the following tick, so half the ticks silently no-op.
_LEASE_TTL_S = max(60, int(_INTERVAL_S) - 30)

_ENABLED = (os.environ.get("PAPRIKA_ROLE_ROLLUP", "1") or "1").strip().lower() not in (
    "0", "false", "no", "off",
)


async def _try_acquire_lease(state) -> bool:
    """Cross-hub lease so only ONE hub runs a pass. No redis (single-hub dev)
    means no coordination needed -- just run."""
    redis = getattr(state, "redis", None)
    if redis is None:
        return True
    hub_id = str(getattr(state, "hub_id", "")) or "hub-?"
    try:
        return bool(await redis.set(_LEASE_KEY, hub_id, ex=_LEASE_TTL_S, nx=True))
    except Exception as e:
        log.info("[role-rollup] lease check failed (assume taken): %s", e)
        return False


def _build_rows(
    host: str,
    counts: list[tuple[str, int, int]],
    page_templates: list[str],
) -> list[dict]:
    """Turn ``(template, n, nv)`` counts into rollup rows for one host.

    ``page_templates`` only needs the host's PAGINATING templates (those
    with a literal ``page`` segment), not its whole list: the co-occurrence
    rule asks "does some sibling under my prefix paginate", so a template
    without a ``page`` segment can never contribute a prefix. Passing the
    full list works too -- ``page_sibling_prefixes`` filters it the same
    way -- which is what the full build does, since it already holds it."""
    from server.hub._page_role import (
        classify_template, has_page_sibling, page_sibling_prefixes,
    )
    prefixes = page_sibling_prefixes(page_templates)
    out: list[dict] = []
    for tpl, n, nv in counts:
        sib = has_page_sibling(tpl, prefixes)
        role, conf, reason = classify_template(
            tpl, n=n, nv=nv, has_page_sibling=sib
        )
        out.append({
            "host": host, "template": tpl, "n": n, "nv": nv,
            "has_page_sibling": sib, "role": role,
            "confidence": conf, "reason": reason,
        })
    return out


async def _flush(pool, rows: list[dict]) -> int:
    from server.hub.mariadb import host_template_roles_upsert
    written = 0
    for i in range(0, len(rows), _BATCH):
        written += await host_template_roles_upsert(pool, rows[i:i + _BATCH])
    return written


async def build_full(pool) -> int:
    """Initial build: aggregate the whole history table in one pass."""
    from server.hub.mariadb import host_template_all_pairs
    t0 = time.monotonic()
    pairs = await host_template_all_pairs(pool)
    by_host: dict[str, list[tuple[str, int, int]]] = collections.defaultdict(list)
    for host, tpl, n, nv in pairs:
        by_host[host].append((tpl, n, nv))
    rows: list[dict] = []
    for host, counts in by_host.items():
        # Full build already holds every template for the host, so the
        # prefix set comes straight from the counts -- no extra query.
        rows.extend(_build_rows(host, counts, [t for t, _n, _nv in counts]))
    written = await _flush(pool, rows)
    log.info(
        "[role-rollup] full build: %d hosts, %d templates in %.1fs",
        len(by_host), written, time.monotonic() - t0,
    )
    return written


async def refresh_incremental(pool, lookback_min: int = _LOOKBACK_MIN) -> int:
    """Recount only the (host, template) pairs touched since ``lookback_min``."""
    from server.hub.mariadb import (
        host_page_templates, host_template_changed_pairs, host_template_recount,
    )
    t0 = time.monotonic()
    pairs = await host_template_changed_pairs(pool, lookback_min)
    if not pairs:
        return 0
    by_host: dict[str, list[str]] = collections.defaultdict(list)
    for host, tpl in pairs:
        by_host[host].append(tpl)

    written = 0
    for host, tpls in by_host.items():
        try:
            counts = await host_template_recount(pool, host, tpls)
            if not counts:
                continue
            # The changed templates alone can't tell us whether a `page`
            # sibling exists elsewhere on this host, so pull that host's
            # paginating templates for the co-occurrence bit.
            page_tpls = await host_page_templates(pool, host)
            written += await _flush(pool, _build_rows(host, counts, page_tpls))
        except Exception as e:
            log.info("[role-rollup] host %s failed: %s: %s", host, type(e).__name__, e)
    log.info(
        "[role-rollup] incremental: %d pairs / %d hosts -> %d rows in %.1fs",
        len(pairs), len(by_host), written, time.monotonic() - t0,
    )
    return written


async def run_once() -> int:
    """One pass. Full build when the rollup is empty, incremental after."""
    from server.hub._state import state
    from server.hub.mariadb import host_template_roles_count
    pool = getattr(state, "mariadb_pool", None)
    if pool is None:
        return 0
    if not await _try_acquire_lease(state):
        return 0
    try:
        existing = await host_template_roles_count(pool)
    except Exception as e:
        log.info("[role-rollup] count failed: %s: %s", type(e).__name__, e)
        return 0
    try:
        if existing == 0:
            return await build_full(pool)
        return await refresh_incremental(pool)
    except Exception as e:
        log.warning("[role-rollup] pass failed: %s: %s", type(e).__name__, e)
        return 0


async def scheduler_loop() -> None:
    """Periodic refresher. Started from the hub lifespan."""
    if not _ENABLED:
        log.info("[role-rollup] disabled via PAPRIKA_ROLE_ROLLUP")
        return
    # Let the hub finish coming up (pool + redis) before the first pass, and
    # stagger hubs so they don't all race for the lease at the same instant.
    await asyncio.sleep(30.0)
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[role-rollup] loop error: %s: %s", type(e).__name__, e)
        try:
            await asyncio.sleep(_INTERVAL_S)
        except asyncio.CancelledError:
            raise
