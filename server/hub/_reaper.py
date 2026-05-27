"""Session reaper + orphan-job recovery.

Two long-running loops the hub spawns at lifespan-start. Pulled out
of app.py so the module is just FastAPI() + lifespan + include_router
stanzas + _apply_route_tags() now.

* ``_session_reaper_loop``: forever-loop that closes sessions whose
  ``idle_ttl_s`` / ``absolute_ttl_s`` has elapsed, and bumps
  keep_session jobs back to "keepalive" phase when noVNC RFB
  activity goes quiet for ``_RUNNING_TO_KEEPALIVE_QUIET_S`` seconds.

* ``_recover_orphan_running_jobs``: one-shot run at hub startup.
  Anything persisted as ``status=running`` but missing from the
  ``state.local_tasks`` map is by definition an orchestrator killed
  by a hub restart -- mark them failed so the admin UI shows a
  terminal state instead of an eternal yellow "running" badge.

close_session is imported via app.py's re-export chain (originally
from routes/sessions.py) to dodge the routes <-> hub circular at
boot time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from server.hub._state import state
from server.protocol import JobStatus
from server.runner import DONE_SENTINEL

log = logging.getLogger(__name__)


async def close_session(session_id: str):
    """Lazy bridge to the route-layer close_session. The reaper imports
    this module at lifespan-start time when app.py is still loading,
    so an eager ``from server.hub.app import close_session`` would
    race the partial import. Wrap with a function-level lookup so the
    resolution happens at first call (= reaper tick), by which point
    app.py is fully loaded."""
    from server.hub.app import close_session as _impl

    return await _impl(session_id)


# ----------------------------------------------------------------------------
# Background tasks
# ----------------------------------------------------------------------------

# How often the reaper looks for expired sessions, seconds. Also drives
# the running->keepalive phase demotion below, so this cannot be slower
# than _RUNNING_TO_KEEPALIVE_QUIET_S without making the demote feel
# laggy.
_REAPER_INTERVAL_S = 5

# When a keep_session job's phase is bumped to "running" by RFB activity
# (mouse / keyboard / clipboard from the noVNC viewer), how long the
# session can sit with no fresh RFB activity before we demote it back
# to "keepalive". Must be > _ACTIVITY_THROTTLE_S (10 s, see the noVNC
# proxy) so a continuous mouse drag -- which produces one throttled
# touch every 10 s -- doesn't flicker running->keepalive->running
# between touches.
_RUNNING_TO_KEEPALIVE_QUIET_S = 15


async def _recover_orphan_running_jobs() -> int:
    """Scan the job store at hub startup and mark any job persisted as
    ``status=running`` but no longer driven by a local task as failed.

    Hub restart kills the orchestrator coroutine mid-flight; without
    this recovery, the killed jobs stay ``status=running`` in the
    admin UI forever (Recent Jobs row stuck at a yellow "running"
    badge, never resolves).

    Returns the count of jobs reclassified.
    """
    assert state.store is not None
    # state.local_tasks is empty at this point (fresh process), so any
    # in-store "running" job is by definition an orphan.
    try:
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
    return n


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
        now = datetime.utcnow()
        for s in list(state.sessions.all()):
            if s.state == "closing":
                continue
            # Skip sessions with an action currently in flight. The
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
            if s.state == "running" or s.state == "fetch_running":
                continue
            try:
                idle = (now - s.last_active_at).total_seconds()
                age = (now - s.created_at).total_seconds()
            except Exception:
                continue

            # ---- running -> keepalive demote ----------------------
            # Operator was interacting (phase==running) but the noVNC
            # proxy hasn't seen an RFB event for QUIET_S seconds. Flip
            # the parent job back to "keepalive" so the screenshot tile
            # changes color (orange) and the operator can tell the
            # session is now just warming up, not actively driven.
            # Skipped when no parent job (read-only inspection sessions
            # opened from /sessions don't have a job tag) and when the
            # session is already past its idle TTL (the close branch
            # below will reap it in this same tick).
            if (
                s.job_id
                and idle > _RUNNING_TO_KEEPALIVE_QUIET_S
                and not (s.idle_ttl_s and idle > s.idle_ttl_s)
            ):
                try:
                    jinfo = await state.store.get_job_info(s.job_id)
                except Exception:
                    jinfo = None
                if (
                    jinfo is not None
                    and jinfo.status == JobStatus.running
                    and jinfo.progress is not None
                    and jinfo.progress.phase == "running"
                ):
                    jinfo.progress.phase = "keepalive"
                    try:
                        await state.store.save_job_info(jinfo)
                    except Exception:
                        pass

            expired = (s.idle_ttl_s and idle > s.idle_ttl_s) or (
                s.absolute_ttl_s and age > s.absolute_ttl_s
            )
            if not expired:
                continue
            reason = "idle_ttl" if (s.idle_ttl_s and idle > s.idle_ttl_s) else "absolute_ttl"
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
