"""Queued-job redrive — place ``queued`` jobs onto free worker lanes.

WHY (incident 2026-06-06): worker-dispatched jobs (``fetch`` / ``vision-agent``)
are dispatched *inline* by ``POST /jobs`` (routes/jobs/lifecycle.py). The job row
is persisted ``status=queued`` up front, then the handler tries ONCE to place it
on a free lane. If that handler is abandoned mid-dispatch (the client disconnects
during a burst, or no lane is free in the 8s grace) the job is simply LEFT
``queued`` -- and nothing re-drives it. ``_job_lease_loop`` only re-claims
``running`` codegen/rerun jobs; the stale-job reconciler only *fails* queued jobs
at the 180s deadline. So a queued fetch job sits idle until the reaper kills it
with ``"queued for >180s without assignment"`` -- EVEN THOUGH lanes free up within
seconds. Measured: 80% of recent failures were exactly this, while ~46 lanes sat
idle. The queue was a death-row, not a work-queue.

THIS LOOP makes it a real work-queue. Every few seconds each hub:
  1. cheap-gates on "do I have any free local lane?"
  2. scans ``queued`` jobs oldest-first (FIFO),
  3. for each, while a lane is free: atomically CLAIMS it (store CAS
     ``queued`` -> ``running`` -- :meth:`claim_queued_job`, the cross-hub mutex)
     then sends the worker assignment (mirroring the inline POST dispatch:
     per-host cookies + popup, shared profile, asset blacklist, live session).
  4. if the send fails, reverts the claim so a later pass retries.

Properties:
  * Cross-hub safe -- only the hub whose CAS matched ``queued`` dispatches; the
    others see ``running`` and skip. No double-dispatch.
  * Capacity-correct -- ``assign`` bumps ``worker.in_flight``, so sequential
    ``pick_worker`` within a pass naturally stops at the free-lane count.
  * Additive + kill-switchable -- the POST hot path is untouched. Set
    ``PAPRIKA_QUEUE_REDRIVE_DISABLE=1`` to revert to inline-only dispatch.

Scope (v1): worker-dispatched modes only (fetch / vision-agent) -- the modes that
actually strand. ``codegen-loop`` / ``rerun`` are hub-orchestrated (create_task)
and belong to ``redispatch_orphan_job``; they are skipped here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from server.hub._state import state
from server.protocol import JobStatus

log = logging.getLogger(__name__)


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# How often each hub sweeps the queue. Short enough that a burst drains within
# seconds as lanes free, long enough to stay cheap (one indexed SELECT + N WS
# sends per pass, only while lanes are actually free).
_INTERVAL_S = _num("PAPRIKA_QUEUE_REDRIVE_INTERVAL_S", 3.0)
# Safety cap on placements per pass so one hub can't monopolise a shared queue;
# the next pass (≈3s later) continues. 0 = unbounded (place every free lane).
_MAX_PER_PASS = int(_num("PAPRIKA_QUEUE_REDRIVE_MAX_PER_PASS", 0))
# Don't touch a queued job until it's older than this -- it MUST exceed the
# worst-case time a live POST /jobs handler can hold a job as queued+unassigned
# (8s dispatch grace + up to 60s cross-hub forward). Younger queued rows may
# still be mid-dispatch by POST (possibly forwarded to a peer that's about to
# run it), so claiming one would risk a double send. Past this age the handler
# has definitively finished -- a still-queued+unassigned row is genuinely
# stranded. The CAS ``worker_id IS NULL`` guard is the belt; this is the braces.
_MIN_AGE_S = _num("PAPRIKA_QUEUE_REDRIVE_MIN_AGE_S", 90.0)


async def _assign_worker_job(info, worker, base: str, started) -> bool:
    """Build + send the worker assignment for a queued worker-dispatched job,
    mirroring the inline dispatch in ``routes/jobs/lifecycle.py`` (per-host
    cookies + popup policy + shared profile + asset blacklist + live session).
    The job row was already CAS-claimed ``queued`` -> ``running`` by the caller.
    Returns True iff the worker accepted the assignment.

    NOTE: kept in lock-step with the POST dispatch block. The one intentional
    omission is the pre-fetch auto-login *network* refresh (a best-effort
    optimisation the POST path also try/excepts) -- a redrive must stay cheap;
    stored cookies still ride along, so login-gated fetches keep working.
    """
    from server.protocol import HubAssignJob
    from server.hub.hosts import _normalise_host, cookies_for_cdp
    from server.hub.sessions import SessionInfo, new_session_id
    from server.hub._helpers import _asset_upload_url

    opts = info.options
    mode = (opts.mode if opts else None) or "fetch"

    # --- per-host cookies + popup policy (mirrors POST 828-913) ---
    auto_cookies = None
    auto_host = None
    auto_popup_policy = "kill"
    if state.hosts is not None:
        try:
            host_raw = urlparse(info.url).hostname or ""
            host = _normalise_host(host_raw)
            rec = state.hosts.get(host) if host else None
            if rec:
                auto_popup_policy = rec.popup_policy or "kill"
                # Phase 2b: same tenant gate as the inline POST dispatch — a
                # host record owned by another tenant is invisible to this
                # job (no cookie inject, no save-back). No-op off/optional.
                from server.hub.auth import owner_can_use
                _host_usable = owner_can_use(
                    getattr(rec, "owner_id", "default"),
                    job_owner=getattr(info, "owner_id", "default"),
                    shared=getattr(rec, "shared", True),
                )
                if mode == "fetch" and _host_usable:
                    auto_host = host
                    if rec.cookies:
                        auto_cookies = cookies_for_cdp(rec.cookies)
                    # pre-baked fetch recipe (pure function, no network)
                    if (
                        not opts.fetch_recipe
                        and getattr(opts, "fetch_strategy", "recipe") != "normal"
                    ):
                        try:
                            picked = rec.pick_recipe(info.url)
                            if picked is not None:
                                opts.fetch_recipe = picked.to_json()
                        except Exception:
                            pass
        except Exception:
            auto_cookies = None
            auto_host = None
            auto_popup_policy = "kill"

    # --- profile resolution (mirrors POST 1049-1086) ---
    profile_url = None
    profile_name = ((opts.use_profile or "").strip() or None) if opts else None
    profile_etag = None
    try:
        from server.hub.routes.profiles import _shared_default, _shared_meta
        if profile_name is None:
            profile_name = await _shared_default()
        if profile_name:
            pmeta = await _shared_meta(profile_name)
            if pmeta is None:
                profile_name = None  # stale default -> run with the lane's stock profile
            else:
                # Phase 2b: only inject the profile when this job's owner may
                # use it (shared / same tenant); a private default never rides
                # onto another tenant's redriven job. No-op off/optional.
                from server.hub.auth import owner_can_use
                if owner_can_use(
                    pmeta.get("owner_id"),
                    job_owner=getattr(info, "owner_id", "default"),
                    shared=bool(pmeta.get("shared", True)),
                ):
                    profile_url = f"{base}/profiles/{profile_name}"
                    profile_etag = pmeta.get("etag") or None
                else:
                    profile_name = None  # run with the lane's stock profile
    except Exception:
        profile_url = None
        profile_name = None
        profile_etag = None

    # --- asset URL blacklist (mirrors POST 1088-1102) ---
    asset_url_blacklist: list[str] = []
    if state.settings is not None:
        try:
            raw = (state.settings.get("asset_url_blacklist", "") or "").strip()
            asset_url_blacklist = [
                ln.strip()
                for ln in raw.splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
        except Exception:
            asset_url_blacklist = []

    # --- live session for admin noVNC parity (fetch only; mirrors POST 1009-1047) ---
    fetch_sid = None
    if mode == "fetch" and state.sessions is not None:
        try:
            fetch_sid = new_session_id()
            keep = bool(getattr(opts, "keep_session", False)) if opts else False
            sinfo = SessionInfo(
                session_id=fetch_sid,
                worker_id=worker.worker_id,
                initial_url=info.url,
                idle_ttl_s=60 if keep else 600,
                absolute_ttl_s=24 * 3600 if keep else 3600,
                job_id=info.job_id,
            )
            sinfo.state = "fetch_running"
            state.sessions.add(sinfo)
            info.session_id = fetch_sid
        except Exception:
            fetch_sid = None

    assign = HubAssignJob(
        job_id=info.job_id,
        url=info.url,
        options=opts,
        asset_upload_base=_asset_upload_url(base, info.job_id),
        lane_hint=None,
        cookies=auto_cookies,
        save_cookies_host=auto_host if mode == "fetch" else None,
        session_id=fetch_sid,
        popup_policy=auto_popup_policy,
        profile_url=profile_url,
        profile_name=profile_name,
        profile_etag=profile_etag,
        asset_url_blacklist=asset_url_blacklist,
    )
    ok = await state.registry.assign(worker, assign)
    if not ok:
        # send failed -> roll back the eagerly-registered session
        if fetch_sid and state.sessions is not None:
            try:
                state.sessions.remove(fetch_sid)
            except Exception:
                pass
            info.session_id = None
        return False

    # Persist the running state (CAS already flipped status+worker_id+started_at;
    # this also records novnc_url + session_id so the admin UI can watch it).
    info.status = JobStatus.running
    info.worker_id = worker.worker_id
    info.started_at = started
    novnc = worker.capabilities.novnc_url
    if novnc:
        sep = "&" if "?" in novnc else "?"
        info.novnc_url = (
            f"{novnc}{sep}autoconnect=1&resize=scale&reconnect=1"
            if "autoconnect" not in novnc
            else novnc
        )
    try:
        await state.store.save_job_info(info)
    except Exception:
        pass
    log.info(
        "redrive: placed queued job %s -> worker %s (in_flight=%d/%d)",
        info.job_id, worker.worker_id, worker.in_flight,
        worker.capabilities.max_concurrent,
    )
    return True


async def _redrive_dispatch_one(info) -> bool:
    """Claim one queued worker-dispatched job and place it on a free local lane.
    Returns True iff the job was successfully dispatched by THIS hub."""
    store = state.store
    if store is None or state.registry is None:
        return False
    mode = (info.options.mode if info.options else None) or "fetch"
    if mode in ("codegen-loop", "rerun"):
        return False  # hub-orchestrated -> redispatch_orphan_job's domain
    worker = state.registry.pick_worker()
    if worker is None:
        return False
    base = getattr(worker, "public_base_url", None)
    if not base:
        # No dial-in URL recorded -> can't build the asset-upload base. Rare;
        # leave queued (the POST path uses the request host as a fallback).
        return False

    started = datetime.now(timezone.utc).replace(tzinfo=None)
    # Atomic cross-hub claim. Only the hub whose UPDATE matches still-`queued`
    # proceeds; everyone else (and the original POST path, if it's mid-flight)
    # gets False and backs off.
    try:
        won = await store.claim_queued_job(info.job_id, worker.worker_id, started)
    except Exception:
        log.debug("redrive: claim(%s) failed", info.job_id, exc_info=True)
        return False
    if not won:
        return False

    ok = False
    try:
        ok = await _assign_worker_job(info, worker, base, started)
    except Exception:
        log.warning("redrive: assign(%s) crashed", info.job_id, exc_info=True)
        ok = False
    if not ok:
        # Couldn't actually hand it to the worker -> give the claim back so the
        # next pass (here or on a peer) can retry it before the queue deadline.
        try:
            await store.release_claimed_job(info.job_id)
        except Exception:
            log.debug("redrive: release(%s) failed", info.job_id, exc_info=True)
    return ok


async def _redrive_pass() -> int:
    """One sweep: place as many queued worker-dispatched jobs as there are free
    lanes (oldest first). Returns the number placed."""
    store = state.store
    if store is None or state.registry is None:
        return 0
    # Cheap gate: nothing to do if no lane is free right now.
    if state.registry.pick_worker() is None:
        return 0
    try:
        infos, _total = await store.list_job_infos(status=["queued"], limit=0)
    except Exception:
        log.debug("redrive: list queued failed", exc_info=True)
        return 0
    if not infos:
        return 0
    # list_job_infos returns newest-first; redrive FIFO -> oldest first.
    infos.sort(key=lambda i: i.created_at or datetime.min)
    now = datetime.utcnow()  # JobInfo.created_at is naive-UTC (mirrors the reaper)
    placed = 0
    for info in infos:  # oldest first
        try:
            age = (now - info.created_at).total_seconds() if info.created_at else 1e9
        except Exception:
            age = 1e9
        if age < _MIN_AGE_S:
            break  # sorted oldest-first -> everything after is younger too
        if info.worker_id:
            continue  # POST already handed this to a worker -> not stranded
        if _MAX_PER_PASS and placed >= _MAX_PER_PASS:
            break
        if state.registry.pick_worker() is None:
            break  # all local lanes full -> stop; next pass continues
        if await _redrive_dispatch_one(info):
            placed += 1
    if placed:
        log.info("redrive: placed %d stranded queued job(s) onto free lanes", placed)
    return placed


async def _queued_redrive_loop() -> None:
    """Periodically drain ``queued`` worker-dispatched jobs onto free lanes.
    Kill-switch: ``PAPRIKA_QUEUE_REDRIVE_DISABLE=1`` -> never starts (system
    reverts to inline-only dispatch)."""
    if _flag("PAPRIKA_QUEUE_REDRIVE_DISABLE", False):
        log.info("redrive: kill-switch PAPRIKA_QUEUE_REDRIVE_DISABLE set -- not started")
        return
    log.info("redrive: queued-job redrive loop started (interval=%.0fs)", _INTERVAL_S)
    first = True
    while True:
        try:
            await asyncio.sleep(2 if first else _INTERVAL_S)
        except asyncio.CancelledError:
            return
        first = False
        try:
            await _redrive_pass()
        except Exception:
            log.warning("redrive: pass failed", exc_info=True)
