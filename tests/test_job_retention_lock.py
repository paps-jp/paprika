"""Regression: the job-retention pass hands its redis lock back when it is done.

Until 2026-11-02 the pass took ``SET NX EX INTERVAL*0.9`` and never touched the
key again, so a 2-minute pass still held the lock for the full 9 minutes and
the fleet-wide drain rate was pinned to MAX_PER_TICK per INTERVAL (8000 per
600 s = 1.15M/day) against a 1.6M/day inflow -- the table grew for weeks while
every individual delete finished fast. These tests pin the new contract:

* after a pass the lock's TTL is cut to the short cooldown (not left at TTL);
* only OUR lock is shortened -- a lock another hub re-took is left alone;
* a batch in which every purge fails ends the pass instead of re-fetching the
  same ids forever;
* a pass that found candidates but deleted none says so at WARNING.
"""

import logging

import pytest

from server.hub import _reaper
from server.hub._state import config


class _FakeSettings:
    def __init__(self, **vals):
        self._v = dict(vals)

    def get(self, key, default=None):
        return self._v.get(key, default)


class _FakeRedis:
    """Just enough of redis.asyncio for the lock: SET NX EX / GET / EXPIRE."""

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


class _FakeStore:
    def __init__(self, ids, *, fail_deletes=False):
        self._ids = list(ids)
        self.deleted = []
        self.queries = 0
        self._fail = fail_deletes
        self._r = _FakeRedis()

    async def list_deletable_job_ids(self, cutoff, status_in, limit):
        self.queries += 1
        remaining = [j for j in self._ids if j not in self.deleted]
        return remaining[: int(limit)]

    async def delete_job(self, jid):
        if self._fail:
            raise RuntimeError("DB says no")
        self.deleted.append(jid)


@pytest.fixture(autouse=True)
def _in_memory_only(monkeypatch):
    import server.hub.objstore as objstore
    from server.hub import _state

    monkeypatch.setattr(objstore, "enabled", lambda: False, raising=False)
    monkeypatch.setattr(
        _state.state,
        "settings",
        _FakeSettings(job_retention_enabled=True, job_retention_days=5),
        raising=False,
    )
    monkeypatch.setattr(config, "hub_id", "hub-test", raising=False)
    monkeypatch.setattr(_reaper, "_RETENTION_COOLDOWN_S", 7)
    yield


def _install_store(monkeypatch, store):
    from server.hub import _state

    monkeypatch.setattr(_state.state, "store", store, raising=False)


@pytest.mark.asyncio
async def test_lock_is_shortened_to_cooldown_after_the_pass(monkeypatch):
    store = _FakeStore(["a", "b", "c"])
    _install_store(monkeypatch, store)
    n = await _reaper._run_job_retention_once()
    assert n == 3
    key = _reaper._RETENTION_LOCK_KEY
    assert store._r.kv[key] == "hub-test"
    # Not the INTERVAL*0.9 it was taken with -- the cooldown.
    assert store._r.ttl[key] == 7


@pytest.mark.asyncio
async def test_lock_ttl_is_its_own_knob_not_the_interval(monkeypatch):
    """The lock must outlive the slowest pass. It used to be INTERVAL*0.9, so a
    pass longer than 9 minutes lost its lock mid-flight and a second hub piled
    on. Pin it to the dedicated setting, independent of INTERVAL."""
    monkeypatch.setattr(_reaper, "_RETENTION_INTERVAL_S", 600.0)
    monkeypatch.setattr(_reaper, "_RETENTION_LOCK_TTL_S", 1800)
    seen = {}
    orig_set = _FakeRedis.set

    async def _spy_set(self, key, value, nx=False, ex=None):
        seen["ex"] = ex
        return await orig_set(self, key, value, nx=nx, ex=ex)

    monkeypatch.setattr(_FakeRedis, "set", _spy_set)
    store = _FakeStore(["a"])
    _install_store(monkeypatch, store)
    await _reaper._run_job_retention_once()
    assert seen["ex"] == 1800
    assert seen["ex"] != int(600 * 0.9)


@pytest.mark.asyncio
async def test_another_hubs_lock_is_left_alone(monkeypatch):
    store = _FakeStore(["a"])
    _install_store(monkeypatch, store)
    key = _reaper._RETENTION_LOCK_KEY

    # Simulate: our TTL expired mid-pass and hub-other re-took the lock.
    orig_list = store.list_deletable_job_ids

    async def _list_then_steal(cutoff, status_in, limit):
        store._r.kv[key] = "hub-other"
        store._r.ttl[key] = 540
        return await orig_list(cutoff, status_in, limit)

    store.list_deletable_job_ids = _list_then_steal
    await _reaper._run_job_retention_once()
    assert store._r.kv[key] == "hub-other"
    assert store._r.ttl[key] == 540  # untouched


@pytest.mark.asyncio
async def test_held_lock_means_this_hub_skips(monkeypatch):
    store = _FakeStore(["a", "b"])
    _install_store(monkeypatch, store)
    await store._r.set(_reaper._RETENTION_LOCK_KEY, "hub-other", nx=True, ex=540)
    n = await _reaper._run_job_retention_once()
    assert n == 0
    assert store.deleted == []
    assert store.queries == 0


@pytest.mark.asyncio
async def test_all_failed_batch_ends_the_pass(monkeypatch, caplog):
    store = _FakeStore(["a", "b", "c"], fail_deletes=True)
    _install_store(monkeypatch, store)
    monkeypatch.setattr(_reaper, "_RETENTION_MAX_PER_TICK", 1000)
    with caplog.at_level(logging.WARNING, logger="server.hub._reaper"):
        n = await _reaper._run_job_retention_once()
    assert n == 0
    # One candidate query, one failed batch, out -- not a spin on the same ids.
    assert store.queries == 1
    assert any("0/3 candidates deleted" in r.getMessage() for r in caplog.records)
    # Lock still handed back on the failure path.
    assert store._r.ttl[_reaper._RETENTION_LOCK_KEY] == 7


@pytest.mark.asyncio
async def test_lock_is_released_even_when_the_pass_raises(monkeypatch):
    store = _FakeStore(["a"])
    _install_store(monkeypatch, store)

    async def _boom(*a, **k):
        raise ValueError("candidate query exploded in a way we don't catch")

    monkeypatch.setattr(_reaper, "_purge_job", _boom)
    with pytest.raises(ValueError):
        await _reaper._run_job_retention_once()
    assert store._r.ttl[_reaper._RETENTION_LOCK_KEY] == 7
