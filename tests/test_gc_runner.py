"""server/hub/tools/gc_runner.py: the resident, sharded jobs-table GC.

Pins the contracts the 2026-11-03 drain relied on:

* shard i of N owns exactly the rows with ``CRC32(job_id) % N == i`` and only
  shard 0 holds the fleet lock key (so the in-hub loops stay out);
* the runner KEEPS its lock across passes (refresh if ours, SET NX otherwise)
  and the stock release becomes a no-op -- the gap the stock cooldown opened
  let a hub loop take the key for a 10-minute pass while the runner spun on 0;
* shutdown deletes only OUR key, never one a hub has since taken.
"""

import pytest

from server.hub import _reaper
from server.hub._state import config
from server.hub.tools import gc_runner


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        self.ttl[key] = int(ex or 0)
        return True

    async def get(self, key):
        v = self.kv.get(key)
        return None if v is None else v.encode()

    async def expire(self, key, seconds):
        if key in self.kv:
            self.ttl[key] = int(seconds)
            return True
        return False

    async def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0


@pytest.fixture
def stock_lock(monkeypatch):
    """install_lock_hold rebinds module functions; restore them after. The
    stock SET NX writes ``config.hub_id`` as the holder, so pin it too."""
    monkeypatch.setattr(_reaper, "_acquire_pass_lock", _reaper._acquire_pass_lock)
    monkeypatch.setattr(_reaper, "_release_pass_lock", _reaper._release_pass_lock)
    monkeypatch.setattr(config, "hub_id", "gc-runner")
    yield


def test_shard_settings_validated(monkeypatch):
    monkeypatch.setenv("GC_SHARDS", "3")
    monkeypatch.setenv("GC_SHARD", "2")
    assert gc_runner.shard_settings() == (3, 2)
    monkeypatch.setenv("GC_SHARD", "3")
    with pytest.raises(SystemExit):
        gc_runner.shard_settings()
    monkeypatch.delenv("GC_SHARDS")
    monkeypatch.delenv("GC_SHARD")
    assert gc_runner.shard_settings() == (1, 0)


def test_only_shard_zero_holds_fleet_key():
    assert gc_runner.lock_key_for(0) == _reaper._RETENTION_LOCK_KEY
    assert gc_runner.lock_key_for(0) == "paprika:retention:lock"
    assert gc_runner.lock_key_for(4) == "paprika:retention:lock:s4"
    assert gc_runner.hub_id_for(0) == "gc-runner"
    assert gc_runner.hub_id_for(4) == "gc-runner-s4"


def test_sharded_sql_filters_on_residue():
    sql = gc_runner.sharded_candidate_sql(3, 4)
    # ``%%`` is the driver's escaped modulo (aiomysql formats params with %)
    assert "CRC32(job_id) %% %s = %s" in sql
    assert "status IN (%s,%s,%s,%s)" in sql
    assert sql.endswith("ORDER BY completed_at ASC LIMIT %s")


@pytest.mark.asyncio
async def test_sharded_lister_binds_shard_params():
    captured = {}

    class _Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params):
            captured["sql"], captured["params"] = sql, params

        async def fetchall(self):
            return [("j1",), ("j2",)]

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

    class _Pool:
        def acquire(self):
            return _Conn()

    lister = gc_runner.make_sharded_lister(_Pool(), 3, 2)
    assert await lister("CUTOFF", ["completed", "failed"], 500) == ["j1", "j2"]
    assert captured["params"] == ["CUTOFF", "completed", "failed", 3, 2, 500]
    assert await lister("CUTOFF", [], 500) == []


@pytest.mark.asyncio
async def test_lock_is_held_across_passes(stock_lock):
    r = _FakeRedis()
    gc_runner.install_lock_hold("gc-runner")
    assert await _reaper._acquire_pass_lock(r, "k", 3600) is True
    assert r.kv["k"] == "gc-runner"
    # stock release would shorten the TTL to the cooldown; ours leaves it
    await _reaper._release_pass_lock(r, "k", 30)
    assert r.ttl["k"] == 3600
    # second pass: refresh, not a failed SET NX
    r.ttl["k"] = 5
    assert await _reaper._acquire_pass_lock(r, "k", 3600) is True
    assert r.ttl["k"] == 3600


@pytest.mark.asyncio
async def test_lock_held_by_hub_is_not_taken(stock_lock):
    r = _FakeRedis()
    await r.set("k", "hub-57", nx=True, ex=1800)
    gc_runner.install_lock_hold("gc-runner")
    assert await _reaper._acquire_pass_lock(r, "k", 3600) is False
    assert r.kv["k"] == "hub-57"
    assert r.ttl["k"] == 1800


@pytest.mark.asyncio
async def test_release_own_lock_only():
    r = _FakeRedis()
    await r.set("k", "hub-57", nx=True, ex=1800)
    assert await gc_runner.release_own_lock(r, "k", "gc-runner") is False
    assert "k" in r.kv
    r.kv["k"] = "gc-runner"
    assert await gc_runner.release_own_lock(r, "k", "gc-runner") is True
    assert "k" not in r.kv
    assert await gc_runner.release_own_lock(None, "k", "gc-runner") is False
