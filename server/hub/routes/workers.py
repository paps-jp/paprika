"""Worker registry HTTP routes: /workers list, host inventory, status
toggle, source tarball download.

The WebSocket control channel (``/workers/{id}/link``) and the lane
preview endpoint (``/workers/{id}/lanes/{idx}/preview``) stay in
app.py for now -- the WS handler is the 500-line worker protocol
loop and the preview depends on session-routing code that hasn't
migrated yet. Both follow when their dependencies do.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

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


# ----------------------------------------------------------------------------
# /workers/capacity: Stale-While-Revalidate cache.
#
# Crawler clients poll this at ~5-10 req/s to gate job submission. Each
# request does a cross-hub stats_async() + a MariaDB COUNT on queued jobs,
# and BOTH can spike to 1-3s under hub event-loop pressure (see
# [[hub-eventloop-stalls]]). With the crawler's short HTTP timeout, that
# spike shows up as a wave of HTTP 499 (client closed request) at the nginx
# edge -- "20 連続失敗" complaint, incident 2026-06-15.
#
# Strategy: stale-while-revalidate (SWR).
#   * cache FRESH (within TTL): return instantly.
#   * cache STALE (past TTL but exists): return STALE instantly, kick off a
#     background refresh task. Subsequent calls keep returning stale until
#     the background refresh updates the cache. A slow underlying compute
#     thus NEVER blocks a request -- the worst latency a client sees after
#     the first-ever request is microseconds.
#   * NO cache (first request after boot): compute synchronously, populate.
#
# Tunable via env ``PAPRIKA_WORKERS_CAPACITY_CACHE_S`` (default 1.0; set to
# 0 to disable). A higher TTL just means more stale data; under SWR the
# refresh still keeps it converging.
# ----------------------------------------------------------------------------
try:
    _CAPACITY_CACHE_TTL_S = float(
        os.environ.get("PAPRIKA_WORKERS_CAPACITY_CACHE_S") or 1.0
    )
except (TypeError, ValueError):
    _CAPACITY_CACHE_TTL_S = 1.0
_CAPACITY_CACHE_TTL_S = max(0.0, _CAPACITY_CACHE_TTL_S)
_capacity_cache: tuple[dict | None, float] = (None, 0.0)
_capacity_lock = asyncio.Lock()
_capacity_refresh_in_flight = False


async def _refresh_capacity_cache_bg() -> None:
    """Background-task entry point: recompute and update the SWR cache.
    Coalesced via ``_capacity_refresh_in_flight`` so a burst of stale-cache
    hits only schedules ONE background refresh."""
    global _capacity_cache, _capacity_refresh_in_flight
    try:
        data = await _compute_capacity()
        _capacity_cache = (data, time.monotonic() + _CAPACITY_CACHE_TTL_S)
    except Exception:
        log.warning(
            "workers_capacity: background refresh failed",
            exc_info=True,
        )
    finally:
        _capacity_refresh_in_flight = False


async def _compute_capacity() -> dict:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    try:
        load_factor = float(os.environ.get("PAPRIKA_FETCH_LOAD_FACTOR") or 0.8)
    except (TypeError, ValueError):
        load_factor = 0.8
    load_factor = max(0.05, min(load_factor, 1.0))
    if state.registry is None:
        return {
            "max_concurrent": 0, "recommended_concurrency": 0, "available": 0,
            "running": 0, "utilization_pct": 0, "load_factor": load_factor,
            "lanes": {"total": 0, "busy": 0, "free": 0},
            "workers": {"eligible": 0, "active": 0, "alive": 0},
            "queued": None, "note": "no worker registry", "as_of": now_iso,
        }
    payload = await state.registry.stats_async()
    workers = payload.get("workers", [])

    def _cap(w) -> int:
        c = w.get("capacity")
        return int(c) if isinstance(c, (int, float)) and c and c > 0 else 1

    active = [
        w for w in workers
        if w.get("alive") and (w.get("status") or "active") == "active"
    ]
    max_concurrent = sum(_cap(w) for w in active)
    running = sum(int(w.get("in_flight") or 0) for w in active)

    def _dispatchable(w) -> bool:
        return (
            len(w.get("lane_novnc_urls") or []) > 0
            or len(w.get("slot_novnc_urls") or []) > 0
            or bool(w.get("novnc_url"))
        ) and float(w.get("disk_pct") or 0.0) < 90.0

    eligible = [w for w in active if _dispatchable(w)]
    available = sum(max(0, _cap(w) - int(w.get("in_flight") or 0)) for w in eligible)

    # Best-effort queued backlog (one indexed COUNT; never fail the endpoint).
    queued = None
    try:
        _lji = getattr(state.store, "list_job_infos", None)
        if callable(_lji):
            _, queued = await _lji(status=["queued"], limit=1)
    except Exception:
        queued = None

    return {
        "max_concurrent": max_concurrent,
        "recommended_concurrency": round(max_concurrent * load_factor),
        "load_factor": load_factor,
        "available": available,
        "running": running,
        "utilization_pct": round(100 * running / max_concurrent) if max_concurrent else 0,
        "lanes": {"total": max_concurrent, "busy": running, "free": available},
        "workers": {
            "eligible": len(eligible),
            "active": len(active),
            "alive": sum(1 for w in workers if w.get("alive")),
        },
        "queued": queued,
        "note": (
            "1 fetch = 1 lane; any free lane runs a fetch. Cap parallel fetches "
            "at `available`; beyond it, jobs queue (redrive) or POST /jobs 503s."
        ),
        "as_of": now_iso,
    }


@router.get("/workers/capacity")
async def workers_capacity() -> dict:
    """How many fetches the fleet can run AT ONCE (concurrent-fetch capacity).

    A fetch -- like every job -- runs on one worker *lane* (a worker's
    ``capacity`` = its ``max_concurrent`` lanes), and fetch shares lanes with
    all modes, so **max simultaneous fetches = total eligible lanes**.

    * ``max_concurrent`` -- the HARD ceiling (sum of active workers' lanes).
    * ``recommended_concurrency`` -- what a client should actually cap at:
      ``round(max_concurrent * load_factor)`` (``load_factor`` env
      ``PAPRIKA_FETCH_LOAD_FACTOR``, default 0.8 = 80% of capacity). Reserves
      headroom for worker churn / bursts / non-fetch jobs (keeps the fleet out
      of saturation). This is the number to use, not the raw ``max_concurrent``.
    * ``available`` -- free lanes you can start RIGHT NOW without queuing;
      beyond it, new jobs queue (redrive) or ``POST /jobs`` returns 503. This is
      the number a client should cap its parallel fetches at.
    * ``running`` -- lanes busy now.

    Fleet-wide (all hubs, via the same cross-hub aggregation ``/workers`` uses,
    so it survives a single hub's Redis hiccup). ``available`` mirrors the
    dispatcher's eligibility (:meth:`pick_worker`): alive + status ``active`` +
    has a Chrome lane + disk < 90%. Lightweight: no per-job hydration.

    Response is cached for ``PAPRIKA_WORKERS_CAPACITY_CACHE_S`` (default 1.0s)
    under a stale-while-revalidate policy: past the TTL, the LAST KNOWN value
    is returned instantly while a background task recomputes -- so a slow
    underlying compute (hub event-loop spike) NEVER stalls a request after
    the first-ever boot. Set the env to ``0`` to disable caching entirely."""
    if _CAPACITY_CACHE_TTL_S <= 0:
        return await _compute_capacity()
    global _capacity_cache, _capacity_refresh_in_flight
    now = time.monotonic()
    cached, expires_at = _capacity_cache
    if cached is not None and now < expires_at:
        return cached  # FRESH
    if cached is not None:
        # STALE: return last-known immediately and kick off a single background
        # refresh (the ``_capacity_refresh_in_flight`` flag coalesces a burst
        # of stale hits into one recompute).
        if not _capacity_refresh_in_flight:
            _capacity_refresh_in_flight = True
            try:
                asyncio.create_task(_refresh_capacity_cache_bg())
            except RuntimeError:
                # No running loop (highly unlikely inside an HTTP handler);
                # reset the flag so a future request can try again.
                _capacity_refresh_in_flight = False
        return cached
    # NO cache yet -- first request after this hub's boot. Compute under the
    # lock so a thundering herd of first requests collapses to one compute.
    async with _capacity_lock:
        cached, expires_at = _capacity_cache
        if cached is not None and time.monotonic() < expires_at:
            return cached
        data = await _compute_capacity()
        _capacity_cache = (data, time.monotonic() + _CAPACITY_CACHE_TTL_S)
        return data


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


# These tarballs were rebuilt -- tar+gzip of the whole server+core tree --
# SYNCHRONOUSLY ON THE EVENT LOOP for EVERY worker self-update request. During
# a self-update wave (fleet version churn) that blocked the loop for seconds
# per build, stalling /jobs, /overview, even /health (confirmed via py-spy:
# tarfile.add -> gzip.write running on the loop). Now: cache the bytes keyed by
# source version, (re)build at most once per version, and do the build in a
# worker thread so the loop never blocks on tar+gzip. The bytes are identical
# for every worker on a given version, so one build serves the whole wave.
_TARBALL_CACHE: dict[str, bytes] = {}
_TARBALL_LOCK = asyncio.Lock()


async def _cached_tarball(kind: str, builder) -> tuple[str, bytes]:
    """Return ``(version, tarball_bytes)`` for ``kind`` ("source"/"plugins"),
    building at most once per ``_hub_version()`` and off the event loop."""
    from server.hub._version import _hub_version

    ver = _hub_version()
    key = f"{kind}:{ver}"
    data = _TARBALL_CACHE.get(key)
    if data is not None:
        return ver, data
    async with _TARBALL_LOCK:
        data = _TARBALL_CACHE.get(key)  # double-check after awaiting the lock
        if data is None:
            data = await asyncio.to_thread(builder)
            # Keep only current-version entries so the cache can't grow.
            for k in [k for k in _TARBALL_CACHE if not k.endswith(":" + ver)]:
                _TARBALL_CACHE.pop(k, None)
            _TARBALL_CACHE[key] = data
    return ver, data


@router.post("/internal/prepare-restart")
async def prepare_restart(timeout_s: float = 120.0) -> dict:
    """Graceful pre-restart drain (in-flight protection, layer 1).

    Marks this hub's locally-connected workers ``drain`` -- so this hub stops
    handing them NEW jobs and (via the Redis status mirror) peer hubs' cross-hub
    dispatch routes new jobs elsewhere -- then waits up to ``timeout_s`` for
    their in-flight jobs to finish (local in_flight -> 0). A deploy calls this
    BEFORE ``docker restart`` so the restart doesn't fail in-flight work.
    Workers reset to ``active`` on their reconnect after the restart, so there
    is nothing to un-drain. Returns ok=False (with remaining_in_flight) if the
    timeout elapses first -- e.g. long-lived keepalive sessions hold a lane and
    won't drain; those need the layer-2 reconnect-reattach / Redis sessions."""
    if state.registry is None:
        return {"ok": False, "reason": "registry not ready"}
    import asyncio as _asyncio
    drained = await state.registry.drain_local_workers()
    _deadline = time.monotonic() + max(0.0, float(timeout_s))
    inflight = state.registry.local_in_flight()
    while inflight > 0 and time.monotonic() < _deadline:
        await _asyncio.sleep(1.0)
        inflight = state.registry.local_in_flight()
    log.info(
        "prepare-restart: drained %d local worker(s); remaining in_flight=%d",
        drained, inflight,
    )
    return {
        "ok": inflight <= 0,
        "drained_workers": drained,
        "remaining_in_flight": inflight,
    }


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
    try:
        ver, data = await _cached_tarball("source", _build_worker_source_tarball)
    except Exception as e:
        raise HTTPException(500, f"failed to build tarball: {e}")
    headers = {
        # Version baked into the response so the operator can sanity-
        # check what they're about to ship out to the fleet (curl -I).
        "X-Paprika-Version": ver,
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
    try:
        ver, data = await _cached_tarball("plugins", _build_worker_plugins_tarball)
    except Exception as e:
        raise HTTPException(500, f"failed to build plugins tarball: {e}")
    headers = {
        "X-Paprika-Version": ver,
        "Content-Disposition": 'attachment; filename="paprika-worker-plugins.tar.gz"',
    }
    return Response(content=data, media_type="application/gzip", headers=headers)


async def _maybe_forward_worker(
    worker_id: str, request: Request, *, forward_timeout: float = 30.0,
):
    """If ``worker_id``'s control WS isn't held by THIS hub but the Redis
    owner lease says a peer hub owns it, reverse-proxy the request there
    and return that Response. Returns None = "handle locally": the single-
    hub path, a worker connected here, an already-forwarded hop (loop
    guard via _FWD_MARK), or a worker owned by no live hub (let the local
    handler 404 / forget as before).

    This is the worker-scoped twin of sessions._maybe_forward_session --
    needed because /workers/{id}/* requests round-robin across hubs but a
    worker's WS (status, logs, screenshot RPCs) lives on exactly one hub.
    """
    if state.registry is None:
        return None
    if state.registry.connections.get(worker_id) is not None:
        return None
    from server.hub.routes.sessions import _FWD_MARK, _proxy_request_to_hub
    if request.headers.get(_FWD_MARK):
        return None
    try:
        owner = await state.registry.owner_of(worker_id)
    except Exception:
        owner = None
    if not owner or owner == (config.hub_id or ""):
        return None
    return await _proxy_request_to_hub(owner, request, forward_timeout)


@router.post("/workers/{worker_id}/status")
async def set_worker_status(worker_id: str, body: dict, request: Request) -> dict:
    """Operator switch: active / drain / standby.

    * ``active``  -- normal scheduling.
    * ``drain``   -- skipped by pick_worker; in-flight jobs continue
                     to completion. Use before maintenance / rolling
                     restarts.
    * ``standby`` -- same scheduler effect as drain; semantically
                     "do not auto-resume". State is in-memory only
                     and resets to active when the hub restarts.
    """
    # Multi-hub: the status lives on the hub holding the worker's WS;
    # forward there when nginx routed this POST to a peer (else it 404s).
    fwd = await _maybe_forward_worker(worker_id, request)
    if fwd is not None:
        return fwd
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
async def delete_worker(worker_id: str, request: Request) -> dict:
    """Forget a worker entirely (Redis row + index + in-process logs).

    The Workers tab keeps disconnected workers visible so operators can
    review what ran where -- but eventually the list gets noisy with
    one-off / decommissioned hosts. This endpoint lets the operator
    prune them with the trash-can button next to each row.

    Refuses with 409 if the worker is currently connected: the operator
    should drain it (status=drain) and disconnect it first; otherwise
    we'd be racing the WS loop. Returns 404 if the id is unknown.
    """
    # Multi-hub: a worker connected to a PEER hub looks "not connected"
    # locally, so a non-owner hub would wrongly skip the 409 guard and
    # forget a LIVE worker. Forward to the owner, which sees its WS and
    # refuses correctly. A worker owned by no live hub (owner_of -> None)
    # is genuinely offline -> handle locally and forget.
    fwd = await _maybe_forward_worker(worker_id, request)
    if fwd is not None:
        return fwd
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


async def _fetch_peer_events(
    owner_hub: str, *, limit: int, kinds: str, since_s: float, timeout: float = 4.0,
) -> list[dict]:
    """GET a peer hub's LOCAL recovery events for the fleet-wide merge. The
    existing ``_FWD_MARK`` header makes the peer skip its own fan-out, so
    there's no recursion -- same pattern as sessions._fetch_peer_sessions.
    Best-effort: any failure yields [] so one slow peer never breaks merge."""
    try:
        import httpx
        from server.hub.routes.sessions._base import _FWD_MARK, _hub_internal_url
        headers = {_FWD_MARK: config.hub_id or "1"}
        if getattr(config, "worker_secret", None):
            headers["X-Paprika-Worker-Secret"] = config.worker_secret
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.get(
                _hub_internal_url(owner_hub, "/workers/events"),
                params={"limit": limit, "kinds": kinds, "since_s": since_s},
                headers=headers,
            )
            if r.status_code == 200:
                return (r.json() or {}).get("events") or []
    except Exception:
        pass
    return []


@router.get("/workers/recovery-events")
async def get_recovery_events_route(
    limit: int = 200,
    since_s: float = 0.0,
    worker_id: str = "",
) -> dict:
    """Durable, fleet-wide salvage recovery history (段階4 永続化). Backed by
    the shared MariaDB ``recovery_events`` ledger, so unlike GET
    /workers/events (in-memory ring buffer, per-hub) this survives hub
    restarts and reads identically on every hub -- no peer fan-out needed.
    Returns ``durable: false`` + empty when the store is in-memory (no
    MariaDB configured). Each event: ``{worker_id, hub_id, ip, method,
    result, detail, ts}``, recent-first."""
    store = getattr(state, "store", None)
    fn = getattr(store, "get_recovery_events", None)
    if fn is None:
        return {"count": 0, "events": [], "durable": False}
    try:
        evs = await fn(
            limit=max(1, min(int(limit), 1000)),
            since_s=(float(since_s) if since_s else None),
            worker_id=(worker_id or None),
        )
    except Exception:
        return {"count": 0, "events": [], "durable": True, "error": "query failed"}
    return {"count": len(evs), "events": evs, "durable": True}


@router.get("/workers/events")
async def get_worker_events(
    request: Request,
    limit: int = 200,
    kinds: str = "lifecycle,warn,error",
    since_s: float = 3600.0,
) -> dict:
    """Fleet-wide recent recovery/lifecycle events, aggregated across every
    worker on this hub.

    Backed by the same per-worker ring buffer ``GET /workers/{id}/logs``
    surfaces (server/scheduler.py log_event). Each entry is shaped:
    ``{"ts": <unix>, "kind": str, "line": str, "worker_id": str}``.

    Filters:
      * ``kinds``    — csv of ``info``/``job``/``warn``/``error``/``lifecycle``
                       (default = lifecycle+warn+error: "recovery interesting").
      * ``since_s``  — only events within this many seconds ago.
      * ``limit``    — cap on returned rows (after kind + since filter).

    Multi-hub note: ring buffers are in-memory on the hub that owns the
    worker's WS; under nginx round-robin a single GET sees only this hub's
    workers. The admin UI scopes to whichever hub it hit; for a true
    fleet-wide view the operator can switch hubs (rare for recovery audit).
    """
    if state.registry is None:
        raise HTTPException(503, "registry not ready")
    import time as _time
    allowed = {k.strip() for k in (kinds or "").split(",") if k.strip()}
    cutoff = _time.time() - max(0.0, float(since_s)) if since_s else 0.0
    out: list[dict] = []
    # Iterate the registry's per-worker buffers directly; get_logs() returns
    # a list copy per worker which would O(N*M) us up to 60w * 200 entries.
    try:
        logs_map = getattr(state.registry, "_worker_logs", {}) or {}
    except Exception:
        logs_map = {}
    for wid, buf in logs_map.items():
        if not wid or buf is None:
            continue
        for ev in list(buf):
            if not isinstance(ev, dict):
                continue
            k = ev.get("kind") or "info"
            ts = float(ev.get("ts") or 0.0)
            if allowed and k not in allowed:
                continue
            if cutoff and ts < cutoff:
                continue
            out.append({
                "worker_id": wid,
                "ts": ts,
                "kind": k,
                "line": ev.get("line") or "",
            })
    # Fleet-wide fan-out: unless this call is already a forwarded hop (the
    # peer's recursion guard), query every live peer hub for its local-buffer
    # slice and merge. Same pattern as sessions.list_sessions; the _FWD_MARK
    # header keeps peers from re-fanning. Best-effort: a slow peer just
    # contributes nothing.
    try:
        from server.hub.routes.sessions._base import _FWD_MARK
        _is_forwarded = bool(request.headers.get(_FWD_MARK))
    except Exception:
        _is_forwarded = False
    if not _is_forwarded and state.hubs is not None:
        import asyncio as _asyncio
        try:
            hubs = await state.hubs.list_all()
        except Exception:
            hubs = []
        peers = [
            h.get("hub_id")
            for h in hubs
            if h.get("alive") and h.get("hub_id") and h.get("hub_id") != config.hub_id
        ]
        if peers:
            results = await _asyncio.gather(
                *[
                    _fetch_peer_events(hid, limit=limit, kinds=kinds, since_s=since_s)
                    for hid in peers
                ],
                return_exceptions=True,
            )
            seen_keys = {(e.get("worker_id"), e.get("ts"), e.get("line")) for e in out}
            for res in results:
                if not isinstance(res, list):
                    continue
                for ev in res:
                    k = (ev.get("worker_id"), ev.get("ts"), ev.get("line"))
                    if k not in seen_keys:
                        seen_keys.add(k)
                        out.append(ev)
    out.sort(key=lambda e: (e.get("ts") or 0.0), reverse=True)
    if limit > 0:
        out = out[:limit]
    return {"count": len(out), "events": out, "kinds": sorted(allowed)}


@router.get("/workers/{worker_id}/logs")
async def get_worker_logs(worker_id: str, request: Request, limit: int = 200) -> dict:
    """Recent hub-side activity log for one worker.

    Backed by the in-memory ring buffer maintained by ``WorkerRegistry``;
    entries are dropped on hub restart. Each item:
    ``{"ts": <unix>, "kind": "info|job|warn|error|lifecycle", "line": "<text>"}``.

    Used by the admin UI's Workers tab "..." menu to surface "what has
    this worker been doing recently" without SSH access. Job-log lines
    come from forwarded WorkerJobLog messages and are prefixed with the
    short job id so the operator can correlate.
    """
    # Multi-hub: the ring buffer is in-memory on the hub that owns the
    # worker's WS; a peer hub has no entries for it. Forward to the owner
    # so the "..." logs menu isn't empty when nginx routes us to a peer.
    fwd = await _maybe_forward_worker(worker_id, request)
    if fwd is not None:
        return fwd
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
    request: Request,
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
        # Multi-hub: the admin UI is served by whichever hub nginx picked, but
        # this worker's control WS (and thus its screenshot capability) may live
        # on a PEER hub. Forward there -- one hop; a request that already carries
        # the forward marker is handled locally (loop guard).
        from server.hub.routes.sessions import _FWD_MARK, _proxy_request_to_hub
        if not request.headers.get(_FWD_MARK):
            try:
                owner = await state.registry.owner_of(worker_id)
            except Exception:
                owner = None
            if owner and owner != config.hub_id:
                # > the owner's ~8s capture deadline so we don't time out first.
                return await _proxy_request_to_hub(owner, request, 12.0)
        raise HTTPException(404, f"worker '{worker_id}' not connected")
    ffmpeg_q = _ffmpeg_q_from_quality_pct(quality)
    # Bound concurrency the SAME way the batch path does: every screenshot RPC
    # on this hub -- whether it arrived as a direct single-tile GET or was
    # forwarded here by a peer hub's cross-hub preview batch -- shares the one
    # per-hub _preview_semaphore. Without this, a peer's full-grid batch fans
    # ~20+ forwarded GETs at us at once and each fires an UNBOUNDED screenshot
    # RPC: the worker fleet gets slammed and captures that take ~0.1s in
    # isolation balloon past the deadline -> 504 storms on multi-hub #screens.
    # Forwarded tiles also take the short batch deadline so a busy worker
    # releases its slot fast; the focused single-tile panel (direct GET) keeps
    # the generous default so a human staring at one slow lane still gets it.
    from server.hub.routes.sessions import _FWD_MARK as _fwd_hdr
    forwarded = bool(request.headers.get(_fwd_hdr))
    rpc_timeout = _PREVIEW_RPC_TIMEOUT if forwarded else 8.0
    try:
        async with _preview_semaphore():
            reply = await worker.request_screenshot(
                lane_idx,
                max_width=width,
                quality=ffmpeg_q,
                timeout=rpc_timeout,
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
    request: Request,
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
        request=request,
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


_fwd_http_client = None  # one pooled httpx client for cross-hub preview forwards


def _fwd_http():
    """Lazily-created shared AsyncClient (keep-alive connection pool) for
    forwarding lane-preview captures to peer hubs. One client process-wide so
    the TCP connection to each peer hub is reused across tiles/polls instead of
    a fresh handshake per forward. Bound to the running loop on first use."""
    global _fwd_http_client
    if _fwd_http_client is None:
        import httpx
        _fwd_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(6.0, connect=2.0),
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=64),
        )
    return _fwd_http_client


async def _capture_via_owner(owner_hub, wid, lane, width, quality_pct):
    """Multi-hub: fetch one lane preview from the PEER hub that owns the
    worker's control WS (this hub can't capture a worker it doesn't hold).
    Returns (jpeg_b64, error) like _do_capture_frame. One hop -- the peer's
    single-preview endpoint runs locally (its _FWD_MARK guard stops a bounce)."""
    import base64

    from server.hub.routes.sessions import _FWD_MARK, _hub_internal_url
    url = _hub_internal_url(owner_hub, f"/workers/{wid}/lanes/{lane}/preview")
    headers = {_FWD_MARK: config.hub_id or "1"}
    if getattr(config, "worker_secret", ""):
        headers["X-Paprika-Worker-Secret"] = config.worker_secret
    try:
        # Reuse ONE pooled keep-alive client (see _fwd_http) instead of a fresh
        # AsyncClient per tile: a full #screens grid forwards ~30+ tiles to peer
        # hubs every poll, and a new client each meant ~30 cold TCP handshakes
        # per batch -- a big chunk of multi-hub batch latency. The 6s timeout
        # (down from 12s) bounds a slow/stalled peer so the batch fails that tile
        # FAST (-> stale-frame fallback) instead of hanging ~12-22s and letting
        # successive 5s polls pile into an event-loop stall. It still exceeds the
        # peer's 4s forwarded-capture deadline so a clean 504 wins the race.
        r = await _fwd_http().get(
            url,
            params={"width": width, "quality": quality_pct},
            headers=headers,
            timeout=6.0,
        )
    except Exception as e:
        return None, f"peer {owner_hub}: {type(e).__name__}"
    if r.status_code != 200:
        return None, f"peer {owner_hub} HTTP {r.status_code}"
    return base64.b64encode(r.content).decode("ascii"), None


# On a capture failure, reuse the most recent good frame if it's younger than
# this. A monitoring grid should ride out a transient hiccup -- a worker that's
# momentarily too busy to screenshot, or a peer hub that briefly stalls under
# live load (an event-loop block makes ALL its forwarded tiles time out at
# once) -- by showing the last frame for a poll or two instead of flashing a
# red "Capture failed" tile, then refreshing the instant a capture succeeds.
# A worker genuinely dark for longer than this finally surfaces the error.
# Ghosts never reach this path: the owner-gated alive set (scheduler
# _fetch_known_workers) keeps disconnected workers out of the grid entirely.
_PREVIEW_STALE_MAX = 30.0


def _stale_fallback(key, err):
    """Failed capture -> (last_good_b64, None, stale=True) when a recent frame
    is cached, else (None, err, False)."""
    hit = _PREVIEW_FRAME_CACHE.get(key)
    if hit is not None and hit[1] and (time.monotonic() - hit[0]) <= _PREVIEW_STALE_MAX:
        return hit[1], None, True
    return None, err, False


async def _do_capture_frame(key, quality_pct=30):
    """Serve one grid tile -- a PURE Redis cache read (push-based previews).
    Returns (jpeg_b64, error_str, stale): jpeg_b64 XOR error_str non-None;
    stale=True means the served frame is a recent-but-not-fresh push. Workers
    self-capture watched lanes on their own ~10s timer and PUSH frames the hub
    caches in Redis (interest-gated -- see scheduler.preview_subscribe_loop), so
    a full-grid poll is O(redis get) per tile with NO live capture and NO
    cross-hub hop. That decouples capture rate from admin poll rate, which is
    what eliminates the per-poll capture storm / hub-event-loop cascade. Never
    raises -- a single bad lane must not tear down the batch stream."""
    wid, lane, width, ffmpeg_q = key
    if state.registry is None:
        return None, "registry not ready", False
    reg = state.registry
    # Push-based fast path: if the worker self-captures and PUSHES frames, serve
    # the latest from Redis -- NO live capture, NO cross-hub forward. This is
    # what stops a full #screens grid from triggering a per-poll capture storm:
    # capture runs on the worker's own ~10s timer; the grid just reads cache.
    # Marking watch keeps the worker's OWNER hub subscribing it. Falls through
    # to the legacy live path for workers not yet pushing (mixed fleet / no redis).
    try:
        await reg.preview_mark_watch(wid)
        pf = await reg.preview_get_frame(wid, lane)
    except Exception:
        pf = None
    if pf and pf.get("b"):
        b64 = pf["b"]
        _PREVIEW_FRAME_CACHE[key] = (time.monotonic(), b64)
        try:
            stale = (time.time() - float(pf.get("t") or 0.0)) > 15.0
        except Exception:
            stale = False
        return b64, None, stale
    # No pushed frame yet (worker warming up its push loop, not watched long
    # enough, or an old build that doesn't push): serve the last-known frame if
    # still recent, else a light "warming" miss -- the worker's ~10s push
    # refreshes it shortly. The grid deliberately does NOT live-capture here:
    # a synchronous per-tile capture (esp. the cross-hub forward) is exactly
    # what made a full-grid poll cascade into a hub-event-loop stall under load.
    # On-demand live capture still lives in the focused single-tile GET
    # (worker_lane_preview), which is low-volume.
    return _stale_fallback(key, "warming")


async def _capture_one_frame(key, quality_pct=30):
    """Cache-first, coalescing wrapper around _do_capture_frame."""
    hit = _PREVIEW_FRAME_CACHE.get(key)
    if hit is not None and (time.monotonic() - hit[0]) <= _PREVIEW_CACHE_TTL:
        return hit[1], None, False
    task = _PREVIEW_INFLIGHT.get(key)
    if task is None:
        task = asyncio.ensure_future(_do_capture_frame(key, quality_pct))
        _PREVIEW_INFLIGHT[key] = task
        task.add_done_callback(lambda t, k=key: _PREVIEW_INFLIGHT.pop(k, None))
    # shield: if THIS request's client disconnects, our awaiter is cancelled
    # but the shared capture task keeps running -> it still populates the
    # cache for the next poll, and any coalesced waiters aren't torn down.
    return await asyncio.shield(task)


async def _preview_line(wid, lane, width, ffmpeg_q, quality_pct=30):
    """Produce one NDJSON line for a lane (capture + serialize)."""
    b64, err, stale = await _capture_one_frame((wid, lane, width, ffmpeg_q), quality_pct)
    rec = {"wid": wid, "lane": lane}
    if err:
        rec["error"] = err
    else:
        rec["jpeg_b64"] = b64
        if stale:
            rec["stale"] = True
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
            asyncio.ensure_future(_preview_line(wid, lane, width, ffmpeg_q, quality_pct))
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
from server.hub.sessions import SessionInfo, session_from_json
from server.protocol import (
    ASSET_CAPTURE_MARKER,
    JOB_PROGRESS_MARKER,
    LINKS_CAPTURE_MARKER,
    NET_CAPTURE_MARKER,
    HubRegistered,
    HubExpectedVersion,
    JobResult,
    JobStatus,
    HubUpdateGate,
    WorkerDraining,
    WorkerHeartbeat,
    WorkerJobAccepted,
    WorkerJobComplete,
    WorkerJobFailed,
    WorkerEngineUsage,
    WorkerJobLog,
    WorkerJobProgress,
    WorkerPreviewFrame,
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


# ----------------------------------------------------------------------------
# Rolling-update slot manager
#
# Replaces the previous "every worker self-updates as soon as it sees a new
# expected_worker_version" thundering herd, where 20+ workers would fetch the
# tarball + exit(42) within the same second, causing the entire fleet to be
# offline for 15-30s while every container restarted in lockstep.
#
# Now: workers send WorkerDraining on mismatch and wait. The hub grants at
# most ``_UPDATE_MAX_PARALLEL`` simultaneous "go ahead, fetch + exit" slots
# at a time so the fleet rolls forward in batches. Each grant also carries
# a randomised ``jitter_s`` (0..30s) so even within one batch the fetch +
# restart times don't perfectly align. When a granted worker disconnects
# (= it's about to restart on new code), its slot is freed and the next
# queued updater gets a green light.
#
# Self-healing: a granted worker that never disconnects (crash before
# restart, machine wedged) has its slot reclaimed after
# ``_UPDATE_SLOT_TIMEOUT_S`` so the queue can't deadlock indefinitely.
# ----------------------------------------------------------------------------
_UPDATE_MAX_PARALLEL = int(os.environ.get("PAPRIKA_ROLLING_UPDATE_MAX_PARALLEL", "3"))
_UPDATE_SLOT_TIMEOUT_S = float(os.environ.get("PAPRIKA_ROLLING_UPDATE_SLOT_TIMEOUT_S", "600"))
_UPDATE_JITTER_MAX_S = float(os.environ.get("PAPRIKA_ROLLING_UPDATE_JITTER_MAX_S", "30"))

# worker_id -> time.time() when its slot was granted. Capacity check uses
# len(_active_update_slots); reclaim happens lazily on the next attempt.
_active_update_slots: dict[str, float] = {}

# Workers waiting for a slot. FIFO so the order in which workers detected
# the mismatch is the order in which they're allowed to update.
# Entry: (worker_id, to_version, requested_at).
_update_queue: list[tuple[str, str, float]] = []


def _gc_active_update_slots() -> None:
    """Reclaim slots held longer than the timeout. Lazy GC -- runs
    whenever we touch the slot map, no separate task needed."""
    now = time.time()
    for wid, t in list(_active_update_slots.items()):
        if now - t > _UPDATE_SLOT_TIMEOUT_S:
            log.warning(
                "rolling-update: reclaiming slot for %s "
                "(held %.0fs > %.0fs timeout); worker likely never restarted",
                wid, now - t, _UPDATE_SLOT_TIMEOUT_S,
            )
            _active_update_slots.pop(wid, None)


def _try_grant_update_slot(worker_id: str) -> tuple[bool, str]:
    """Try to grab one of the ``_UPDATE_MAX_PARALLEL`` slots. If the
    worker already holds one (re-sent WorkerDraining), keep it."""
    _gc_active_update_slots()
    if worker_id in _active_update_slots:
        return True, "already holding a slot"
    if len(_active_update_slots) >= _UPDATE_MAX_PARALLEL:
        return False, (
            f"queue full ({len(_active_update_slots)}/{_UPDATE_MAX_PARALLEL})"
        )
    _active_update_slots[worker_id] = time.time()
    return True, f"granted ({len(_active_update_slots)}/{_UPDATE_MAX_PARALLEL})"


def _release_update_slot(worker_id: str) -> None:
    """Free a worker's slot (called on WS disconnect). No-op if the
    worker wasn't holding one."""
    if _active_update_slots.pop(worker_id, None) is not None:
        # Drop any queue entries for this worker too (it's gone).
        global _update_queue
        _update_queue = [e for e in _update_queue if e[0] != worker_id]


async def _drain_update_queue() -> None:
    """Try to grant slots to FIFO-queued updaters. Called after a
    slot is freed and right after enqueueing a new request."""
    import random as _rand
    while _update_queue:
        wid, to_ver, _req_at = _update_queue[0]
        ok, why = _try_grant_update_slot(wid)
        if not ok:
            return  # still full, leave the queue head pending
        _update_queue.pop(0)
        conn = state.registry.connections.get(wid)
        if conn is None:
            # Worker disconnected while queued -- release the slot we
            # just grabbed and try the next one.
            _active_update_slots.pop(wid, None)
            continue
        jitter = _rand.uniform(0, _UPDATE_JITTER_MAX_S)
        try:
            await conn.send(
                HubUpdateGate(allow_now=True, why=why, jitter_s=jitter)
            )
            log.info(
                "rolling-update: granted slot to %s -> %s (jitter %.1fs); %s",
                wid, to_ver[:12], jitter, why,
            )
        except Exception:
            log.warning(
                "rolling-update: failed to send grant to %s",
                wid, exc_info=True,
            )
            _active_update_slots.pop(wid, None)


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


def _ip_derived_worker_id(ip: str | None, registry) -> str | None:
    """Deterministic, STABLE worker_id pinned to the worker's LAN IP.

    A worker's persisted id file (/root/.paprika/worker_id) may not survive a
    self-update, and cloned hosts share a base hostname, so a worker could
    otherwise pick up a fresh random id on every update -- churning admin
    history AND the consistent-hash worker-WS routing (re-homing / hub
    imbalance). The IP is stable, so we pin the id to it: same IP -> same id.

    Format ``w<3rd><4th>`` (e.g. 10.10.50.150 -> ``w50150``). If that short form
    is already held by a DIFFERENT, still-alive IP (only possible across /16s,
    e.g. 10.10.5.150 vs 10.10.51.50), fall back to the full IP
    ``w<1>-<2>-<3>-<4>``. Returns None when ``ip`` isn't a usable IPv4 (the
    caller then keeps the worker's current id). Assumes one worker per IP
    (true for the LXC fleet)."""
    if not ip:
        return None
    octs = ip.split(".")
    if len(octs) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octs):
        return None
    short = f"w{octs[2]}{octs[3]}"
    held = registry.connections.get(short)
    if held is not None:
        held_ip = getattr(held, "client_address", None)
        held_alive = (time.time() - held.last_heartbeat) < WORKER_TTL
        if held_alive and held_ip and held_ip != ip:
            return f"w{octs[0]}-{octs[1]}-{octs[2]}-{octs[3]}"
    return short


@router.websocket("/workers/{worker_id}/link")
async def worker_link(ws: WebSocket, worker_id: str):
    """Bidirectional control channel for a worker.

    Protocol:
      1. Worker sends WorkerRegister (must match worker_id in URL).
      2. Hub sends HubRegistered.
      3. Loop: hub may send HubAssignJob/HubCancelJob/HubPing; worker sends
         heartbeat/progress/log/complete/failed.
    """
    if config.admin_mode:
        # Admin/management process never owns worker control channels.
        await ws.close(code=1013, reason="admin service: no worker WS here")
        return
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

    # Stable, deterministic worker_id pinned to the worker's LAN IP. The
    # persisted id file may not survive a self-update and clones share a base
    # hostname, so a worker could otherwise pick up a fresh random id on every
    # update -- churning admin history + the consistent-hash worker-WS routing
    # (re-homing / hub imbalance). Same IP -> same id, forever. This ALSO
    # subsumes clone-collision handling: two hosts presenting the same persisted
    # id get DIFFERENT ids because their IPs differ. Source the real IP from
    # X-Real-IP / first X-Forwarded-For hop -- behind the nginx front
    # ws.client.host is the proxy IP (e.g. 10.10.50.34) for EVERY worker.
    _new_real_ip = (
        ws.headers.get("x-real-ip")
        or (ws.headers.get("x-forwarded-for") or "").split(",")[0]
    ).strip()
    new_ip = _new_real_ip or (ws.client.host if ws.client else None)
    desired_id = _ip_derived_worker_id(new_ip, state.registry)
    if desired_id and desired_id != worker_id:
        log.info(
            "worker %s reassigned to stable IP-derived id %s (ip=%s)",
            worker_id,
            desired_id,
            new_ip,
        )
        try:
            await ws.send_text(
                encode_msg(
                    HubRegistered(
                        worker_id=worker_id,
                        assigned_worker_id=desired_id,
                    )
                )
            )
        except Exception:
            pass
        await ws.close(code=1000, reason="assigned stable IP-derived worker_id")
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
    # Persist the IP into the worker's Redis row NOW (not only on the first
    # heartbeat) so a NON-OWNER hub serving /workers shows it immediately --
    # closes the cross-hub window where the IP column flickered to '-' for a
    # freshly-(re)connected worker until its first heartbeat re-stamped it.
    try:
        await state.registry.persist_client_address(worker_id, worker.client_address)
    except Exception:
        pass
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
        # Send the current egress proxy pool (Settings.proxy_pool) so a
        # freshly-connected worker picks its exit proxy immediately. Live
        # edits arrive separately via _broadcast_proxy_pool. Fire-and-forget.
        try:
            from server.hub.routes.settings import send_proxy_pool_to_worker
            await send_proxy_pool_to_worker(worker)
        except Exception:
            log.warning(
                "initial proxy_pool sync to %s failed",
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
        # Free any rolling-update slot this worker was holding so the
        # next queued updater can proceed. Safe to call unconditionally
        # (no-op if the worker wasn't updating).
        _release_update_slot(worker_id)
        # Drain the queue: this disconnect may have just freed a slot
        # the next worker is waiting on.
        try:
            await _drain_update_queue()
        except Exception:
            log.warning("rolling-update: drain after disconnect failed", exc_info=True)
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
                if jinfo.status == JobStatus.queued:
                    # Worker disconnected BEFORE reporting WorkerJobAccepted --
                    # the job is still queued, which means the worker provably
                    # never started it (status would be running otherwise). No
                    # partial work to recover; re-queue and let redrive
                    # re-dispatch onto the next free worker.
                    # Pre-fix this dropped a perfectly-good job into "failed"
                    # for a transient WS blip (deploy churn, heartbeat miss,
                    # nginx ghost). Worker-disconnect was the #1 failure mode
                    # — ~78% of all failures (incident 2026-06-15).
                    jinfo.worker_id = None
                    jinfo.started_at = None
                    if jinfo.progress is not None:
                        jinfo.progress.phase = "queued"
                    try:
                        await state.store.save_job_info(jinfo)
                    except Exception:
                        pass
                    log.info(
                        "worker %s disconnect: re-queued job %s (worker never ack'd)",
                        worker_id, jid,
                    )
                    continue
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
            # P2: no JobInfo trail, but a job-less interactive session can still
            # have survived in the Redis owner map (full SessionInfo). If it
            # has, rebuild it HERE and take ownership -- the worker re-homed to
            # this hub (consistent-hash re-route after a restart), so the
            # session follows it -- instead of orphan-ending a live session.
            rec = None
            try:
                rec = await state.sessions.get_redis_record(sid)
            except Exception:
                rec = None
            if isinstance(rec, dict):
                try:
                    sinfo = session_from_json(rec)
                    sinfo.worker_id = worker.worker_id
                    if sinfo.lane_idx is None and snap.lane_idx is not None:
                        sinfo.lane_idx = snap.lane_idx
                    if snap.is_fetch_owned:
                        sinfo.state = "fetch_running"
                    sinfo.detached = bool(snap.detached or sinfo.detached)
                    state.sessions.add(sinfo)  # add() re-writes Redis hub = us
                    rebuilt += 1
                    rebuilt_from_job = True
                except Exception:
                    pass
        if not rebuilt_from_job:
            # No JobInfo trail and not in Redis -> true orphan. Tell the worker
            # to end this session so its lane comes back to the pool.
            try:
                await worker.end_session(sid, timeout=10.0)
                orphaned += 1
            except Exception:
                pass

    # Pass 3: in_flight + committed_jobs reconcile. Count = lanes the worker
    # is holding (snapshot length, since each snapshot has a lane_idx).
    # Seeds the hub-side ``committed_jobs`` set from each snapshot's
    # ``job_id`` (where present) so the scheduler picks correctly after a
    # hub restart -- pre-fix, an empty ``committed_jobs`` would let
    # pick_worker treat a still-running worker as idle and over-dispatch.
    # Flipping ``awaiting_announce`` here is the only place pick_worker is
    # allowed to consider this worker; until reconcile fills committed_jobs
    # the worker is invisible to the scheduler (small <1s window between
    # WS handshake and announce).
    try:
        worker.in_flight = len(snapshots)
        worker.committed_jobs = {
            s.job_id for s in snapshots if getattr(s, "job_id", None)
        }
        worker.awaiting_announce = False
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


_WORKER_SEEN_SAVE: dict = {}  # worker_id -> monotonic ts of last ledger last_seen save (throttle)


async def _handle_worker_message(worker, msg) -> None:
    """Dispatch a worker->hub message."""
    assert state.store is not None and state.registry is not None

    if isinstance(msg, WorkerHeartbeat):
        await state.registry.heartbeat(
            worker.worker_id,
            msg.in_flight,
            cpu_pct=msg.cpu_pct,
            mem_pct=msg.mem_pct,
            disk_pct=msg.disk_pct,
            disk_free_gb=msg.disk_free_gb,
            load1=msg.load1,
        )
        # Keep the MariaDB ledger's last_seen_at fresh on heartbeat (NOT just on
        # register) so salvage's age-window can tell a just-ghosted worker from a
        # long-dead VM. Without this, every row's last_seen stayed at register
        # time -> all workers aged out of [min,max] and salvage detected zero
        # ghosts. Throttled ~60s/worker to bound DB writes (fleet*hb is a lot).
        try:
            import time as _t
            _mono = _t.monotonic()
            if _mono - _WORKER_SEEN_SAVE.get(worker.worker_id, 0.0) > 60.0:
                _WORKER_SEEN_SAVE[worker.worker_id] = _mono
                if hasattr(state.store, "save_worker"):
                    await state.store.save_worker(
                        worker.worker_id, ip=worker.client_address, status="connected")
        except Exception:
            pass
        # Mirror the cache snapshot onto the ConnectedWorker so
        # GET /workers can render it without an extra round-trip
        # to the worker. The list is short (typical operator has
        # ≤ 5 profiles) so a per-heartbeat copy is fine.
        try:
            worker.profiles_cached = [p.model_dump() for p in (msg.profiles_cached or [])]
        except Exception:
            worker.profiles_cached = []
        # Zero-downtime rolling update: nudge an OUTDATED worker to self-update
        # without a hub restart. HubRegistered.expected_worker_version only fires
        # on connect; re-advertising the current source hash each heartbeat lets
        # a worker on a stale build start its rolling drain + self-update within
        # ~one heartbeat of a deploy. Skip workers already draining
        # (pending_update_to) to avoid re-sending; the worker side is idempotent
        # and the hub's HubUpdateGate still batches the actual fetch/exit.
        try:
            if not getattr(worker, "pending_update_to", None):
                _exp = _hub_version()
                # Echo the expected version on EVERY heartbeat (not only on a
                # version MISMATCH). For an up-to-date worker this is a cheap,
                # idempotent application-layer liveness ACK -- proof that THIS hub
                # is actually consuming the worker's heartbeats. uvicorn/nginx
                # answer protocol WS pings on their own, so a live ping/pong does
                # NOT prove the hub app still serves the link -- that gap is how a
                # stale proxied WS turns a worker into a reaped "ghost". The
                # worker inbound-liveness watchdog keys on receiving this; a hub
                # that stops consuming stops echoing, so the worker notices and
                # self-restarts onto a healthy hub. Out-of-date workers still
                # self-update (versions differ); matched ones no-op in
                # _maybe_begin_self_update.
                if _exp:
                    await worker.send(
                        HubExpectedVersion(expected_worker_version=_exp)
                    )
        except Exception:
            pass
        return

    if isinstance(msg, WorkerJobAccepted):
        # Move pending -> committed: the worker is now running this job, so it
        # stops being a "pending dispatch" and becomes a "live commitment".
        # Both contribute to pick_worker's effective load, so this transition
        # is invisible to the scheduler -- it just stabilises the load against
        # heartbeat resets that would otherwise let a second picker slip in.
        try:
            worker.pending_assigns.discard(msg.job_id)
            worker.committed_jobs.add(msg.job_id)
        except Exception:
            pass
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
                # 課題(review): the fetch returned a result but its content was
                # blocked by a full-screen login / age / consent / paywall
                # overlay (measured structurally by the worker's occlusion
                # probe; classified here). Re-bucket into the distinct
                # terminal status WITHOUT discarding the result -- the operator
                # can still inspect the captured wall, and escalation below
                # still runs. Best-effort + gated (Settings review_flag_enabled,
                # default on; env PAPRIKA_REVIEW_DISABLE kill-switch).
                try:
                    from server.hub._review import classify_review

                    _rv = classify_review(info, msg.result)
                    if _rv:
                        info.status = JobStatus.review
                        info.progress.phase = "review"
                        info.progress.last_log = _rv
                        try:
                            msg.result.status = JobStatus.review
                            msg.result.review_reason = _rv
                        except Exception:
                            pass
                        log.info("job %s bucketed as 課題(review): %s", msg.job_id, _rv)
                except Exception:
                    log.debug("review classify crashed for %s", msg.job_id, exc_info=True)
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
                from server.hub._escalate import real_fetch_success

                async def _perception_bg(jid: str, jurl: str, jstatus: str, jerror: str, jmode: str | None, jsuccess: bool) -> None:
                    try:
                        await asyncio.wait_for(
                            save_perception_for_job(
                                job_id=jid,
                                url=jurl,
                                data_dir=get_storage_dir(),
                                log=None,
                                mode=jmode,
                                success=jsuccess,
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
                                success=jsuccess,
                                job_id=jid,
                                reason=(
                                    (jerror or "")[:200] if jstatus != "completed"
                                    else ("" if jsuccess else "completed but no content (video requested, none downloaded)")
                                ),
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
                                success=jsuccess,
                                error=jerror or "",
                                perception=perception_dict,
                                stdout_tail="",
                                stderr_tail="",
                                script="",
                                data_dir=get_storage_dir(),
                                url=jurl,
                            )
                    except Exception as e:
                        log.info(
                            "distiller-r1 crashed for %s: %s: %s",
                            jid,
                            type(e).__name__,
                            e,
                        )

                _jstatus = (
                    info.status.value if hasattr(info.status, "value")
                    else str(info.status)
                )
                # ① real success: a completed fetch that delivered no video
                # (download_video set, none saved) is NOT a success -- the same
                # signal the escalator keys on. Overlay/auth walls are already
                # excluded (re-bucketed to review -> status != completed).
                _jsuccess = real_fetch_success(
                    _jstatus,
                    bool(getattr(info.options, "download_video", False)) if info.options else False,
                    msg.result,
                )
                asyncio.create_task(_perception_bg(
                    msg.job_id, info.url,
                    _jstatus,
                    info.error or "",
                    (info.options.mode if info.options else None),
                    _jsuccess,
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
        # Auto-escalate fetch jobs that COMPLETED but didn't deliver
        # (login page captured / video detected-but-not-downloaded) into
        # the AI codegen-loop. Most real "認証画面" / "動画DL失敗" outcomes
        # land HERE, not in the failed path -- the worker returns a
        # FetchResult rather than raising. keep_session jobs aren't done
        # yet -> skip. Best-effort + backgrounded; the escalator self-gates
        # (OFF by default + conservative completed-classifier, _escalate.py).
        if info is not None and not keep_session_active:
            try:
                from server.hub._escalate import maybe_escalate_completed_fetch
                asyncio.create_task(
                    maybe_escalate_completed_fetch(info, msg.result)
                )
            except Exception:
                pass
            # Persist this URL into the durable host_url_history table so the
            # per-host page-role predictor (server/hub/_page_role.py) keeps
            # learning beyond the jobs-table rolling purge. Fire-and-forget;
            # never raises.
            try:
                from server.hub._page_role import record_url
                _vid = bool(getattr(msg.result, "video_detection", None)) or bool(
                    getattr(msg.result, "video_urls_seen", None)
                )
                record_url(getattr(info, "url", "") or "", has_video_evidence=_vid)
            except Exception:
                pass
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
        # NO-FREE-LANE recovery (Case C, 2026-06-15 incident): when the worker
        # rejects an assign because its lane_pool was full, that's NOT a real
        # job failure -- the URL never ran. Marking the job failed throws away
        # work that any other worker (or the same one after a stuck lane is
        # released) would handle fine. Instead: drop the row back to
        # ``queued`` so redrive re-dispatches it onto a different worker, and
        # drain THIS worker for a few minutes so it stops attracting more
        # assigns until either a real lane releases or operator intervenes.
        # The drain auto-clears after PAPRIKA_NO_FREE_LANE_DRAIN_S (default
        # 900 s = 15 min); recurring failures on the same worker re-arm it.
        # All other WorkerJobFailed errors keep their original "mark failed"
        # semantics so a real bad URL doesn't bounce around the fleet.
        _err_lc = (msg.error or "").lower()
        _is_no_free_lane = (
            "no free lane in pool" in _err_lc
            or "no free lane (lane_hint=" in _err_lc
        )
        info = await state.store.get_job_info(msg.job_id)
        if _is_no_free_lane and info is not None and info.status in (
            JobStatus.queued, JobStatus.running,
        ):
            try:
                _drain_s = float(os.environ.get(
                    "PAPRIKA_NO_FREE_LANE_DRAIN_S",
                ) or 900.0)
            except Exception:
                _drain_s = 900.0
            try:
                worker.status = "drain"
                worker.drain_until = time.monotonic() + max(60.0, _drain_s)
            except Exception:
                pass
            log.info(
                "no-free-lane recovery: worker %s drained for %.0fs; "
                "re-queueing job %s (status was %s)",
                worker.worker_id, _drain_s, msg.job_id, info.status,
            )
            try:
                state.registry.log_event(
                    worker.worker_id,
                    f"[{jid_short}] no-free-lane: drained for {int(_drain_s)}s, job re-queued",
                    kind="warn",
                )
            except Exception:
                pass
            info.status = JobStatus.queued
            info.worker_id = None
            info.started_at = None
            if info.progress is not None:
                info.progress.phase = "queued"
            try:
                await state.store.save_job_info(info)
            except Exception:
                pass
            state.registry.release(worker.worker_id, msg.job_id)
            _drop_fetch_session_if_any(info)
            return
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
            # Auto-escalate recoverable fetch failures (video-dl / auth
            # gate) into the AI codegen-loop, when enabled + the GPU is
            # idle. This is the WORKER-reported failure path, so restart-
            # orphans (settled by the reaper) never reach here -- exactly
            # "失敗を再起動以外で AI に回す". Best-effort + backgrounded;
            # the escalator self-gates (OFF by default, see _escalate.py).
            try:
                from server.hub._escalate import maybe_escalate_failed_fetch
                asyncio.create_task(
                    maybe_escalate_failed_fetch(info, msg.error or "")
                )
            except Exception:
                pass
        return

    if isinstance(msg, WorkerScreenshotReply):
        worker.deliver_screenshot_reply(msg)
        return

    if isinstance(msg, WorkerPreviewFrame):
        # Worker self-captured a watched lane and pushed it. Cache in Redis so
        # ANY hub serves it to #screens without a live cross-hub capture.
        if state.registry is not None and msg.jpeg_b64:
            await state.registry.preview_put_frame(
                worker.worker_id, msg.lane_idx, msg.jpeg_b64, msg.ts, msg.width,
            )
        return

    if isinstance(msg, WorkerEngineUsage):
        # Worker-side LLM call (page.ask / observe / extract / agent) -> fold
        # its token usage into the shared engine_usage counter so qwen's
        # vision/agent traffic (which never reaches the hub's own
        # record_engine_usage) shows up in #engines. Resolve the slug from
        # the model when the worker didn't name one (same model-match
        # attribution the hub-side env-default path uses). Best-effort.
        try:
            from server.hub.codegen import (
                _schedule_engine_usage_db,
                _slug_for_model,
                _slug_for_worker_agent,
            )
            # explicit slug -> model match -> operator-flagged worker-agent
            # engine (covers page.agent /act, whose AGENT_MODEL_NAME usually
            # matches no registered engine model).
            slug = (
                (msg.engine_slug or "").strip()
                or _slug_for_model(msg.model or "")
                or _slug_for_worker_agent()
            )
            if slug:
                reg = getattr(state, "engine_usage", None)
                if reg is not None:
                    try:
                        reg.record(slug, msg.prompt_tokens or 0, msg.completion_tokens or 0)
                    except Exception:
                        pass
                _schedule_engine_usage_db(
                    slug, msg.prompt_tokens or 0, msg.completion_tokens or 0
                )
        except Exception:
            pass
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
    if isinstance(msg, WorkerDraining):
        # The worker noticed expected_worker_version > its own and is
        # entering drain mode to prepare for self-update. Mark it so
        # pick_worker() skips it, remember the target version, and
        # either grant a slot immediately or queue.
        worker.status = "drain"
        worker.pending_update_to = msg.to_version
        state.registry.log_event(
            worker.worker_id,
            f"draining for self-update -> {msg.to_version[:12]}",
            kind="info",
        )
        ok, why = _try_grant_update_slot(worker.worker_id)
        import random as _rand
        if ok:
            jitter = _rand.uniform(0, _UPDATE_JITTER_MAX_S)
            try:
                await worker.send(
                    HubUpdateGate(allow_now=True, why=why, jitter_s=jitter)
                )
                log.info(
                    "rolling-update: granted slot to %s -> %s (jitter %.1fs); %s",
                    worker.worker_id, msg.to_version[:12], jitter, why,
                )
            except Exception:
                log.warning(
                    "rolling-update: failed to send immediate grant to %s",
                    worker.worker_id, exc_info=True,
                )
                _active_update_slots.pop(worker.worker_id, None)
        else:
            # Capacity full -- enqueue and let the worker keep draining.
            if not any(e[0] == worker.worker_id for e in _update_queue):
                _update_queue.append(
                    (worker.worker_id, msg.to_version, time.time())
                )
            try:
                await worker.send(
                    HubUpdateGate(allow_now=False, why=why, jitter_s=0.0)
                )
            except Exception:
                pass
            log.info(
                "rolling-update: %s queued for update -> %s; %s "
                "(queue depth %d)",
                worker.worker_id, msg.to_version[:12], why, len(_update_queue),
            )
        return
