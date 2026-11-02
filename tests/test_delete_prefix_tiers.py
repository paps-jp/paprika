"""Regression: ``objstore.delete_prefix`` walks every tier and keeps its
contract while doing so in parallel.

Until 2026-11-02 the tiers were walked one after another on a single pool
thread, so a job with nothing in any tier still paid four sequential LIST
round-trips -- which was the whole cost of the retention pass. The walk is
now one executor job per tier, gathered. These tests pin what callers rely
on: objects/bytes are summed across tiers, ``tiers`` counts every configured
tier, a tier that errors makes ``deleted`` False (never silently True), and
``dry_run`` lists without deleting.
"""

import threading
import time

import pytest

from server.hub import objstore


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kw):
        for p in self._pages:
            yield p


class _FakeClient:
    def __init__(self, keys, *, fail=False, delay=0.0):
        self._keys = list(keys)
        self._fail = fail
        self._delay = delay
        self.deleted: list[str] = []
        self.threads: set[int] = set()

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        self.threads.add(threading.get_ident())
        if self._delay:
            time.sleep(self._delay)
        if self._fail:
            raise RuntimeError("tier down")
        contents = [{"Key": k, "Size": 10} for k in self._keys]
        return _FakePaginator([{"Contents": contents}])

    def delete_objects(self, Bucket, Delete):
        self.deleted.extend(o["Key"] for o in Delete["Objects"])


@pytest.fixture
def tiers(monkeypatch):
    monkeypatch.setattr(objstore, "enabled", lambda: True)
    monkeypatch.setattr(objstore, "_job_prefix", lambda jid: f"jobs/{jid}")
    made = []

    def _install(clients):
        made[:] = [(c, f"bucket{i}") for i, c in enumerate(clients)]
        monkeypatch.setattr(objstore, "_all_cbs", lambda: list(made))
        return made

    return _install


@pytest.mark.asyncio
async def test_sums_every_tier_and_deletes(tiers):
    a = _FakeClient(["jobs/j1/a", "jobs/j1/b"])
    b = _FakeClient([])
    c = _FakeClient(["jobs/j1/c"])
    tiers([a, b, c])
    out = await objstore.delete_prefix("j1")
    assert out == {"objects": 3, "bytes": 30, "deleted": True, "tiers": 3, "failed_tiers": 0}
    assert sorted(a.deleted) == ["jobs/j1/a", "jobs/j1/b"]
    assert c.deleted == ["jobs/j1/c"]
    assert b.deleted == []


@pytest.mark.asyncio
async def test_a_failed_tier_is_never_reported_as_deleted(tiers):
    ok = _FakeClient(["jobs/j1/a"])
    bad = _FakeClient(["jobs/j1/x"], fail=True)
    tiers([ok, bad])
    out = await objstore.delete_prefix("j1")
    assert out["deleted"] is False
    assert out["failed_tiers"] == 1
    assert out["tiers"] == 2
    # The healthy tier was still purged -- one bad tier must not block the rest.
    assert ok.deleted == ["jobs/j1/a"]


@pytest.mark.asyncio
async def test_dry_run_counts_but_deletes_nothing(tiers):
    a = _FakeClient(["jobs/j1/a", "jobs/j1/b"])
    tiers([a])
    out = await objstore.delete_prefix("j1", dry_run=True)
    assert out["objects"] == 2 and out["bytes"] == 20
    assert out["deleted"] is False  # dry-run never claims success
    assert a.deleted == []


@pytest.mark.asyncio
async def test_tiers_are_walked_in_parallel(tiers):
    """Four tiers that each take 0.3 s must finish in well under 1.2 s."""
    clients = [_FakeClient([], delay=0.3) for _ in range(4)]
    tiers(clients)
    t0 = time.monotonic()
    out = await objstore.delete_prefix("j1")
    took = time.monotonic() - t0
    assert out["tiers"] == 4 and out["deleted"] is True
    assert took < 0.9, f"tiers ran sequentially? took {took:.2f}s"
    # and not on the event-loop thread
    assert threading.get_ident() not in set().union(*(c.threads for c in clients))
