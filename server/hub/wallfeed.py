"""取得結果の滝 (results waterfall) -- cross-hub feed of just-fetched pages.

Every completed fetch already captures a full-page JPEG
(``core/fetcher.py`` -> ``{job}/final.jpg``, ~250 KB p50) and uploads it to
its owner hub. This module taps that upload, thumbnails a *sampled* slice of
it with ffmpeg, and keeps the last N in Redis as a ring buffer that ``/wall``
streams to the browser.

Why sample instead of taking everything: the fleet accepts ~600 jobs/min
(~10/s) and no human can read 10 cards a second, so admitting more than a
handful per second buys nothing but CPU. ``PAPRIKA_WALL_MAX_PER_SEC``
(default 4) is a **fleet-wide** gate (one shared Redis counter, not per-hub),
which is what keeps the cost flat no matter how many hubs are running or how
hard the fleet is pushed. The *true* rate is shown separately as a number,
read from the per-second accept buckets -- the waterfall is the texture, the
counter is the truth.

Storage layout (all in the shared Redis so ANY hub can serve the page):
  ``paprika:wall:seq``          INCR -- monotonic id, also the client cursor
  ``paprika:wall:feed``         LPUSH'd list of metadata JSON (ring, LTRIM'd)
  ``paprika:wall:thumb:<seq>``  base64 JPEG, short TTL
  ``paprika:wall:rl:<epoch_s>`` per-second admission counter

Nothing here is on the request's critical path: ``offer()`` never raises and
never blocks the upload it is called from -- a failure just means one card
missing from a decorative wall.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (env-overridable; all have demo-sane defaults)

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def wall_enabled() -> bool:
    """Master switch. Off => offer() no-ops and /wall serves an empty feed."""
    return _env_flag("PAPRIKA_WALL_ENABLED", True)


def _max_per_sec() -> int:
    """Fleet-wide admission rate. 4/s fills a 40-card wall in 10s and keeps
    thumbnailing under ~0.4 CPU-seconds per second across ALL hubs."""
    return max(1, _env_int("PAPRIKA_WALL_MAX_PER_SEC", 4))


def _ring_size() -> int:
    """Cards the ring holds. The page shows fewer; the surplus is backlog for
    a browser that just opened or was asleep."""
    return max(8, _env_int("PAPRIKA_WALL_RING", 160))


def _thumb_width() -> int:
    return max(120, min(1280, _env_int("PAPRIKA_WALL_THUMB_WIDTH", 420)))


def _thumb_ratio() -> float:
    """Crop height as a fraction of width. A full-page shot is ~1:1.6
    portrait; cropping to the top gives the "above the fold" look people
    recognise as *the page*, instead of an unreadable full-page sliver."""
    return max(0.3, min(2.0, _env_float("PAPRIKA_WALL_THUMB_RATIO", 0.78)))


def _thumb_q() -> int:
    return max(2, min(31, _env_int("PAPRIKA_WALL_THUMB_Q", 6)))


def _thumb_ttl() -> int:
    return max(60, _env_int("PAPRIKA_WALL_THUMB_TTL", 900))


K_SEQ = "paprika:wall:seq"
K_FEED = "paprika:wall:feed"
K_THUMB = "paprika:wall:thumb:"
K_RL = "paprika:wall:rl:"

#: Guard against a pathological upload eating an ffmpeg slot for nothing.
_MIN_JPEG = 1024
_MAX_JPEG = 8 * 1024 * 1024
_FFMPEG_TIMEOUT = 8.0


def _redis():
    """The hub's shared async Redis client, or None.

    ``state.store._r`` is decode_responses=True (str in / str out), which is
    why thumbnails are stored base64 rather than as raw bytes. The registry's
    client is the fallback for deploys where the store has none; it may hand
    back bytes, so every read below normalises via ``_s``.
    """
    try:
        from server.hub._state import state

        r = getattr(state.store, "_r", None)
        if r is not None:
            return r
        return getattr(state.registry, "_r", None)
    except Exception:
        return None


def _s(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)


# ---------------------------------------------------------------------------
# Thumbnailing


def _thumbnail_sync(jpeg: bytes) -> bytes | None:
    """Full-page JPEG -> small top-crop card. Runs in a worker thread.

    ffmpeg is already in the hub image (docker/hub/Dockerfile). Measured on a
    1896x3000 production screenshot: 331 KB -> 15 KB at 420 px in ~85 ms.
    """
    w = _thumb_width()
    ratio = _thumb_ratio()
    # The `\,` escapes the comma INSIDE min() -- an unescaped one would end
    # the crop filter and turn scale=... into a positional arg to it.
    vf = f"crop=iw:min(ih\\,iw*{ratio:.4f}):0:0,scale={w}:-2"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "image2pipe", "-i", "pipe:0",
        "-vf", vf,
        "-frames:v", "1",
        "-f", "image2", "-vcodec", "mjpeg", "-q:v", str(_thumb_q()),
        "pipe:1",
    ]
    try:
        p = subprocess.run(
            cmd,
            input=jpeg,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_FFMPEG_TIMEOUT,
        )
    except Exception as e:
        log.debug("wall: ffmpeg thumbnail failed: %s", e)
        return None
    if p.returncode != 0 or not p.stdout:
        log.debug("wall: ffmpeg rc=%s err=%s", p.returncode, p.stderr[:200])
        return None
    return p.stdout


# ---------------------------------------------------------------------------
# Write side


async def _admit(r) -> bool:
    """Fleet-wide token check for THIS second. One INCR + one EXPIRE."""
    key = K_RL + str(int(time.time()))
    try:
        n = await r.incr(key)
        if int(n) == 1:
            await r.expire(key, 3)
        return int(n) <= _max_per_sec()
    except Exception:
        return False


def _host_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = (urlsplit(url).hostname or "")
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _elapsed_ms(started_at, now: float) -> int | None:
    """Seconds the fetch has been running, from the job row's ``started_at``.

    Job timestamps are stored naive-UTC (see prod-clock-utc-vs-jst), so a
    naive value is pinned to UTC rather than to the hub's local zone.
    """
    if started_at is None:
        return None
    try:
        from datetime import datetime, timezone

        started = started_at
        if isinstance(started, str):
            started = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if isinstance(started, datetime):
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            ts = started.timestamp()
        else:
            ts = float(started)
        return max(0, int((now - ts) * 1000))
    except Exception:
        return None


async def _publish(
    job_id: str,
    jpeg: bytes,
    *,
    url: str | None = None,
    worker_id: str | None = None,
    lane_idx: int | None = None,
    started_at=None,
) -> None:
    """Thumbnail + ring-buffer write for an ALREADY-ADMITTED page."""
    if not jpeg or not (_MIN_JPEG <= len(jpeg) <= _MAX_JPEG):
        return
    r = _redis()
    if r is None:
        return
    try:
        thumb_bytes = await asyncio.to_thread(_thumbnail_sync, jpeg)
        if not thumb_bytes:
            return
        now = time.time()
        seq = int(await r.incr(K_SEQ))
        item = {
            "seq": seq,
            "job_id": job_id,
            "url": url or "",
            "host": _host_of(url),
            "worker_id": worker_id or "",
            "lane_idx": lane_idx if lane_idx is not None else -1,
            "elapsed_ms": _elapsed_ms(started_at, now),
            "bytes": len(jpeg),
            "ts": round(now, 3),
        }
        await r.set(
            K_THUMB + str(seq),
            base64.b64encode(thumb_bytes).decode("ascii"),
            ex=_thumb_ttl(),
        )
        await r.lpush(K_FEED, json.dumps(item, ensure_ascii=False))
        await r.ltrim(K_FEED, 0, _ring_size() - 1)
    except Exception:
        log.debug("wall: publish failed for %s", job_id, exc_info=True)


async def offer_file(job_id: str, path, **meta) -> None:
    """Offer one just-finished page's ``final.jpg`` to the waterfall.

    Called from the upload handler, which is the only moment the bytes are
    guaranteed to exist: job directories are a bounded cache that gets purged
    once a consumer has taken the job (measured: ``page.html`` 404s within ~3
    minutes at production throughput), so anything that read them back later
    would show an ever-emptier wall.

    Admission is checked BEFORE the file is read, so a page the wall is not
    going to show costs one Redis INCR and nothing else -- no disk read, no
    ffmpeg. Never raises.
    """
    if not wall_enabled():
        return
    r = _redis()
    if r is None:
        return
    try:
        if not await _admit(r):
            return
        from pathlib import Path

        jpeg = await asyncio.to_thread(Path(path).read_bytes)
    except Exception:
        return
    await _publish(job_id, jpeg, **meta)


#: Holds fire-and-forget tasks so the loop's only reference isn't a local
#: that the GC can collect mid-flight (same pattern as _jobrunner._bg_tasks).
_bg_tasks: set[asyncio.Task] = set()


def spawn_offer_file(job_id: str, path, **meta) -> None:
    """Fire-and-forget :func:`offer_file`. The upload response must not wait
    on a decorative thumbnail."""
    if not wall_enabled():
        return
    try:
        t = asyncio.create_task(offer_file(job_id, path, **meta))
    except RuntimeError:
        return
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


# ---------------------------------------------------------------------------
# Read side


def _norm_args(after, limit) -> tuple[int, int]:
    try:
        after = int(after or 0)
    except (TypeError, ValueError):
        after = 0
    return after, max(1, min(int(limit or 40), _ring_size()))


def _parse_feed(raw, after: int, limit: int) -> dict:
    out: list[dict] = []
    latest = after
    for row in raw or []:
        try:
            item = json.loads(_s(row))
        except Exception:
            continue
        seq = int(item.get("seq") or 0)
        if seq > latest:
            latest = seq
        if seq > after:
            out.append(item)
    # LPUSH order is newest-first and races slightly across hubs; sort by the
    # authoritative seq rather than trusting list position.
    out.sort(key=lambda it: it.get("seq") or 0)
    if len(out) > limit:
        out = out[-limit:]
    return {"items": out, "seq": latest, "enabled": True}


async def feed(after: int = 0, limit: int = 40) -> dict:
    """Cards newer than ``after`` (the last seq the client rendered),
    **oldest first** so the browser can append them in order."""
    after, limit = _norm_args(after, limit)
    if not wall_enabled():
        return {"items": [], "seq": after, "enabled": False}
    r = _redis()
    if r is None:
        return {"items": [], "seq": after, "enabled": True}
    # Only the head, never the whole ring: the list is newest-first, so the
    # first `limit` entries already contain everything newer than any cursor
    # a client could still act on (it can't render more than `limit` anyway).
    # Reading all 160 on every 1s poll would pull ~30 KB per viewer per second
    # off the shared Redis for rows the page would immediately discard.
    try:
        raw = await r.lrange(K_FEED, 0, limit - 1)
    except Exception:
        raw = []
    return _parse_feed(raw, after, limit)


async def thumb(seq: int) -> bytes | None:
    """Raw JPEG bytes for one card, or None once it has aged out."""
    r = _redis()
    if r is None:
        return None
    try:
        raw = await r.get(K_THUMB + str(int(seq)))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return base64.b64decode(_s(raw))
    except Exception:
        return None


def _accept_keys(now: int, series_s: int) -> list[str]:
    return [f"paprika:tp:accepts:{now - i}" for i in range(series_s)]


def _parse_stats(vals, workers, now: int) -> dict:
    counts = [int(v or 0) for v in (vals or [])]
    if not counts:
        return _empty_stats(now)
    per_sec = round(sum(counts[:10]) / 10.0, 1)
    cap = _max_per_sec()
    return {
        "per_sec": per_sec,
        "per_min": sum(counts[:60]),
        "last_10s": sum(counts[:10]),
        # Oldest-first so the sparkline reads left-to-right like a clock.
        "series": list(reversed(counts)),
        "workers": int(workers or 0),
        # How many cards per second this wall can actually be fed: the fleet's
        # rate, clipped by the admission cap. The page paces its playout off
        # THIS rather than guessing from how many cards each poll happened to
        # return -- the poll is jittery, the supply rate is not.
        "card_cap": cap,
        "card_rate": round(min(per_sec, float(cap)), 2),
        "ts": now,
        "source": "redis",
    }


def _empty_stats(now: int) -> dict:
    return {
        "per_sec": 0.0, "per_min": 0, "last_10s": 0, "series": [],
        "workers": 0, "card_cap": _max_per_sec(), "card_rate": 0.0,
        "ts": now, "source": "unavailable",
    }


async def bundle(after: int = 0, limit: int = 40, series_s: int = 60) -> dict:
    """Everything one poll needs, in **ONE Redis round-trip**.

    Measured 2026-09-11 on prod: a single round-trip to the shared Redis costs
    ~300 ms under crawl load, so issuing lrange + mget + zcard sequentially put
    ``/wall/feed`` at ~1 s p50 (13 s p90). Since the browser holds one poll in
    flight at a time, that latency showed up on the wall directly as dead air
    followed by a clump of cards. Pipelining collapses it to one RTT.
    """
    after, limit = _norm_args(after, limit)
    now = int(time.time())
    if not wall_enabled():
        return {"items": [], "seq": after, "enabled": False, "stats": _empty_stats(now)}
    r = _redis()
    if r is None:
        return {"items": [], "seq": after, "enabled": True, "stats": _empty_stats(now)}
    try:
        series_s = max(10, min(int(series_s or 60), 600))
    except (TypeError, ValueError):
        series_s = 60
    try:
        async with r.pipeline(transaction=False) as pipe:
            pipe.lrange(K_FEED, 0, limit - 1)
            pipe.mget(_accept_keys(now, series_s))
            pipe.zcard("paprika:workers")
            raw, vals, workers = await pipe.execute()
    except Exception:
        # Pipelines are the fast path, not a requirement: fall back to the
        # plain calls so a client library quirk degrades to "slow" not "dead".
        out = await feed(after=after, limit=limit)
        out["stats"] = await stats(series_s=series_s)
        return out
    out = _parse_feed(raw, after, limit)
    out["stats"] = _parse_stats(vals, workers, now)
    return out


async def stats(series_s: int = 60) -> dict:
    """Fleet speed for the wall's header, on its own (see :func:`bundle` for
    the one-round-trip version the route actually uses).

    Reads the same per-second accept buckets ``GET /jobs/throughput`` uses
    (``paprika:tp:accepts:<epoch_s>``), so the wall's headline number is the
    *real* fleet rate -- not the sampled rate of the cards on screen.
    """
    try:
        series_s = max(10, min(int(series_s or 60), 600))
    except (TypeError, ValueError):
        series_s = 60
    now = int(time.time())
    r = _redis()
    if r is None:
        return _empty_stats(now)
    try:
        vals = await r.mget(_accept_keys(now, series_s))
    except Exception:
        return _empty_stats(now)
    try:
        workers = await r.zcard("paprika:workers")
    except Exception:
        workers = 0
    return _parse_stats(vals, workers, now)
