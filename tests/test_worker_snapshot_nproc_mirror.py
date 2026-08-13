"""Regression: ``nproc`` must survive the heartbeat -> Redis -> peer-hub trip.

``load1`` is host-scoped (an LXC CT reports its Proxmox node's getloadavg),
so it is only interpretable divided by ``nproc``. The heartbeat mirror wrote
``load1`` but not ``nproc``, and the Redis row is what every hub OTHER than
the worker's owner reads -- so ~6/7 of the fleet appeared with nproc=0.
Consequences (2026-08-09): ``io_sat`` rendered as "—" fleet-wide, and
/workers/capacity's health formula fell back to comparing a 128-thread
node's raw load1 against a flat ref, pinning 42% of the fleet at the 0.3
health floor and gating admission with ~150 lanes idle.

The read-back side (``_fetch_known_workers``) already whitelisted ``nproc``;
that is exactly the "mirroring is only half the job" trap its own comment
warns about, so this test pins the ROUND TRIP rather than either half.
"""

import asyncio
import json

import pytest

from server import scheduler as sched
from server.protocol import WorkerCapabilities


class _Pipeline:
    """Minimal redis pipeline: queue (op, key), replay against the store."""

    def __init__(self, store):
        self._store = store
        self._ops = []

    def get(self, key):
        self._ops.append(("get", key))

    def zscore(self, key, member):
        self._ops.append(("zscore", (key, member)))

    def hgetall(self, key):
        self._ops.append(("hgetall", key))

    async def execute(self):
        out = []
        for op, arg in self._ops:
            if op == "get":
                out.append(self._store.kv.get(arg))
            elif op == "zscore":
                out.append(self._store.zsets.get(arg[0], {}).get(arg[1]))
            else:
                out.append(self._store.hashes.get(arg, {}))
        return out


class _FakeRedis:
    def __init__(self):
        self.kv = {}
        self.hashes = {}
        self.zsets = {}

    async def set(self, key, value, ex=None):
        self.kv[key] = value

    async def get(self, key):
        return self.kv.get(key)

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrange(self, key, start, stop):
        return list(self.zsets.get(key, {}))

    async def hset(self, key, mapping=None, **kw):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def pipeline(self, transaction=False):
        return _Pipeline(self)


class _WS:
    async def send_text(self, _):
        pass


def _registry_with_worker(nproc, load1):
    reg = sched.WorkerRegistry()
    reg._r = _FakeRedis()
    reg._hub_id = "hub-test"
    caps = WorkerCapabilities(max_concurrent=2, version="test")
    asyncio.run(reg.register("w51199", _WS(), caps))
    # Seed the row the mirror updates in place (register may not create it).
    reg._r.kv[sched._k_worker("w51199")] = json.dumps({"worker_id": "w51199"})
    asyncio.run(
        reg.heartbeat("w51199", in_flight=0, load1=load1, nproc=nproc)
    )
    return reg


def _row(reg):
    raw = reg._r.kv[sched._k_worker("w51199")]
    return json.loads(raw)


def test_heartbeat_mirrors_nproc_into_the_redis_row():
    reg = _registry_with_worker(nproc=128, load1=70.0)
    row = _row(reg)
    assert row["nproc"] == 128
    # load1 alone was never the useful half.
    assert row["load1"] == 70.0


def test_peer_hub_view_reconstructs_nproc():
    """A hub that does NOT own the WS reads the row back -- the path that
    was returning nproc=0 for 132 of 151 workers."""
    reg = _registry_with_worker(nproc=128, load1=70.0)
    # exclude_ids empty = pretend this hub owns no live connection to it.
    rows = asyncio.run(reg._fetch_known_workers(set()))
    assert len(rows) == 1
    assert rows[0]["nproc"] == 128
    assert rows[0]["load1"] == 70.0


def test_nproc_survives_a_heartbeat_that_omits_it():
    """nproc is static and a beat may leave it at the 0 default; the last
    known non-zero value must persist rather than zeroing the row."""
    reg = _registry_with_worker(nproc=128, load1=70.0)
    asyncio.run(reg.heartbeat("w51199", in_flight=1, load1=71.0, nproc=0))
    assert _row(reg)["nproc"] == 128


def test_missing_nproc_reads_back_as_zero_not_an_error():
    """A legacy row written before this mirror existed must still decode --
    the capacity fallback path depends on nproc simply being falsy."""
    reg = _registry_with_worker(nproc=0, load1=70.0)
    rows = asyncio.run(reg._fetch_known_workers(set()))
    assert rows[0]["nproc"] == 0
