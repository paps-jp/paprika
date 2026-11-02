"""Redis work list for pull dispatch (phase 1 — built, not enabled).

Today a hub decides which worker runs a job: it checks fleet-wide capacity,
picks from the workers whose WebSocket it happens to hold, forwards to a peer
when it has none free, and waits out a grace window when the peer is full too.
Those four mechanisms exist to bridge one mismatch — nginx spreads submissions
evenly across seven hubs while workers pin to a hub by consistent hash, so the
per-hub lane split runs from 10 to 32 workers. On 2026-08-14 the peer forward
collided with the placeholder row its own hub had just written, 409'd every
forward, and 246,166 URLs were marked crawled without ever being fetched.

Under pull the hub stops choosing. Submit appends a job id here and returns;
a worker with a free lane pops one and claims it. "Which lanes are free" needs
no registry — it is exactly the set of workers blocked on ``BLPOP``.

Cost, against measured production load (152 workers, 304 lanes, 4.5 jobs/s):
one ``LPUSH`` plus one wake per job — about 9 ops/s on top of 1,684, or +0.5%.
Each lane pops once per ~67s, and blocks rather than polls in between.

Everything here is inert until ``PAPRIKA_PULL_DISPATCH`` is on.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

#: Phase 1 ships both paths and enables neither. Flip per hub to migrate.
ENABLED = (os.environ.get("PAPRIKA_PULL_DISPATCH", "0") or "0").strip().lower() in (
    "1", "true", "yes", "on",
)

#: One list for the whole fleet. Redis wakes exactly one blocked client per
#: push and wakes the longest-waiting one first, so a single key already gives
#: FIFO fairness across lanes; sharding it would only cost that.
QUEUE_KEY = os.environ.get("PAPRIKA_PULL_QUEUE_KEY") or "paprika:jobq"

#: Submissions are rejected with 503 past this depth. Replaces the fleet-wide
#: capacity aggregation, which cost 1.5s at 152 workers and grows with the
#: fleet; ``LLEN`` is O(1) at any size.
MAX_DEPTH = int(os.environ.get("PAPRIKA_PULL_QUEUE_MAX") or 5000)


#: Imported at module level so tests can patch ``_pull_queue.state.redis``
#: the same way the rest of the hub is patched.
from server.hub._state import state  # noqa: E402


def _redis():
    """The hub's async Redis client, or None.

    There is no ``state.redis``: the client is owned by whoever created it.
    The job store makes one for pub/sub (``state.store._r``, decode_responses
    on) and the hub registry makes its own. Reaching for ``state.redis`` --
    which several other modules also do -- silently yields None, and this
    module's contract turns that into "push failed, dispatch inline". So the
    first run of pull dispatch looked completely healthy: 180 submissions on
    hub-41 logged "queue push failed; dispatching inline instead" and every
    job still ran, over the WS, exactly as before. The fallback did its job;
    the feature simply never engaged.
    """
    st = getattr(state, "store", None)
    r = getattr(st, "_r", None) if st is not None else None
    if r is not None:
        return r
    hubs = getattr(state, "hubs", None)
    r = getattr(hubs, "_r", None) if hubs is not None else None
    if r is not None:
        return r
    return getattr(state, "redis", None)


async def push(job_id: str) -> bool:
    """Append a job id for whichever worker pops next. False if it didn't land.

    A false return must leave the caller free to dispatch the old way — during
    migration Redis is not yet allowed to be a single point of failure for
    dispatch.
    """
    if not job_id:
        return False
    r = _redis()
    if r is None:
        return False
    try:
        await r.rpush(QUEUE_KEY, job_id)
        return True
    except Exception:
        log.warning("[pull] push failed for %s", job_id, exc_info=True)
        return False


async def depth() -> int:
    """Current backlog. -1 when Redis can't answer, so callers can tell
    "empty" from "unknown" and not reject on an unknown."""
    r = _redis()
    if r is None:
        return -1
    try:
        return int(await r.llen(QUEUE_KEY))
    except Exception:
        return -1


async def is_full() -> bool:
    """Backpressure gate for submit. Unknown depth never rejects."""
    d = await depth()
    return d >= 0 and d >= MAX_DEPTH


async def remove_many(job_ids: list[str]) -> int:
    """Drop every copy of each id in ``job_ids`` from the list, in ONE read.

    ``LREM`` walks the whole list, so evicting N ids one by one is O(N * len).
    The retention pass learned this the hard way on 2026-11-02: with ~4,000
    ids queued, 20,000 per-job LREMs took 3.0 s EACH behind the fleet's other
    Redis traffic -- 96% of a 34-minute pass (phases: evict=60110 s vs
    db=1145 / minio=1405). Retired jobs are almost never on the list, so read it
    once (``LRANGE 0 -1``, a few thousand short strings), intersect, and issue
    LREM only for the handful that are actually there. Same guarantee as calling
    :func:`remove` per id; returns the number of entries removed."""
    ids = [j for j in job_ids if j]
    if not ids:
        return 0
    r = _redis()
    if r is None:
        return 0
    try:
        present = await r.lrange(QUEUE_KEY, 0, -1)
    except Exception:
        log.debug("[pull] remove_many: lrange failed", exc_info=True)
        return 0
    want = set(ids)
    hits = {
        (p.decode() if isinstance(p, (bytes, bytearray)) else p) for p in (present or [])
    } & want
    removed = 0
    for jid in hits:
        try:
            removed += int(await r.lrem(QUEUE_KEY, 0, jid))
        except Exception:
            log.debug("[pull] remove_many: lrem %s failed", jid, exc_info=True)
    return removed


async def remove(job_id: str) -> int:
    """Drop every copy of a job id from the list.

    Needed when a job is cancelled or deleted between submit and pop: without
    it a worker pops a dead id, the claim fails, and the lane bounces. Also
    keeps the redrive idempotent — it can re-push without stacking duplicates.
    """
    if not job_id:
        return 0
    r = _redis()
    if r is None:
        return 0
    try:
        return int(await r.lrem(QUEUE_KEY, 0, job_id))
    except Exception:
        return 0
