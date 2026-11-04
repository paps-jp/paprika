"""Retention cutoff is clamped to the downstream consumers' watermarks.

The pull plugins (image-pull / video-pull / links-pull) record per slug the
``completed_at`` up to which they have fetched everything. A job that finished
after that point has NOT been read yet; deleting it loses its images and links
silently. video-pull sat 13 days behind on 2026-10-13 -- with a 5-day retention
that would have been 8 days of videos gone without a trace.

Contract pinned here:

* cutoff = min(now - days, min(watermark) - margin) when the watermarks are
  fresh enough nothing changes; when a consumer lags, the cutoff moves back;
* FAIL-CLOSED: unreadable table / missing slug / no pool / bad table name ->
  the pass deletes nothing and hands the lock back;
* code default is OFF (so fakes without a DB keep working); the settings
  schema default is ON -- see server/hub/settings.py.
"""

from datetime import datetime, timedelta

import pytest

from server.hub import _reaper
from server.hub import settings as _settings
from server.hub._state import config, state

NOW = datetime(2026, 11, 4, 12, 0, 0)
SLUGS = "paprika-image-pull,paprika-video-pull,paprika-links-pull"


class _FakeSettings:
    def __init__(self, **vals):
        self._v = dict(vals)

    def get(self, key, default=None):
        return self._v.get(key, default)


class _Cur:
    def __init__(self, rows, fail=False):
        self._rows, self._fail = rows, fail
        self.sql = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params=None):
        if self._fail:
            raise RuntimeError("table gone")
        self.sql = sql
        self.params = params

    async def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def cursor(self):
        return self._cur


class _Pool:
    def __init__(self, rows, fail=False):
        self.cur = _Cur(rows, fail)

    def acquire(self):
        return _Conn(self.cur)


def _wm_settings(**over):
    base = dict(
        job_retention_watermark_enabled=True,
        job_retention_watermark_table="delian.paprika_pull_watermark",
        job_retention_watermark_slugs=SLUGS,
        job_retention_watermark_margin_hours=6,
    )
    base.update(over)
    return _FakeSettings(**base)


def _rows(image, video, links):
    return [
        ("paprika-image-pull", image),
        ("paprika-video-pull", video),
        ("paprika-links-pull", links),
    ]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(_reaper, "_watermark_last_warn_mono", 0.0)
    monkeypatch.setattr(state, "mariadb_pool", None, raising=False)
    yield


def test_schema_defaults_are_on_and_cover_the_three_consumers():
    sch = _settings._SCHEMA
    assert sch["job_retention_watermark_enabled"] == (True, "bool")
    assert sch["job_retention_watermark_table"][0] == "delian.paprika_pull_watermark"
    assert set(sch["job_retention_watermark_slugs"][0].split(",")) == set(SLUGS.split(","))
    assert sch["job_retention_watermark_margin_hours"] == (6, "int")


@pytest.mark.asyncio
async def test_fresh_watermarks_leave_cutoff_alone(monkeypatch):
    monkeypatch.setattr(state, "settings", _wm_settings(), raising=False)
    monkeypatch.setattr(state, "mariadb_pool", _Pool(_rows(NOW, NOW, NOW)), raising=False)
    cutoff = NOW - timedelta(days=2)
    assert await _reaper._watermark_clamp_cutoff(cutoff, "t") == cutoff


@pytest.mark.asyncio
async def test_lagging_consumer_pulls_cutoff_back_by_margin(monkeypatch):
    monkeypatch.setattr(state, "settings", _wm_settings(), raising=False)
    behind = NOW - timedelta(days=1)  # video-pull 1 day behind
    monkeypatch.setattr(state, "mariadb_pool", _Pool(_rows(NOW, behind, NOW)), raising=False)
    cutoff = NOW - timedelta(hours=12)  # a 12h retention would eat unread videos
    got = await _reaper._watermark_clamp_cutoff(cutoff, "t")
    assert got == behind - timedelta(hours=6)


@pytest.mark.asyncio
async def test_query_binds_only_configured_slugs(monkeypatch):
    monkeypatch.setattr(state, "settings", _wm_settings(), raising=False)
    pool = _Pool(_rows(NOW, NOW, NOW))
    monkeypatch.setattr(state, "mariadb_pool", pool, raising=False)
    await _reaper._watermark_clamp_cutoff(NOW, "t")
    assert pool.cur.sql.startswith(
        "SELECT slug, last_completed_at FROM delian.paprika_pull_watermark"
    )
    assert pool.cur.params == SLUGS.split(",")


@pytest.mark.asyncio
async def test_missing_slug_is_fail_closed(monkeypatch):
    monkeypatch.setattr(state, "settings", _wm_settings(), raising=False)
    rows = [("paprika-image-pull", NOW), ("paprika-links-pull", NOW)]  # no video-pull
    monkeypatch.setattr(state, "mariadb_pool", _Pool(rows), raising=False)
    assert await _reaper._watermark_clamp_cutoff(NOW, "t") is None


@pytest.mark.asyncio
async def test_unreadable_table_is_fail_closed(monkeypatch):
    monkeypatch.setattr(state, "settings", _wm_settings(), raising=False)
    monkeypatch.setattr(state, "mariadb_pool", _Pool([], fail=True), raising=False)
    assert await _reaper._watermark_clamp_cutoff(NOW, "t") is None


@pytest.mark.asyncio
async def test_no_pool_is_fail_closed(monkeypatch):
    monkeypatch.setattr(state, "settings", _wm_settings(), raising=False)
    assert await _reaper._watermark_clamp_cutoff(NOW, "t") is None


@pytest.mark.asyncio
async def test_bad_table_name_is_fail_closed_not_injected(monkeypatch):
    monkeypatch.setattr(
        state,
        "settings",
        _wm_settings(job_retention_watermark_table="x; DROP TABLE jobs"),
        raising=False,
    )
    monkeypatch.setattr(state, "mariadb_pool", _Pool(_rows(NOW, NOW, NOW)), raising=False)
    assert await _reaper._watermark_clamp_cutoff(NOW, "t") is None


@pytest.mark.asyncio
async def test_disabled_or_no_slugs_is_passthrough(monkeypatch):
    monkeypatch.setattr(
        state, "settings", _wm_settings(job_retention_watermark_enabled=False), raising=False
    )
    assert await _reaper._watermark_clamp_cutoff(NOW, "t") == NOW
    monkeypatch.setattr(
        state, "settings", _wm_settings(job_retention_watermark_slugs=""), raising=False
    )
    assert await _reaper._watermark_clamp_cutoff(NOW, "t") == NOW


# ── the pass itself honours the clamp ────────────────────────────────────────


class _FakeRedis:
    def __init__(self):
        self.kv, self.ttl = {}, {}

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
    def __init__(self, ids):
        self._ids, self.deleted, self.cutoffs = list(ids), [], []
        self._r = _FakeRedis()

    async def list_deletable_job_ids(self, cutoff, status_in, limit):
        self.cutoffs.append(cutoff)
        remaining = [j for j in self._ids if j not in self.deleted]
        return remaining[: int(limit)]

    async def delete_job(self, jid):
        self.deleted.append(jid)


@pytest.fixture
def _pass_env(monkeypatch):
    import server.hub.objstore as objstore

    monkeypatch.setattr(objstore, "enabled", lambda: False, raising=False)
    monkeypatch.setattr(config, "hub_id", "hub-test", raising=False)
    monkeypatch.setattr(_reaper, "_RETENTION_COOLDOWN_S", 7)
    monkeypatch.setattr(
        state,
        "settings",
        _wm_settings(job_retention_enabled=True, job_retention_days=2),
        raising=False,
    )
    yield


@pytest.mark.asyncio
async def test_pass_deletes_nothing_and_returns_lock_when_fail_closed(_pass_env, monkeypatch):
    store = _FakeStore(["a", "b"])
    monkeypatch.setattr(state, "store", store, raising=False)
    # no pool -> fail-closed
    n = await _reaper._run_job_retention_once()
    assert n == 0 and store.deleted == [] and store.cutoffs == []
    key = _reaper._RETENTION_LOCK_KEY
    assert store._r.kv[key] == "hub-test" and store._r.ttl[key] == 7  # handed back


@pytest.mark.asyncio
async def test_pass_uses_the_clamped_cutoff(_pass_env, monkeypatch):
    store = _FakeStore(["a"])
    monkeypatch.setattr(state, "store", store, raising=False)
    behind = datetime.utcnow() - timedelta(days=3)  # consumers 3 days behind
    monkeypatch.setattr(state, "mariadb_pool", _Pool(_rows(behind, behind, behind)), raising=False)
    n = await _reaper._run_job_retention_once()
    assert n == 1
    (cutoff,) = store.cutoffs
    # 2-day retention would be now-48h; the clamp pulls it back to behind-6h = now-78h
    assert abs((cutoff - (behind - timedelta(hours=6))).total_seconds()) < 1
