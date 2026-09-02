"""Regression: a hub must never answer /workers with its LOCAL workers only.

Symptom the operator reported (2026-08-30, "worker の数が不安定"): the Workers
tab count bounced between the true fleet size (272) and small numbers (7-26).
nginx round-robins /workers across 19 hubs, so each poll asked a different
hub -- and a hub whose cross-hub Redis aggregation had gone stale answered
with just the workers whose control-WS it personally holds. One sweep caught
8 of 19 hubs collapsed simultaneously.

Mechanism: the aggregation ran INLINE under ``asyncio.wait_for(..., 1.5)``.
A timeout there cancels the fetch, so it never refreshes the cache; hub event
loops stall for multiple seconds often enough that whole episodes produced
zero successful fetches, and once the 60s fallback TTL expired the fleet view
collapsed. The count is not cosmetic -- /workers/capacity is derived from the
same call and gates crawler admission.

These tests pin the contract: stale is allowed, ABSENT is not.
"""

import asyncio
import json
import time as _real_time

import pytest

from server import scheduler as sched
from server.protocol import WorkerCapabilities


class _Clock:
    """Stand-in for the ``time`` module with a steerable wall clock."""

    def __init__(self):
        self.offset = 0.0

    def time(self):
        return _real_time.time() + self.offset

    def monotonic(self):
        return _real_time.monotonic() + self.offset


class _Pipeline:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def mget(self, keys):
        self._ops.append(("mget", list(keys)))

    def hgetall(self, key):
        self._ops.append(("hgetall", key))

    async def execute(self):
        await self._store.gate()
        out = []
        for op, arg in self._ops:
            if op == "mget":
                out.append([self._store.kv.get(k) for k in arg])
            else:
                out.append(self._store.hashes.get(arg, {}))
        return out


class _FakeRedis:
    """Fake with a ``mode`` switch so a test can stall or break the fetch."""

    def __init__(self):
        self.kv = {}
        self.hashes = {}
        self.zsets = {}
        self.mode = "ok"          # ok | hang | error
        self.fetches = 0

    async def gate(self):
        self.fetches += 1
        if self.mode == "hang":
            await asyncio.sleep(3600)
        if self.mode == "error":
            raise RuntimeError("redis blip")

    async def set(self, key, value, ex=None):
        self.kv[key] = value

    async def get(self, key):
        return self.kv.get(key)

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrange(self, key, start, stop, withscores=False):
        await self.gate()
        z = self.zsets.get(key, {})
        if withscores:
            return [(m, float(sc)) for m, sc in z.items()]
        return list(z)

    async def hset(self, key, mapping=None, **kw):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def pipeline(self, transaction=False):
        return _Pipeline(self)


class _WS:
    async def send_text(self, _):
        pass


def _seed(reg, clock, n_peers=20):
    """One LOCAL connection on this hub + ``n_peers`` rows owned by peers."""
    r = reg._r
    for i in range(n_peers):
        wid = f"w51{i:03d}"
        r.zsets.setdefault(sched._k_index(), {})[wid] = clock.time()
        r.kv[sched._k_worker(wid)] = json.dumps({
            "worker_id": wid, "hub_id": "hub-peer",
            "capabilities": {"max_concurrent": 2},
            "status": "active", "nproc": 128, "load1": 4.0,
        })
        r.kv[sched._k_owner(wid)] = "hub-peer"


@pytest.fixture
def reg(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(sched, "time", clock)
    monkeypatch.setattr(sched, "_KNOWN_WORKERS_COLD_WAIT_S", 0.2)
    r = WorkerRegistryFixture = sched.WorkerRegistry()
    r._r = _FakeRedis()
    r._hub_id = "hub-local"
    asyncio.get_event_loop_policy()
    asyncio.run(r.register(
        "w51999", _WS(), WorkerCapabilities(max_concurrent=2, version="t"),
    ))
    _seed(r, clock)
    r.clock = clock
    return r


def test_warm_view_merges_local_and_peer_rows(reg):
    async def go():
        return await reg.stats_async()
    snap = asyncio.run(go())
    assert snap["count"] == 21, snap["count"]


def test_stale_cache_is_served_instead_of_collapsing_to_local_only(reg):
    """The bug: after the fallback TTL expired, a hub answered `1`."""
    async def go():
        first = await reg.stats_async()
        # Redis now stalls past every deadline, exactly like a hub whose
        # event loop is starved. Age the clock well past the 60s fallback
        # TTL that used to hard-expire the cache.
        reg._r.mode = "hang"
        reg.clock.offset += 300.0
        return first, await reg.stats_async()
    first, later = asyncio.run(go())
    assert first["count"] == 21
    # Before the fix this asserted 1 (local only) -- the flap the operator saw.
    assert later["count"] == 21, later["count"]
    # Stale is allowed; claiming stale liveness is not. Every peer row's
    # heartbeat is now 300s old, i.e. past WORKER_TTL.
    peers = [w for w in later["workers"] if w["worker_id"] != "w51999"]
    assert peers and not any(w["alive"] for w in peers)
    assert later["aggregate_stale_s"] >= 300.0


def test_a_broken_fetch_does_not_drop_the_fleet_either(reg):
    async def go():
        await reg.stats_async()
        reg._r.mode = "error"
        reg.clock.offset += 120.0
        return await reg.stats_async()
    assert asyncio.run(go())["count"] == 21


def test_concurrent_callers_share_one_fetch(reg):
    """Single-flight. /workers, /workers/capacity, the reapers and salvage all
    call stats_async; a stalled hub must not pile up N pipelines against the
    shared single-threaded Redis."""
    async def go():
        await reg.stats_async()             # warm the cache
        reg._r.mode = "hang"
        reg.clock.offset += 10.0
        before = reg._r.fetches
        await asyncio.gather(*(reg.stats_async() for _ in range(25)))
        return before, reg._r.fetches
    before, after = asyncio.run(go())
    # One zrange from the single background refresh, not 25.
    assert after - before == 1, (before, after)


def test_request_path_does_not_await_redis_once_warm(reg):
    """A stalled Redis must cost freshness, never latency: the old inline
    wait_for made every /workers poll pay up to 1.5s during a stall."""
    async def go():
        await reg.stats_async()
        reg._r.mode = "hang"
        reg.clock.offset += 10.0
        t0 = _real_time.monotonic()
        await reg.stats_async()
        return _real_time.monotonic() - t0
    assert asyncio.run(go()) < 0.25
