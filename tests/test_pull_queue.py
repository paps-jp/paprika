"""Pull-dispatch work list — phase 1: built, not enabled.

The list is what lets submit stop choosing a worker. Today a hub checks
fleet-wide capacity, picks from the workers whose WebSocket it holds, forwards
to a peer when it has none free, and waits out a grace window when the peer is
full too — four mechanisms bridging one mismatch (nginx spreads submissions
evenly; workers pin to a hub by consistent hash, giving a 10-to-32 split). On
2026-08-14 the peer forward collided with the placeholder row its own hub had
just written and 246,166 URLs were marked crawled without being fetched.

Phase 1 ships the list and the submit-side branch with the flag off, so this
file pins the properties that make enabling it safe later:

  1. off by default — the flag must be opt-in;
  2. a Redis failure degrades to the existing dispatch, never strands a job;
  3. "unknown depth" is distinguishable from "empty" so backpressure never
     rejects on a Redis blip;
  4. ids can be removed, so a cancelled job doesn't make a worker pop a dead
     id and bounce its lane.
"""

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub import _pull_queue


class _Redis:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.items: list[str] = []

    async def rpush(self, key, val):
        if self.fail:
            raise RuntimeError("redis down")
        self.items.append(val)
        return len(self.items)

    async def llen(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        return len(self.items)

    async def lrem(self, key, count, val):
        if self.fail:
            raise RuntimeError("redis down")
        n = self.items.count(val)
        self.items = [i for i in self.items if i != val]
        return n


class _Store:
    """Stands in for the job store, which is where the client actually lives."""

    def __init__(self, r):
        self._r = r


@pytest.fixture
def redis(monkeypatch):
    def _install(r):
        # Via the STORE, not state.redis -- see _redis()'s docstring. Reaching
        # for state.redis silently yielded None and the feature never engaged.
        monkeypatch.setattr(_pull_queue.state, "store", _Store(r), raising=False)
        monkeypatch.setattr(_pull_queue.state, "hubs", None, raising=False)
        return r
    return _install


def test_client_comes_from_the_store():
    """THE bug that made the first enablement a no-op. There is no
    state.redis; the client is on the store. Pinning the lookup because the
    failure is invisible: push returns False, the caller dispatches inline,
    every job still runs, and nothing looks wrong."""
    import inspect
    src = inspect.getsource(_pull_queue._redis)
    assert '"store"' in src and '"_r"' in src
    assert src.index('"store"') < src.index('"redis"')


def test_disabled_by_default():
    """Phase 1 builds both paths and enables neither."""
    assert _pull_queue.ENABLED is False


@pytest.mark.asyncio
async def test_push_appends_for_fifo(redis):
    r = redis(_Redis())
    assert await _pull_queue.push("job-1") is True
    assert await _pull_queue.push("job-2") is True
    # RPUSH + BLPOP is FIFO; LPUSH here would silently make it LIFO and
    # starve the oldest submissions under load.
    assert r.items == ["job-1", "job-2"]


@pytest.mark.asyncio
async def test_push_failure_reports_false_so_the_caller_can_fall_back(redis):
    """Redis must not be a single point of failure for dispatch during
    migration — a false return sends the caller back to inline dispatch."""
    redis(_Redis(fail=True))
    assert await _pull_queue.push("job-3") is False


@pytest.mark.asyncio
async def test_push_without_redis_is_false_not_an_exception(monkeypatch):
    monkeypatch.setattr(_pull_queue.state, "store", None, raising=False)
    monkeypatch.setattr(_pull_queue.state, "hubs", None, raising=False)
    monkeypatch.setattr(_pull_queue.state, "redis", None, raising=False)
    assert await _pull_queue.push("job-4") is False


@pytest.mark.asyncio
async def test_empty_and_unknown_depth_are_distinguishable(redis, monkeypatch):
    r = redis(_Redis())
    assert await _pull_queue.depth() == 0        # genuinely empty
    r.fail = True
    assert await _pull_queue.depth() == -1       # unknown
    monkeypatch.setattr(_pull_queue.state, "store", None, raising=False)
    monkeypatch.setattr(_pull_queue.state, "hubs", None, raising=False)
    monkeypatch.setattr(_pull_queue.state, "redis", None, raising=False)
    assert await _pull_queue.depth() == -1


@pytest.mark.asyncio
async def test_backpressure_trips_at_the_cap(redis, monkeypatch):
    r = redis(_Redis())
    monkeypatch.setattr(_pull_queue, "MAX_DEPTH", 3)
    for i in range(2):
        await _pull_queue.push(f"j{i}")
    assert await _pull_queue.is_full() is False
    await _pull_queue.push("j2")
    assert await _pull_queue.is_full() is True


@pytest.mark.asyncio
async def test_unknown_depth_never_rejects_a_submission(redis):
    """Rejecting on a Redis blip would turn a degraded dependency into a
    fleet-wide intake outage."""
    redis(_Redis(fail=True))
    assert await _pull_queue.is_full() is False


@pytest.mark.asyncio
async def test_remove_drops_every_copy(redis):
    """A cancelled or deleted job must not sit in the list: a worker would
    pop a dead id, fail the claim and bounce its lane. Removing all copies
    also keeps a redrive re-push idempotent."""
    r = redis(_Redis())
    for j in ("a", "b", "a", "c", "a"):
        await _pull_queue.push(j)
    assert await _pull_queue.remove("a") == 3
    assert r.items == ["b", "c"]


@pytest.mark.asyncio
async def test_remove_survives_redis_failure(redis):
    redis(_Redis(fail=True))
    assert await _pull_queue.remove("a") == 0
