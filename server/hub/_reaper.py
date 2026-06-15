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

close_session is imported via app.py's re-export chain (originally
from routes/sessions.py) to dodge the routes <-> hub circular at
boot time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
    # Multi-hub safety (clone-safe): if ANY other hub is alive in the shared
    # presence registry, this process is joining a LIVE cluster (e.g. a freshly
    # cloned VM, or a peer hub starting up). The in-store "running" jobs then
    # belong to live peers, NOT to a dead local orchestrator -- blanket-failing
    # them would nuke the whole fleet's work. Skip; genuine dead-hub orphans are
    # handled by the lease reaper (when leasing is on).
    try:
        if state.hubs is not None:
            peers = [
                h for h in await state.hubs.list_all()
                if h.get("alive")
                and not h.get("local")
                and str(h.get("hub_id")) != config.hub_id
            ]
            if peers:
                log.info(
                    "recovery: %d live peer hub(s) present (%s) -> skipping "
                    "blanket orphan recovery (multi-hub safety)",
                    len(peers),
                    ", ".join(str(h.get("hub_id")) for h in peers),
                )
                return 0
    except Exception:
        log.debug("recovery: peer-presence check failed", exc_info=True)
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
_RETIRE_MIN_USE = 5
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
