"""Regression: the retention pass evicts a batch from the pull list with ONE
read, and the candidate query is keyed on completed_at.

2026-11-02, first pass with phase timings: 20,000 jobs in 2,039 s, of which
``evict`` summed to 60,110 s across the 32 concurrent purges -- one LREM per
job, 3.0 s each against a 4,000-entry list under the fleet's Redis load, 96%
of the pass. ``_pull_queue.remove_many`` reads the list once and LREMs only the
ids actually present; the retention passes call it per batch and tell
``_purge_job`` not to repeat the per-job LREM.
"""

import pytest

import server.hub
from server.hub import _pull_queue, _reaper
from server.hub._state import config


class _FakeRedis:
    def __init__(self, queue):
        self.queue = list(queue)
        self.lrange_calls = 0
        self.lrem_calls = []

    async def lrange(self, key, start, stop):
        self.lrange_calls += 1
        return [q.encode() for q in self.queue]

    async def lrem(self, key, count, value):
        self.lrem_calls.append(value)
        n = self.queue.count(value)
        self.queue = [q for q in self.queue if q != value]
        return n

    # lock plumbing used by the pass
    async def set(self, key, value, nx=False, ex=None):
        return True

    async def get(self, key):
        return None

    async def expire(self, key, seconds):
        return True


@pytest.mark.asyncio
async def test_remove_many_reads_once_and_lrems_only_hits(monkeypatch):
    r = _FakeRedis(["a", "b", "c", "b"])
    monkeypatch.setattr(_pull_queue, "_redis", lambda: r)
    removed = await _pull_queue.remove_many(["b", "zzz", "c", "", "nope"])
    assert removed == 3  # b x2 + c
    assert r.lrange_calls == 1
    assert sorted(r.lrem_calls) == ["b", "c"]  # never for ids not on the list
    assert r.queue == ["a"]


@pytest.mark.asyncio
async def test_remove_many_is_inert_without_redis(monkeypatch):
    monkeypatch.setattr(_pull_queue, "_redis", lambda: None)
    assert await _pull_queue.remove_many(["a"]) == 0


class _FakeSettings:
    def get(self, key, default=None):
        return {"job_retention_enabled": True, "job_retention_days": 5}.get(key, default)


class _FakeStore:
    def __init__(self, ids, redis):
        self._ids = list(ids)
        self.deleted = []
        self._r = redis

    async def list_deletable_job_ids(self, cutoff, status_in, limit):
        remaining = [j for j in self._ids if j not in self.deleted]
        return remaining[: int(limit)]

    async def delete_job(self, jid):
        self.deleted.append(jid)


@pytest.mark.asyncio
async def test_retention_pass_evicts_per_batch_not_per_job(monkeypatch):
    import server.hub.objstore as objstore
    from server.hub import _state

    monkeypatch.setattr(objstore, "enabled", lambda: False, raising=False)
    monkeypatch.setattr(_state.state, "settings", _FakeSettings(), raising=False)
    monkeypatch.setattr(config, "hub_id", "hub-test", raising=False)

    calls = {"many": [], "single": []}

    class _FakePQ:
        ENABLED = True

        @staticmethod
        async def remove_many(ids):
            calls["many"].append(list(ids))
            return 0

        @staticmethod
        async def remove(jid):
            calls["single"].append(jid)
            return 0

    monkeypatch.setattr(server.hub, "_pull_queue", _FakePQ, raising=False)
    store = _FakeStore(["j1", "j2", "j3"], _FakeRedis([]))
    monkeypatch.setattr(_state.state, "store", store, raising=False)
    monkeypatch.setattr(_reaper, "_RETENTION_BATCH", 2)
    monkeypatch.setattr(_reaper, "_RETENTION_MAX_PER_TICK", 100)

    n = await _reaper._run_job_retention_once()
    assert n == 3
    assert calls["many"] == [["j1", "j2"], ["j3"]]  # one call per batch
    assert calls["single"] == []  # no per-job LREM inside the pass


@pytest.mark.asyncio
async def test_single_job_purge_still_evicts_per_job(monkeypatch, tmp_path):
    """Reconciler-style single purges keep the per-job evict (default)."""
    import asyncio
    import server.hub.objstore as objstore
    from server.hub import _state

    monkeypatch.setattr(objstore, "enabled", lambda: False, raising=False)
    monkeypatch.setattr(_state, "get_storage_dir", lambda: tmp_path, raising=False)
    evicted = []

    class _FakePQ:
        ENABLED = True

        @staticmethod
        async def remove(jid):
            evicted.append(jid)
            return 1

    monkeypatch.setattr(server.hub, "_pull_queue", _FakePQ, raising=False)
    store = _FakeStore(["j1"], _FakeRedis([]))
    ok = await _reaper._purge_job(store, "j1", asyncio.Semaphore(1), 5.0)
    assert ok and evicted == ["j1"]
