"""Session reaper + orphan-job recovery.

Two long-running loops the hub spawns at lifespan-start. Pulled out
of app.py so the module is just FastAPI() + lifespan + include_router
stanzas + _apply_route_tags() now.

* ``_session_reaper_loop``: forever-loop that closes sessions whose
  ``idle_ttl_s`` / ``absolute_ttl_s`` has elapsed. (The old
  running<->keepalive phase oscillation was removed in state-model v1;
  keepalive is now a stable phase.)

* ``_recover_orphan_running_jobs``: one-shot run at hub startup.
  Anything persisted as ``status=running`` but missing from the
  ``state.local_tasks`` map is by definition an orchestrator killed
  by a hub restart -- mark them failed so the admin UI shows a
  terminal state instead of an eternal yellow "running" badge.
  Fires off ``_recover_orphan_running_jobs_scan`` as a background task
  (the scan is a full-table walk and must never block the lifespan),
  and that scan refuses to run at all while any peer hub answers
  ``/health`` -- in a shared store the "orphans" would be theirs.

close_session is imported via app.py's re-export chain (originally
from routes/sessions.py) to dodge the routes <-> hub circular at
boot time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime

from server.hub._state import config, state
from server.protocol import JobStatus
from server.runner import DONE_SENTINEL

log = logging.getLogger(__name__)


async def close_session(session_id: str):
    """Lazy bridge to the route-layer close_session (defined in
    routes/sessions.py). The reaper imports this module at
    lifespan-start time when the route modules are still loading, so
    resolve at first call (= reaper tick) rather than at import."""
    from server.hub.routes.sessions import close_session as _impl

    return await _impl(session_id)


# ----------------------------------------------------------------------------
# Background tasks
# ----------------------------------------------------------------------------

# How often the reaper looks for expired sessions, seconds.
_REAPER_INTERVAL_S = 5


def _peer_health_url(hub_id: str) -> str:
    """Internal /health URL of a sibling hub.

    Deliberately duplicates routes/sessions/_base.py ``_hub_internal_url``
    (same env knobs, same ``hub-NN`` -> LAN IP scheme) instead of importing it:
    this runs at lifespan-start, when the route package is still mid-import,
    and pulling it in here reintroduces the routes<->hub circular the rest of
    this module goes out of its way to dodge. Keep the two in sync.
    """
    fmt = os.environ.get("PAPRIKA_HUB_INTERNAL_FMT")
    if fmt:
        return f"{fmt.format(hub=hub_id).rstrip('/')}/health"
    m = re.match(r"^hub-(\d{1,3})$", hub_id or "")
    if m:
        subnet = os.environ.get("PAPRIKA_HUB_SUBNET", "10.10.50")
        port = os.environ.get("PAPRIKA_HUB_INTERNAL_PORT", "8100")
        return f"http://{subnet}.{m.group(1)}:{port}/health"
    return f"http://{hub_id}:8000/health"


async def _any_peer_hub_alive() -> bool:
    """True if ANY sibling hub is actually serving right now.

    Redis presence alone is NOT trustworthy for this question. A hub that
    booted while Redis was unavailable (e.g. mid-``LOADING``) keeps running
    with ``redis_client=None`` forever: it serves traffic and owns jobs, but
    never writes ``paprika:hubs:{id}``. Judging liveness by presence then
    concludes "I am alone" while six healthy peers are working -- which is
    exactly how 2026-08-03 blanket-failed 10 running jobs owned by peers.

    So: presence is only a *candidate source*. Membership is decided by a real
    ``/health`` probe, the same call the nginx reconciler switched to in
    commit 74b760e for the identical reason.
    """
    if state.hubs is None:
        return False
    try:
        rows = await state.hubs.list_all()
    except Exception:
        # Can't enumerate peers -> we cannot prove we're alone. Assume we are
        # NOT (fail safe: skipping recovery is recoverable, blanket-failing
        # a live fleet's jobs is not).
        return True
    candidates: list[str] = []
    for h in rows:
        hid = str(h.get("hub_id") or "")
        if not hid or h.get("local") or hid == config.hub_id:
            continue
        if h.get("alive"):
            # Presence row is live -> peer is definitely up, no probe needed.
            return True
        candidates.append(hid)
    if not candidates:
        return False

    async def _probe(hid: str) -> bool:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.get(_peer_health_url(hid))
            return r.status_code == 200
        except Exception:
            return False

    results = await asyncio.gather(
        *[_probe(h) for h in candidates], return_exceptions=True
    )
    for hid, ok in zip(candidates, results):
        if ok is True:
            log.info(
                "recovery: peer hub %s answers /health despite no Redis "
                "presence -> treating cluster as LIVE", hid,
            )
            return True
    return False


# Upper bound on how many running rows the startup orphan scan will touch.
# Production holds ~500-700 running rows at any moment; anything near this cap
# means the store is not what we think it is, and the scan must not grow
# without bound (see _recover_orphan_running_jobs_scan).
_ORPHAN_SCAN_MAX = int(os.environ.get("PAPRIKA_ORPHAN_SCAN_MAX", "20000") or 20000)


async def _recover_orphan_running_jobs() -> int:
    """Schedule the orphan-running-job recovery scan in the background.

    Returns 0 immediately. The scan walks EVERY job in the store
    (``list_job_ids`` + a ``get_job_info`` round-trip each); against the
    production MariaDB (~860k rows) that takes minutes. It used to be
    awaited inline in the lifespan, so uvicorn never reached
    "Application startup complete" and never bound the port -- the hub
    logged happily from its background loops while ``/health`` refused
    connections, which read exactly like a crash and failed the deploy
    script's health gate (2026-08-03).

    Startup must not depend on a full-table walk, so it runs as a task.
    """
    asyncio.create_task(_recover_orphan_running_jobs_scan())
    return 0


async def _recover_orphan_running_jobs_scan() -> int:
    """Scan the job store at hub startup and mark any job persisted as
    ``status=running`` but no longer driven by a local task as failed.

    Hub restart kills the orchestrator coroutine mid-flight; without
    this recovery, the killed jobs stay ``status=running`` in the
    admin UI forever (Recent Jobs row stuck at a yellow "running"
    badge, never resolves).

    Returns the count of jobs reclassified.
    """
    assert state.store is not None
    # Multi-hub phase 4: when job leasing is ON, a "running" job in the store
    # might belong to a SIBLING hub that's alive and refreshing its lease --
    # blanket-failing every running job at our startup would wrongly kill
    # their work. Hand orphan handling to the lease reaper, which only
    # re-dispatches/fails jobs whose lease has actually expired. Default OFF
    # (single hub): unchanged -- we still fail orphaned running jobs here.
    try:
        from server.hub import _leases as _l
        if _l.enabled():
            return 0
    except Exception:
        pass
    # Multi-hub safety (clone-safe): if ANY other hub is alive, this process is
    # joining a LIVE cluster (e.g. a freshly cloned VM, or a peer hub starting
    # up). The in-store "running" jobs then belong to live peers, NOT to a dead
    # local orchestrator -- blanket-failing them would nuke the whole fleet's
    # work. Skip; genuine dead-hub orphans are handled by the lease reaper
    # (when leasing is on) and by _stale_job_reconciler_loop.
    #
    # Liveness is decided by a real /health probe, NOT by Redis presence --
    # see _any_peer_hub_alive for why presence silently lies.
    try:
        if await _any_peer_hub_alive():
            log.info(
                "recovery: live peer hub(s) detected -> skipping blanket "
                "orphan recovery (multi-hub safety)",
            )
            return 0
    except Exception:
        # Never let a failed liveness check fall through into the blanket
        # scan: unknown cluster state must mean "do nothing".
        log.warning(
            "recovery: peer-liveness check errored -> skipping blanket "
            "orphan recovery (fail safe)", exc_info=True,
        )
        return 0
    # state.local_tasks is empty at this point (fresh process), so any
    # in-store "running" job is by definition an orphan.
    #
    # Ask the store for RUNNING ids only when it can (idx_status_created,
    # a few hundred rows) instead of walking every id. The unbounded walk is
    # what froze one hub per fleet-wide restart on 2026-11-02 (CT901 17:40,
    # CT128 18:33): with every peer restarting at the same moment the
    # liveness probe above found nobody, the scan pulled all 52M job ids
    # into memory (RSS 0.4 -> 5.6 GB, main thread 100%, accept queue 1,038)
    # and the hub never answered /health until it was restarted by hand.
    try:
        lister = getattr(state.store, "list_jobs_by_status_between", None)
        if lister is not None:
            rows = await lister(
                JobStatus.running.value, datetime(1970, 1, 1), datetime.utcnow(),
                _ORPHAN_SCAN_MAX,
            )
            job_ids = [r[0] for r in rows]
        else:
            job_ids = await state.store.list_job_ids()
    except Exception:
        return 0
    n = 0
    for jid in job_ids:
        try:
            info = await state.store.get_job_info(jid)
            if info is None:
                continue
            if info.status != JobStatus.running:
                continue
            info.status = JobStatus.failed
            info.completed_at = info.completed_at or datetime.utcnow()
            info.error = (
                "orchestrator killed by hub restart (before deploy/crash); "
                "job's previous attempts are preserved under /jobs/{id}/attempts."
            )
            if info.progress is not None:
                info.progress.phase = "failed"
                info.progress.last_log = "[hub recovery] hub restart killed in-flight orchestrator"
            await state.store.save_job_info(info)
            try:
                await state.store.publish_log(
                    info.job_id,
                    "[hub recovery] orchestrator was killed by hub restart; job marked failed.",
                )
                await state.store.publish_log(info.job_id, DONE_SENTINEL)
            except Exception:
                pass
            n += 1
        except Exception:
            pass
    # The caller (lifespan) no longer awaits this, so report here or the
    # blast radius of a bad run is invisible.
    log.info(
        "recovery: orphan scan finished -- walked %d job(s), marked %d "
        "orphan running job(s) failed", len(job_ids), n,
    )
    return n


#: How many TTL-expired "running"/"fetch_running" sessions the reaper may
#: check against the job store in one 5s tick. Only expired sessions are ever
#: probed, so a healthy hub does zero of these; the cap exists for the leaked
#: state, where the alternative is thousands of store reads per tick.
_SESSION_JOB_PROBES_PER_TICK = int(
    os.environ.get("PAPRIKA_SESSION_JOB_PROBES_PER_TICK", "50")
)


async def _session_job_is_over(s) -> str | None:
    """Terminal disposition of the job that pins session ``s``, else None.

    None means "leave it alone": no job id to check, an unreadable store, or a
    job that really is still running. Only a positive answer -- a terminal
    status, or a row that no longer exists -- releases the session, so a store
    blip can never evict a live one.
    """
    job_id = getattr(s, "job_id", None)
    if not job_id or state.store is None:
        return None
    try:
        info = await state.store.get_job_info(job_id)
    except Exception:
        return None
    if info is None:
        return "gone"
    try:
        status = getattr(info.status, "value", info.status)
    except Exception:
        return None
    status = str(status or "").split(".")[-1].lower()
    if status in ("completed", "failed", "cancelled", "canceled", "review"):
        return status
    return None


async def _session_reaper_loop():
    """Periodically close sessions whose idle_ttl_s / absolute_ttl_s
    has elapsed. Runs forever; cancelled by the lifespan teardown.
    """
    while True:
        try:
            await asyncio.sleep(_REAPER_INTERVAL_S)
        except asyncio.CancelledError:
            return
        if state.sessions is None:
            continue
        # Keep the cross-hub session owner-map (Redis) fresh for every live
        # session: add() writes each entry once with a short fetch-tied TTL
        # that lapses on long-lived / keepalive sessions, after which a
        # non-owner hub can't forward session actions to the owner (-> 404).
        # Cheap SET per live session every few seconds.
        try:
            await state.sessions.touch_redis_map()
        except Exception:
            pass
        now = datetime.utcnow()
        probes_left = _SESSION_JOB_PROBES_PER_TICK
        for s in list(state.sessions.all()):
            if s.state == "closing":
                continue
            # Sessions with an action currently in flight are protected --
            # see the state check further down, after the TTL test. The
            # reaper used to only look at last_active_at, which gets
            # updated AFTER the action returns -- so a legitimately
            # long page.download_video() (10+ min for big yt-dlp jobs)
            # or page.agent() multi-step LLM call would race the
            # default idle_ttl_s=300 and the session would 404 from
            # under the next call. Job b79ab7d5e813 hit this: 652 s
            # download_video -> session evicted -> 40+ retries on the
            # same dead session_id afterwards.
            #
            # Anchor: when an action actually completes,
            # _send_session_action flips state back to "idle" AND
            # refreshes last_active_at, so the next reaper tick sees
            # the fresh timestamp. If the worker disconnects
            # mid-action, drop_by_worker tears the session down
            # directly, so no zombie "running" state can sit forever.
            try:
                idle = (now - s.last_active_at).total_seconds()
                age = (now - s.created_at).total_seconds()
            except Exception:
                continue

            # NOTE: the running<->keepalive phase oscillation was removed
            # (state-model v1). A keep_session job's phase is set to
            # "keepalive" ONCE when its capture finishes and stays there;
            # noVNC RFB activity still refreshes last_active_at (touch, in
            # routes/novnc.py) so an operator who is watching isn't
            # reaped, but we no longer flip the job phase back to
            # "running". The TTL reap below is unchanged.

            expired = (s.idle_ttl_s and idle > s.idle_ttl_s) or (
                s.absolute_ttl_s and age > s.absolute_ttl_s
            )
            if not expired:
                continue
            reason = "idle_ttl" if (s.idle_ttl_s and idle > s.idle_ttl_s) else "absolute_ttl"
            # An action is in flight -- normally. The skip above used to be
            # unconditional and permanent: a session sits in "fetch_running"
            # until its job's WorkerJobComplete flips it back, and a job that
            # the HUB terminates instead (stale-job reconciler, lease reaper,
            # downloading reaper, DELETE /jobs/{id}) never sends one. The
            # session then outlived every TTL with nothing able to collect it,
            # holding a worker lane the whole time. Measured 2026-08-18: 494
            # sessions, ALL "fetch_running", p50 age 2.3h, oldest 18.9h, 367
            # of them past idle_ttl.
            #
            # So: still never evict a session whose job is genuinely running
            # (that was the b79ab7d5e813 fix -- a 652s download_video must not
            # be reaped mid-action), but a TTL-expired session whose job the
            # store says is OVER is exactly the leak, and it goes.
            if s.state == "running" or s.state == "fetch_running":
                if probes_left <= 0:
                    # Bounded work per tick: in a leaked fleet this branch can
                    # be thousands of sessions, and the reaper must not turn
                    # into a store-scan storm. The backlog drains over the
                    # next few ticks (5s apart).
                    continue
                probes_left -= 1
                over = await _session_job_is_over(s)
                if not over:
                    continue
                reason = f"{reason} + job {over}"
            log.info(
                "reaper: closing %s (reason=%s idle=%.0fs age=%.0fs)",
                s.session_id,
                reason,
                idle,
                age,
            )
            try:
                await close_session(s.session_id)
            except Exception:
                log.warning(
                    "reaper: failed to close %s", s.session_id, exc_info=True
                )


# ----------------------------------------------------------------------------
# Dead-worker reaper (clean up stale Redis registrations)
# ----------------------------------------------------------------------------
# Workers persist their registration in Redis (sorted by last heartbeat
# in ``_k_index()``) so the admin UI can show "this worker was here
# yesterday but disappeared". After a while (clone collision burst,
# version-mismatch loop, container churn from a redeploy) those
# entries pile up and bury the live row in the Workers tab. This
# loop deletes any entry whose last heartbeat is older than
# ``_DEAD_WORKER_MAX_AGE_S``. Live, currently-connected workers are
# never touched (they're not even on the Redis path -- live entries
# come straight from registry.connections).
# How long a worker's last heartbeat may be stale before its (dead,
# disconnected) registration is auto-pruned, and how often we scan.
# Raised 300 -> 3600: a "ghost" worker (VM alive but its control WS pinned
# by nginx consistent-hash to a hub that doesn't consume it -- see
# worker-ghost-proxied-ws) stops heartbeating, so the OLD 300s reap deleted a
# STILL-ALIVE VM from the Redis index within ~5 min, collapsing the fleet
# count (e.g. 60 -> 37) faster than re-home / salvage could recover it. 3600s
# gives the deploy-time hub-drain (案B) + hub-side force-disconnect salvage
# (案D) time to re-home the worker before we ever prune a live VM; a genuinely
# dead VM still ages out within the hour. Live (connected) workers are never
# pruned (the loop skips alive=true).
_DEAD_WORKER_MAX_AGE_S = float(os.environ.get("WORKER_STALE_REAP_S", "3600"))
_DEAD_WORKER_REAPER_INTERVAL_S = float(os.environ.get("WORKER_REAP_INTERVAL_S", "60"))


async def _dead_worker_reaper_loop():
    """Periodically delete dead-worker Redis registrations older than
    ``_DEAD_WORKER_MAX_AGE_S``. Best-effort; logs and continues on any
    exception (Redis blip etc.).
    """
    import time as _time
    first = True
    while True:
        try:
            await asyncio.sleep(60 if first else _DEAD_WORKER_REAPER_INTERVAL_S)
        except asyncio.CancelledError:
            return
        first = False
        reg = state.registry
        if reg is None:
            continue
        try:
            snap = await reg.stats_async()
        except Exception:
            log.info("dead-worker reaper: stats_async failed", exc_info=True)
            continue
        now = _time.time()
        pruned = 0
        for w in snap.get("workers", []):
            if w.get("alive"):
                continue
            last_hb = w.get("last_heartbeat") or 0
            try:
                age_s = now - float(last_hb)
            except (TypeError, ValueError):
                continue
            if age_s < _DEAD_WORKER_MAX_AGE_S:
                continue
            wid = w.get("worker_id")
            if not wid:
                continue
            try:
                ok = await reg.forget(wid)
                if ok:
                    pruned += 1
            except Exception:
                log.info(
                    "dead-worker reaper: forget(%s) failed",
                    wid,
                    exc_info=True,
                )
        if pruned:
            log.info(
                "dead-worker reaper: pruned %d stale registration(s) "
                "(heartbeat older than %ds)",
                pruned,
                int(_DEAD_WORKER_MAX_AGE_S),
            )


# ----------------------------------------------------------------------------
# Stale-job reconciler (durable backstop for orphaned running + stuck queued)
# ----------------------------------------------------------------------------
# WHY THIS EXISTS (incident 2026-06-06: 35 orphan-running + 113 stuck-queued
# jobs piled up; admin "実行中" badge read 204).
#
# Every other recovery net is tied to a single hub PROCESS or a single
# OBSERVED event, and all three were defeated by the multi-hub topology +
# the heavy redeploy/worker-id-migration churn of 2026-06-05/06:
#
#   * _recover_orphan_running_jobs (startup one-shot) -> returns early when
#     ANY peer hub is alive, so in a 3-hub prod cluster it never fires.
#   * _queued_timeout_guard (in-process asyncio task) -> dies with the hub on
#     every rolling restart and is never re-armed for jobs already in the
#     store, so a job queued <180s before a restart is stranded forever.
#   * the worker-disconnect settle in routes/workers.py -> only fires while
#     the OWNING hub observes the WS drop AND the job still has a registered
#     session; a worker that changed id (d41662a57eef-* -> w50*) during the
#     stable-worker-id migration disconnected once, unobserved, and its old
#     id never reconnects, so that path can never fire for it again.
#
# This loop is the missing piece: a periodic, idempotent, cluster-wide
# reconciler that diffs the job store against the *fleet-wide* live worker
# set (stats_async, NOT just this hub's local connections) and settles what
# the per-process nets missed. Runs on every hub; terminal writes are
# content-identical so concurrent passes converge (no lease/claim needed —
# unlike the lease reaper, which re-DISPATCHES and therefore must single-win).
_STALE_RECONCILE_INTERVAL_S = float(
    os.environ.get("PAPRIKA_STALE_RECONCILE_INTERVAL_S", "90")
)
# A running job younger than this is spared even when its worker_id is absent
# from the alive set. Covers the worker self-update / WS-reconnect / re-announce
# window (P2 session survival: a worker can briefly drop and come back with the
# SAME id, re-adopting its jobs). Must comfortably exceed a worker's rolling
# self-update gap so we never fail a job whose worker is mid-update.
_STALE_RUNNING_GRACE_S = float(
    os.environ.get("PAPRIKA_STALE_RUNNING_GRACE_S", "300")
)
# Durable twin of routes/jobs/_base.py::_QUEUE_TIMEOUT_S. Same env var so the
# in-process guard and this backstop agree on the budget; whichever fires first
# wins (idempotent). A job still `queued` past this had no worker/lane pick it
# up and no re-dispatch loop exists, so fail it as timed_out.
_STALE_QUEUED_TIMEOUT_S = float(os.environ.get("PAPRIKA_QUEUE_TIMEOUT_S", "180"))


# Downloading-job reaper (incident 2026-06-23): a job that finished its
# fetch handler but is still inside a deferred yt-dlp download lives in
# ``JobStatus.downloading``. A live HLS stream, a wedged CDN at 25 KiB/s,
# or a worker that died holding the detached download all leave the row
# stuck there indefinitely -- progress.download_pct keeps ticking (or
# freezes) but the job never transitions to ``completed``. The operator
# accumulated 10+ jobs older than 12h before this reaper was added.
#
# Policy: jobs older than ``PAPRIKA_DOWNLOADING_TIMEOUT_S`` (default 1h) are
# asked to wrap up gracefully via ``HubForceCompleteJob`` -- the worker
# SIGTERMs yt-dlp/ffmpeg, ffmpeg-remuxes whatever fragments it has, uploads
# the partial mp4 as an asset, and sends WorkerJobComplete. If the worker
# is no longer connected (= disconnected, dead, mid-self-update), the row
# is marked ``failed`` directly so it stops hogging the UI count.
_DOWNLOADING_TIMEOUT_S = float(
    os.environ.get("PAPRIKA_DOWNLOADING_TIMEOUT_S", "3600")
)
_DOWNLOADING_REAPER_INTERVAL_S = float(
    os.environ.get("PAPRIKA_DOWNLOADING_REAPER_INTERVAL_S", "60")
)


async def _downloading_reaper_loop():
    """Periodically wrap up jobs stuck in ``JobStatus.downloading`` for
    longer than the timeout. Idempotent; multiple hubs may run this loop --
    the per-job HubForceCompleteJob send is naturally rate-limited by the
    worker's idempotent handler (the second force-complete is a no-op once
    the first one removed the in-flight download)."""
    first = True
    while True:
        try:
            await asyncio.sleep(15 if first else _DOWNLOADING_REAPER_INTERVAL_S)
        except asyncio.CancelledError:
            return
        first = False
        try:
            await _reap_stuck_downloading()
        except Exception:
            log.debug("downloading reaper pass failed", exc_info=True)


async def _reap_stuck_downloading() -> int:
    """One pass: find ``downloading`` jobs older than the timeout and ask
    them to finalise. Returns the number actioned."""
    from server.hub._state import state
    from server.protocol import HubForceCompleteJob, JobStatus, JobResult

    store = state.store
    if store is None or state.registry is None:
        return 0
    try:
        infos, _ = await store.list_job_infos(
            offset=0, limit=500, status=["downloading"],
        )
    except Exception:
        log.debug("downloading reaper: list_job_infos failed", exc_info=True)
        return 0
    if not infos:
        return 0
    now = datetime.utcnow()
    actioned = 0
    for info in infos:
        started = getattr(info, "started_at", None)
        if started is None:
            continue
        try:
            # JobInfo.started_at is naive-UTC (worker side writes utcnow())
            age_s = (now - started).total_seconds()
        except Exception:
            continue
        if age_s < _DOWNLOADING_TIMEOUT_S:
            continue
        worker_id = getattr(info, "worker_id", None)
        worker = (
            state.registry.connections.get(worker_id) if worker_id else None
        )
        # ----- Path A: worker is alive locally -> ask it to wrap up -----
        if worker is not None:
            try:
                await state.registry.send(
                    worker,
                    HubForceCompleteJob(
                        job_id=info.job_id,
                        reason=(
                            f"downloading exceeded {int(_DOWNLOADING_TIMEOUT_S)}s; "
                            f"saving partial"
                        ),
                    ),
                )
                log.info(
                    "downloading reaper: sent force-complete to worker %s for "
                    "job %s (age=%.0fs, pct=%s)",
                    worker_id, info.job_id, age_s,
                    getattr(info.progress, "download_pct", None) if info.progress else None,
                )
                actioned += 1
            except Exception:
                log.debug(
                    "downloading reaper: force-complete send failed for %s",
                    info.job_id, exc_info=True,
                )
            continue
        # ----- Path B: worker is not on THIS hub -> Redis-known? -----
        # The worker's WS lives on whichever hub the worker's consistent-hash
        # mapped to. Skip the row from this hub; the owning hub's reaper will
        # action it. (We can't tell from here if it's owned by a peer or
        # truly dead -- but the peer's reaper will hit the same row a few
        # seconds later. Belt-and-braces: see Path C below.)
        try:
            known_workers = await state.registry._fetch_known_workers()
        except Exception:
            known_workers = {}
        if worker_id and worker_id in known_workers:
            continue
        # ----- Path C: worker truly gone fleet-wide -> mark failed -----
        # No live WS anywhere in the fleet to action the request. The
        # detached video subprocess died with the worker; nothing to save.
        info.status = JobStatus.failed
        info.error = (
            f"downloading exceeded {int(_DOWNLOADING_TIMEOUT_S)}s and worker "
            f"{worker_id} is gone fleet-wide; no partial save possible"
        )
        if info.progress is not None:
            info.progress.phase = "failed"
        info.completed_at = datetime.utcnow()
        try:
            await store.save_job_info(info)
            await store.save_job_result(JobResult(
                job_id=info.job_id, status=JobStatus.failed, error=info.error,
            ))
        except Exception:
            log.debug(
                "downloading reaper: save_job_info failed for %s",
                info.job_id, exc_info=True,
            )
            continue
        log.info(
            "downloading reaper: marked %s as failed (worker %s gone "
            "fleet-wide, age=%.0fs)",
            info.job_id, worker_id, age_s,
        )
        actioned += 1
    return actioned


async def _stale_job_reconciler_loop():
    """Periodically settle jobs the per-process recovery nets missed:
    running jobs whose worker is gone fleet-wide, and queued jobs that
    timed out without assignment. Idempotent; safe to run on every hub."""
    first = True
    while True:
        try:
            await asyncio.sleep(45 if first else _STALE_RECONCILE_INTERVAL_S)
        except asyncio.CancelledError:
            return
        first = False
        try:
            await _reconcile_stale_jobs()
        except Exception:
            log.debug("stale-job reconciler pass failed", exc_info=True)


async def _reconcile_stale_jobs() -> tuple[int, int]:
    """One reconciliation pass. Returns (running_settled, queued_failed).

    Pulled out of the loop so it can be unit-/dry-run-tested directly.
    """
    store = state.store
    if store is None or not hasattr(store, "list_job_infos"):
        return (0, 0)

    now = datetime.utcnow()

    # ---- fleet-wide alive worker set (cross-hub, via Redis) ----
    # CRITICAL: this hub's state.registry.connections only holds ~1/3 of the
    # fleet (the workers whose control WS lands here). A running job's worker
    # may be alive on a PEER hub. stats_async() merges local connections +
    # the Redis-known peers (alive = fresh heartbeat AND owner present), which
    # is exactly the set GET /workers shows. Judging liveness off local
    # connections would mass-fail healthy peer-owned jobs.
    alive_ids: set[str] = set()
    have_fleet_view = False
    if state.registry is not None:
        try:
            snap = await state.registry.stats_async()
            alive_ids = {
                w.get("worker_id")
                for w in snap.get("workers", [])
                if w.get("alive") and w.get("worker_id")
            }
            have_fleet_view = True
        except Exception:
            log.debug("reconciler: stats_async failed; skipping running pass", exc_info=True)
            have_fleet_view = False

    running_settled = 0
    queued_failed = 0

    # ---- pass 1: orphaned running jobs ----
    # Only when we actually have a fleet view -- an empty alive set from a
    # Redis blip must NEVER be read as "every worker is dead".
    if have_fleet_view and alive_ids:
        try:
            infos, _ = await store.list_job_infos(status=["running"], limit=0)
        except Exception:
            infos = []
        for info in infos:
            jid = info.job_id
            # codegen-loop / rerun run in-process on the owning hub; a live
            # local task means it's alive here. (We can't see a peer hub's
            # local_tasks, but those are covered by the lease reaper; this
            # pass targets worker-orchestrated fetch jobs.)
            if jid in state.local_tasks:
                continue
            if info.status != JobStatus.running:
                continue
            wid = info.worker_id
            if wid and wid in alive_ids:
                continue  # worker is alive somewhere in the fleet -> leave it
            try:
                started = info.started_at or info.created_at
                age = (now - started).total_seconds()
            except Exception:
                age = _STALE_RUNNING_GRACE_S + 1
            if age < _STALE_RUNNING_GRACE_S:
                continue  # grace: worker may be mid-update / re-announcing
            phase = getattr(info.progress, "phase", "") if info.progress else ""
            if phase == "keepalive":
                # Crawl already finished + assets saved; only the interactive
                # keepalive session died with the worker. Mirror the
                # disconnect-settle: complete cleanly, not a failure.
                info.status = JobStatus.completed
                if info.progress is not None:
                    info.progress.phase = "completed"
                    info.progress.last_log = (
                        "[reconciler] keepalive worker gone; capture already saved"
                    )
                settle_reason = "completed (keepalive worker gone)"
            else:
                info.status = JobStatus.failed
                info.error = (
                    f"worker {wid or '(unassigned)'} no longer in the live fleet "
                    f"(orphaned by a hub/worker restart or self-update; ran for "
                    f"{age/3600:.1f}h); previous attempts preserved under "
                    f"/jobs/{{id}}/attempts. Re-submit to retry."
                )
                if info.progress is not None:
                    info.progress.phase = "failed"
                    info.progress.last_log = "[reconciler] orphaned running job settled"
                settle_reason = "failed (orphaned)"
            info.completed_at = info.completed_at or now
            try:
                await store.save_job_info(info)
                await store.publish_log(jid, "  !! [reconciler] " + (info.error or settle_reason))
                await store.publish_log(jid, DONE_SENTINEL)
            except Exception:
                continue
            running_settled += 1
            log.info(
                "reconciler: settled orphaned running job %s -> %s "
                "(worker=%s age=%.1fh)",
                jid, settle_reason, wid, age / 3600,
            )
    elif have_fleet_view and not alive_ids:
        log.info(
            "reconciler: fleet alive set is empty (no live workers or Redis "
            "blip) -> skipping running-orphan pass this tick (safety)"
        )

    # ---- pass 2: stuck queued jobs (durable queued-timeout) ----
    # Independent of the fleet view: a queued job has no worker yet. If it's
    # still queued past the timeout, no synchronous dispatch and no in-process
    # guard ever resolved it -> fail it out. New queued jobs (< timeout) are
    # left for the normal POST-time dispatch / in-process guard.
    try:
        qinfos, _ = await store.list_job_infos(status=["queued"], limit=0)
    except Exception:
        qinfos = []
    for info in qinfos:
        jid = info.job_id
        if jid in state.local_tasks:
            continue  # codegen/rerun pre-dispatch; its own task will resolve it
        if info.status != JobStatus.queued:
            continue
        try:
            age = (now - info.created_at).total_seconds()
        except Exception:
            age = _STALE_QUEUED_TIMEOUT_S + 1
        if age < _STALE_QUEUED_TIMEOUT_S:
            continue
        info.status = JobStatus.failed
        info.completed_at = now
        if info.progress is not None:
            info.progress.phase = "timed_out"
            info.progress.last_log = "[reconciler] queued without assignment past timeout"
        info.error = (
            f"queued for {age/60:.0f} min without assignment (no worker/lane "
            f"picked it up; durable reconciler). Re-submit to retry."
        )
        try:
            await store.save_job_info(info)
            await store.publish_log(jid, "  !! [reconciler] " + info.error)
            await store.publish_log(jid, DONE_SENTINEL)
        except Exception:
            continue
        # Retiring the ROW is not enough: the id may still be sitting on the
        # redis pull list, and a worker that BLPOPs it puts the job straight
        # back into flight -- which is half of the 2026-08-30 resurrection
        # cycle (the other half was redrive having no upper age bound). Evict
        # it so "failed" actually sticks.
        await _evict_from_pull_queue(jid, "reconciler")
        queued_failed += 1
        log.info(
            "reconciler: failed stuck queued job %s (queued %.0f min)",
            jid, age / 60,
        )

    if running_settled or queued_failed:
        log.info(
            "reconciler: pass settled %d orphaned-running + %d stuck-queued job(s)",
            running_settled, queued_failed,
        )
    return (running_settled, queued_failed)


# ----------------------------------------------------------------------------
# Skill / convention retire reaper (selection loop, retire phase)
# ----------------------------------------------------------------------------
# How often to scan the registries. Slow -- fitness only shifts over many
# jobs, so hourly is plenty and keeps the log quiet.
_RETIRE_INTERVAL_S = 3600
# Need at least this many injections before a low success_rate is trusted
# as a real "dud" verdict (small samples are noise).
# Lowered 5 -> 3 on 2026-07-14: the middle-tier of never-won skills
# (use=3..9 succ=0) was accumulating and injecting noise; tighter dud
# threshold catches them earlier.
_RETIRE_MIN_USE = 3
# Higher water mark: a skill repeatedly injected this many times with
# success_count == 0 is a "high-use never-won" dud and is retired even
# under the cold-start guard (otherwise an early-stuck-at-0 system can
# never escape the dud loop -- exactly what happened with
# age-gate-media-extraction at use=45 / succ=0).
# Lowered 20 -> 10 on 2026-07-14: 10 uses is already enough signal to
# call a skill dead, and the top-5 winners routinely hit 30-70 uses so
# we won't false-positive them.
_RETIRE_HIGH_USE_DUD = 10
# success_rate at/below this (with >= _RETIRE_MIN_USE uses) = repeatedly
# rode along yet rarely correlated with success.
_RETIRE_MAX_RATE = 0.15
# Auto-tier record never injected AND older than this = zombie the operator
# never groomed; safe to drop.
_RETIRE_IDLE_DAYS = 30

# Snapshot of the most recent _skill_convention_reaper_loop pass so the admin
# UI can answer "is this thing alive and what did it just do?" without
# tailing logs. Updated at the END of every pass (so a long-running pass is
# only reflected when it finishes; that's fine, passes take ~ms).
_last_skill_conv_reaper_run_at: datetime | None = None
_last_skill_conv_reaper_pass: dict = {}


def get_skill_convention_reaper_status() -> dict:
    """Snapshot for routes/skills.py:/ai/grooming-status. Pure read; never
    raises. Returns the last-pass stats + interval so the UI can compute
    "next run at" and explain WHY the candidate list is empty (most often:
    cold-start dud guard, see ``allow_dud_*`` flags)."""
    nxt_iso = None
    try:
        from datetime import timedelta as _td
        if _last_skill_conv_reaper_run_at is not None:
            nxt_iso = (_last_skill_conv_reaper_run_at + _td(seconds=_RETIRE_INTERVAL_S)).isoformat() + "Z"
    except Exception:
        pass
    return {
        "last_run_at": (_last_skill_conv_reaper_run_at.isoformat() + "Z") if _last_skill_conv_reaper_run_at else None,
        "next_run_at": nxt_iso,
        "interval_s": _RETIRE_INTERVAL_S,
        "min_use_for_dud": _RETIRE_MIN_USE,
        "max_rate_for_dud": _RETIRE_MAX_RATE,
        "zombie_idle_days": _RETIRE_IDLE_DAYS,
        "last_pass": dict(_last_skill_conv_reaper_pass or {}),
    }


# Token-Jaccard threshold for calling two AUTO records near-duplicates.
# High on purpose -- a false merge silently loses a distinct skill, so we
# only fold things that are almost the same one-liner.
_DEDUP_SIM = 0.82
_DEDUP_WORD_RE = re.compile(r"[a-z0-9]+")
# Drop function words + crudely singularise so morphological / stopword
# noise ("image" vs "images", "the") doesn't sink the similarity of two
# descriptions that mean the same thing.
_DEDUP_STOP = {
    "a", "an", "the", "of", "to", "for", "and", "or", "in", "on", "with",
    "at", "by", "from", "that", "this", "it", "is", "are", "be", "as",
    "into", "via", "after", "then", "any", "all", "n",
}


def _dedup_text(rec, kind: str) -> str:
    """The retrieval/injection-facing one-liner -- the cleanest similarity
    signal (a skill's ``description``, a convention's ``advice``)."""
    if kind == "skill":
        return getattr(rec, "description", "") or getattr(rec, "name", "") or ""
    return getattr(rec, "advice", "") or getattr(rec, "name", "") or ""


def _dedup_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for w in _DEDUP_WORD_RE.findall((text or "").lower()):
        if w in _DEDUP_STOP:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]  # crude singularise: images -> image, pages -> page
        out.add(w)
    return out


def _dedup_clusters(records, kind: str) -> list[list]:
    """Near-duplicate clusters (size >= 2) among AUTO-tier records, by token
    Jaccard on the description/advice. Curated records are excluded -- they
    are operator-owned and never auto-merged."""
    autos = [r for r in records if getattr(r, "tier", "auto") == "auto"]
    toks = {r.slug: _dedup_tokens(_dedup_text(r, kind)) for r in autos}
    parent = {r.slug: r.slug for r in autos}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(autos)):
        for j in range(i + 1, len(autos)):
            a, b = autos[i].slug, autos[j].slug
            ta, tb = toks[a], toks[b]
            if not ta or not tb:
                continue
            if len(ta & tb) / len(ta | tb) >= _DEDUP_SIM:
                parent[find(a)] = find(b)

    by_slug = {r.slug: r for r in autos}
    groups: dict[str, list] = {}
    for slug in parent:
        groups.setdefault(find(slug), []).append(by_slug[slug])
    return [g for g in groups.values() if len(g) >= 2]


def _dedup_pick(cluster):
    """Pick the survivor (best fitness, then most-used, then newest) and
    return ``(keep, drops)``."""
    def key(r):
        uc = getattr(r, "use_count", 0) or 0
        sc = getattr(r, "success_count", 0) or 0
        return ((sc / uc) if uc else 0.0, uc, getattr(r, "created_at", "") or "")

    keep = max(cluster, key=key)
    return keep, [r for r in cluster if r.slug != keep.slug]


def _retire_reason(rec, *, allow_dud: bool) -> str | None:
    """Why this record should be retired, or None to keep it. Pure /
    tier-agnostic; the caller decides whether to ACT (auto) or just
    suggest (curated).

    ``allow_dud`` gates the success_rate-based "dud" verdict. It must be
    False for conventions (they ride along on EVERY job, so their rate is
    just the global job-success rate, not a per-rule signal) and during
    cold start (no successes recorded yet -- every rate is a meaningless
    0.0). The zombie verdict (never injected + old) is always safe."""
    uc = getattr(rec, "use_count", 0) or 0
    sc = getattr(rec, "success_count", 0) or 0
    # High-water dud: independently of the cold-start guard, a skill that
    # has been injected this many times with zero successes is wasting AI
    # cycles -- retire it. This unblocks the "everything is at succ=0 so
    # the cold-start guard suppresses ALL dud verdicts" deadlock.
    if uc >= _RETIRE_HIGH_USE_DUD and sc == 0:
        return f"dud (high-use never-won, use={uc} success=0)"
    if allow_dud and uc >= _RETIRE_MIN_USE and (sc / uc) <= _RETIRE_MAX_RATE:
        return f"dud (use={uc} success={sc} rate={sc / uc:.2f})"
    if uc == 0:
        try:
            created = datetime.fromisoformat((rec.created_at or "").replace("Z", ""))
            age_days = (datetime.utcnow() - created).days
        except Exception:
            age_days = 0
        if age_days >= _RETIRE_IDLE_DAYS:
            return f"zombie (never injected, age={age_days}d)"
    return None


async def _skill_convention_reaper_loop():
    """Retire auto-tier skills / conventions that aren't earning their
    keep -- repeatedly injected but rarely tied to success (duds), or
    never exercised and old (zombies). The fitness signal comes from the
    selection loop (success_count / use_count).

    Safety rails:
      * CURATED entries are operator-approved and NEVER auto-deleted --
        a curated dud is only logged as a suggestion.
      * Auto-tier deletion is gated by the ``auto_retire_enabled`` setting
        (default off). When off, candidates are logged as a dry-run so the
        operator can review before turning it on. Human stays in the loop.
    """
    first = True
    while True:
        try:
            # First scan shortly after startup so the operator sees the
            # dry-run candidate list promptly; hourly thereafter.
            await asyncio.sleep(120 if first else _RETIRE_INTERVAL_S)
        except asyncio.CancelledError:
            return
        first = False
        enabled = False
        try:
            if state.settings is not None:
                enabled = bool(state.settings.get("auto_retire_enabled", False))
        except Exception:
            enabled = False
        # Snapshot accumulators -- updated under both branches (dry-run + act)
        # and written into the module-level state at the end of the pass.
        global _last_skill_conv_reaper_run_at, _last_skill_conv_reaper_pass
        _pass_stats: dict = {"by_kind": {}}
        for kind, reg in (("skill", state.skills), ("convention", state.conventions)):
            if reg is None:
                continue
            try:
                records = reg.list_all()
            except Exception:
                continue
            # The success_rate "dud" verdict is only meaningful for SKILLS
            # (retrieved per-job, so the rate is per-skill) AND only once
            # the registry has recorded at least one success (otherwise we
            # are in cold start, every rate is a meaningless 0.0). For
            # conventions (injected every job) the rate is just the global
            # job-success rate, never a per-rule signal -- so only the
            # zombie verdict applies to them.
            total_success = sum(getattr(r, "success_count", 0) or 0 for r in records)
            allow_dud = kind == "skill" and total_success > 0
            _ks: dict = {
                "records": len(records),
                "total_success": int(total_success),
                "allow_dud": bool(allow_dud),
                "retire_candidates": 0,
                "retire_acted": 0,
                "dedup_candidates": 0,
                "dedup_acted": 0,
                "cold_start_skip_reason": (
                    "skill 全体の success=0 のため dud 判定を skip 中 (zombie 判定は常時有効)"
                    if kind == "skill" and not allow_dud else
                    "convention は use rate が per-rule signal でないため dud 判定 無効 (zombie のみ)"
                    if kind == "convention" else ""
                ),
            }
            _pass_stats["by_kind"][kind] = _ks
            for rec in records:
                reason = _retire_reason(rec, allow_dud=allow_dud)
                if not reason:
                    continue
                _ks["retire_candidates"] += 1
                tier = getattr(rec, "tier", "auto")
                slug = getattr(rec, "slug", "?")
                if tier != "auto":
                    log.info(
                        "retire: curated %s %r looks stale -- %s "
                        "(curated is never auto-deleted; review manually)",
                        kind, slug, reason,
                    )
                    continue
                if not enabled:
                    log.info(
                        "retire(dry-run): would delete auto %s %r -- %s "
                        "(set auto_retire_enabled=true to act)",
                        kind, slug, reason,
                    )
                    continue
                try:
                    reg.delete(slug, tier="auto")
                    _ks["retire_acted"] += 1
                    log.info("retire: deleted auto %s %r -- %s", kind, slug, reason)
                except Exception:
                    log.warning(
                        "retire: failed to delete auto %s %r", kind, slug, exc_info=True
                    )

            # ---- dedup pass: consolidate near-duplicate AUTO records ----
            dedup_enabled = False
            try:
                if state.settings is not None:
                    dedup_enabled = bool(state.settings.get("auto_dedup_enabled", False))
            except Exception:
                dedup_enabled = False
            try:
                clusters = _dedup_clusters(reg.list_all(), kind)
            except Exception:
                clusters = []
            for cluster in clusters:
                keep, drops = _dedup_pick(cluster)
                drop_slugs = [d.slug for d in drops]
                _ks["dedup_candidates"] += 1
                if not dedup_enabled:
                    log.info(
                        "dedup(dry-run): would merge auto %s %r <- %r "
                        "(set auto_dedup_enabled=true to act)",
                        kind, keep.slug, drop_slugs,
                    )
                    continue
                try:
                    reg.merge(keep.slug, drop_slugs)
                    _ks["dedup_acted"] += 1
                    log.info("dedup: merged auto %s %r <- %r", kind, keep.slug, drop_slugs)
                except Exception:
                    log.warning(
                        "dedup: merge failed %s %r", kind, keep.slug, exc_info=True
                    )
        # End of pass: commit the snapshot so the admin UI can read it.
        _last_skill_conv_reaper_run_at = datetime.utcnow()
        _pass_stats["ran_at"] = _last_skill_conv_reaper_run_at.isoformat() + "Z"
        _pass_stats["auto_retire_enabled"] = bool(enabled)
        _pass_stats["auto_dedup_enabled"] = bool(dedup_enabled)
        _last_skill_conv_reaper_pass = _pass_stats


# ----------------------------------------------------------------------------
# Job-lease loop (multi-hub control-plane phase 4: dead-hub recovery)
# ----------------------------------------------------------------------------
# Two responsibilities, both gated behind PAPRIKA_JOB_LEASE_ENABLED (OFF by
# default -> the loop returns immediately and nothing below runs, so single-
# hub behaviour is unchanged):
#
#   1. REFRESH -- for every hub-orchestrated job this hub is running locally
#      (state.local_tasks: codegen-loop + rerun), re-write its lease so peers
#      know it's alive. Finished tasks are pruned and their lease released.
#
#   2. REAP -- scan recent store jobs for ones marked ``running`` that this
#      hub does NOT hold and whose lease has expired (= the owning hub died).
#      Atomically re-claim (SET NX) and re-dispatch on this hub, bounded by a
#      durable requeue counter so a poison job can't bounce forever.

# How many recent jobs to scan per reap pass. Running jobs are few and live
# near the head of the recency index, so a bounded window keeps the Redis cost
# flat regardless of total job history.
_LEASE_REAP_SCAN = int(os.environ.get("PAPRIKA_JOB_LEASE_SCAN", "150"))


async def _job_lease_loop():
    """Refresh local job leases + reap orphaned ones. Dormant unless
    PAPRIKA_JOB_LEASE_ENABLED is set AND a Redis client is available."""
    from server.hub import _leases as leases

    if not leases.enabled():
        return  # default: feature off -> loop never runs
    redis = getattr(state.store, "_r", None)
    if redis is None:
        log.info(
            "job-lease loop: PAPRIKA_JOB_LEASE_ENABLED set but no Redis client "
            "(store_kind=%s); leasing disabled", state.store_kind,
        )
        return

    hub_id = config.hub_id
    interval = leases.refresh_interval_s()
    log.info(
        "job-lease loop: ENABLED (hub=%s ttl=%ds refresh=%.0fs max_requeues=%d)",
        hub_id, leases.lease_ttl_s(), interval, leases.max_requeues(),
    )
    first = True
    while True:
        try:
            # Small initial delay so a freshly dispatched job gets its first
            # lease written promptly; steady cadence thereafter.
            await asyncio.sleep(5 if first else interval)
        except asyncio.CancelledError:
            return
        first = False

        # (1) refresh leases for locally-running jobs; prune finished ones.
        for jid, task in list(state.local_tasks.items()):
            try:
                if task.done():
                    state.local_tasks.pop(jid, None)
                    await leases.release(redis, jid)
                    continue
                cur = await leases.read(redis, jid)
                rq = int(cur.get("requeues", 0)) if cur else 0
                await leases.refresh(redis, jid, hub_id, requeues=rq)
            except Exception:
                log.debug("job-lease refresh(%s) failed", jid, exc_info=True)

        # (2) reap orphaned running jobs that belong to a dead hub.
        try:
            await _reap_orphan_leased_jobs(redis, hub_id)
        except Exception:
            log.debug("job-lease reap pass failed", exc_info=True)


async def _reap_orphan_leased_jobs(redis, hub_id: str) -> None:
    """Find ``running`` hub-orchestrated jobs with an expired lease and
    re-dispatch them on this hub (or fail them out once requeues exhausted).
    Only one surviving hub wins each job, guaranteed by the atomic SET NX
    re-claim."""
    from server.hub import _leases as leases

    store = state.store
    if store is None:
        return
    try:
        job_ids = await store.list_job_ids(0, _LEASE_REAP_SCAN)
    except Exception:
        return

    ttl = leases.lease_ttl_s()
    maxr = leases.max_requeues()
    now = datetime.utcnow()

    for jid in job_ids:
        if jid in state.local_tasks:
            continue  # we own it locally -> alive, refreshed above
        try:
            info = await store.get_job_info(jid)
        except Exception:
            continue
        if info is None or info.status != JobStatus.running:
            continue
        mode = (info.options.mode if info.options else None) or "fetch"
        if mode not in ("codegen-loop", "rerun"):
            continue  # only hub-orchestrated jobs die with the hub process

        # Grace window: a just-dispatched job on a live peer may not have
        # written its first lease yet. Skip anything younger than one TTL so
        # we never steal a job that simply hasn't ticked once.
        try:
            age = (now - (info.started_at or info.created_at)).total_seconds()
        except Exception:
            age = ttl + 1
        if age < ttl:
            continue

        # A live lease means a peer is actively refreshing it -> alive.
        if await leases.read(redis, jid) is not None:
            continue

        rq = await leases.get_requeues(redis, jid)

        # Exhausted: don't re-run again, fail it out (once, via the claim).
        if rq >= maxr:
            if await leases.acquire(redis, jid, hub_id, requeues=rq):
                await _fail_orphan_job(
                    info,
                    f"orphaned by a dead hub and exceeded max re-dispatches "
                    f"({rq}/{maxr}); previous attempts preserved under "
                    f"/jobs/{{id}}/attempts.",
                )
                await leases.release(redis, jid)
            continue

        # Claim atomically; only the winning hub proceeds.
        if not await leases.acquire(redis, jid, hub_id, requeues=rq + 1):
            continue
        await leases.bump_requeues(redis, jid)
        try:
            from server.hub._jobrunner import redispatch_orphan_job
            ok = await redispatch_orphan_job(jid)
        except Exception:
            log.warning("job-lease: re-dispatch of %s crashed", jid, exc_info=True)
            ok = False
        if ok:
            log.info(
                "job-lease: recovered orphaned %s job %s (requeue %d/%d)",
                mode, jid, rq + 1, maxr,
            )
        else:
            # Couldn't re-run here (e.g. unresolvable rerun source) -> fail
            # it out and drop our claim so it doesn't sit half-claimed.
            await _fail_orphan_job(
                info,
                "orphaned by a dead hub and could not be re-dispatched on a "
                "surviving hub; previous attempts preserved.",
            )
            await leases.release(redis, jid)


async def _fail_orphan_job(info, reason: str) -> None:
    """Mark an unrecoverable orphaned job failed + close its live-log stream.
    Best-effort; mirrors _recover_orphan_running_jobs's terminal write."""
    if state.store is None:
        return
    try:
        info.status = JobStatus.failed
        info.completed_at = info.completed_at or datetime.utcnow()
        info.error = reason
        if info.progress is not None:
            info.progress.phase = "failed"
            info.progress.last_log = "[lease recovery] " + reason[:160]
        await state.store.save_job_info(info)
        try:
            await state.store.publish_log(info.job_id, "  !! [lease recovery] " + reason)
            await state.store.publish_log(info.job_id, DONE_SENTINEL)
        except Exception:
            pass
    except Exception:
        log.debug("job-lease: fail_orphan_job(%s) failed", getattr(info, "job_id", "?"), exc_info=True)


# --------------------------------------------------------------------------- #
# Auto job-retention GC
# --------------------------------------------------------------------------- #
# Permanently delete terminal jobs older than settings["job_retention_days"]
# -- DB row (+CASCADE job_results/job_logs) + MinIO/S3 objects + local cache --
# so the jobs table (800k+ rows, job_results ~15 GB) + the bucket don't grow
# unbounded. Opt-in (settings["job_retention_enabled"], default OFF) because it
# is irreversible. SQL-selected + batched (list_deletable_job_ids) so it scales
# where the manual O(N) /jobs/cleanup times out. A redis lock runs ONE hub per
# tick (delete is idempotent, but this avoids 7x MinIO churn). Kill-switch env
# PAPRIKA_JOB_RETENTION_DISABLE=1.
_RETENTION_INTERVAL_S = float(
    os.environ.get("PAPRIKA_JOB_RETENTION_INTERVAL_S", "1200") or 1200
)
_RETENTION_BATCH = int(os.environ.get("PAPRIKA_JOB_RETENTION_BATCH", "500") or 500)
# Cap per tick so a huge first-run backlog drains GRADUALLY instead of one
# multi-hour delete storm hammering MariaDB + MinIO. 20k is sized against the
# 2026-11-02 measurement: a pass purges ~75 jobs/s (MinIO p50 0.6 s at 32-way),
# so this is a ~4.5 min pass, well inside one INTERVAL even on a slow day.
_RETENTION_MAX_PER_TICK = int(
    os.environ.get("PAPRIKA_JOB_RETENTION_MAX_PER_TICK", "20000") or 20000
)
# After a pass FINISHES the lock is shortened to this instead of being left to
# run out its full TTL. Until 2026-11-02 the holder kept the lock for the whole
# INTERVAL*0.9 even when the pass took 2 min, so the fleet-wide drain rate was
# pinned to MAX_PER_TICK per INTERVAL (8000/600 s = 1.15M/day) against an inflow
# of 1.6M/day -- the table grew for weeks with every delete finishing fast. The
# cooldown is only there so 20 hubs don't start back-to-back passes the same
# second; the TTL stays as the crash safety net.
_RETENTION_COOLDOWN_S = max(
    1, int(os.environ.get("PAPRIKA_JOB_RETENTION_COOLDOWN_S", "30") or 30)
)
# How long the pass lock may be held before another hub treats the holder as
# dead. It used to be INTERVAL*0.9 (540 s), which the first 20k-job passes
# (488-969 s measured 2026-11-02) ran straight through: the lock lapsed
# mid-pass, the next hub started its own pass on top, and both slowed down
# sharing MinIO. This is a crash safety net, not a scheduler -- the lock is
# normally handed back by _release_pass_lock within seconds of the pass
# finishing -- so it only has to be comfortably longer than the slowest pass.
_RETENTION_LOCK_TTL_S = max(
    60, int(os.environ.get("PAPRIKA_JOB_RETENTION_LOCK_TTL_S", "1800") or 1800)
)
# Per-job deletes (MinIO purge + DB row + local rmtree) run BOUNDED-PARALLEL
# within a batch instead of strictly sequential -- the MinIO delete_prefix RTT
# was the pass's wall-clock bottleneck, so one slow object tier throttled the
# whole drain to ~inflow rate and the backlog never shrank. Concurrency is
# capped so a drain can't storm MariaDB/MinIO (same reasoning as the per-tick
# cap). All three underlying deletes are already async / to_thread, so this
# just overlaps their waits; it does NOT add loop-blocking work.
_RETENTION_DELETE_CONCURRENCY = max(
    1, int(os.environ.get("PAPRIKA_JOB_RETENTION_DELETE_CONCURRENCY", "10") or 10)
)
_RETENTION_STATUSES = ["completed", "failed", "cancelled", "review"]
# Per-job MinIO purge timeout. A hung delete (slow / unreachable object tier)
# must never STALL the whole pass on one job -- past this it skips that object
# purge (logged) and moves on; the DB row is still deleted and a rare skipped
# prefix is reclaimable by a later pass. Defense-in-depth added 2026-07-11 while
# hardening auto-retention against a silent stall.
_RETENTION_MINIO_TIMEOUT_S = float(
    os.environ.get("PAPRIKA_JOB_RETENTION_MINIO_TIMEOUT_S", "30") or 30
)
_RETENTION_LOCK_KEY = "paprika:retention:lock"


async def _acquire_pass_lock(r, key: str, ttl_s: float) -> bool:
    """Elect ONE hub for a GC pass via ``SET NX EX``. Best-effort: a redis
    error means "proceed" (delete is idempotent, the lock only avoids N-fold
    MinIO churn), a held lock means "not this hub, not this tick"."""
    if r is None:
        return True
    try:
        got = await r.set(key, config.hub_id or "1", nx=True, ex=max(60, int(ttl_s)))
        return bool(got)
    except Exception:
        return True


async def _release_pass_lock(r, key: str, cooldown_s: float) -> None:
    """Hand the lock back as soon as the pass is over (keeping it for
    ``cooldown_s`` so the next hub's tick isn't the very same second).
    Only our own lock is touched: if the TTL already expired and another hub
    re-took it, leave theirs alone. Best-effort -- failure here just means
    the lock runs out its original TTL like it always did."""
    if r is None:
        return
    try:
        cur = await r.get(key)
        if cur is not None and not isinstance(cur, str):
            cur = cur.decode("utf-8", "replace")
        if cur == (config.hub_id or "1"):
            await r.expire(key, max(1, int(cooldown_s)))
    except Exception:
        log.debug("retention: lock release failed", exc_info=True)


_last_retention_run_at: datetime | None = None
_last_retention_deleted = 0


async def _evict_from_pull_queue(jid: str, why: str) -> None:
    """Drop a job id from the redis pull list. Best-effort and inert when pull
    dispatch is off.

    Every path that makes a job "no longer runnable" -- terminal state, row
    deleted -- must call this. The list is the ONE piece of job state that
    outlives the DB row, so an id left behind is a job that comes back from the
    dead the moment a worker has a free lane."""
    try:
        from server.hub import _pull_queue

        if _pull_queue.ENABLED:
            await _pull_queue.remove(jid)
    except Exception:
        log.debug("%s: pull-list evict %s failed", why, jid, exc_info=True)


async def _evict_many_from_pull_queue(ids: list[str], why: str) -> int:
    """Batch form of :func:`_evict_from_pull_queue` for a retention batch: one
    LRANGE for the whole batch instead of one LREM per job (see
    ``_pull_queue.remove_many`` for the 2026-11-02 numbers). Best-effort."""
    try:
        from server.hub import _pull_queue

        if _pull_queue.ENABLED and ids:
            return int(await _pull_queue.remove_many(list(ids)))
    except Exception:
        log.debug("%s: batch pull-list evict failed", why, exc_info=True)
    return 0


async def _purge_job(
    store, jid: str, sem, minio_timeout: float, timings: dict | None = None,
    *, evict: bool = True,
) -> bool:
    """Purge one job everywhere: MinIO/S3 prefix + DB row (+CASCADE) + local
    cache dir. Returns True on a successful DB-row delete. A MinIO / FS hiccup
    is logged but never fails the row delete (a stray object is reclaimable by
    a later pass). CancelledError propagates so shutdown tears the pass down.

    Shared by the general job-retention pass and the review-specific expiry
    pass so both delete identically (one code path = one behaviour to reason
    about). ``sem`` bounds how many purges run at once.

    ``timings`` (optional) accumulates wall-seconds per phase -- ``minio`` /
    ``db`` / ``evict`` / ``fs`` -- summed across every purge of a pass, so the
    pass summary can say WHERE a slow pass spent its time. Standalone probes
    (2026-11-02) put every phase at milliseconds while in-hub passes took
    8-16 min for 20k jobs; without this the gap could only be guessed at.

    ``evict=False`` skips the per-job pull-list LREM; the caller must then have
    evicted the whole batch up front with :func:`_evict_many_from_pull_queue`
    (the retention passes do). Single-job callers keep the default."""
    from server.hub import objstore
    from server.hub._state import get_storage_dir
    import shutil as _shutil

    def _tick(phase: str, t0: float) -> None:
        if timings is not None:
            timings[phase] = timings.get(phase, 0.0) + (time.monotonic() - t0)

    async with sem:
        try:
            if objstore.enabled():
                t0 = time.monotonic()
                try:
                    await asyncio.wait_for(
                        objstore.delete_prefix(jid), timeout=minio_timeout
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Includes TimeoutError: a hung tier can't stall the pass.
                    log.debug("retention: MinIO purge %s failed", jid, exc_info=True)
                _tick("minio", t0)
            t0 = time.monotonic()
            await store.delete_job(jid)
            _tick("db", t0)
            # The row is gone, but the id can still be on the redis pull list
            # (it outlives the row). A worker that pops it re-dispatches the
            # job, and save_job_info -- an upsert -- re-INSERTS the row with its
            # ORIGINAL created_at. That is how deleted May/June jobs kept
            # reappearing at the head of the deletion frontier on 2026-08-30,
            # walking it backwards from 08-22 to 05-30. Deleting without this
            # is not a delete, it is a pause.
            if evict:
                t0 = time.monotonic()
                await _evict_from_pull_queue(jid, "retention")
                _tick("evict", t0)
            t0 = time.monotonic()
            try:
                d = get_storage_dir() / jid
                if d.exists():
                    await asyncio.to_thread(_shutil.rmtree, d, True)
            except Exception:
                pass
            _tick("fs", t0)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("retention: delete %s failed", jid, exc_info=True)
            return False


async def _purge_batch(
    store, ids: list[str], sem, minio_timeout: float, timings: dict | None = None,
) -> int:
    """Purge a whole retention batch: MinIO prefixes bounded-parallel, then
    the DB rows in ONE statement, then the local cache dirs. Returns the
    number of rows the DB reported deleted.

    This is :func:`_purge_job` restructured around the batch so the DB phase
    is one pool acquisition instead of one per job (see
    ``MariaDBJobStore.delete_jobs`` for the 2026-11-02 numbers). Stores
    without ``delete_jobs`` fall back to per-job :func:`_purge_job`. The
    caller has already evicted the batch from the pull list."""
    from server.hub import objstore
    from server.hub._state import get_storage_dir
    import shutil as _shutil

    if not ids:
        return 0
    if not hasattr(store, "delete_jobs"):
        results = await asyncio.gather(
            *(_purge_job(store, j, sem, minio_timeout, timings, evict=False) for j in ids)
        )
        return sum(1 for ok in results if ok)

    def _tick(phase: str, t0: float) -> None:
        if timings is not None:
            timings[phase] = timings.get(phase, 0.0) + (time.monotonic() - t0)

    async def _minio_one(jid: str) -> None:
        async with sem:
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(objstore.delete_prefix(jid), timeout=minio_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("retention: MinIO purge %s failed", jid, exc_info=True)
            _tick("minio", t0)

    if objstore.enabled():
        await asyncio.gather(*(_minio_one(j) for j in ids))

    t0 = time.monotonic()
    try:
        deleted = int(await store.delete_jobs(list(ids)))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("retention: batch delete failed", exc_info=True)
        deleted = 0
    _tick("db", t0)

    t0 = time.monotonic()
    try:
        root = get_storage_dir()
        for jid in ids:
            d = root / jid
            if d.exists():
                await asyncio.to_thread(_shutil.rmtree, d, True)
    except Exception:
        pass
    _tick("fs", t0)
    return deleted


async def _clear_own_stale_lock(r, key: str) -> None:
    """At loop start, drop a pass lock that THIS hub still holds from a
    previous life. A hub that is restarted mid-pass (docker restart does not
    wait for the pass's ``finally``) leaves its lock behind for the full TTL,
    and with the TTL at 30 min (crash safety net) every other hub sits out
    that long -- 2026-11-02 17:45-18:14, one single-hub restart, zero passes
    fleet-wide. Only our own id is touched; another hub's live lock is left."""
    if r is None:
        return
    try:
        cur = await r.get(key)
        if cur is not None and not isinstance(cur, str):
            cur = cur.decode("utf-8", "replace")
        if cur == (config.hub_id or "1"):
            await r.delete(key)
            log.info("retention: cleared stale %s left by our previous process", key)
    except Exception:
        log.debug("retention: stale-lock check failed", exc_info=True)


async def _job_retention_loop() -> None:
    """Background auto-retention GC. Idempotent; wired only on non-admin hubs."""
    if os.environ.get("PAPRIKA_JOB_RETENTION_DISABLE") == "1":
        return
    await _clear_own_stale_lock(getattr(state.store, "_r", None), _RETENTION_LOCK_KEY)
    first = True
    while True:
        try:
            await asyncio.sleep(300 if first else _RETENTION_INTERVAL_S)
        except asyncio.CancelledError:
            return
        first = False
        try:
            await _run_job_retention_once()
        except Exception:
            # WARNING (not debug): a silent pass failure is exactly what made a
            # non-draining retention loop invisible for hours (2026-07-11).
            log.warning("job-retention pass failed", exc_info=True)


async def _run_job_retention_once() -> int:
    """One retention pass. Returns #jobs deleted. Extracted for dry-run/test."""
    global _last_retention_run_at, _last_retention_deleted
    store = state.store
    if store is None or not hasattr(store, "list_deletable_job_ids"):
        return 0
    # Settings gate (cross-hub shared; OFF by default).
    if state.settings is None or not bool(
        state.settings.get("job_retention_enabled", False)
    ):
        return 0
    try:
        days = int(state.settings.get("job_retention_days", 10) or 10)
    except (TypeError, ValueError):
        days = 10
    if days <= 0:
        return 0
    # Elect ONE hub per tick. Delete is idempotent so a missing lock only costs
    # duplicate work, never correctness -> best-effort; proceed if redis absent.
    r = getattr(store, "_r", None)
    if not await _acquire_pass_lock(r, _RETENTION_LOCK_KEY, _RETENTION_LOCK_TTL_S):
        return 0
    from datetime import timedelta as _td

    cutoff = datetime.utcnow() - _td(days=days)
    deleted = 0
    attempted = 0
    t0 = time.monotonic()
    phases: dict[str, float] = {}

    # Bound how many job purges run at once (see _RETENTION_DELETE_CONCURRENCY).
    _sem = asyncio.Semaphore(_RETENTION_DELETE_CONCURRENCY)

    try:
        while deleted < _RETENTION_MAX_PER_TICK:
            try:
                ids = await store.list_deletable_job_ids(
                    cutoff, _RETENTION_STATUSES, _RETENTION_BATCH
                )
            except Exception:
                log.warning("job-retention: candidate query failed", exc_info=True)
                break
            if not ids:
                break
            # Pull-list eviction for the WHOLE batch before the rows go: one
            # LRANGE + LREM for the (rare) hits, instead of 500 blind LREMs.
            te = time.monotonic()
            await _evict_many_from_pull_queue(ids, "retention")
            phases["evict"] = phases.get("evict", 0.0) + (time.monotonic() - te)
            # MinIO purges BOUNDED-PARALLEL, then ONE batched row delete (see
            # _purge_batch). Per-job errors are swallowed inside; CancelledError
            # still propagates out for a clean shutdown.
            ok_n = await _purge_batch(
                store, ids, _sem, _RETENTION_MINIO_TIMEOUT_S, timings=phases
            )
            attempted += len(ids)
            deleted += ok_n
            if ok_n == 0:
                # Every delete in the batch failed. The candidate query would
                # hand back the SAME ids next round, so without this the pass
                # spins on them forever (the loop only ends on progress).
                break
            # Yield between batches so we never monopolise the DB pool / loop.
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            if len(ids) < _RETENTION_BATCH:
                break  # backlog drained for this window
    finally:
        await _release_pass_lock(r, _RETENTION_LOCK_KEY, _RETENTION_COOLDOWN_S)
    _last_retention_run_at = datetime.utcnow()
    _last_retention_deleted = deleted
    took = time.monotonic() - t0
    if deleted:
        # Phase sums are summed over CONCURRENT purges, so they add up to
        # roughly took * concurrency, not to took; read them as proportions.
        log.info(
            "job-retention: deleted %d/%d jobs older than %d days in %.1fs "
            "(DB + MinIO + cache)%s phases[s]: %s",
            deleted, attempted, days, took,
            " [hit per-tick cap]" if deleted >= _RETENTION_MAX_PER_TICK else "",
            " ".join(f"{k}={v:.0f}" for k, v in sorted(phases.items())) or "-",
        )
    elif attempted:
        # A pass that found candidates and deleted none used to leave no trace
        # at all, which is how a non-draining fleet stays invisible (2026-11-02:
        # unexplained 50-70 min gaps between passes with nothing in any log).
        log.warning(
            "job-retention: 0/%d candidates deleted in %.1fs -- every purge "
            "failed (DB/MinIO error? see debug log)", attempted, took,
        )
    return deleted


# --------------------------------------------------------------------------- #
# Review(課題)-specific expiry
# --------------------------------------------------------------------------- #
# A SEPARATE GC just for status=review, decoupled from the general
# job_retention_days. The general pass deletes oldest-first across ALL terminal
# statuses, so review rows (~2% of the table) are only reached once the deletion
# frontier sweeps past them -- their effective TTL is pinned to the general
# frontier depth, not to any review target. This pass queries review-ONLY with
# its own (shorter) cutoff + budget, so review is bounded at review_retention_days
# regardless of the general completed/failed backlog. Same purge path
# (_purge_job: DB row + MinIO prefix + local cache). OFF by default; dry_run
# default ON (first rollout only LOGS candidate counts). Kill-switch env
# PAPRIKA_REVIEW_RETENTION_DISABLE=1.
_REVIEW_RETENTION_INTERVAL_S = float(
    os.environ.get("PAPRIKA_REVIEW_RETENTION_INTERVAL_S", "1200") or 1200
)
_REVIEW_RETENTION_BATCH = int(
    os.environ.get("PAPRIKA_REVIEW_RETENTION_BATCH", "500") or 500
)
_REVIEW_RETENTION_MAX_PER_TICK = int(
    os.environ.get("PAPRIKA_REVIEW_RETENTION_MAX_PER_TICK", "5000") or 5000
)
_REVIEW_RETENTION_MINIO_TIMEOUT_S = float(
    os.environ.get("PAPRIKA_REVIEW_RETENTION_MINIO_TIMEOUT_S", "30") or 30
)
_REVIEW_RETENTION_LOCK_KEY = "paprika:review_retention:lock"

_last_review_retention_run_at: datetime | None = None
_last_review_retention_deleted = 0


async def _review_retention_loop() -> None:
    """Background review(課題) expiry GC. Idempotent; safe to run on every hub
    (a redis lock elects one hub per tick). Inert unless review_retention_enabled."""
    if os.environ.get("PAPRIKA_REVIEW_RETENTION_DISABLE") == "1":
        return
    await _clear_own_stale_lock(
        getattr(state.store, "_r", None), _REVIEW_RETENTION_LOCK_KEY
    )
    first = True
    while True:
        try:
            await asyncio.sleep(300 if first else _REVIEW_RETENTION_INTERVAL_S)
        except asyncio.CancelledError:
            return
        first = False
        try:
            await _run_review_retention_once()
        except Exception:
            # WARNING not debug: a silent failure is what let the general
            # retention loop stall invisibly (2026-07-11); mirror that lesson.
            log.warning("review-retention pass failed", exc_info=True)


async def _run_review_retention_once() -> dict:
    """One review-expiry pass. Returns a small stats dict.

    ``{"enabled", "dry_run", "days", "candidates", "deleted"}``. In dry_run the
    candidate count is probed (single indexed query, capped at MAX_PER_TICK) and
    NOTHING is deleted -- the operator reads the log to size the impact before
    flipping dry_run off. Extracted for on-demand / test invocation."""
    global _last_review_retention_run_at, _last_review_retention_deleted
    out = {"enabled": False, "dry_run": True, "days": 0, "candidates": 0, "deleted": 0}
    store = state.store
    if store is None or not hasattr(store, "list_deletable_job_ids"):
        return out
    if state.settings is None or not bool(
        state.settings.get("review_retention_enabled", False)
    ):
        return out
    out["enabled"] = True
    # NB: do NOT use ``or 7`` here -- that would coerce an explicit 0 back to
    # the default and delete at 7d. Respect an explicit 0/negative as "inert"
    # so the operator can zero the days to PAUSE the pass without unchecking.
    try:
        days = int(state.settings.get("review_retention_days", 7))
    except (TypeError, ValueError):
        days = 7
    if days <= 0:
        return out
    out["days"] = days
    dry = bool(state.settings.get("review_retention_dry_run", True))
    out["dry_run"] = dry

    # Elect ONE hub per tick (delete is idempotent; the lock just avoids 7x
    # MinIO churn). Best-effort -- proceed if redis absent.
    r = getattr(store, "_r", None)
    if not await _acquire_pass_lock(
        r, _REVIEW_RETENTION_LOCK_KEY, _RETENTION_LOCK_TTL_S
    ):
        return out

    from datetime import timedelta as _td

    cutoff = datetime.utcnow() - _td(days=days)

    try:
        # DRY-RUN: probe candidate volume without touching anything. One
        # indexed query (capped) -- if it returns the cap, report ">=cap".
        if dry:
            try:
                ids = await store.list_deletable_job_ids(
                    cutoff, ["review"], _REVIEW_RETENTION_MAX_PER_TICK
                )
            except Exception:
                log.warning("review-retention: dry-run candidate query failed", exc_info=True)
                return out
            out["candidates"] = len(ids)
            _last_review_retention_run_at = datetime.utcnow()
            _last_review_retention_deleted = 0
            log.info(
                "review-retention[dry-run]: %s%d review job(s) older than %d days "
                "would be purged (DB + MinIO + cache). Set review_retention_dry_run=false "
                "to enact.",
                (">=" if len(ids) >= _REVIEW_RETENTION_MAX_PER_TICK else ""),
                len(ids), days,
            )
            return out

        # ENACT: delete review-only, oldest-first, bounded-parallel + per-tick cap.
        _sem = asyncio.Semaphore(_RETENTION_DELETE_CONCURRENCY)
        deleted = 0
        while deleted < _REVIEW_RETENTION_MAX_PER_TICK:
            try:
                ids = await store.list_deletable_job_ids(
                    cutoff, ["review"], _REVIEW_RETENTION_BATCH
                )
            except Exception:
                log.warning("review-retention: candidate query failed", exc_info=True)
                break
            if not ids:
                break
            await _evict_many_from_pull_queue(ids, "review-retention")
            ok_n = await _purge_batch(
                store, ids, _sem, _REVIEW_RETENTION_MINIO_TIMEOUT_S
            )
            deleted += ok_n
            if ok_n == 0:
                break  # same ids would come back; don't spin (see job-retention)
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            if len(ids) < _REVIEW_RETENTION_BATCH:
                break
    finally:
        await _release_pass_lock(r, _REVIEW_RETENTION_LOCK_KEY, _RETENTION_COOLDOWN_S)
    out["candidates"] = deleted
    out["deleted"] = deleted
    _last_review_retention_run_at = datetime.utcnow()
    _last_review_retention_deleted = deleted
    if deleted:
        log.info(
            "review-retention: deleted %d review(課題) jobs older than %d days "
            "(DB + MinIO + cache)",
            deleted, days,
        )
    return out


# --------------------------------------------------------------------------- #
# failed 資産の早期 expiry
# --------------------------------------------------------------------------- #
# status=failed の job の MinIO 資産だけを failed_asset_expiry_hours 後に消す。
# DB 行は残す -- 消えるのは重い資産 (スクリーンショット / 画像 / page.html) だけで、
# /jobs の失敗一覧・error 文言・job_results はそのまま。行そのものは従来どおり
# job_retention_days で _job_retention_loop が消す。
#
# WHY このパスが要るのか (2026-08-29 実測):
#   * 画像 RAM (.47) の 29,101 prefix / 37.4 GB を全件突合したところ、
#     failed が 15,407 prefix / 21.7 GB = 58% を占めていた。全件 24 時間以内で、
#     「古いゴミの堆積」ではなく 1 日分の流量そのもの。
#   * pipeline 側の image-pull は completed/review しか消費しないので、
#     failed の画像は誰にも読まれないまま滞留する。
#   * 一般 retention は全 terminal ステータスを created_at 昇順で舐めるため
#     failed だけを早く消せない -- 実効 TTL は削除フロンティアの深さ (実測 7.4 日)
#     に固定される。21.7 GB/日 x 7.4 日 = 約 160 GB で、spill 閾値 216 GB の
#     3/4 を failed だけで食う。review_retention_loop を足したのと同じ理屈。
#   * 失敗率が 8.1% (8/25) -> 40.1% (8/29) に跳ねた 8/26 以降に顕在化した。
#     失敗率が 1 桁だった頃は 32 GB 程度で収まっていたので問題にならなかった。
#
# assets/ だけ消して page.html / final.jpg / log.txt を残す案も測ったが、
# assets/ は全体の 69% しかない (19.91 / 29.05 GB)。31% を 5 日間残すと定常
# 50 GB になるので prefix 丸ごと消す。
#
# カーソルは redis の watermark (created_at) で前進させる。下限があるので
# 「毎 tick 同じ先頭を読み直す」ことがなく、走査量は流入分だけで済む。
# 削除に失敗した job は retry セットへ積んで次 tick で拾う -- ここを黙って
# 捨てると _purge_job と同じく「DB には無いが MinIO には在る」孤児を作る。
_FAILED_ASSET_EXPIRY_INTERVAL_S = float(
    os.environ.get("PAPRIKA_FAILED_ASSET_EXPIRY_INTERVAL_S", "300") or 300
)
_FAILED_ASSET_EXPIRY_BATCH = int(
    os.environ.get("PAPRIKA_FAILED_ASSET_EXPIRY_BATCH", "500") or 500
)
# 流入は実測 183k failed/日 = 7.6k/時。interval 300s なら 1 tick 635 件で足りる
# ので、既定 20000 は「詰まったあとの取り戻し」用の余裕。実測 108 job/s
# (concurrency 32) なので 20000 件でも約 185s、ロック TTL 270s に収まる。
_FAILED_ASSET_EXPIRY_MAX_PER_TICK = int(
    os.environ.get("PAPRIKA_FAILED_ASSET_EXPIRY_MAX_PER_TICK", "20000") or 20000
)
# delete_prefix は 4 層 (.48 / .8 / .47 / .17) を毎回 LIST するので直列だと
# 1 件 1.5 秒かかる (実測)。並列度がそのままスループット。S3 IO プールは
# 40 スレッド (PAPRIKA_S3_IO_THREADS) なので、常駐アップロードの取り分を
# 残して 32 まで。
_FAILED_ASSET_EXPIRY_CONCURRENCY = max(
    1, int(os.environ.get("PAPRIKA_FAILED_ASSET_EXPIRY_CONCURRENCY", "32") or 32)
)
_FAILED_ASSET_EXPIRY_MINIO_TIMEOUT_S = float(
    os.environ.get("PAPRIKA_FAILED_ASSET_EXPIRY_MINIO_TIMEOUT_S", "30") or 30
)
# watermark が無い (初回 / redis flush 後) ときにどこまで遡るか。
# 短くしてある理由: 実測で [now-24h, now-2h) には failed が 176,573 件あり、
# その大半は既に prefix が消えている (image-pull のスイープ / 一般 retention /
# one-shot の後)。空の prefix でも delete_prefix は 4 層を LIST するので、
# 24h ぶん遡ると初回有効化だけで 2 時間以上 MinIO を叩き続けることになる。
# 既存バックログは一般 retention の担当と割り切り、ここは「hub や redis が
# 短時間落ちた分を取り戻す」ぶんだけ見る。取りこぼしても job_retention_days
# で必ず消えるので、悪化はしない。
_FAILED_ASSET_EXPIRY_COLD_START_H = float(
    os.environ.get("PAPRIKA_FAILED_ASSET_EXPIRY_COLD_START_H", "6") or 6
)
_FAILED_ASSET_EXPIRY_RETRY_PER_TICK = int(
    os.environ.get("PAPRIKA_FAILED_ASSET_EXPIRY_RETRY_PER_TICK", "2000") or 2000
)
# Hard cap on the retry set. If a tier is down every purge fails and the set
# would otherwise grow by MAX_PER_TICK every tick (~5.7M/day) -- unbounded
# redis memory for prefixes a later orphan sweep can find anyway.
_FAILED_ASSET_EXPIRY_RETRY_MAX = int(
    os.environ.get("PAPRIKA_FAILED_ASSET_EXPIRY_RETRY_MAX", "200000") or 200000
)
_FAILED_ASSET_EXPIRY_LOCK_KEY = "paprika:failed_asset_expiry:lock"
_FAILED_ASSET_EXPIRY_WM_KEY = "paprika:failed_asset_expiry:watermark"
_FAILED_ASSET_EXPIRY_RETRY_KEY = "paprika:failed_asset_expiry:retry"
_WM_FMT = "%Y-%m-%d %H:%M:%S.%f"

_last_failed_asset_expiry_run_at: datetime | None = None
_last_failed_asset_expiry_purged = 0


async def _expire_job_assets(jid: str, sem, minio_timeout: float) -> bool:
    """Delete ONE failed job's objects across every tier. Leaves the DB row.

    Returns True only when every tier was purged cleanly.
    ``objstore.delete_prefix`` never raises on a tier error -- it reports
    ``deleted=False`` / ``failed_tiers>0`` instead -- so a caller that ignores
    the return value (as ``_purge_job`` still does) cannot tell a real purge
    from a partial one. That is exactly how orphaned prefixes get minted, so
    the False is propagated here and the caller re-queues."""
    from server.hub import objstore

    async with sem:
        try:
            r = await asyncio.wait_for(
                objstore.delete_prefix(jid), timeout=minio_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Includes TimeoutError: one hung tier must not stall the pass.
            log.debug("failed-asset-expiry: purge %s raised", jid, exc_info=True)
            return False
        return bool(r.get("deleted"))


async def _failed_asset_expiry_loop() -> None:
    """Background failed-asset expiry. Idempotent; safe on every hub (a redis
    lock elects one per tick). Inert unless failed_asset_expiry_enabled."""
    if os.environ.get("PAPRIKA_FAILED_ASSET_EXPIRY_DISABLE") == "1":
        return
    first = True
    while True:
        try:
            await asyncio.sleep(300 if first else _FAILED_ASSET_EXPIRY_INTERVAL_S)
        except asyncio.CancelledError:
            return
        first = False
        try:
            await _run_failed_asset_expiry_once()
        except Exception:
            # WARNING not debug -- a silent pass failure is what kept the
            # general retention loop's stall invisible for hours (2026-07-11).
            log.warning("failed-asset-expiry pass failed", exc_info=True)


async def _run_failed_asset_expiry_once() -> dict:
    """One failed-asset expiry pass. Returns a small stats dict."""
    global _last_failed_asset_expiry_run_at, _last_failed_asset_expiry_purged
    from datetime import timedelta as _td

    out = {
        "enabled": False, "dry_run": True, "hours": 0,
        "candidates": 0, "purged": 0, "requeued": 0, "retried": 0,
    }
    store = state.store
    if store is None or not hasattr(store, "list_jobs_by_status_between"):
        return out
    if state.settings is None or not bool(
        state.settings.get("failed_asset_expiry_enabled", False)
    ):
        return out
    out["enabled"] = True
    # NB: no ``or 2`` -- an explicit 0 must mean "paused", not "fall back to
    # the default and start deleting 2h-old assets" (same trap as
    # review_retention_days).
    try:
        hours = int(state.settings.get("failed_asset_expiry_hours", 2))
    except (TypeError, ValueError):
        hours = 2
    if hours <= 0:
        return out
    out["hours"] = hours
    dry = bool(state.settings.get("failed_asset_expiry_dry_run", True))
    out["dry_run"] = dry

    r = getattr(store, "_r", None)
    # Elect ONE hub per tick. Best-effort: purge is idempotent, so a missing
    # lock only costs duplicate LISTs, never correctness.
    if r is not None:
        try:
            got = await r.set(
                _FAILED_ASSET_EXPIRY_LOCK_KEY, config.hub_id or "1",
                nx=True, ex=max(60, int(_FAILED_ASSET_EXPIRY_INTERVAL_S * 0.9)),
            )
            if not got:
                return out
        except Exception:
            pass

    now = datetime.utcnow()
    hi = now - _td(hours=hours)
    lo = now - _td(hours=_FAILED_ASSET_EXPIRY_COLD_START_H)
    if r is not None:
        try:
            raw = await r.get(_FAILED_ASSET_EXPIRY_WM_KEY)
            if raw:
                lo = max(lo, datetime.strptime(raw, _WM_FMT))
        except Exception:
            log.debug("failed-asset-expiry: watermark read failed", exc_info=True)
    if lo >= hi:
        return out

    if dry:
        try:
            rows = await store.list_jobs_by_status_between(
                "failed", lo, hi, _FAILED_ASSET_EXPIRY_MAX_PER_TICK
            )
        except Exception:
            log.warning(
                "failed-asset-expiry: dry-run candidate query failed", exc_info=True
            )
            return out
        out["candidates"] = len(rows)
        _last_failed_asset_expiry_run_at = now
        _last_failed_asset_expiry_purged = 0
        log.info(
            "failed-asset-expiry[dry-run]: %s%d failed job(s) in [%s, %s) would "
            "have their MinIO assets purged (DB rows kept). Set "
            "failed_asset_expiry_dry_run=false to enact.",
            (">=" if len(rows) >= _FAILED_ASSET_EXPIRY_MAX_PER_TICK else ""),
            len(rows), lo, hi,
        )
        return out

    t0 = datetime.utcnow()
    sem = asyncio.Semaphore(_FAILED_ASSET_EXPIRY_CONCURRENCY)
    purged = failures = retried = 0
    watermark = lo

    async def _requeue(ids: list[str]) -> None:
        """Park prefixes we could not purge so a later tick revisits them.
        Without this the watermark walks past them and the objects become
        orphans nobody lists again."""
        if not ids or r is None:
            return
        try:
            if await r.scard(_FAILED_ASSET_EXPIRY_RETRY_KEY) >= (
                _FAILED_ASSET_EXPIRY_RETRY_MAX
            ):
                # Backlog this deep means a tier is down, not that individual
                # prefixes are cursed. Stop growing the set (WARNING, not
                # debug -- this is the one state an operator must see) and let
                # the orphan sweep mop up once the tier is back.
                log.warning(
                    "failed-asset-expiry: retry set at cap (%d); dropping %d "
                    "prefix(es) -- object tier likely down",
                    _FAILED_ASSET_EXPIRY_RETRY_MAX, len(ids),
                )
                return
            await r.sadd(_FAILED_ASSET_EXPIRY_RETRY_KEY, *ids)
        except Exception:
            log.debug("failed-asset-expiry: requeue failed", exc_info=True)

    # 1) Drain the retry set first -- those are already past their expiry.
    if r is not None:
        try:
            stale = await r.spop(
                _FAILED_ASSET_EXPIRY_RETRY_KEY, _FAILED_ASSET_EXPIRY_RETRY_PER_TICK
            )
        except Exception:
            stale = None
        stale = list(stale or [])
        if stale:
            res = await asyncio.gather(
                *(
                    _expire_job_assets(j, sem, _FAILED_ASSET_EXPIRY_MINIO_TIMEOUT_S)
                    for j in stale
                )
            )
            retried = sum(1 for ok in res if ok)
            await _requeue([j for j, ok in zip(stale, res) if not ok])
            purged += retried
            failures += len(stale) - retried

    # 2) Walk the watermark forward through newly-expired failed jobs.
    while purged < _FAILED_ASSET_EXPIRY_MAX_PER_TICK:
        try:
            rows = await store.list_jobs_by_status_between(
                "failed", watermark, hi, _FAILED_ASSET_EXPIRY_BATCH
            )
        except Exception:
            log.warning("failed-asset-expiry: candidate query failed", exc_info=True)
            break
        if not rows:
            watermark = hi  # window fully drained
            break
        ids = [j for j, _ in rows]
        res = await asyncio.gather(
            *(
                _expire_job_assets(j, sem, _FAILED_ASSET_EXPIRY_MINIO_TIMEOUT_S)
                for j in ids
            )
        )
        ok_n = sum(1 for ok in res if ok)
        purged += ok_n
        failures += len(ids) - ok_n
        await _requeue([j for j, ok in zip(ids, res) if not ok])
        # created_at is the cursor. The window is half-open on the low side, so
        # stepping to the LAST row's created_at would re-read that row forever;
        # nudge past it by 1ms (the column is DATETIME(3), so 1ms is exactly
        # one representable step).
        watermark = rows[-1][1] + _td(milliseconds=1)
        if r is not None:
            try:
                await r.set(_FAILED_ASSET_EXPIRY_WM_KEY, watermark.strftime(_WM_FMT))
            except Exception:
                log.debug("failed-asset-expiry: watermark write failed", exc_info=True)
        try:
            await asyncio.sleep(0.2)  # never monopolise the DB pool / loop
        except asyncio.CancelledError:
            raise
        if len(rows) < _FAILED_ASSET_EXPIRY_BATCH:
            break

    out["purged"] = purged
    out["retried"] = retried
    out["candidates"] = purged + failures
    out["requeued"] = failures
    _last_failed_asset_expiry_run_at = datetime.utcnow()
    _last_failed_asset_expiry_purged = purged
    if purged or failures:
        # INFO with elapsed time + failure count on purpose: "deleted N" alone
        # cannot distinguish "caught up" from "wedged", which is what made the
        # general retention loop's shortfall invisible for so long.
        log.info(
            "failed-asset-expiry: purged %d failed-job asset prefix(es) older "
            "than %dh (%d requeued, %d from retry set) in %.1fs; watermark=%s",
            purged, hours, failures, retried,
            (datetime.utcnow() - t0).total_seconds(), watermark,
        )
    return out
