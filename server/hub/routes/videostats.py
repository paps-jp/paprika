"""``/stats/video-leakage`` -- how much video the fleet asks for and never gets.

A ``download_video=true`` fetch job reaches ``completed`` whether or not the
deferred yt-dlp download produced a file, so "we asked for the video and got
nothing" never shows up as a failure. This endpoint makes it a number, per
host, next to the host's no-browser reachability verdicts
(``video_probe_stats``: decouplable / tokened / gated / drm), so the design
options for the deferred download (cookies, keepalive, download tier) can be
weighed against what is actually being lost.

  ``GET /stats/video-leakage?hours=1&limit=40[&refresh=1]``

Served from a process-local cache (``_CACHE_TTL_S``): the query scans every
completed job in the window (~28 k rows an hour on prod), so the admin tab
must not re-run it on every open. ``refresh=1`` forces a re-query.

Its own router (not jobs/) on purpose: ``/jobs/{job_id}`` is registered
before any later ``/jobs/<literal>`` path and would swallow it.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Query

from server.hub._state import state
from server.hub.mariadb import VIDEO_LEAK_MAX_HOURS

log = logging.getLogger(__name__)

router = APIRouter(tags=["Stats"])

_CACHE_TTL_S = 300.0
# (hours, limit) -> (monotonic_ts, payload)
_cache: dict[tuple[int, int], tuple[float, dict]] = {}


def _cache_get(key: tuple[int, int], now: float | None = None) -> dict | None:
    hit = _cache.get(key)
    if not hit:
        return None
    ts, payload = hit
    if (now if now is not None else time.monotonic()) - ts > _CACHE_TTL_S:
        return None
    return payload


def _cache_put(key: tuple[int, int], payload: dict, now: float | None = None) -> None:
    _cache[key] = (now if now is not None else time.monotonic(), payload)


@router.get("/stats/video-leakage")
async def get_video_leakage(
    hours: int = Query(1, ge=1, le=VIDEO_LEAK_MAX_HOURS),
    limit: int = Query(40, ge=1, le=500),
    refresh: int = Query(0, ge=0, le=1),
):
    pool = getattr(state, "mariadb_pool", None)
    if pool is None:
        raise HTTPException(503, "MariaDB is not configured on this hub")
    key = (int(hours), int(limit))
    if not refresh:
        hit = _cache_get(key)
        if hit is not None:
            return {**hit, "cached": True}
    from server.hub.mariadb import load_video_leakage

    t0 = time.monotonic()
    try:
        data = await load_video_leakage(pool, hours=hours, limit=limit)
    except Exception as e:  # noqa: BLE001 -- surfaced to the admin, not swallowed
        log.warning("video-leakage query failed: %s: %s", type(e).__name__, e)
        raise HTTPException(500, f"query failed: {type(e).__name__}: {e}")
    data["query_ms"] = int((time.monotonic() - t0) * 1000)
    data["generated_at"] = time.time()
    _cache_put(key, data)
    return {**data, "cached": False}
