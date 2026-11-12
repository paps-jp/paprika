"""Hub -> learner: one compact "what happened" event per finished job.

This is the ONLY coupling between the hub and the learning system
(internal/design/autonomous-learning-loop-2026-11-12.html §15). The hub
does not judge, learn, or call an LLM here: it copies facts it already
holds into a Redis Stream and moves on. Everything downstream -- the
verifier, the ledger, the scheduler -- lives in the out-of-process
``learner/`` service and can be redeployed without touching the hub.

Contract for callers: ``emit_*`` never raises and never blocks the WS
handler on Redis. A lost event costs one ledger row, nothing else.

Gate: Settings ``outcome_stream_enabled`` (live, cross-hub; default off)
or env ``PAPRIKA_OUTCOME_STREAM=1``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlsplit

from server.hub._state import config, state

log = logging.getLogger(__name__)

STREAM_KEY = os.environ.get("PAPRIKA_OUTCOME_STREAM_KEY") or "paprika:learn:outcomes"

#: Approximate cap on stream length. The learner consumes continuously, so
#: this is a buffer for learner downtime, not storage. Measured on real job
#: results (2026-11-12) an event is ~0.75 KB, and the fleet finishes ~32k
#: jobs/hour: 50k entries is roughly 1.5 hours and ~40-50 MB of Redis.
#: Overflow drops the OLDEST events (a gap in the ledger, never a stall).
MAXLEN = int(os.environ.get("PAPRIKA_OUTCOME_STREAM_MAXLEN") or 50_000)

#: Bound on how long one XADD may take before we give up on that event.
XADD_TIMEOUT_S = float(os.environ.get("PAPRIKA_OUTCOME_STREAM_TIMEOUT_S") or 2.0)

SCHEMA_VERSION = 1

_VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".ts")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")
#: Decorative files are not content (same idea as _is_decorative_asset).
_DECOR_EXT = (".svg", ".ico")

_stats = {"sent": 0, "dropped": 0, "disabled": 0}
_inflight: set = set()


def stats() -> dict:
    return dict(_stats)


def enabled() -> bool:
    if (os.environ.get("PAPRIKA_OUTCOME_STREAM") or "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return True
    try:
        if state.settings is not None:
            return bool(state.settings.get("outcome_stream_enabled", False))
    except Exception:
        pass
    return False


def _redis():
    # Same resolution as _pull_queue._redis(): there is no state.redis.
    st = getattr(state, "store", None)
    r = getattr(st, "_r", None) if st is not None else None
    if r is not None:
        return r
    hubs = getattr(state, "hubs", None)
    r = getattr(hubs, "_r", None) if hubs is not None else None
    if r is not None:
        return r
    return getattr(state, "redis", None)


def _host(url: str | None) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except Exception:
        return ""


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def summarize_assets(assets) -> dict:
    """Counts/bytes by coarse kind + the largest video names (for joins
    against facts.media). Pure."""
    out = {
        "n": 0, "n_video": 0, "n_image": 0, "n_decor": 0,
        "bytes_video": 0, "bytes_image": 0, "videos": [],
    }
    vids: list[tuple[int, str]] = []
    for a in assets or []:
        name = str(_get(a, "name", "") or "")
        low = name.lower()
        try:
            size = int(_get(a, "size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        mime = str(_get(a, "mime", "") or "").lower()
        out["n"] += 1
        if low.endswith(_DECOR_EXT) or "svg" in mime or "icon" in mime:
            out["n_decor"] += 1
        elif low.endswith(_VIDEO_EXT) or mime.startswith("video/"):
            out["n_video"] += 1
            out["bytes_video"] += size
            vids.append((size, name))
        elif low.endswith(_IMAGE_EXT) or mime.startswith("image/"):
            out["n_image"] += 1
            out["bytes_image"] += size
    vids.sort(reverse=True)
    out["videos"] = [{"name": n[:160], "size": s} for s, n in vids[:8]]
    return out


def _occlusion_subset(occ) -> dict:
    """Only the numeric/boolean signals; drop bulky selector dumps."""
    if not isinstance(occ, dict) or not occ:
        return {}
    out = {}
    for k, v in occ.items():
        if isinstance(v, (bool, int, float)) or v is None:
            out[k] = v
        elif isinstance(v, str) and len(v) <= 80:
            out[k] = v
    return out


def build_event(info, result=None, *, error: str | None = None) -> dict:
    """Flatten JobInfo (+ JobResult) into the wire event. Pure; no I/O."""
    opts = _get(info, "options")
    url = str(_get(info, "url", "") or "")
    status = _get(info, "status")
    status = getattr(status, "value", status)
    recipe = _get(opts, "fetch_recipe")
    learn = _get(opts, "learn")
    learn = learn if isinstance(learn, dict) else {}
    ytd = list(_get(result, "ytdlp_results", []) or [])
    yt_fail = [y for y in ytd if not _get(y, "ok", False)]
    vdet = _get(result, "video_detection", {}) or {}
    started = _get(info, "started_at")
    completed = _get(info, "completed_at")
    try:
        dur_s = round((completed - started).total_seconds(), 1) if started and completed else None
    except Exception:
        dur_s = None
    ev = {
        "v": SCHEMA_VERSION,
        "ev": "job_terminal",
        "ts": round(time.time(), 3),
        "job_id": str(_get(info, "job_id", "") or ""),
        "hub": str(getattr(config, "hub_id", "") or ""),
        "worker_id": str(_get(info, "worker_id", "") or ""),
        "url": url[:600],
        "host": _host(url),
        "mode": str(getattr(_get(opts, "mode"), "value", _get(opts, "mode")) or "fetch"),
        "status": str(status or ""),
        "dur_s": dur_s,
        "download_video": bool(_get(opts, "download_video", False)),
        "capture_assets": bool(_get(opts, "capture_assets", True)),
        "fetch_strategy": str(_get(opts, "fetch_strategy", "") or ""),
        "escalated_from": str(_get(opts, "escalated_from", "") or ""),
        "recipe_applied": bool(recipe),
        "recipe_pattern": (
            str((recipe or {}).get("pattern") or "")[:300] if isinstance(recipe, dict)
            else str(learn.get("pattern") or "")[:300]
        ),
        # Experiment arm stamped at dispatch (server/hub/_learn_arm.py):
        # "recipe" | "holdout" (a recipe matched but was withheld) | "".
        "arm": str(learn.get("arm") or "")[:16],
        # Playbook experiment ids (server/hub/_playbooks.py): the playbook
        # this job was assigned to, its version, and -- for an ``active``
        # job that doubles as a trial's comparison group -- that trial.
        "pb": _int_or_none(learn.get("pb")),
        "pb_ver": _int_or_none(learn.get("ver")),
        "pb_vs": _int_or_none(learn.get("vs")),
        "error": (str(error if error is not None else _get(info, "error", "") or ""))[:300],
        "assets": summarize_assets(_get(result, "assets", [])),
        "assets_failed": int(_get(result, "assets_failed", 0) or 0),
        "ytdlp": {
            "n": len(ytd),
            "fail": len(yt_fail),
            "msgs": [str(_get(y, "message", "") or "")[:120] for y in yt_fail[:2]],
        },
        "video_seen": {
            "urls": len(_get(result, "video_urls_seen", []) or []),
            "videos": len((vdet.get("videos") if isinstance(vdet, dict) else None) or []),
            "iframes": len((vdet.get("iframes") if isinstance(vdet, dict) else None) or []),
        },
        "occlusion": _occlusion_subset(_get(result, "occlusion", {})),
        "review_reason": str(_get(result, "review_reason", "") or "")[:200],
        "partial": bool(_get(result, "partial", False)),
        "facts": _get(result, "facts", {}) or {},
    }
    return ev


async def _xadd(ev: dict) -> None:
    r = _redis()
    if r is None:
        _stats["dropped"] += 1
        return
    try:
        await asyncio.wait_for(
            r.xadd(
                STREAM_KEY,
                {"d": json.dumps(ev, ensure_ascii=False, separators=(",", ":"), default=str)},
                maxlen=MAXLEN,
                approximate=True,
            ),
            timeout=XADD_TIMEOUT_S,
        )
        _stats["sent"] += 1
    except Exception:
        _stats["dropped"] += 1
        log.debug("[outcome] xadd failed for %s", ev.get("job_id"), exc_info=True)


def emit_job_terminal(info, result=None, *, error: str | None = None) -> None:
    """Fire-and-forget. Safe to call from any hub handler; never raises."""
    try:
        if info is None:
            return
        if not enabled():
            _stats["disabled"] += 1
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Called off the event loop: nothing to schedule on. Drop rather
            # than create a coroutine that is never awaited.
            _stats["dropped"] += 1
            return
        ev = build_event(info, result, error=error)
        task = loop.create_task(_xadd(ev))
        # Keep a reference until done: a bare create_task result can be
        # garbage-collected mid-flight.
        _inflight.add(task)
        task.add_done_callback(_inflight.discard)
    except Exception:
        _stats["dropped"] += 1
        log.debug("[outcome] emit failed", exc_info=True)
