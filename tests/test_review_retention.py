"""Regression: the review(課題)-specific expiry pass.

The general job-retention loop deletes OLDEST-FIRST across all terminal
statuses, so review rows (a tiny fraction of the table) are only reached once
the deletion frontier sweeps past them -- their effective TTL is pinned to the
general backlog depth, not to any review target. ``_run_review_retention_once``
is a SEPARATE pass that queries review-ONLY with its own cutoff + budget so
review is bounded independently. These tests pin that contract: the gate, the
dry-run (probe-only, never deletes), and the enact path (review-only, purges
DB + MinIO + cache via the shared _purge_job).
"""

import pytest

from server.hub import _reaper


class _FakeSettings:
    def __init__(self, **vals):
        self._v = dict(vals)

    def get(self, key, default=None):
        return self._v.get(key, default)

    def set(self, key, value):
        self._v[key] = value


class _FakeStore:
    """Minimal store: hands back a fixed candidate list and records deletes.

    ``list_deletable_job_ids`` asserts it is ONLY ever asked for review, and
    returns the not-yet-deleted candidates oldest-first (so the enact loop
    drains and terminates)."""

    def __init__(self, review_ids):
        self._review = list(review_ids)
        self.deleted = []
        self._r = None  # no redis -> lock is best-effort skipped
        self.status_queries = []

    async def list_deletable_job_ids(self, cutoff, status_in, limit):
        self.status_queries.append(list(status_in))
        assert status_in == ["review"], f"expected review-only, got {status_in}"
        remaining = [j for j in self._review if j not in self.deleted]
        return remaining[: int(limit)]

    async def delete_job(self, jid):
        self.deleted.append(jid)


@pytest.fixture(autouse=True)
def _no_objstore_no_fs(monkeypatch):
    """Neutralise the side-effectful bits of _purge_job so the test stays in
    memory: MinIO disabled, storage dir irrelevant."""
    import server.hub.objstore as objstore

    monkeypatch.setattr(objstore, "enabled", lambda: False, raising=False)
    yield


def _install_settings(monkeypatch, **vals):
    from server.hub import _state

    monkeypatch.setattr(_state.state, "settings", _FakeSettings(**vals), raising=False)


def _install_store(monkeypatch, store):
    from server.hub import _state

    monkeypatch.setattr(_state.state, "store", store, raising=False)


@pytest.mark.asyncio
async def test_disabled_is_inert(monkeypatch):
    store = _FakeStore(["a", "b", "c"])
    _install_store(monkeypatch, store)
    _install_settings(monkeypatch, review_retention_enabled=False)
    out = await _reaper._run_review_retention_once()
    assert out["enabled"] is False
    assert store.deleted == []
    assert store.status_queries == []  # never even queried


@pytest.mark.asyncio
async def test_dry_run_probes_but_never_deletes(monkeypatch):
    store = _FakeStore(["a", "b", "c"])
    _install_store(monkeypatch, store)
    _install_settings(
        monkeypatch,
        review_retention_enabled=True,
        review_retention_dry_run=True,
        review_retention_days=7,
    )
    out = await _reaper._run_review_retention_once()
    assert out["enabled"] is True
    assert out["dry_run"] is True
    assert out["days"] == 7
    assert out["candidates"] == 3
    assert out["deleted"] == 0
    assert store.deleted == []  # dry-run deletes NOTHING


@pytest.mark.asyncio
async def test_dry_run_is_the_default_when_key_absent(monkeypatch):
    # review_retention_dry_run omitted -> must default to True (safe).
    store = _FakeStore(["a"])
    _install_store(monkeypatch, store)
    _install_settings(
        monkeypatch, review_retention_enabled=True, review_retention_days=7
    )
    out = await _reaper._run_review_retention_once()
    assert out["dry_run"] is True
    assert store.deleted == []


@pytest.mark.asyncio
async def test_enact_deletes_review_only(monkeypatch):
    store = _FakeStore(["j1", "j2", "j3", "j4"])
    _install_store(monkeypatch, store)
    _install_settings(
        monkeypatch,
        review_retention_enabled=True,
        review_retention_dry_run=False,
        review_retention_days=7,
    )
    out = await _reaper._run_review_retention_once()
    assert out["enabled"] is True and out["dry_run"] is False
    assert out["deleted"] == 4
    assert sorted(store.deleted) == ["j1", "j2", "j3", "j4"]
    # Every candidate query was review-scoped (asserted inside the fake too).
    assert all(q == ["review"] for q in store.status_queries)


@pytest.mark.asyncio
async def test_zero_days_is_noop(monkeypatch):
    store = _FakeStore(["a", "b"])
    _install_store(monkeypatch, store)
    _install_settings(
        monkeypatch,
        review_retention_enabled=True,
        review_retention_dry_run=False,
        review_retention_days=0,
    )
    out = await _reaper._run_review_retention_once()
    assert store.deleted == []  # days<=0 must not delete everything
