"""Job leases — multi-hub control-plane *phase 4* (dead-hub recovery).

A *lease* is a short-lived Redis key that says "hub X is actively running
job Y right now". The owning hub re-writes (refreshes) it on a timer while
the job's in-process orchestrator task is alive. If that hub dies, nothing
refreshes the key, it expires, and a surviving hub can re-claim the job and
re-dispatch it — so a crashed hub's in-flight jobs don't vanish.

    key   = paprika:job:{job_id}:lease
    value = {"hub": <hub_id>, "requeues": <int>, "ts": <unix_seconds>}
    TTL   = PAPRIKA_JOB_LEASE_TTL_S (default 90s)

Scope: this covers **hub-orchestrated** jobs (codegen-loop + rerun), which
run as an ``asyncio.Task`` inside the hub process and therefore die with it.
Worker-dispatched fetch jobs live on a worker that re-homes to another hub
on its own when a hub drops, so they are out of scope here.

DORMANT BY DEFAULT. Everything is gated behind ``PAPRIKA_JOB_LEASE_ENABLED``
(default OFF). When off, ``enabled()`` is False and the lease loop never
runs, never writes a key, and never reaps — single-hub behaviour is byte-for-
byte unchanged. Turn it on only in the multi-hub scale-out (where every hub
shares one Redis / Sentinel) by setting ``PAPRIKA_JOB_LEASE_ENABLED=1`` on
every replica. See deploy/scale/README.md.
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger(__name__)


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """True iff job leasing is switched on (multi-hub). Default OFF."""
    return _env_flag("PAPRIKA_JOB_LEASE_ENABLED")


def lease_ttl_s() -> int:
    """How long a lease key lives without a refresh. A hub that goes silent
    is considered dead after this window. Default 90s."""
    try:
        return max(15, int(os.environ.get("PAPRIKA_JOB_LEASE_TTL_S", "90")))
    except (TypeError, ValueError):
        return 90


def refresh_interval_s() -> float:
    """How often the owning hub re-writes its leases. Must be comfortably
    below lease_ttl_s so a brief GC pause / slow tick can't expire a healthy
    lease. Default 30s (= ttl/3)."""
    try:
        return max(5.0, float(os.environ.get("PAPRIKA_JOB_LEASE_REFRESH_S", "30")))
    except (TypeError, ValueError):
        return 30.0


def max_requeues() -> int:
    """How many times an orphaned job may be re-dispatched before we give up
    and fail it, so a job that reliably kills its hub (or that no hub can
    finish) can't bounce around the fleet forever. Default 1."""
    try:
        return max(0, int(os.environ.get("PAPRIKA_JOB_LEASE_MAX_REQUEUES", "1")))
    except (TypeError, ValueError):
        return 1


def _k_lease(job_id: str) -> str:
    return f"paprika:job:{job_id}:lease"


def _encode(hub_id: str, requeues: int) -> str:
    return json.dumps({"hub": hub_id or "", "requeues": int(requeues), "ts": time.time()})


async def acquire(redis, job_id: str, hub_id: str, *, requeues: int = 0) -> bool:
    """Atomically claim a lease iff no live one exists (``SET NX EX``).

    Returns True only for the hub that wins the claim. Used both for the
    first claim at dispatch and for re-claiming an *expired* (= orphaned)
    lease — the NX guarantees that, when several surviving hubs race to
    recover the same orphan, exactly one proceeds.
    """
    if redis is None:
        return False
    try:
        ok = await redis.set(
            _k_lease(job_id), _encode(hub_id, requeues), nx=True, ex=lease_ttl_s(),
        )
        return bool(ok)
    except Exception as e:
        log.debug("lease acquire(%s) failed: %s", job_id, e)
        return False


async def refresh(redis, job_id: str, hub_id: str, *, requeues: int = 0) -> None:
    """Re-write the lease (no NX) to push its TTL out. Called on a timer by
    the hub that owns the running job. Best-effort: a transient Redis blip
    just means the lease ages slightly toward expiry."""
    if redis is None:
        return
    try:
        await redis.set(_k_lease(job_id), _encode(hub_id, requeues), ex=lease_ttl_s())
    except Exception as e:
        log.debug("lease refresh(%s) failed: %s", job_id, e)


async def release(redis, job_id: str) -> None:
    """Delete the lease (job finished / no longer ours). Best-effort; a
    missed release self-heals because the key has a TTL anyway."""
    if redis is None:
        return
    try:
        await redis.delete(_k_lease(job_id))
    except Exception as e:
        log.debug("lease release(%s) failed: %s", job_id, e)


async def read(redis, job_id: str) -> dict | None:
    """Return the current lease value ``{hub, requeues, ts}`` or None when no
    live lease exists (key absent / expired = orphaned)."""
    if redis is None:
        return None
    try:
        raw = await redis.get(_k_lease(job_id))
        if not raw:
            return None
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except Exception as e:
        log.debug("lease read(%s) failed: %s", job_id, e)
        return None


# --- requeue counter -------------------------------------------------------
#
# The lease value carries a ``requeues`` field, but that value DIES with the
# lease when its TTL lapses -- which is exactly the moment we need to know how
# many times a job has already been re-dispatched. So the durable count lives
# in a separate key with a long TTL that outlives many lease cycles. It caps
# how often a poison job (one that reliably kills whatever hub runs it) may
# bounce around the fleet before we give up and fail it.

# How long the requeue counter survives. Long enough to span the whole life
# of a recovering job; short enough to self-clean abandoned ids. Default 1d.
def _requeue_counter_ttl_s() -> int:
    try:
        return max(3600, int(os.environ.get("PAPRIKA_JOB_LEASE_REQUEUE_TTL_S", "86400")))
    except (TypeError, ValueError):
        return 86400


def _k_requeues(job_id: str) -> str:
    return f"paprika:job:{job_id}:requeues"


async def get_requeues(redis, job_id: str) -> int:
    """How many times this job has already been re-dispatched (0 if never)."""
    if redis is None:
        return 0
    try:
        raw = await redis.get(_k_requeues(job_id))
        return int(raw) if raw else 0
    except Exception:
        return 0


async def bump_requeues(redis, job_id: str) -> int:
    """Atomically increment + (re)arm the TTL on the requeue counter, returning
    the new value. Called by the reaper after it wins the re-claim."""
    if redis is None:
        return 0
    try:
        n = await redis.incr(_k_requeues(job_id))
        try:
            await redis.expire(_k_requeues(job_id), _requeue_counter_ttl_s())
        except Exception:
            pass
        return int(n)
    except Exception as e:
        log.debug("lease bump_requeues(%s) failed: %s", job_id, e)
        return 0
