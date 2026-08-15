"""Pull dispatch, worker side (phase 1 — built, off by default).

Today a hub decides which worker runs a job, and it can only see the workers
whose WebSocket it happens to hold. nginx spreads submissions evenly over the
hubs while workers pin to one by consistent hash, so the per-hub lane split
runs 10-to-32 and a hub routinely has nothing free while the fleet is half
idle. Four mechanisms exist to bridge that — a fleet-wide capacity
aggregation, a peer forward, an 8s grace loop, and a second dedup check — and
on 2026-08-14 the forward collided with its own hub's placeholder row, 409'd
every forward, and 246,166 URLs were marked crawled without being fetched.

Under pull the worker asks. It only asks when it has a free lane, so "which
lanes are free" needs no registry at all: it is exactly the set of workers
blocked on ``BLPOP``.

The claim goes to THIS worker's own hub — the one holding its WebSocket —
because that hub already has us in its registry and can deliver the assignment
over the existing WS. So nothing downstream changes: the job arrives as an
ordinary ``HubAssignJob`` and ``_run_assigned_job`` handles it exactly as it
does a pushed one.

Cost per job: one blocking pop plus one HTTP call. Measured fleet load is
1,684 Redis ops/s; at 4.5 jobs/s this adds ~9 ops/s (+0.5%), and each lane
pops about once per 67 seconds.

Inert unless ``PAPRIKA_PULL_DISPATCH`` is set.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time

log = logging.getLogger(__name__)

PULL_ENABLED = (
    os.environ.get("PAPRIKA_PULL_DISPATCH", "0") or "0"
).strip().lower() in ("1", "true", "yes", "on")

QUEUE_KEY = os.environ.get("PAPRIKA_PULL_QUEUE_KEY") or "paprika:jobq"

#: Blocking pop timeout. Bounds how long a drain / shutdown waits for the loop
#: to notice, and how often we re-check our own lane state.
_POP_TIMEOUT_S = float(os.environ.get("PAPRIKA_PULL_POP_TIMEOUT_S") or 5.0)

#: Backoff after a Redis error, so a Redis outage doesn't become a hot loop.
_ERROR_BACKOFF_S = float(os.environ.get("PAPRIKA_PULL_ERROR_BACKOFF_S") or 5.0)

#: How long a won claim reserves its lane while waiting for the assign to
#: arrive over the WS. Long enough to cover a slow hub dispatch, short enough
#: that a lost assign doesn't idle the lane.
_ASSIGN_GRACE_S = float(os.environ.get("PAPRIKA_PULL_ASSIGN_GRACE_S") or 20.0)

#: Pause after an id we had to put back. The queue can hold ids this worker
#: cannot claim (a hub that does not hold our WS, a hub we cannot reach), and
#: without a pause the loop spins pop -> claim -> requeue at request rate.
#: Jittered so 300 lanes that all start failing do not resynchronise into a
#: single herd against the hubs.
_CLAIM_BACKOFF_S = float(os.environ.get("PAPRIKA_PULL_CLAIM_BACKOFF_S") or 0.5)


def _claim_backoff() -> float:
    return _CLAIM_BACKOFF_S * (0.5 + random.random())


class _PullMixin:
    """Adds the pull loop. Composed into WorkerAgent alongside the other
    mixins; does nothing at all while ``PULL_ENABLED`` is false."""

    def _pull_free_lanes(self) -> int:
        """Free lanes right now. Counted, never acquired.

        Acquiring here would double-book: the assignment arrives over the WS
        and ``_run_assigned_job`` acquires the lane itself. Counting keeps one
        owner for lane state while still stopping a worker with no usable lane
        from taking work it cannot run -- the case that made ``pick_worker``
        grow a ``lane_novnc_urls == []`` guard after a lane-less worker
        swallowed three codegen attempts.
        """
        pool = getattr(self, "lane_pool", None)
        if pool is None:
            return 0
        try:
            return sum(1 for lane in pool.lanes if not lane.busy)
        except Exception:
            return 0

    def _pull_should_ask(self) -> bool:
        """Whether to ask for work at all this round."""
        if getattr(self, "_draining", False):
            return False
        # A worker mid self-update is on its way out; taking a job now just
        # means abandoning it at exit.
        if getattr(self, "_pending_update_to", None):
            return False
        # No WebSocket, no claim: the assignment comes back over it, so a
        # disconnected worker can only pop an id and throw it away. Observed on
        # 2026-08-15 -- w5110 sat with alive=False and kept draining the queue,
        # every claim answered "not connected to this hub" while the popped
        # rows waited out the redrive.
        if getattr(self, "_ws", None) is None:
            return False
        # Subtract claims we have already won but whose assignment has not
        # landed yet. Between a 200 and the WS assign that marks the lane
        # busy, ``_pull_free_lanes`` still reports that lane free, so a worker
        # popping a non-empty queue claims the same lane over and over. Seen
        # 2026-08-15 once every hub started pushing: in_flight reached 5 on a
        # 2-lane worker, the extra Chromes starved the event loop, the hub's
        # keepalive ping went unanswered and the WS died with 1011 -- failing
        # every job that worker was running as "disconnected before the job
        # finished". Over-claiming is not just unfair scheduling; it is how a
        # worker kills itself.
        return self._pull_free_lanes() - getattr(self, "_pull_pending", 0) > 0

    async def _pull_settle(self) -> None:
        """Keep a won claim reserved until its lane actually goes busy.

        Released early the moment lane state catches up, and on a deadline
        regardless -- a claim whose assign never arrives (hub restart between
        the CAS and the send) must not reserve a lane forever.
        """
        before = self._pull_free_lanes()
        deadline = time.monotonic() + _ASSIGN_GRACE_S
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                if self._pull_free_lanes() < before:
                    return
        finally:
            self._pull_pending = max(0, getattr(self, "_pull_pending", 0) - 1)

    async def _pull_claim(self, http, job_id: str) -> str:
        """Claim a popped job. Returns what to do with the id afterwards.

        ``"ok"``       we run it; the reservation becomes a busy lane.
        ``"done"``     someone else owns it -- drop the id, it is handled.
        ``"requeue"``  WE could not take it but nobody else has it either;
                       put it back so another worker gets it now rather than
                       leaving it for the redrive minutes later.

        Telling those last two apart is the whole point. "job is already
        running" and "worker is not connected to this hub" are both 409, and
        they demand opposite responses: the first means the work is in hand,
        the second means the work is stranded. Treating them alike is how the
        first production run of pull dispatch discarded every id it popped.

        ``http`` is a long-lived client. Building one per claim -- which this
        did -- defeats connection pooling and is called out by name in the
        HTTPX docs ("don't instantiate a client inside a hot loop"); at fleet
        scale the churn was a large part of what starved the event loop until
        the hub's keepalive ping went unanswered and the WS died with 1011.

        The path carries OUR id, not the job's, because ``hub_http_url`` is the
        nginx front and nginx routes on the path alone.
        """
        url = f"{self.hub_http_url.rstrip('/')}/workers/{self.worker_id}/claim"
        try:
            resp = await http.post(url, json={"job_id": job_id})
        except Exception as e:
            # Never reached the hub: the job is still queued and unowned.
            log.info("[pull] claim %s failed to send: %s", job_id, e)
            return "requeue"
        if resp.status_code == 200:
            log.info("[pull] claim %s -> ok", job_id)
            return "ok"
        try:
            _why = resp.text[:160]
        except Exception:
            _why = ""
        if resp.status_code == 404:
            # Cancelled or deleted between submit and pop. Gone for good.
            log.info("[pull] claim %s -> 404 %s", job_id, _why)
            return "done"
        if resp.status_code == 409:
            log.info("[pull] claim %s -> 409 %s", job_id, _why)
            return "done" if "already" in _why else "requeue"
        log.warning("[pull] claim %s -> HTTP %s %s", job_id, resp.status_code, _why)
        return "requeue"

    def _pull_reserve(self) -> bool:
        """Take a slot BEFORE polling, or return False.

        Reserving first is the ordering Temporal's workers use -- a poller does
        not ask the task queue for work unless a slot is already held -- and it
        is stricter than reserving after the claim. ``BLPOP`` removes the id
        from the queue, so popping work we then cannot run strands it until the
        redrive notices; not popping it at all leaves it for a worker that can
        start immediately.
        """
        if not self._pull_should_ask():
            return False
        self._pull_pending = getattr(self, "_pull_pending", 0) + 1
        return True

    def _pull_release(self) -> None:
        self._pull_pending = max(0, getattr(self, "_pull_pending", 0) - 1)

    async def _pull_requeue(self, client, job_id: str) -> None:
        """Put back an id we popped but could not take.

        Appends, so it goes behind work that has been waiting -- a job we
        failed to claim must not head-of-line block the queue by bouncing
        between the front and one worker.
        """
        try:
            await client.rpush(QUEUE_KEY, job_id)
        except Exception:
            # The row is still `queued`; the redrive is the backstop. Losing
            # the id here costs latency, never the job.
            log.warning("[pull] requeue of %s failed", job_id, exc_info=True)

    async def _pull_loop(self) -> None:
        """Ask for work whenever we have a lane free."""
        if not PULL_ENABLED:
            return
        try:
            import redis.asyncio as aioredis
        except Exception:
            log.warning("[pull] redis client unavailable; pull loop not starting")
            return
        redis_url = os.environ.get("PAPRIKA_REDIS_URL") or ""
        if not redis_url:
            log.warning("[pull] PAPRIKA_REDIS_URL unset; pull loop not starting")
            return

        log.info("[pull] loop starting (queue=%s, %d lanes)",
                 QUEUE_KEY, len(getattr(getattr(self, "lane_pool", None), "lanes", [])))
        import httpx
        client = aioredis.from_url(redis_url, decode_responses=True)
        # One client for the life of the loop: see _pull_claim.
        http = httpx.AsyncClient(timeout=30.0)
        try:
            while True:
                try:
                    if not self._pull_reserve():
                        # No free slot, draining, updating, or no WS: idle
                        # without holding a blocked connection on the queue.
                        await asyncio.sleep(1.0)
                        continue
                    handed_off = False
                    try:
                        got = await client.blpop(QUEUE_KEY, timeout=int(_POP_TIMEOUT_S))
                        if not got or not got[1]:
                            continue      # timeout -- re-check our lane state
                        job_id = got[1]
                        outcome = await self._pull_claim(http, job_id)
                        if outcome == "ok":
                            # The reservation lives on until the assign lands;
                            # _pull_settle owns releasing it from here.
                            asyncio.create_task(self._pull_settle())
                            handed_off = True
                        elif outcome == "requeue":
                            await self._pull_requeue(client, job_id)
                            # Yield before popping again. Without this a queue
                            # full of ids we cannot claim becomes a tight
                            # pop/claim loop that starves the event loop and
                            # kills the WS with a keepalive timeout.
                            await asyncio.sleep(_claim_backoff())
                    finally:
                        if not handed_off:
                            self._pull_release()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("[pull] loop error: %s: %s", type(e).__name__, e)
                    await asyncio.sleep(_ERROR_BACKOFF_S)
        finally:
            for _c in (client, http):
                try:
                    await _c.aclose()
                except Exception:
                    pass
