"""Worker registry HTTP routes: /workers list, host inventory, status
toggle, source tarball download.

The WebSocket control channel (``/workers/{id}/link``) and the lane
preview endpoint (``/workers/{id}/lanes/{idx}/preview``) stay in
app.py for now -- the WS handler is the 500-line worker protocol
loop and the preview depends on session-routing code that hasn't
migrated yet. Both follow when their dependencies do.
"""

from __future__ import annotations

import io
import logging
import tarfile
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

from server.hub._state import state
from server.scheduler import ALLOWED_STATUSES, WORKER_TTL

log = logging.getLogger(__name__)
router = APIRouter(tags=["Workers"])


@router.get("/workers")
async def list_workers() -> dict:
    if state.registry is None:
        return {"count": 0, "workers": []}
    # ``stats_async`` includes historical (disconnected) workers we
    # still have a Redis row for, surfaced as ``alive=False``. Without
    # this the Workers tab would drop a worker the moment its WS link
    # closed -- which is the operator complaint we're fixing here.
    payload = await state.registry.stats_async()
    # Rewrite lane viewer URLs for Windows portable workers so the
    # Live Preview tiles link to the CDP screencast viewer instead of
    # the placeholder URL (= which would 404 because no TightVNC bridge
    # exists in v1.0). Per-lane: if a session is currently bound to
    # this (worker_id, lane_idx), use that session's screencast URL.
    # Otherwise empty string so admin.js renders an unclickable tile.
    if state.sessions is not None:
        for w in payload.get("workers", []):
            labels = w.get("labels") or {}
            if not (
                labels.get("edition") == "portable"
                or labels.get("platform") == "windows"
            ):
                continue
            wid = w.get("worker_id")
            if not wid:
                continue
            lane_urls = list(w.get("lane_novnc_urls") or [])
            if not lane_urls:
                continue
            # Build a quick lane_idx -> session map for this worker.
            bound: dict[int, str] = {}
            try:
                for s in state.sessions.all():
                    if s.worker_id == wid and s.lane_idx is not None:
                        bound[int(s.lane_idx)] = s.session_id
            except Exception:
                pass
            rewritten: list[str] = []
            for lane_idx, _ in enumerate(lane_urls):
                sid = bound.get(lane_idx)
                if sid:
                    # Session bound → session-keyed screencast viewer
                    # (also touches session.last_active_at while open).
                    rewritten.append(f"/sessions/{sid}/screencast/")
                else:
                    # Idle lane → worker/lane-keyed variant. Same
                    # viewer, attaches directly to the worker's
                    # Chromium without needing a session. Lets the
                    # operator peek at idle Chrome (e.g. before
                    # submitting the first job).
                    rewritten.append(
                        f"/workers/{wid}/lanes/{lane_idx}/screencast/"
                    )
            w["lane_novnc_urls"] = rewritten
            # Mirror to the legacy alias the admin UI still reads.
            w["slot_novnc_urls"] = rewritten
    return payload


@router.get("/workers/hosts")
async def list_worker_hosts(include_internal: bool = False) -> dict:
    """Distinct worker host IP addresses currently connected to the hub.

    The hub records each worker's TCP source address (``client_address``)
    at WS-handshake time, so this is the authoritative live inventory of
    where the fleet actually runs -- no static ``scripts/workers.env`` to
    drift out of sync. Intended for fleet-management scripts (deploy,
    health-check, rolling restart) that need to reach every worker host:

        # e.g. fan a command out to every worker host
        for ip in $(curl -s http://paprika.lan:8000/workers/hosts \\
                      | jq -r '.hosts[].address'); do
            ssh "root@$ip" 'cd /opt/paprika && git pull && \\
                            docker compose restart worker'
        done

    By default docker-internal / loopback addresses (127.* / ::1 /
    172.16-31.* RFC1918 bridge ranges) are filtered out because they
    aren't SSH-reachable as standalone hosts -- those are workers
    running in the hub's own compose network (reach them on the hub
    host itself). Pass ``include_internal=true`` to keep them.

    Returns::

        {
          "count": <distinct host count>,
          "hosts": [
            {"address": "192.168.1.140",
             "worker_ids": ["d41662a57eef-e3uj", ...],
             "lanes": 2,             # total lanes across those workers
             "alive": true},        # any worker on this host alive
            ...
          ],
          "filtered_internal": <how many internal hosts were dropped>
        }
    """
    if state.registry is None:
        return {"count": 0, "hosts": [], "filtered_internal": 0}

    def _is_internal(addr: str) -> bool:
        if not addr:
            return True
        if addr.startswith("127.") or addr in ("::1", "localhost"):
            return True
        if addr.startswith("10.") is False and addr.startswith("172."):
            # RFC1918 docker bridge range 172.16.0.0 - 172.31.255.255
            try:
                second = int(addr.split(".", 2)[1])
                if 16 <= second <= 31:
                    return True
            except (ValueError, IndexError):
                pass
        return False

    grouped: dict[str, dict] = {}
    filtered = 0
    now = time.time()
    for w in state.registry.connections.values():
        addr = (w.client_address or "").strip()
        if _is_internal(addr) and not include_internal:
            if addr:
                filtered += 1
            continue
        g = grouped.setdefault(
            addr,
            {
                "address": addr,
                "worker_ids": [],
                "lanes": 0,
                "alive": False,
            },
        )
        g["worker_ids"].append(w.worker_id)
        g["lanes"] += len(w.capabilities.lane_novnc_urls or [])
        if (now - w.last_heartbeat) < WORKER_TTL:
            g["alive"] = True

    hosts = sorted(grouped.values(), key=lambda h: h["address"])
    for h in hosts:
        h["worker_ids"].sort()
    return {
        "count": len(hosts),
        "hosts": hosts,
        "filtered_internal": filtered,
    }


# Paths the hub publishes as the "fleet source of truth". A worker
# that detects a version mismatch downloads this set, extracts over its
# own /app/<name>, and self-restarts so the new code is picked up.
#
# Kept narrow ON PURPOSE: anything outside server / core / VERSION
# travels via a separate channel (see _WORKER_PLUGINS_TREE_PATHS below).
# History: in early v2 we tried bundling ``data/tools/installed`` into
# this same tarball, but it broke the entire fleet the day we added the
# data top-level: every worker still on the previous version refused
# the tarball ("unexpected top-level in tarball: 'data'") and entered an
# exit-42 / restart / refuse loop. Splitting source vs plugins keeps
# the source pipe permanently stable -- old workers always accept
# whatever the hub serves here.
_WORKER_SOURCE_TREE_PATHS: list[tuple[Path, str]] = [
    (Path("/app/server"), "server"),
    (Path("/app/core"), "core"),
    (Path("/app/VERSION"), "VERSION"),
]

# Plugin registry. Hub reads from /data/tools/installed (bind-mounted
# from ./data/tools on the host); workers extract this tarball to
# /app/data/tools/installed/ so fetcher._load_ytdlp_adapter() and
# friends find the adapter modules at their relative fallback path.
# Catalog.json rides along so the worker can render the same catalog
# metadata locally if a future feature wants it.
_WORKER_PLUGINS_TREE_PATHS: list[tuple[Path, str]] = [
    (Path("/data/tools/installed"), "data/tools/installed"),
    (Path("/data/tools/catalog.json"), "data/tools/catalog.json"),
]


def _build_tarball(paths: list[tuple[Path, str]]) -> bytes:
    """Build a gzipped tar of the given (src_path, arcname) pairs.

    Shared by _build_worker_source_tarball() and
    _build_worker_plugins_tarball(). Filters out ``__pycache__`` /
    ``*.pyc`` so the worker never imports stale bytecode after
    extraction, plus hidden files (``.env``-style foot-guns).
    """

    def _filter(tinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = tinfo.name
        parts = name.split("/")
        if any(p.startswith(".") for p in parts if p):
            return None  # hidden files / dirs
        if "__pycache__" in parts:
            return None
        if name.endswith(".pyc") or name.endswith(".pyo"):
            return None
        # Drop ownership info -- the worker will own them as root in
        # its own container, embedding hub's uid/gid is useless noise.
        tinfo.uid = 0
        tinfo.gid = 0
        tinfo.uname = ""
        tinfo.gname = ""
        return tinfo

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        for src, arcname in paths:
            if src.exists():
                tar.add(str(src), arcname=arcname, filter=_filter)
    return buf.getvalue()


def _build_worker_source_tarball() -> bytes:
    """Server / core / VERSION only. Stable top-level layout forever
    so older workers can keep self-updating no matter what new dirs
    are added to the hub-side data tree."""
    return _build_tarball(_WORKER_SOURCE_TREE_PATHS)


def _build_worker_plugins_tarball() -> bytes:
    """data/tools/installed + catalog.json. Independent cadence from
    source updates: a new plugin only invalidates this tarball, not
    the worker's main code path."""
    return _build_tarball(_WORKER_PLUGINS_TREE_PATHS)


@router.get("/worker-source.tar.gz")
async def get_worker_source_tarball() -> Response:
    """Tarball of the source tree the hub expects connected workers to run.

    Hit by workers when ``HubRegistered.expected_worker_version`` does
    not match their local VERSION and the operator has opted into
    ``PAPRIKA_WORKER_AUTO_FETCH_SOURCE`` (default on). The worker
    downloads, extracts over ``/app/server`` / ``/app/core`` /
    ``/app/VERSION``, then exits with code 42 so the docker restart
    policy boots it on the fresh code.

    Plugins (``data/tools/installed/*``) ride a separate endpoint --
    see ``/worker-plugins.tar.gz``. Splitting the two prevents adding
    a new plugin from breaking workers stuck on an older whitelist.

    Implicit auth: the endpoint is open to anyone on the hub's LAN-
    facing network (same network plane as the WS endpoint). Behind a
    hostile network, gate this with a reverse proxy or add the same
    ``WORKER_SECRET`` check the WS endpoint uses.
    """
    # Lazy import: _hub_version still lives in app.py (file read +
    # cache). One round trip's worth of ms is fine for a tarball.
    from server.hub._version import _hub_version

    try:
        data = _build_worker_source_tarball()
    except Exception as e:
        raise HTTPException(500, f"failed to build tarball: {e}")
    headers = {
        # Version baked into the response so the operator can sanity-
        # check what they're about to ship out to the fleet (curl -I).
        "X-Paprika-Version": _hub_version(),
        "Content-Disposition": 'attachment; filename="paprika-worker-source.tar.gz"',
    }
    return Response(content=data, media_type="application/gzip", headers=headers)


@router.get("/worker-plugins.tar.gz")
async def get_worker_plugins_tarball() -> Response:
    """Tarball of the plugin tree (data/tools/installed + catalog.json).

    Fetched by workers on registration (best-effort) so each worker
    has a local copy of every installed plugin -- needed because
    ``core/fetcher.py::_load_ytdlp_adapter()`` and similar plugin-
    invoking call sites run inside the worker process, not on the hub.

    Top-level shape is always ``data/tools/installed/...`` (plus the
    optional ``data/tools/catalog.json``). Stable -- the worker
    extractor enforces this exact prefix.
    """
    from server.hub._version import _hub_version

    try:
        data = _build_worker_plugins_tarball()
    except Exception as e:
        raise HTTPException(500, f"failed to build plugins tarball: {e}")
    headers = {
        "X-Paprika-Version": _hub_version(),
        "Content-Disposition": 'attachment; filename="paprika-worker-plugins.tar.gz"',
    }
    return Response(content=data, media_type="application/gzip", headers=headers)


@router.post("/workers/{worker_id}/status")
async def set_worker_status(worker_id: str, body: dict) -> dict:
    """Operator switch: active / drain / standby.

    * ``active``  -- normal scheduling.
    * ``drain``   -- skipped by pick_worker; in-flight jobs continue
                     to completion. Use before maintenance / rolling
                     restarts.
    * ``standby`` -- same scheduler effect as drain; semantically
                     "do not auto-resume". State is in-memory only
                     and resets to active when the hub restarts.
    """
    if state.registry is None:
        raise HTTPException(503, "registry not ready")
    worker = state.registry.connections.get(worker_id)
    if worker is None:
        raise HTTPException(404, f"worker '{worker_id}' not connected")
    status = (body or {}).get("status")
    if status not in ALLOWED_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(ALLOWED_STATUSES)}")
    worker.status = status
    log.info("worker %s status -> %s", worker_id, status)
    try:
        state.registry.log_event(
            worker_id,
            f"status -> {status}",
            kind="lifecycle",
        )
    except Exception:
        pass
    return {"worker_id": worker_id, "status": status}


@router.delete("/workers/{worker_id}")
async def delete_worker(worker_id: str) -> dict:
    """Forget a worker entirely (Redis row + index + in-process logs).

    The Workers tab keeps disconnected workers visible so operators can
    review what ran where -- but eventually the list gets noisy with
    one-off / decommissioned hosts. This endpoint lets the operator
    prune them with the trash-can button next to each row.

    Refuses with 409 if the worker is currently connected: the operator
    should drain it (status=drain) and disconnect it first; otherwise
    we'd be racing the WS loop. Returns 404 if the id is unknown.
    """
    if state.registry is None:
        raise HTTPException(503, "registry not ready")
    if state.registry.connections.get(worker_id) is not None:
        raise HTTPException(
            409,
            f"worker '{worker_id}' is still connected; disconnect it before deleting",
        )
    removed = await state.registry.forget(worker_id)
    if not removed:
        raise HTTPException(404, f"worker '{worker_id}' not found in history")
    log.info("worker %s forgotten (operator-initiated)", worker_id)
    return {"worker_id": worker_id, "deleted": True}


@router.get("/workers/{worker_id}/logs")
async def get_worker_logs(worker_id: str, limit: int = 200) -> dict:
    """Recent hub-side activity log for one worker.

    Backed by the in-memory ring buffer maintained by ``WorkerRegistry``;
    entries are dropped on hub restart. Each item:
    ``{"ts": <unix>, "kind": "info|job|warn|error|lifecycle", "line": "<text>"}``.

    Used by the admin UI's Workers tab "..." menu to surface "what has
    this worker been doing recently" without SSH access. Job-log lines
    come from forwarded WorkerJobLog messages and are prefixed with the
    short job id so the operator can correlate.
    """
    if state.registry is None:
        raise HTTPException(503, "registry not ready")
    rows = state.registry.get_logs(worker_id, limit=limit)
    return {"worker_id": worker_id, "count": len(rows), "logs": rows}


# ============================================================================
# Worker lane preview (#2B-G3-partial): live thumbnail of one lane's screen,
# polled by the admin UI's Live Preview tile grid. Distinct from
# /jobs/{id}/screenshot (high-quality, persisted) -- this is low-res,
# ephemeral, no disk write.
# ============================================================================

import asyncio


# _ffmpeg_q_from_quality_pct lives in app.py (also used by worker_lane_preview
# AND routes/jobs.py:take_job_screenshot). Lazy-import to dodge the
# app->routes/workers->app cycle.
def _ffmpeg_q_from_quality_pct(pct: int) -> int:
    from server.hub._helpers import _ffmpeg_q_from_quality_pct as _impl

    return _impl(pct)


@router.get("/workers/{worker_id}/lanes/{lane_idx}/preview")
async def worker_lane_preview(
    worker_id: str,
    lane_idx: int,
    width: int = 320,
    quality: int = 30,
):
    """**PREVIEW**: light, ephemeral, "what's on screen now".

    Used by the admin UI's Live Preview tile grid + each Live panel's
    preview tab. Polled every few seconds × 25 lanes, so the defaults
    are tuned for low bandwidth and CPU:

      ``width=320``  -> ~half of full-screen 1920×1080 -> ~7x fewer pixels
      ``quality=30`` -> ffmpeg q=22, ~80% smaller than quality=80

    Together this yields ~30-80 KB per frame instead of ~300-500 KB
    with the old defaults, cutting Live Preview traffic by ~10x.

    ``quality`` is on a 0-100 perceptual scale (100 = best).

    For high-fidelity, persisted screenshots use
    :func:`take_job_screenshot` (``POST /jobs/{id}/screenshot``)
    instead -- that path writes a JPEG asset to disk + sidecar
    metadata. This endpoint never persists; cache headers also tell
    intermediaries not to keep frames so a simple
    ``<img src="...?t=NNN">`` polling loop never gets a stale image.
    """
    if state.registry is None:
        raise HTTPException(503, "registry not ready")
    worker = state.registry.connections.get(worker_id)
    if worker is None:
        raise HTTPException(404, f"worker '{worker_id}' not connected")
    ffmpeg_q = _ffmpeg_q_from_quality_pct(quality)
    try:
        reply = await worker.request_screenshot(
            lane_idx,
            max_width=width,
            quality=ffmpeg_q,
        )
    except TimeoutError:
        raise HTTPException(504, f"screenshot timed out for lane {lane_idx}")
    except Exception as e:
        raise HTTPException(502, f"screenshot send failed: {e}")
    if reply is None:
        # request_screenshot returns None when the capture was cancelled
        # mid-flight (a timeout raises instead). No frame to serve.
        raise HTTPException(504, f"screenshot unavailable for lane {lane_idx}")
    if reply.error:
        raise HTTPException(502, f"worker error: {reply.error}")
    import base64

    try:
        jpeg = base64.b64decode(reply.jpeg_b64)
    except Exception:
        raise HTTPException(502, "worker returned invalid base64")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.get(
    "/workers/{worker_id}/lanes/{lane_idx}/screenshot",
    include_in_schema=False,
)
async def worker_lane_preview_legacy(
    worker_id: str,
    lane_idx: int,
    width: int = 320,
    quality: int = 30,
):
    """Deprecated alias of ``/lanes/{lane_idx}/preview``.

    Kept for one release cycle so external clients (cached admin UI
    HTML, curl bookmarks, screenshots-page deep links) keep working
    through the screenshot -> preview rename. Hidden from the OpenAPI
    schema so new integrations land on the new name. Drop on next
    breaking-change release.
    """
    return await worker_lane_preview(
        worker_id=worker_id,
        lane_idx=lane_idx,
        width=width,
        quality=quality,
    )


# ============================================================================
# Batched lane-preview streaming: one POST -> NDJSON stream of many frames
# ----------------------------------------------------------------------------
# The Live Preview grid used to fire one GET /lanes/{i}/preview per tile --
# ~20 requests every few seconds. The admin UI is served over plain HTTP/1.1
# (nginx, no TLS/h2 on :8000), so the browser's ~6-connections-per-host cap
# makes those requests queue in waves AND starve other admin XHRs (e.g. the
# /overview poll) behind the preview flood.
#
# This endpoint takes the whole tile set in ONE POST, fans the screenshot
# RPCs out across workers (bounded by a semaphore), and streams each frame
# back as an NDJSON line the instant its RPC resolves -- a slow/busy lane
# never blocks the fast ones (no head-of-line). A short-TTL frame cache plus
# in-flight coalescing protect the workers: repeat asks for the same
# (worker, lane, size) within the TTL reuse one capture instead of triggering
# a fresh ffmpeg encode per viewer / per overlapping poll. The single-tile
# GET endpoints above are untouched (still used by each Live panel's preview
# tab) -- this is purely additive.
# ============================================================================

import json as _json

from fastapi import Request as _Request
from fastapi.responses import StreamingResponse as _StreamingResponse

# (wid, lane, width, ffmpeg_q) -> (monotonic_ts, jpeg_b64)
_PREVIEW_FRAME_CACHE = {}
# (wid, lane, width, ffmpeg_q) -> in-flight capture asyncio.Task (coalesce)
_PREVIEW_INFLIGHT = {}
_PREVIEW_CACHE_TTL = 1.5        # secs a frame stays "live enough" to reuse
_PREVIEW_MAX_CONCURRENCY = 8    # simultaneous screenshot RPCs across the fleet
_PREVIEW_MAX_LANES = 96         # hard cap on lanes per batch (abuse guard)
_PREVIEW_RPC_TIMEOUT = 4.0      # per-lane capture deadline. Kept below the
                                # grid's poll interval so one slow/idle lane
                                # can't hold the whole batch open (and delay
                                # the next tick) -- the straggler's frame still
                                # lands in the cache for the following round.
                                # (The single-tile GET keeps the 8s default for
                                # the focused Live-panel view.)
_preview_sem = None             # lazily bound to the running loop


def _preview_semaphore():
    # Created lazily so it binds to uvicorn's running event loop, not the
    # (possibly different) import-time loop.
    global _preview_sem
    if _preview_sem is None:
        _preview_sem = asyncio.Semaphore(_PREVIEW_MAX_CONCURRENCY)
    return _preview_sem


async def _do_capture_frame(key):
    """Actually capture one lane frame. Returns (jpeg_b64, error_str) with
    exactly one non-None. Never raises -- a single bad lane must not tear
    down the whole batch stream. Result is cached on success."""
    wid, lane, width, ffmpeg_q = key
    if state.registry is None:
        return None, "registry not ready"
    worker = state.registry.connections.get(wid)
    if worker is None:
        return None, "worker not connected"
    async with _preview_semaphore():
        try:
            reply = await worker.request_screenshot(
                lane, max_width=width, quality=ffmpeg_q,
                timeout=_PREVIEW_RPC_TIMEOUT,
            )
        except TimeoutError:
            return None, "timeout"
        except Exception as e:
            return None, f"send failed: {e}"
    if reply is None:
        # request_screenshot returns None on mid-flight cancellation.
        return None, "cancelled"
    if reply.error:
        return None, reply.error
    b64 = reply.jpeg_b64 or ""
    _PREVIEW_FRAME_CACHE[key] = (time.monotonic(), b64)
    return b64, None


async def _capture_one_frame(key):
    """Cache-first, coalescing wrapper around _do_capture_frame."""
    hit = _PREVIEW_FRAME_CACHE.get(key)
    if hit is not None and (time.monotonic() - hit[0]) <= _PREVIEW_CACHE_TTL:
        return hit[1], None
    task = _PREVIEW_INFLIGHT.get(key)
    if task is None:
        task = asyncio.ensure_future(_do_capture_frame(key))
        _PREVIEW_INFLIGHT[key] = task
        task.add_done_callback(lambda t, k=key: _PREVIEW_INFLIGHT.pop(k, None))
    # shield: if THIS request's client disconnects, our awaiter is cancelled
    # but the shared capture task keeps running -> it still populates the
    # cache for the next poll, and any coalesced waiters aren't torn down.
    return await asyncio.shield(task)


async def _preview_line(wid, lane, width, ffmpeg_q):
    """Produce one NDJSON line for a lane (capture + serialize)."""
    b64, err = await _capture_one_frame((wid, lane, width, ffmpeg_q))
    rec = {"wid": wid, "lane": lane}
    if err:
        rec["error"] = err
    else:
        rec["jpeg_b64"] = b64
    return _json.dumps(rec, separators=(",", ":")) + "\n"


@router.post("/workers/previews")
async def workers_previews_batch(request: _Request):
    """**BATCH PREVIEW**: one POST -> NDJSON stream of lane frames.

    Body::

        {"lanes": [{"wid": "<worker_id>", "lane": 0}, ...],
         "width": 320, "quality": 30}

    Streams one JSON object per line (``application/x-ndjson``) as each
    lane's capture resolves::

        {"wid": "...", "lane": 0, "jpeg_b64": "..."}     # success
        {"wid": "...", "lane": 1, "error": "timeout"}    # failure

    ``quality`` is the 0-100 perceptual scale (same as the single-tile GET
    endpoint); the hub converts it to ffmpeg's q internally. Replaces the
    grid's ~20-GETs-per-tick with a single request.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    lanes_in = body.get("lanes") if isinstance(body, dict) else None
    if not isinstance(lanes_in, list) or not lanes_in:
        raise HTTPException(400, "body.lanes must be a non-empty array")
    if len(lanes_in) > _PREVIEW_MAX_LANES:
        raise HTTPException(413, f"too many lanes (max {_PREVIEW_MAX_LANES})")
    width = max(80, min(1920, int(body.get("width", 320) or 320)))
    quality_pct = max(0, min(100, int(body.get("quality", 30) or 30)))
    ffmpeg_q = _ffmpeg_q_from_quality_pct(quality_pct)

    # Parse + dedupe (a duplicate (wid,lane) would emit two identical lines).
    seen = set()
    targets = []
    for item in lanes_in:
        if not isinstance(item, dict):
            continue
        wid = item.get("wid") or item.get("worker_id")
        lane = item.get("lane")
        if lane is None:
            lane = item.get("lane_idx")
        if not wid or lane is None:
            continue
        try:
            lane = int(lane)
        except (TypeError, ValueError):
            continue
        k = (str(wid), lane)
        if k in seen:
            continue
        seen.add(k)
        targets.append(k)
    if not targets:
        raise HTTPException(400, "no valid {wid, lane} entries in body.lanes")

    async def _stream():
        # Fan all captures out at once; the semaphore inside _do_capture_frame
        # bounds how many actually hit workers concurrently. as_completed
        # yields each as soon as it resolves -> progressive, no head-of-line.
        tasks = [
            asyncio.ensure_future(_preview_line(wid, lane, width, ffmpeg_q))
            for (wid, lane) in targets
        ]
        try:
            for fut in asyncio.as_completed(tasks):
                yield await fut
        finally:
            # Client disconnected (or closed the stream) before all frames
            # arrived: drop our awaiters. The shared capture tasks behind
            # _capture_one_frame are shielded, so they finish + cache on
            # their own; we only stop forwarding.
            for t in tasks:
                if not t.done():
                    t.cancel()

    return _StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "X-Accel-Buffering": "no",  # don't let nginx buffer the stream
        },
    )


# ============================================================================
# Worker WebSocket control channel (#2B-G3-partial)
# ----------------------------------------------------------------------------
# The full worker protocol loop: register handshake, message dispatch,
# disconnect cleanup (drop sessions, settle orphan jobs). Also brings
# along _mint_unique_worker_id and _worker_dialled_base_url which had
# no other callers.
# ============================================================================

from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

from server.hub._state import config, get_storage_dir
from server.hub.routes.profiles import _sync_all_profiles_to_worker
from server.hub.sessions import SessionInfo
from server.protocol import (
    ASSET_CAPTURE_MARKER,
    JOB_PROGRESS_MARKER,
    LINKS_CAPTURE_MARKER,
    NET_CAPTURE_MARKER,
    HubRegistered,
    JobResult,
    JobStatus,
    WorkerHeartbeat,
    WorkerJobAccepted,
    WorkerJobComplete,
    WorkerJobFailed,
    WorkerJobLog,
    WorkerJobProgress,
    WorkerRegister,
    WorkerScreenshotReply,
    WorkerSessionActionResult,
    WorkerSessionAgentResult,
    WorkerSessionAnnounce,
    WorkerSessionEndAck,
    WorkerSessionStartAck,
    decode_worker_msg,
    encode_msg,
)
from server.runner import DONE_SENTINEL
from server.scheduler import WorkerRegistry


# _hub_version lives in app.py (cached file read); lazy bridge to dodge
# the routes/workers <-> app boot cycle.
def _hub_version() -> str:
    from server.hub._version import _hub_version as _impl

    return _impl()


def _worker_dialled_base_url(ws: WebSocket) -> str:
    """Best-effort: figure out what URL the worker used to dial us.

    The WebSocket `Host` header carries whatever address the worker
    actually reached -- "hub:8000" for an in-compose worker, e.g.
    "paprika.lan" for a worker on the LAN. Echoing that back as
    the asset upload base lets the worker POST to the same hub from
    the same direction, no operator config needed.
    """
    host = ws.headers.get("host") or ws.headers.get("Host")
    if not host:
        # Fall back to whatever the operator configured.
        return (config.public_base_url or "http://hub:8000").rstrip("/")
    scheme = "https" if ws.url.scheme == "wss" else "http"
    return f"{scheme}://{host}".rstrip("/")


def _mint_unique_worker_id(registry: WorkerRegistry, hint: str) -> str:
    """Generate a fresh worker_id that isn't currently held.

    Called when the hub detects a clone collision (same worker_id from a
    different client IP, original still alive). The hint is the colliding
    ID -- we strip any trailing ``-<rand4>`` suffix so repeated clones
    don't grow unboundedly long, then attach a new 4-char suffix and
    keep trying until we land on something unused.
    """
    import random
    import string
    import uuid

    # Strip an existing "-rand4" tail (e.g. "host-aB3z" -> "host") so we
    # never produce IDs like "host-aB3z-Xq91-7vR2" after a few clone
    # generations. If the hint has no dash, use it whole.
    if "-" in hint:
        base, tail = hint.rsplit("-", 1)
        if len(tail) <= 8 and tail.isalnum():
            pass  # base is already the trimmed form
        else:
            base = hint
    else:
        base = hint
    base = base or "worker"

    in_use = set(registry.connections.keys())
    alphabet = string.ascii_lowercase + string.digits
    for _ in range(50):
        suffix = "".join(random.choices(alphabet, k=4))
        cand = f"{base}-{suffix}"
        if cand not in in_use:
            return cand
    # Pathological fallback: use a full uuid4 hex tail.
    return f"{base}-{uuid.uuid4().hex[:8]}"


@router.websocket("/workers/{worker_id}/link")
async def worker_link(ws: WebSocket, worker_id: str):
    """Bidirectional control channel for a worker.

    Protocol:
      1. Worker sends WorkerRegister (must match worker_id in URL).
      2. Hub sends HubRegistered.
      3. Loop: hub may send HubAssignJob/HubCancelJob/HubPing; worker sends
         heartbeat/progress/log/complete/failed.
    """
    await ws.accept()
    assert state.registry is not None and state.store is not None

    # Wait for register
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        msg = decode_worker_msg(raw)
    except Exception as e:
        await ws.close(code=1003, reason=f"register failed: {e}")
        return

    if not isinstance(msg, WorkerRegister):
        await ws.close(code=1003, reason="expected register first")
        return
    if msg.worker_id != worker_id:
        await ws.close(code=1003, reason="worker_id mismatch")
        return
    if config.worker_secret and msg.secret != config.worker_secret:
        await ws.close(code=1008, reason="bad worker secret")
        return

    # Clone-collision detection. If a different host (different client IP)
    # is presenting the same worker_id and the original WS is still alive,
    # this is almost certainly a copy of the worker host that brought the
    # persisted /root/.paprika/worker_id along (LXC clone, Proxmox copy,
    # VMware copy, dd, container volume copy, ...). Mint a fresh ID and
    # hand it back via HubRegistered.assigned_worker_id so the worker can
    # adopt it, persist it, and reconnect.
    new_ip = ws.client.host if ws.client else None
    existing = state.registry.connections.get(worker_id)
    if existing is not None:
        existing_alive = (time.time() - existing.last_heartbeat) < WORKER_TTL
        existing_ip = getattr(existing, "client_address", None)
        if existing_alive and existing_ip and new_ip and existing_ip != new_ip:
            new_id = _mint_unique_worker_id(state.registry, worker_id)
            log.warning(
                "worker_id collision: %s already held by %s; new "
                "connection from %s reassigned to %s",
                worker_id,
                existing_ip,
                new_ip,
                new_id,
            )
            try:
                await ws.send_text(
                    encode_msg(
                        HubRegistered(
                            worker_id=worker_id,
                            assigned_worker_id=new_id,
                        )
                    )
                )
            except Exception:
                pass
            await ws.close(code=1000, reason="worker_id collision; reassigned")
            return

    worker = await state.registry.register(worker_id, ws, msg.capabilities)
    # Remember which address this worker reached us through. Subsequent
    # job assignments will hand it back as the asset upload base so the
    # worker's POSTs land on the same hub it WS-connected to (works
    # transparently for both in-compose and LAN workers).
    worker.public_base_url = _worker_dialled_base_url(ws)
    # Record the worker's source IP so the admin UI can show "which
    # box is this?" alongside the (otherwise opaque) worker_id.
    try:
        # Behind the multi-hub nginx front the WS peer is nginx itself,
        # so ws.client.host is the proxy IP (172.18.x.x). Prefer the real
        # client IP from the headers nginx sets (X-Real-IP, or the first
        # X-Forwarded-For hop); fall back to the socket peer for direct
        # (no-proxy) connections. client_address feeds the noVNC proxy
        # target (_resolve_session_novnc_target) and the clone-collision
        # check, so it MUST be the worker's routable LAN IP -- not nginx's
        # -- or noVNC proxies to the wrong host (502) and clone detection
        # can't tell two hosts apart.
        _real_ip = (
            ws.headers.get("x-real-ip")
            or (ws.headers.get("x-forwarded-for") or "").split(",")[0]
        ).strip()
        worker.client_address = _real_ip or (ws.client.host if ws.client else None)
    except Exception:
        worker.client_address = None
    log.info(
        "worker connected: %s  capacity=%d  labels=%s  base_url=%s  from=%s",
        worker_id,
        worker.capabilities.max_concurrent,
        worker.capabilities.labels,
        worker.public_base_url,
        worker.client_address,
    )
    try:
        state.registry.log_event(
            worker_id,
            (
                f"connected from {worker.client_address or '?'} "
                f"(capacity={worker.capabilities.max_concurrent}, "
                f"version={worker.capabilities.version or '?'})"
            ),
            kind="lifecycle",
        )
    except Exception:
        pass
    try:
        await worker.send(
            HubRegistered(
                worker_id=worker_id,
                expected_worker_version=_hub_version(),
            )
        )
        # Re-sync every registered Chrome profile so the worker's
        # local cache reflects the hub's authoritative state. Fires
        # before the main message loop so prefetches can start
        # immediately; the on-demand fetch path is still the
        # fallback if a prefetch hasn't completed by the time a job
        # arrives. Fire-and-forget.
        try:
            await _sync_all_profiles_to_worker(worker)
        except Exception:
            log.warning(
                "initial profile sync to %s failed",
                worker_id,
                exc_info=True,
            )
        # Main loop
        while True:
            raw = await ws.receive_text()
            try:
                nmsg = decode_worker_msg(raw)
            except Exception:
                log.warning("[%s] decode error", worker_id, exc_info=True)
                continue
            await _handle_worker_message(worker, nmsg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.warning("[%s] WS loop error", worker_id, exc_info=True)
    finally:
        await state.registry.unregister(worker_id)
        # Drop any sessions that were bound to this worker -- their Lane
        # is gone and the next /sessions/{id}/* call should 404, not
        # hang waiting on a dead WS connection.
        orphan_job_ids: list[str] = []
        if state.sessions is not None:
            # Capture the job_ids of sessions on this worker BEFORE we
            # drop them, so the orphan-job sweep below can settle the
            # jobs they belonged to.
            try:
                orphan_job_ids = [
                    s.job_id
                    for s in state.sessions.by_worker(worker_id)
                    if getattr(s, "job_id", None)
                ]
            except Exception:
                orphan_job_ids = []
            dropped = state.sessions.drop_by_worker(worker_id)
            if dropped:
                log.info(
                    "worker %s disconnect: dropped sessions %s",
                    worker_id,
                    dropped,
                )
        # Settle jobs that were dispatched to this worker but never
        # reached a terminal state. A fetch / vision-agent job's whole
        # lifecycle lives on the worker; if the worker vanished (restart
        # / crash / self-update) the job would otherwise sit at
        # queued/running FOREVER -- there is no re-dispatch loop. (Seen
        # in the wild: job 24c33ccf4a53 stuck at queued after its
        # assigned worker self-updated mid-dispatch.)
        #
        # codegen-loop / rerun jobs run in-process on the hub
        # (state.local_tasks) and survive a worker disconnect -- they
        # just lose a session and recover via the session-404 path --
        # so skip anything with a live local task.
        if state.store is not None and orphan_job_ids:
            for jid in set(orphan_job_ids):
                if jid in state.local_tasks:
                    continue
                try:
                    jinfo = await state.store.get_job_info(jid)
                except Exception:
                    jinfo = None
                if jinfo is None:
                    continue
                if jinfo.status not in (JobStatus.queued, JobStatus.running):
                    continue  # already terminal
                phase = getattr(jinfo.progress, "phase", "") if jinfo.progress else ""
                if jinfo.status == JobStatus.running and phase == "keepalive":
                    # Crawl already finished; only the interactive
                    # keepalive session died with the worker. Mirror the
                    # close_session cascade -- complete it cleanly rather
                    # than flagging a failure on a job whose assets are
                    # already saved.
                    jinfo.status = JobStatus.completed
                    if jinfo.progress is not None:
                        # state-model v1: keepalive close = normal
                        # completion (capture already saved). Phase
                        # mirrors status; "keepalive_closed" is retired.
                        jinfo.progress.phase = "completed"
                    jinfo.completed_at = datetime.utcnow()
                else:
                    jinfo.status = JobStatus.failed
                    jinfo.error = (
                        f"worker {worker_id} disconnected before the job "
                        f"finished (restart / crash / self-update); "
                        f"re-submit to retry"
                    )
                    if jinfo.progress is not None:
                        jinfo.progress.phase = "failed"
                    jinfo.completed_at = datetime.utcnow()
                try:
                    await state.store.save_job_info(jinfo)
                    await state.store.publish_log(jid, DONE_SENTINEL)
                except Exception:
                    pass
                log.info(
                    "worker %s disconnect: settled orphaned job %s -> %s",
                    worker_id,
                    jid,
                    jinfo.status,
                )
        log.info("worker disconnected: %s", worker_id)
        try:
            state.registry.log_event(
                worker_id, "disconnected", kind="lifecycle"
            )
        except Exception:
            pass


def _drop_fetch_session_if_any(info) -> None:
    """Remove the fetch-owned SessionInfo a job registered at dispatch.

    Called on WorkerJobComplete / WorkerJobFailed for fetch jobs. The
    SessionInfo's session_id is recorded on JobInfo.session_id so the
    UI can resolve it during the run; after the job ends, this drops
    the session so subsequent /sessions/{sid}/* requests cleanly 404
    instead of being routed to a worker that already disconnected
    its half of the registration.
    """
    if info is None or state.sessions is None:
        return
    sid = getattr(info, "session_id", None)
    if not sid:
        return
    try:
        state.sessions.remove(sid)
    except Exception:
        pass


async def _reconcile_worker_sessions(worker, snapshots: list) -> None:
    """Receive a worker's session announce and bring the hub-side
    SessionRegistry in sync with what the worker actually holds.

    Three passes:

      1. Drop hub-side entries for this worker that the worker does
         NOT have anymore. This covers worker restarts (= worker
         comes back with no sessions; we let go of all of them).

      2. For each session the worker reports:
           * already in hub: confirm worker_id matches + patch
             missing lane_idx from the snapshot,
           * not in hub but parent JobInfo exists and references the
             same session_id (Fetch keep_session) OR has the worker's
             snapshot.job_id (codegen-loop): rebuild a fresh
             SessionInfo from the snapshot + JobInfo,
           * not in hub + no JobInfo trail: tell the worker to end
             the session (= true orphan).

      3. Update worker.in_flight to match the count of lanes the
         worker is actually holding, so the scheduler doesn't
         over-dispatch.

    This is the cure for the "no free lane in pool" cascade we kept
    hitting after hub restarts: previously the hub forgot all
    sessions on restart, didn't know which workers had lanes held
    by detached keepalive sessions, and dispatched new jobs to
    those (apparently free) workers -- which then immediately
    failed because the lane pool was actually full. With this
    reconcile every WS connect (= worker restart, hub restart,
    network blip) brings the hub view back in line with reality.
    """
    if state.sessions is None or state.registry is None:
        return
    if state.store is None:
        return

    snapshot_by_sid = {s.session_id: s for s in snapshots}
    snapshot_sids = set(snapshot_by_sid.keys())

    # Pass 1: drop hub-side entries the worker doesn't have anymore.
    hub_for_worker = {
        s.session_id: s for s in state.sessions.all() if s.worker_id == worker.worker_id
    }
    dropped = 0
    for sid in set(hub_for_worker.keys()) - snapshot_sids:
        try:
            state.sessions.remove(sid)
            dropped += 1
        except Exception:
            pass

    # Pass 2: confirm / rebuild / orphan-cleanup per worker snapshot.
    confirmed = 0
    rebuilt = 0
    orphaned = 0
    for snap in snapshots:
        sid = snap.session_id
        existing = hub_for_worker.get(sid)
        if existing is not None:
            # Patch missing lane_idx if the worker has a real value.
            if existing.lane_idx is None and snap.lane_idx is not None:
                existing.lane_idx = snap.lane_idx
            # Mirror detached/state in case our memory drifted.
            if snap.is_fetch_owned:
                existing.state = "fetch_running"
            elif snap.detached:
                existing.detached = True
            confirmed += 1
            continue
        # Not in hub. Try to rebuild from JobInfo.
        rebuilt_from_job = False
        if snap.job_id:
            try:
                job = await state.store.get_job_info(snap.job_id)
            except Exception:
                job = None
            if job is not None:
                # Reasonable TTLs based on detached flag (mirrors the
                # original dispatch defaults).
                idle_ttl = 120 if snap.detached else 600
                abs_ttl = 24 * 3600 if snap.detached else 3600
                sinfo = SessionInfo(
                    session_id=sid,
                    worker_id=worker.worker_id,
                    lane_idx=snap.lane_idx,
                    novnc_url=snap.novnc_url,
                    initial_url=snap.initial_url or job.url,
                    idle_ttl_s=idle_ttl,
                    absolute_ttl_s=abs_ttl,
                    job_id=snap.job_id,
                )
                if snap.is_fetch_owned:
                    sinfo.state = "fetch_running"
                else:
                    sinfo.state = "idle"
                sinfo.detached = snap.detached
                try:
                    state.sessions.add(sinfo)
                    rebuilt += 1
                    rebuilt_from_job = True
                except Exception:
                    pass
        if not rebuilt_from_job:
            # No JobInfo trail -> true orphan. Tell the worker to end
            # this session so its lane comes back to the pool.
            try:
                await worker.end_session(sid, timeout=10.0)
                orphaned += 1
            except Exception:
                pass

    # Pass 3: in_flight reconcile. Count = lanes the worker is
    # holding (snapshot length, since each snapshot has a lane_idx).
    # Scheduler picks workers by in_flight < capacity, so this stops
    # the over-dispatch failure mode after hub restarts.
    try:
        worker.in_flight = len(snapshots)
    except Exception:
        pass

    log.info(
        "reconcile worker %s: snapshots=%d confirmed=%d rebuilt=%d "
        "orphaned=%d dropped=%d -> in_flight=%d",
        worker.worker_id,
        len(snapshots),
        confirmed,
        rebuilt,
        orphaned,
        dropped,
        worker.in_flight,
    )


async def _handle_worker_message(worker, msg) -> None:
    """Dispatch a worker->hub message."""
    assert state.store is not None and state.registry is not None

    if isinstance(msg, WorkerHeartbeat):
        await state.registry.heartbeat(worker.worker_id, msg.in_flight)
        # Mirror the cache snapshot onto the ConnectedWorker so
        # GET /workers can render it without an extra round-trip
        # to the worker. The list is short (typical operator has
        # ≤ 5 profiles) so a per-heartbeat copy is fine.
        try:
            worker.profiles_cached = [p.model_dump() for p in (msg.profiles_cached or [])]
        except Exception:
            worker.profiles_cached = []
        return

    if isinstance(msg, WorkerJobAccepted):
        try:
            jid_short = (msg.job_id or "")[:8]
            lane = msg.lane_idx if msg.lane_idx is not None else "?"
            state.registry.log_event(
                worker.worker_id,
                f"[{jid_short}] accepted (lane={lane})",
                kind="info",
            )
        except Exception:
            pass
        info = await state.store.get_job_info(msg.job_id)
        if info is not None:
            if info.status == JobStatus.queued:
                info.status = JobStatus.running
                info.started_at = datetime.utcnow()
                info.progress.phase = "running"
            # Per-job lane noVNC URL (overrides worker-level URL set at assign)
            if msg.novnc_url:
                nv = msg.novnc_url
                sep = "&" if "?" in nv else "?"
                info.novnc_url = (
                    f"{nv}{sep}autoconnect=1&resize=scale&reconnect=1"
                    if "autoconnect" not in nv
                    else nv
                )
            if msg.lane_idx is not None:
                info.lane_idx = msg.lane_idx
            await state.store.save_job_info(info)
            # Fetch-mode sessions are registered at dispatch with
            # lane_idx=None (the worker hadn't picked a lane yet at
            # that point -- it's the worker's lane pool that does the
            # assignment, not the hub). Propagate the lane_idx onto
            # the SessionInfo now that the worker has reported it,
            # otherwise _resolve_session_novnc_target() bails with
            # "session 'ses_...' not found or not bound to a lane"
            # the moment the operator opens noVNC.
            # POST /sessions sessions (Code / LLM via paprika-runner)
            # already get this set from the session-start ack, so this
            # only affects the Fetch path.
            if msg.lane_idx is not None and info.session_id and state.sessions is not None:
                sinfo = state.sessions.get(info.session_id)
                if sinfo is not None and sinfo.lane_idx is None:
                    sinfo.lane_idx = msg.lane_idx
        return

    if isinstance(msg, WorkerJobProgress):
        info = await state.store.get_job_info(msg.job_id)
        if info is not None:
            if msg.phase:
                info.progress.phase = msg.phase
            info.progress.assets_saved = msg.assets_saved
            info.progress.assets_failed = msg.assets_failed
            await state.store.save_job_info(info)
        return

    if isinstance(msg, WorkerJobLog):
        # EPHEMERAL markers: broadcast to live /events viewers (progress
        # bars, the Network tab) but do NOT persist them -- per-poll JSON
        # deltas would flood log.txt and the per-worker ring buffer,
        # re-creating the very noise we filter out elsewhere.  publish_log
        # (no append_log_line, no ring mirror) is broadcast-only; markers
        # are never replayed on reconnect because they're not in the
        # stored log.
        #   * JOB_PROGRESS_MARKER -> per-download progress bars
        #   * NET_CAPTURE_MARKER  -> live Network tab (captured URLs)
        #   * ASSET_CAPTURE_MARKER -> gallery refresh signal
        #   * LINKS_CAPTURE_MARKER -> links tab refresh signal
        if msg.line.startswith(
            (
                JOB_PROGRESS_MARKER,
                NET_CAPTURE_MARKER,
                ASSET_CAPTURE_MARKER,
                LINKS_CAPTURE_MARKER,
            )
        ):
            try:
                await state.store.publish_log(msg.job_id, msg.line)
            except Exception:
                pass
            return
        # When the LogBatcher is active (Redis store), buffer the line
        # and let it flush in pipeline batches (50 lines or 100ms).
        # This cuts Redis ops from ~10 000/sec to ~200/sec at scale.
        # For InMemoryJobStore the batcher is None and the direct path is
        # zero-cost; for the MariaDB store the batcher is also None and the
        # direct path runs a synchronous INSERT right here.
        #
        # Persisting a log line is best-effort telemetry and must NEVER
        # tear down the worker control link. A store error -- e.g. a
        # MariaDB FK violation when the parent job was deleted mid-stream
        # (job_logs -> jobs is ON DELETE CASCADE), a deadlock, or a
        # timeout -- would otherwise escape the WS receive loop and
        # disconnect the worker, sending the whole fleet into a
        # reconnect/restart storm.
        try:
            if state.log_batcher is not None:
                await state.log_batcher.add(msg.job_id, msg.line)
            else:
                await state.store.append_log_line(msg.job_id, msg.line)
                await state.store.publish_log(msg.job_id, msg.line)
        except Exception:
            log.debug(
                "drop log line for job %s (store error)",
                msg.job_id, exc_info=True,
            )
        # Mirror onto the per-worker ring buffer so the operator can
        # browse "what has this worker been doing" without correlating
        # job ids by hand. Short job-id prefix keeps lines greppable.
        try:
            jid_short = (msg.job_id or "")[:8]
            state.registry.log_event(
                worker.worker_id,
                f"[{jid_short}] {msg.line}",
                kind="job",
            )
        except Exception:
            pass
        return

    if isinstance(msg, WorkerJobComplete):
        try:
            jid_short = (msg.job_id or "")[:8]
            n_assets = len(getattr(msg.result, "assets", []) or [])
            state.registry.log_event(
                worker.worker_id,
                f"[{jid_short}] complete (assets={n_assets})",
                kind="info",
            )
        except Exception:
            pass
        # GPU gate release (P): if this was a codegen-loop job, free its
        # slot so the next queued codegen-loop job can dispatch.
        # Idempotent; safe even if it wasn't a codegen-loop job.
        try:
            from server.hub._gpu_gate import unregister_codegen_loop
            unregister_codegen_loop(msg.job_id)
        except Exception:
            pass
        info = await state.store.get_job_info(msg.job_id)
        keep_session_active = False
        if info is not None:
            try:
                keep_session_active = bool(getattr(info.options, "keep_session", False))
            except Exception:
                keep_session_active = False

        if info is not None:
            if keep_session_active:
                # Crawl finished but the session is alive. Keep the
                # job in "running" state so:
                #   * the standalone /screenshots tile keeps showing
                #     RUNNING (driven by jobs.status==running),
                #   * the LivePreview tab keeps polling /preview,
                #   * the noVNC link in admin UI stays clickable,
                #   * the operator's mental model stays consistent
                #     ("session is alive" == "job is running").
                # The job transitions to "completed" when the session
                # is finally closed (= reaper / operator DELETE) --
                # see close_session() cascade.
                info.status = JobStatus.running
                info.progress.phase = "keepalive"
            else:
                info.status = JobStatus.completed
                info.progress.phase = "completed"
                info.completed_at = datetime.utcnow()
            info.progress.assets_saved = len(msg.result.assets)
            info.progress.assets_failed = msg.result.assets_failed
            await state.store.save_job_info(info)
        await state.store.save_job_result(msg.result)
        # v2 Phase 1: end-of-job perception observation (fire-and-forget).
        # Runs the eye (Qwen3 / vision LLM) on the captured page artifacts
        # and saves PerceptionResult to data/jobs/{id}/perception.json.
        # Observation-only; never affects job outcome. Backgrounded so the
        # WS handler doesn't wait for the LLM. See
        # internal/v2-architecture.html for context.
        # v2 Phase 5: also bumps HostKnowledge.stats once the perception
        # write completes (the lightweight distiller).
        if info is not None and not keep_session_active:
            try:
                from server.hub.perception_llm import save_perception_for_job
                from server.hub.distiller_light import (
                    host_from_url,
                    record_job_outcome,
                )

                async def _perception_bg(jid: str, jurl: str, jstatus: str, jerror: str, jmode: str | None) -> None:
                    try:
                        await asyncio.wait_for(
                            save_perception_for_job(
                                job_id=jid,
                                url=jurl,
                                data_dir=get_storage_dir(),
                                log=None,
                                mode=jmode,
                                success=(jstatus == "completed"),
                            ),
                            timeout=90.0,
                        )
                    except TimeoutError:
                        log.info("perception observation timed out for job %s", jid)
                    except Exception as e:
                        log.info(
                            "perception observation crashed for %s: %s: %s",
                            jid,
                            type(e).__name__,
                            e,
                        )
                    # Phase 5 lightweight distiller -- runs regardless of
                    # perception outcome. Fetch jobs always reach this
                    # branch (WorkerJobComplete = success path).
                    try:
                        h = host_from_url(jurl)
                        if h:
                            record_job_outcome(
                                host=h,
                                success=(jstatus == "completed"),
                                job_id=jid,
                                reason=(jerror or "")[:200] if jstatus != "completed" else "",
                                data_dir=get_storage_dir(),
                            )
                    except Exception as e:
                        log.info(
                            "distiller-light crashed for %s: %s: %s",
                            jid,
                            type(e).__name__,
                            e,
                        )
                    # v2 Phase 6: R1 Distiller (deep updates). Gated by
                    # PAPRIKA_R1_DISTILLER_MODE. For fetch jobs there's
                    # no codegen script; we send goal=url so R1 has
                    # context. perception.json was just written by
                    # save_perception_for_job() above.
                    try:
                        import json as _json
                        from server.hub.distiller_r1 import distill_for_job
                        h2 = host_from_url(jurl)
                        if h2:
                            perception_dict = None
                            try:
                                _pp = get_storage_dir() / jid / "perception.json"
                                if _pp.is_file():
                                    perception_dict = _json.loads(_pp.read_text(encoding="utf-8"))
                            except Exception:
                                pass
                            await distill_for_job(
                                host=h2,
                                job_id=jid,
                                goal=jurl,
                                success=(jstatus == "completed"),
                                error=jerror or "",
                                perception=perception_dict,
                                stdout_tail="",
                                stderr_tail="",
                                script="",
                                data_dir=get_storage_dir(),
                            )
                    except Exception as e:
                        log.info(
                            "distiller-r1 crashed for %s: %s: %s",
                            jid,
                            type(e).__name__,
                            e,
                        )

                asyncio.create_task(_perception_bg(
                    msg.job_id, info.url,
                    info.status.value if hasattr(info.status, "value") else str(info.status),
                    info.error or "",
                    (info.options.mode if info.options else None),
                ))
            except Exception as e:
                # Import failure / unexpected; never disrupt job completion.
                log.info(
                    "could not schedule perception observation: %s: %s",
                    type(e).__name__,
                    e,
                )
        # Only release the worker's in_flight counter when the job is
        # actually done. For keep_session, the worker's lane is still
        # held by the live session -- if we decremented in_flight,
        # the scheduler would over-dispatch to this worker thinking
        # the lane was free, and the next job would get "no free
        # lane in pool". in_flight is restored when close_session
        # cascade fires.
        if not keep_session_active:
            state.registry.release(worker.worker_id, msg.job_id)
        # Tear down the read-only fetch-inspection session we eagerly
        # registered at dispatch. EXCEPTION: keep_session jobs keep
        # the browser + SessionInfo alive past fetch completion so
        # the operator can interact via noVNC / refresh / etc.
        if not keep_session_active:
            _drop_fetch_session_if_any(info)
        else:
            # Flip session state out of "fetch_running" so the reaper
            # considers it for the idle timeout (the reaper skips
            # "running" / "fetch_running" to avoid mid-action eviction).
            # Reset last_active_at = now() so the 2-min countdown
            # starts from THIS moment.
            if state.sessions is not None and info is not None and info.session_id:
                sinfo = state.sessions.get(info.session_id)
                if sinfo is not None:
                    sinfo.state = "idle"
                    sinfo.detached = True
                    sinfo.last_active_at = datetime.utcnow()
        # DONE_SENTINEL only for actually-completed jobs; keepalive
        # jobs are still "running" so the live log stream stays open.
        if not keep_session_active:
            await state.store.publish_log(msg.job_id, DONE_SENTINEL)
        return

    if isinstance(msg, WorkerJobFailed):
        try:
            jid_short = (msg.job_id or "")[:8]
            err = (msg.error or "")[:200]
            state.registry.log_event(
                worker.worker_id,
                f"[{jid_short}] failed: {err}",
                kind="error",
            )
        except Exception:
            pass
        # GPU gate release (P): if this was a codegen-loop job, free its
        # slot so the next queued codegen-loop job can dispatch.
        # Idempotent; safe even if it wasn't a codegen-loop job.
        try:
            from server.hub._gpu_gate import unregister_codegen_loop
            unregister_codegen_loop(msg.job_id)
        except Exception:
            pass
        info = await state.store.get_job_info(msg.job_id)
        if info is not None:
            info.status = JobStatus.failed
            info.progress.phase = "failed"
            info.error = msg.error
            info.completed_at = datetime.utcnow()
            await state.store.save_job_info(info)
        await state.store.save_job_result(
            JobResult(
                job_id=msg.job_id,
                status=JobStatus.failed,
                error=msg.error,
            )
        )
        state.registry.release(worker.worker_id, msg.job_id)
        _drop_fetch_session_if_any(info)
        await state.store.publish_log(msg.job_id, DONE_SENTINEL)
        # v2 Phase 5: distiller-light also fires on failures so
        # success_rate trends downward when a host starts breaking.
        # Backgrounded; never blocks the WS handler.
        if info is not None:
            try:
                from server.hub.distiller_light import (
                    host_from_url,
                    record_job_outcome,
                )

                async def _distiller_fail_bg(jid: str, jurl: str, jerror: str) -> None:
                    try:
                        h = host_from_url(jurl)
                        if h:
                            record_job_outcome(
                                host=h,
                                success=False,
                                job_id=jid,
                                reason=jerror or "",
                                data_dir=get_storage_dir(),
                            )
                    except Exception as e:
                        log.info(
                            "distiller-light (failure) crashed for %s: %s: %s",
                            jid,
                            type(e).__name__,
                            e,
                        )

                asyncio.create_task(_distiller_fail_bg(
                    msg.job_id, info.url, msg.error or "",
                ))
            except Exception as e:
                log.info(
                    "could not schedule distiller-light on failure: %s: %s",
                    type(e).__name__,
                    e,
                )
        return

    if isinstance(msg, WorkerScreenshotReply):
        worker.deliver_screenshot_reply(msg)
        return

    if isinstance(msg, WorkerSessionStartAck):
        worker.deliver_session_start_ack(msg)
        return
    if isinstance(msg, WorkerSessionActionResult):
        worker.deliver_session_action_result(msg)
        # Touch the session so future TTL enforcement sees activity.
        if state.sessions is not None:
            state.sessions.touch(msg.session_id)
        return
    if isinstance(msg, WorkerSessionEndAck):
        worker.deliver_session_end_ack(msg)
        return
    if isinstance(msg, WorkerSessionAgentResult):
        worker.deliver_session_agent_result(msg)
        if state.sessions is not None:
            state.sessions.touch(msg.session_id)
        return
    if isinstance(msg, WorkerSessionAnnounce):
        await _reconcile_worker_sessions(worker, msg.sessions or [])
        return
