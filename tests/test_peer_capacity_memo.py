"""Regression: the cross-hub peer-capacity lookup must never block a submit.

2026-08-14 throughput investigation, stage 4. After the peer-forward fix, the
two top stages of POST /jobs were both the SAME call:

    stage       count    p50       share
    pick          886   1874ms     47.5%     -> _forward_to_peer_hub
    capacity      820   1504ms     31.5%     -> _fleet_has_spare_capacity

A locally-full hub runs ``_peer_hub_with_spare_capacity()`` twice per submit,
once in the admission gate and once at dispatch, and each call does a
cross-hub Redis aggregation (``stats_async()``) costing ~1.5s. That is ~3s of
a 3.58s p50 -- 79% of measured time -- computing the same answer twice.

The first fix was a 1s TTL + single-flight lock. It only moved ``capacity``
1504ms -> 1309ms, because **a TTL shorter than the computation leaves no
window where the entry is fresh**: every caller either ran the aggregation or
blocked on the lock waiting for someone else's, and turning "N parallel 1.5s
calls" into "N callers waiting 1.5s" shortens no single request. The giveaway
was that the *second* call in the same request did halve (1874 -> 900ms) --
it was the only one finding a briefly-fresh entry.

Hence stale-while-revalidate. This file pins the properties that matter:

  1. a warm cache NEVER awaits the aggregation, even when the entry is stale
     -- that is the whole point, and a plain TTL cannot promise it;
  2. a stale read triggers exactly one background refresh, not one per call;
  3. the refreshed value lands in the cache;
  4. only a cold cache blocks, and concurrent cold callers collapse to one run;
  5. a failing aggregation keeps serving the last good answer instead of
     stampeding;
  6. negative results are cached too (a full fleet must not re-aggregate per
     submit);
  7. the underlying computation still picks the right peer.
"""

import asyncio

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub.routes.jobs import _base


@pytest.fixture(autouse=True)
def _reset():
    _base._peer_cap_cache = None
    _base._peer_cap_refresh = None
    yield
    _base._peer_cap_cache = None
    _base._peer_cap_refresh = None


@pytest.fixture
def compute(monkeypatch):
    """Replace the uncached aggregation with a slow counting stub."""
    calls = {"n": 0}

    def _install(result="hub-37", delay=0.0, boom=False):
        async def _fake():
            calls["n"] += 1
            if delay:
                await asyncio.sleep(delay)
            if boom:
                raise RuntimeError("redis timeout")
            return result
        monkeypatch.setattr(
            _base, "_compute_peer_hub_with_spare_capacity", _fake
        )
        return calls
    return _install


def _make_stale():
    """Age the cached entry past its TTL without sleeping."""
    expires, value = _base._peer_cap_cache
    _base._peer_cap_cache = (expires - _base._PEER_CAP_TTL_S - 1.0, value)


@pytest.mark.asyncio
async def test_stale_read_returns_immediately_without_awaiting(compute):
    """THE regression. The aggregation takes ~1.5s; a stale read must not
    wait for it -- that was the bug the 1s TTL still had."""
    compute("hub-37", delay=1.5)
    _base._peer_cap_cache = (0.0, "hub-37")   # present but stale
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    got = await _base._peer_hub_with_spare_capacity()
    elapsed = loop.time() - t0
    assert got == "hub-37"
    assert elapsed < 0.05, f"stale read blocked for {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_stale_reads_spawn_only_one_refresh(compute):
    calls = compute("hub-37", delay=0.2)
    _base._peer_cap_cache = (0.0, "hub-37")
    for _ in range(25):
        assert await _base._peer_hub_with_spare_capacity() == "hub-37"
    # The warm path never yields (that is the point), so the spawned refresh
    # has not even started running yet -- let the loop schedule it.
    await asyncio.sleep(0.3)
    assert calls["n"] == 1, f"{calls['n']} refreshes for 25 stale reads"


@pytest.mark.asyncio
async def test_refresh_updates_the_cache(compute):
    compute("hub-99")
    _base._peer_cap_cache = (0.0, "hub-old")
    assert await _base._peer_hub_with_spare_capacity() == "hub-old"  # stale served
    await asyncio.sleep(0.05)                                        # refresh lands
    assert await _base._peer_hub_with_spare_capacity() == "hub-99"


@pytest.mark.asyncio
async def test_fresh_read_does_not_refresh(compute):
    calls = compute("hub-37")
    assert await _base._peer_hub_with_spare_capacity() == "hub-37"   # cold -> computes
    assert calls["n"] == 1
    for _ in range(10):
        await _base._peer_hub_with_spare_capacity()
    assert calls["n"] == 1, "a fresh entry must not trigger refreshes"


@pytest.mark.asyncio
async def test_cold_start_blocks_once_and_collapses_concurrent_callers(compute):
    calls = compute("hub-38", delay=0.15)
    got = await asyncio.gather(
        *[_base._peer_hub_with_spare_capacity() for _ in range(12)]
    )
    assert got == ["hub-38"] * 12
    assert calls["n"] == 1, f"cold-start thundering herd: {calls['n']} runs"


@pytest.mark.asyncio
async def test_failing_refresh_keeps_serving_the_last_good_answer(compute):
    calls = compute(boom=True)
    _base._peer_cap_cache = (0.0, "hub-last-good")
    assert await _base._peer_hub_with_spare_capacity() == "hub-last-good"
    await asyncio.sleep(0.05)
    # Still the old answer, and the expiry was pushed out so the next submit
    # does not immediately respawn another doomed refresh.
    assert _base._peer_cap_cache[1] == "hub-last-good"
    assert await _base._peer_hub_with_spare_capacity() == "hub-last-good"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_negative_result_is_cached_too(compute):
    calls = compute(None)
    assert await _base._peer_hub_with_spare_capacity() is None
    assert await _base._peer_hub_with_spare_capacity() is None
    assert calls["n"] == 1, "a full fleet must not re-aggregate per submit"


@pytest.mark.asyncio
async def test_uncached_form_picks_the_peer_with_most_spare(monkeypatch):
    """The caching must not have changed what gets computed: peers only
    (never us), alive+active workers only, most spare lanes wins."""
    class _Reg:
        async def stats_async(self):
            return {"workers": [
                {"hub_id": "hub-me", "alive": True, "status": "active",
                 "capacity": 10, "in_flight": 0},          # us -> ignored
                {"hub_id": "hub-a", "alive": True, "status": "active",
                 "capacity": 4, "in_flight": 2},           # spare 2
                {"hub_id": "hub-b", "alive": True, "status": "active",
                 "capacity": 8, "in_flight": 1},           # spare 7  <- winner
                {"hub_id": "hub-c", "alive": False, "status": "active",
                 "capacity": 8, "in_flight": 0},           # dead -> ignored
                {"hub_id": "hub-d", "alive": True, "status": "draining",
                 "capacity": 8, "in_flight": 0},           # not active -> ignored
            ]}

    class _Hubs:
        hub_id = "hub-me"

    monkeypatch.setattr(_base.state, "registry", _Reg(), raising=False)
    monkeypatch.setattr(_base.state, "hubs", _Hubs(), raising=False)
    assert await _base._compute_peer_hub_with_spare_capacity() == "hub-b"


@pytest.mark.asyncio
async def test_uncached_form_returns_none_when_no_peer_has_room(monkeypatch):
    class _Reg:
        async def stats_async(self):
            return {"workers": [
                {"hub_id": "hub-a", "alive": True, "status": "active",
                 "capacity": 4, "in_flight": 4},
            ]}

    class _Hubs:
        hub_id = "hub-me"

    monkeypatch.setattr(_base.state, "registry", _Reg(), raising=False)
    monkeypatch.setattr(_base.state, "hubs", _Hubs(), raising=False)
    assert await _base._compute_peer_hub_with_spare_capacity() is None


@pytest.mark.asyncio
async def test_aggregation_failure_is_not_fatal(monkeypatch):
    class _Reg:
        async def stats_async(self):
            raise RuntimeError("redis timeout")

    class _Hubs:
        hub_id = "hub-me"

    monkeypatch.setattr(_base.state, "registry", _Reg(), raising=False)
    monkeypatch.setattr(_base.state, "hubs", _Hubs(), raising=False)
    assert await _base._compute_peer_hub_with_spare_capacity() is None
