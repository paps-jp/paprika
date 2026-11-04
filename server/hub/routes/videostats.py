"""``/stats/video-leakage`` -- how much video the fleet asks for and never gets.

A ``download_video=true`` fetch job reaches ``completed`` whether or not the
deferred yt-dlp download produced a file, so "we asked for the video and got
nothing" never shows up as a failure. This endpoint makes it a number, per
host, next to the host's no-browser reachability verdicts
(``video_probe_stats``: decouplable / tokened / gated / drm), so the design
options for the deferred download (cookies, keepalive, download tier) can be
weighed against what is actually being lost.

  ``GET /stats/video-leakage?hours=1&limit=40[&refresh=1]``
    200  the latest result (``cached`` says whether it came from a cache)
    202  ``{"status": "computing"}`` -- poll again in ``retry_after_s``

The query scans every completed job in the window (~28 k rows an hour on
prod; 1 h ~ 25-45 s, 6 h ~ 105 s), so it is never run on the request path
when the fleet has Redis:

  * the result lives in Redis (``_RESULT_TTL_S``), shared by every hub -- the
    front round-robins /stats across ~20 hubs, so a per-process cache would
    have each hub re-run the scan for the same click;
  * the first request without a result takes a Redis NX lock and runs the
    scan in a background task on that hub, answering 202 at once (the request
    itself cannot wait: the front cuts long responses at ~30 s, 2026-11-04);
  * ``refresh=1`` drops the stored result and starts a new scan.

Without Redis (single-hub / dev) it falls back to a process-local cache and
an inline query.

Its own router (not jobs/) on purpose: ``/jobs/{job_id}`` is registered
before any later ``/jobs/<literal>`` path and would swallow it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from server.hub._state import state
from server.hub.mariadb import VIDEO_LEAK_MAX_HOURS

log = logging.getLogger(__name__)

router = APIRouter(tags=["Stats"])

_CACHE_TTL_S = 300.0          # process-local (L1) -- also the no-Redis path
_RESULT_TTL_S = 900           # Redis (L2), shared across hubs
_LOCK_TTL_S = 300             # bounds a hub that dies mid-scan
_RETRY_AFTER_S = 5
_RESULT_KEY = "paprika:video_leak:{hours}:{limit}"
_LOCK_KEY = "paprika:video_leak:lock:{hours}:{limit}"

# (hours, limit) -> (monotonic_ts, payload)
_cache: dict[tuple[int, int], tuple[float, dict]] = {}
# Keep background tasks referenced until done (asyncio drops unreferenced tasks).
_tasks: set[asyncio.Task] = set()


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


def _redis():
    """The hub's shared async Redis client, or None (single-hub / dev)."""
    return getattr(getattr(state, "store", None), "_r", None)


async def _run_query(pool, hours: int, limit: int) -> dict:
    """The scan itself, as the payload the endpoint serves. A failure is a
    payload too (``error``), so a broken query cannot leave pollers waiting
    for a result that never comes."""
    from server.hub.mariadb import load_video_leakage

    t0 = time.monotonic()
    try:
        data = await load_video_leakage(pool, hours=hours, limit=limit)
    except Exception as e:  # noqa: BLE001 -- surfaced to the admin, not swallowed
        log.warning("video-leakage query failed: %s: %s", type(e).__name__, e)
        data = {
            "hours": hours, "hosts": [], "host_count": 0,
            "totals": {"jobs": 0, "with_video": 0, "missed": 0,
                       "no_stream": 0, "miss_rate": 0.0},
            "error": f"query failed: {type(e).__name__}: {e}",
        }
    data["query_ms"] = int((time.monotonic() - t0) * 1000)
    data["generated_at"] = time.time()
    return data


async def _compute_and_store(pool, hours: int, limit: int, redis) -> dict:
    key = (hours, limit)
    data = await _run_query(pool, hours, limit)
    _cache_put(key, data)
    if redis is not None:
        try:
            await redis.set(_RESULT_KEY.format(hours=hours, limit=limit),
                            json.dumps(data), ex=_RESULT_TTL_S)
        except Exception:
            log.debug("video-leakage: redis result write failed", exc_info=True)
        try:
            await redis.delete(_LOCK_KEY.format(hours=hours, limit=limit))
        except Exception:
            log.debug("video-leakage: redis lock release failed", exc_info=True)
    return data


def _spawn(coro) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def _computing(hours: int, limit: int) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={"status": "computing", "hours": hours, "limit": limit,
                 "retry_after_s": _RETRY_AFTER_S},
        headers={"Retry-After": str(_RETRY_AFTER_S)},
    )


@router.get("/stats/video-leakage")
async def get_video_leakage(
    hours: int = Query(1, ge=1, le=VIDEO_LEAK_MAX_HOURS),
    limit: int = Query(40, ge=1, le=500),
    refresh: int = Query(0, ge=0, le=1),
):
    pool = getattr(state, "mariadb_pool", None)
    if pool is None:
        raise HTTPException(503, "MariaDB is not configured on this hub")
    hours, limit = int(hours), int(limit)
    key = (hours, limit)
    redis = _redis()
    rkey = _RESULT_KEY.format(hours=hours, limit=limit)
    lkey = _LOCK_KEY.format(hours=hours, limit=limit)

    if not refresh:
        hit = _cache_get(key)
        if hit is not None:
            return {**hit, "cached": True}
        if redis is not None:
            try:
                raw = await redis.get(rkey)
            except Exception:
                raw = None
                log.debug("video-leakage: redis result read failed", exc_info=True)
            if raw:
                try:
                    val = json.loads(raw)
                except Exception:
                    val = None
                if isinstance(val, dict):
                    _cache_put(key, val)
                    return {**val, "cached": True}

    if redis is None:
        # Single hub, no shared cache: the only place to run it is here.
        data = await _compute_and_store(pool, hours, limit, None)
        return {**data, "cached": False}

    if refresh:
        _cache.pop(key, None)
        try:
            await redis.delete(rkey)
        except Exception:
            log.debug("video-leakage: redis result drop failed", exc_info=True)

    try:
        acquired = await redis.set(lkey, getattr(state, "hub_id", "") or "1",
                                   nx=True, ex=_LOCK_TTL_S)
    except Exception:
        # Redis flaked between the read above and here: compute here rather
        # than answer 202 forever.
        log.debug("video-leakage: redis lock failed; computing inline", exc_info=True)
        data = await _compute_and_store(pool, hours, limit, None)
        return {**data, "cached": False}
    if acquired:
        _spawn(_compute_and_store(pool, hours, limit, redis))
    return _computing(hours, limit)
