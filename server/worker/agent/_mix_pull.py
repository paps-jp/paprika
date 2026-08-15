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
        return self._pull_free_lanes() > 0

    async def _pull_claim(self, job_id: str) -> bool:
        """Claim a popped job on our own hub. False means someone else got it.

        A 409 is ordinary: a redrive or another worker won the CAS, or the job
        left ``queued`` while the id sat in the list. A 404 means the job was
        cancelled or deleted between submit and pop. Neither is an error worth
        retrying -- drop it and pop the next one.

        The path carries OUR id, not the job's, because ``hub_http_url`` is the
        nginx front and nginx routes on the path alone. Addressed as
        ``/jobs/{id}/claim`` the claim round-robined across all seven hubs and
        only the one holding our WebSocket could answer it: 6 in 7 came back
        409 and pull dispatch delivered zero jobs while looking healthy, since
        the redrive re-dispatched every one of them the old way.
        """
        import httpx
        url = f"{self.hub_http_url.rstrip('/')}/workers/{self.worker_id}/claim"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json={"job_id": job_id})
        except Exception as e:
            log.info("[pull] claim %s failed to send: %s", job_id, e)
            return False
        if resp.status_code == 200:
            log.info("[pull] claim %s -> ok", job_id)
            return True
        if resp.status_code in (404, 409):
            # The reason is the diagnostic that matters: "not connected to
            # this hub" means routing is broken, "already running" means we
            # merely lost a race. They demand opposite responses.
            try:
                _why = resp.text[:120]
            except Exception:
                _why = ""
            log.info("[pull] claim %s -> %s %s", job_id, resp.status_code, _why)
            return False
        log.warning("[pull] claim %s -> HTTP %s", job_id, resp.status_code)
        return False

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
        client = aioredis.from_url(redis_url, decode_responses=True)
        try:
            while True:
                try:
                    if not self._pull_should_ask():
                        # No lane, draining, or updating: idle without holding
                        # a blocked connection open against the queue.
                        await asyncio.sleep(1.0)
                        continue
                    got = await client.blpop(QUEUE_KEY, timeout=int(_POP_TIMEOUT_S))
                    if not got:
                        continue          # timeout -- re-check our lane state
                    job_id = got[1]
                    if not job_id:
                        continue
                    await self._pull_claim(job_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("[pull] loop error: %s: %s", type(e).__name__, e)
                    await asyncio.sleep(_ERROR_BACKOFF_S)
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
