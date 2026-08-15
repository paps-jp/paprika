"""The pull loop under load, driven for real against fakes.

Every defect this file pins was invisible to source-level tests and to a queue
that happened to be empty. Pull dispatch ran in production for hours on one
hub, looked healthy, and delivered nothing; when it was enabled on all seven
hubs the queue stopped being empty and the fleet's success rate fell from 0.97
to 0.50 in under an hour. So these tests run ``_pull_loop`` itself with a fake
Redis and a fake hub, and assert on what it did.

The three failures, and the prior art each one has:

  * **over-claiming.** Between the claim's 200 and the WS assign that marks the
    lane busy, the lane still counts as free, so a worker popping a non-empty
    queue claims the same lane again and again -- in_flight reached 5 and 7 on
    two-lane workers. Temporal reserves a task slot BEFORE its poller asks for
    work; Celery ships the same thing as ``worker_prefetch_multiplier=1`` with
    ``task_acks_late``, and 5.6 added ``worker_disable_prefetch`` ("fetch a new
    task only when an execution slot is free").

  * **a tight loop.** A queue holding ids this worker cannot claim turns into
    pop -> claim -> pop at request rate. The extra Chromes plus that spin
    starved the event loop; the hub's keepalive ping went unanswered and
    websockets closed the connection with 1011, failing every job the worker
    was running as "disconnected before the job finished". Kafka hit the same
    coupling in KIP-62 and moved heartbeats off the processing thread.

  * **discarding stranded work.** ``BLPOP`` removes the id. "Already running"
    (someone owns it) and "not connected to this hub" (nobody does) are both
    409; treating them alike left rows queued until the redrive noticed.
"""

import asyncio

import pytest

from server.worker.agent import _mix_pull
from server.worker.agent._mix_pull import _PullMixin


class _Lane:
    def __init__(self, busy=False):
        self.busy = busy


class _Pool:
    def __init__(self, n=2):
        self.lanes = [_Lane() for _ in range(n)]


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeRedis:
    def __init__(self, items=()):
        self.items = list(items)
        self.pushed = []
        self.pops = 0

    async def blpop(self, key, timeout=0):
        # Always yield, even on a hit: a real client awaits the socket, and
        # that is the only reason a loop with no backoff stays *observable*
        # rather than wedging the process. Returning without awaiting made
        # the no-backoff run hang this test suite outright -- which is exactly
        # what it does to a worker's WebSocket keepalive in production.
        await asyncio.sleep(0)
        self.pops += 1
        if self.items:
            return (key, self.items.pop(0))
        await asyncio.sleep(0.01)
        return None

    async def rpush(self, key, val):
        self.pushed.append(val)
        self.items.append(val)

    async def aclose(self):
        pass


class _FakeHTTP:
    """One instance per loop -- the test asserts exactly one is ever built."""

    built = 0

    def __init__(self, *a, **kw):
        type(self).built += 1
        self.posts = []
        self.reply = _Resp(200)

    async def post(self, url, json=None):
        self.posts.append(json.get("job_id"))
        return self.reply(len(self.posts)) if callable(self.reply) else self.reply

    async def aclose(self):
        pass


class _Agent(_PullMixin):
    def __init__(self, lanes=2, ws=object()):
        self.lane_pool = _Pool(lanes)
        self._draining = False
        self._pending_update_to = None
        self._ws = ws
        self.worker_id = "w511"
        self.hub_http_url = "http://hub"


@pytest.fixture
def rig(monkeypatch):
    """Run the real loop against fakes."""
    _FakeHTTP.built = 0
    monkeypatch.setattr(_mix_pull, "PULL_ENABLED", True)
    monkeypatch.setenv("PAPRIKA_REDIS_URL", "redis://fake")
    monkeypatch.setattr(_mix_pull, "_POP_TIMEOUT_S", 1.0)
    monkeypatch.setattr(_mix_pull, "_CLAIM_BACKOFF_S", 0.05)

    def _run(redis, http=None, lanes=2, seconds=0.35):
        http = http or _FakeHTTP()
        import httpx
        import redis.asyncio as aioredis  # noqa: F811  (module, not our fake)
        monkeypatch.setattr(aioredis, "from_url", lambda *a, **kw: _redis_holder[0])
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: http)
        agent = _Agent(lanes=lanes)

        async def _go():
            task = asyncio.create_task(agent._pull_loop())
            await asyncio.sleep(seconds)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return agent, http

        return _go()

    _redis_holder = [None]

    def run(redis, **kw):
        _redis_holder[0] = redis
        return _run(redis, **kw)

    return run


@pytest.mark.asyncio
async def test_never_claims_more_than_it_has_lanes(rig):
    """THE regression. Five ids, two lanes, every claim accepted, and the
    assign never arrives (as when the hub is slow) -- the worker must stop at
    two. Before the reservation it took all five and ran them concurrently."""
    r = _FakeRedis([f"j{i}" for i in range(5)])
    agent, http = await rig(r, lanes=2)
    assert len(http.posts) == 2, f"claimed {len(http.posts)} jobs for 2 lanes"


@pytest.mark.asyncio
async def test_does_not_pop_at_all_without_a_free_lane(rig):
    """Reserve before polling (Temporal's ordering): a worker with no slot
    must not remove an id from the queue, because a popped id it cannot run is
    stranded until the redrive finds it."""
    r = _FakeRedis(["j0", "j1"])
    agent, http = await rig(r, lanes=0)
    assert r.pops == 0
    assert r.items == ["j0", "j1"]


@pytest.mark.asyncio
async def test_a_stranded_id_goes_back_on_the_queue(rig):
    """"not connected to this hub" means nobody owns the job -- put it back
    for a worker that can run it, don't wait out the redrive."""
    r = _FakeRedis(["j0"])
    http = _FakeHTTP()
    http.reply = _Resp(409, '{"detail":"worker \'w511\' is not connected to this hub"}')
    agent, _ = await rig(r, http=http, lanes=2)
    assert "j0" in r.pushed


@pytest.mark.asyncio
async def test_a_job_someone_else_owns_is_dropped(rig):
    """The opposite 409. Requeueing here would loop the id forever between the
    queue and workers that all correctly refuse it."""
    r = _FakeRedis(["j0"])
    http = _FakeHTTP()
    http.reply = _Resp(409, '{"detail":"job is already running"}')
    agent, _ = await rig(r, http=http, lanes=2)
    assert r.pushed == []


@pytest.mark.asyncio
async def test_a_cancelled_job_is_dropped(rig):
    r = _FakeRedis(["j0"])
    http = _FakeHTTP()
    http.reply = _Resp(404, '{"detail":"job no longer exists"}')
    agent, _ = await rig(r, http=http, lanes=2)
    assert r.pushed == []


@pytest.mark.asyncio
async def test_unclaimable_ids_do_not_become_a_tight_loop(rig):
    """The starvation case. One id this worker can never claim, put back each
    time: without the backoff the loop spins at request rate and the event
    loop stops answering the hub's keepalive ping."""
    r = _FakeRedis(["j0"])
    http = _FakeHTTP()
    http.reply = _Resp(409, '{"detail":"worker \'w511\' is not connected to this hub"}')
    agent, _ = await rig(r, http=http, lanes=2, seconds=0.35)
    # 0.35s at a 0.05s jittered backoff is a handful of attempts, not hundreds.
    assert len(http.posts) <= 12, f"{len(http.posts)} attempts -- backoff is not working"


@pytest.mark.asyncio
async def test_one_http_client_for_the_whole_loop(rig):
    """Building a client per claim defeats connection pooling; the HTTPX docs
    call out `async with` inside a hot loop by name."""
    r = _FakeRedis([f"j{i}" for i in range(5)])
    await rig(r, lanes=2)
    assert _FakeHTTP.built == 1


@pytest.mark.asyncio
async def test_a_reservation_is_released_when_the_claim_fails(rig):
    """Leak one per failed claim and the worker silently stops asking for
    work -- idle, alive, and invisible in every metric we collect."""
    r = _FakeRedis(["j0", "j1", "j2"])
    http = _FakeHTTP()
    http.reply = _Resp(409, '{"detail":"job is already running"}')
    agent, _ = await rig(r, http=http, lanes=2)
    assert getattr(agent, "_pull_pending", 0) == 0


@pytest.mark.asyncio
async def test_an_empty_queue_releases_the_reservation(rig):
    """A pop that times out must give the slot back, or a quiet minute would
    retire the worker's lanes one by one."""
    r = _FakeRedis([])
    agent, _ = await rig(r, lanes=2)
    assert getattr(agent, "_pull_pending", 0) == 0
