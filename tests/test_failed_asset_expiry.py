"""Regression: the failed-asset early expiry pass.

``status=failed`` jobs keep their screenshots/images in the image RAM tier
until the general retention frontier reaches them -- measured at 7.4 days, so
21.7 GB/day of failed assets became ~160 GB resident (58% of the .47 tier).
``_run_failed_asset_expiry_once`` purges those objects after a few HOURS while
deliberately LEAVING the DB row for the general retention pass.

These tests pin the contract that makes that safe:
  * the settings gate + dry-run probe (never purges),
  * failed-only + DB rows untouched,
  * the redis watermark advances so a tick never re-reads the same head,
  * a purge that reports failure is re-queued instead of silently orphaned.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from server.hub import _reaper


class _FakeSettings:
    def __init__(self, **vals):
        self._v = dict(vals)

    def get(self, key, default=None):
        return self._v.get(key, default)


class _FakeRedis:
    """Just the four ops the pass uses. ``set`` doubles as the lock (nx) and
    the watermark (plain), which is exactly how the real code uses it."""

    def __init__(self):
        self.kv = {}
        self.sets = {}

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self.kv:
            return False
        self.kv[key] = val
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def sadd(self, key, *vals):
        self.sets.setdefault(key, set()).update(vals)

    async def scard(self, key):
        return len(self.sets.get(key, ()))

    async def spop(self, key, count):
        s = self.sets.get(key) or set()
        out = [s.pop() for _ in range(min(count, len(s)))]
        return out


class _FakeStore:
    """Serves ``(job_id, created_at)`` rows from an in-memory table and records
    any DB deletes (there must be none)."""

    def __init__(self, rows, redis=None):
        self.rows = list(rows)  # [(job_id, created_at, status)]
        self._r = redis
        self.deleted = []
        self.windows = []

    async def list_jobs_by_status_between(self, status, lo, hi, limit):
        self.windows.append((status, lo, hi, limit))
        hits = [
            (j, ca) for j, ca, st in self.rows if st == status and lo <= ca < hi
        ]
        hits.sort(key=lambda t: t[1])
        return hits[: int(limit)]

    async def delete_job(self, jid):  # pragma: no cover - must never be called
        self.deleted.append(jid)


def _install(monkeypatch, store, **settings):
    from server.hub import _state

    monkeypatch.setattr(_state.state, "store", store, raising=False)
    monkeypatch.setattr(
        _state.state, "settings", _FakeSettings(**settings), raising=False
    )


@pytest.fixture
def purges(monkeypatch):
    """Record every delete_prefix call; ``fail`` names ids that report a failed
    tier (delete_prefix returns deleted=False rather than raising)."""
    class _Calls(list):
        """A list that also carries the set of ids whose purge should fail."""
        fail: set

    calls = _Calls()
    calls.fail = set()

    async def _delete_prefix(jid, dry_run=False):
        calls.append(jid)
        ok = jid not in calls.fail
        return {
            "objects": 1, "bytes": 10, "deleted": ok,
            "tiers": 4, "failed_tiers": 0 if ok else 1,
        }

    import server.hub.objstore as objstore

    monkeypatch.setattr(objstore, "enabled", lambda: True, raising=False)
    monkeypatch.setattr(objstore, "delete_prefix", _delete_prefix, raising=False)
    return calls


def _rows(n, *, status="failed", age_h=4.0, start=0):
    """n rows aged ``age_h`` hours, 1 second apart so created_at is a usable
    cursor (the real column is DATETIME(3)).

    The default sits between the 2h expiry cutoff and the 6h cold-start floor,
    i.e. squarely inside the window a fresh pass (no watermark) considers."""
    base = datetime.utcnow() - timedelta(hours=age_h)
    return [
        (f"j{start + i:04d}", base + timedelta(seconds=i), status)
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_disabled_is_inert(monkeypatch, purges):
    store = _FakeStore(_rows(5))
    _install(monkeypatch, store, failed_asset_expiry_enabled=False)
    out = await _reaper._run_failed_asset_expiry_once()
    assert out["enabled"] is False
    assert purges == []


@pytest.mark.asyncio
async def test_zero_hours_pauses_instead_of_defaulting(monkeypatch, purges):
    """An explicit 0 must mean "paused", NOT "fall back to the 2h default" --
    the ``or N`` coercion trap review_retention_days documents."""
    store = _FakeStore(_rows(5))
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=0,
        failed_asset_expiry_dry_run=False,
    )
    out = await _reaper._run_failed_asset_expiry_once()
    assert out["hours"] == 0
    assert purges == []


@pytest.mark.asyncio
async def test_dry_run_probes_only(monkeypatch, purges):
    store = _FakeStore(_rows(7))
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=2,
        failed_asset_expiry_dry_run=True,
    )
    out = await _reaper._run_failed_asset_expiry_once()
    assert out["dry_run"] is True
    assert out["candidates"] == 7
    assert out["purged"] == 0
    assert purges == []


@pytest.mark.asyncio
async def test_enact_purges_assets_but_never_the_db_row(monkeypatch, purges):
    rows = _rows(6)
    store = _FakeStore(rows, redis=_FakeRedis())
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=2,
        failed_asset_expiry_dry_run=False,
    )
    out = await _reaper._run_failed_asset_expiry_once()
    assert out["purged"] == 6
    assert sorted(purges) == sorted(j for j, _, _ in rows)
    # The whole point of this pass: the row survives for job_retention_days.
    assert store.deleted == []


@pytest.mark.asyncio
async def test_only_failed_jobs_are_touched(monkeypatch, purges):
    rows = _rows(3) + _rows(3, status="completed", start=100) + _rows(
        2, status="review", start=200
    )
    store = _FakeStore(rows, redis=_FakeRedis())
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=2,
        failed_asset_expiry_dry_run=False,
    )
    await _reaper._run_failed_asset_expiry_once()
    assert sorted(purges) == ["j0000", "j0001", "j0002"]
    assert {w[0] for w in store.windows} == {"failed"}


@pytest.mark.asyncio
async def test_jobs_inside_the_expiry_window_are_spared(monkeypatch, purges):
    """A job that failed 30 minutes ago must keep its assets: the operator (and
    an escalation retry) may still be looking at them."""
    rows = _rows(3, age_h=4.0) + _rows(3, age_h=0.5, start=50)
    store = _FakeStore(rows, redis=_FakeRedis())
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=2,
        failed_asset_expiry_dry_run=False,
    )
    await _reaper._run_failed_asset_expiry_once()
    assert sorted(purges) == ["j0000", "j0001", "j0002"]


@pytest.mark.asyncio
async def test_watermark_advances_so_the_next_tick_reads_new_rows(
    monkeypatch, purges
):
    """Without a persisted lower bound every tick re-reads the same head and
    re-LISTs prefixes that are already gone -- the cost that pins the general
    retention loop to its per-tick cap."""
    redis = _FakeRedis()
    store = _FakeStore(_rows(4), redis=redis)
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=2,
        failed_asset_expiry_dry_run=False,
    )
    await _reaper._run_failed_asset_expiry_once()
    assert len(purges) == 4
    wm = redis.kv.get(_reaper._FAILED_ASSET_EXPIRY_WM_KEY)
    assert wm, "watermark must be persisted"

    # Second tick: same table, nothing new -> no repeat work.
    redis.kv.pop(_reaper._FAILED_ASSET_EXPIRY_LOCK_KEY, None)
    purges.clear()
    out = await _reaper._run_failed_asset_expiry_once()
    assert purges == []
    assert out["purged"] == 0


@pytest.mark.asyncio
async def test_failed_purge_is_requeued_not_orphaned(monkeypatch, purges):
    """delete_prefix reports a tier failure as deleted=False (it does not
    raise). Walking the watermark past such a job would strand its objects
    where no later pass lists them -- the exact way _purge_job mints orphans."""
    redis = _FakeRedis()
    store = _FakeStore(_rows(3), redis=redis)
    purges.fail.add("j0001")
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=2,
        failed_asset_expiry_dry_run=False,
    )
    out = await _reaper._run_failed_asset_expiry_once()
    assert out["purged"] == 2
    assert out["requeued"] == 1
    assert redis.sets[_reaper._FAILED_ASSET_EXPIRY_RETRY_KEY] == {"j0001"}

    # Next tick drains the retry set first; now that the tier is healthy it
    # succeeds and the set empties.
    purges.fail.clear()
    purges.clear()
    redis.kv.pop(_reaper._FAILED_ASSET_EXPIRY_LOCK_KEY, None)
    out = await _reaper._run_failed_asset_expiry_once()
    assert purges == ["j0001"]
    assert out["retried"] == 1
    assert not redis.sets[_reaper._FAILED_ASSET_EXPIRY_RETRY_KEY]


@pytest.mark.asyncio
async def test_redis_lock_elects_one_hub_per_tick(monkeypatch, purges):
    redis = _FakeRedis()
    store = _FakeStore(_rows(3), redis=redis)
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=2,
        failed_asset_expiry_dry_run=False,
    )
    first, second = await asyncio.gather(
        _reaper._run_failed_asset_expiry_once(),
        _reaper._run_failed_asset_expiry_once(),
    )
    assert sorted([first["purged"], second["purged"]]) == [0, 3]
    assert len(purges) == 3


@pytest.mark.asyncio
async def test_batching_walks_the_whole_window(monkeypatch, purges):
    """More candidates than one batch: the cursor must keep stepping instead of
    stalling on the first BATCH rows."""
    monkeypatch.setattr(_reaper, "_FAILED_ASSET_EXPIRY_BATCH", 10, raising=False)
    store = _FakeStore(_rows(35), redis=_FakeRedis())
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=2,
        failed_asset_expiry_dry_run=False,
    )
    out = await _reaper._run_failed_asset_expiry_once()
    assert out["purged"] == 35
    assert len(set(purges)) == 35, "no prefix purged twice"


@pytest.mark.asyncio
async def test_cold_start_does_not_walk_the_whole_backlog(monkeypatch, purges):
    """With no watermark the pass looks back only COLD_START_H, not forever.

    Measured on production: [now-24h, now-2h) holds ~176k failed rows whose
    prefixes are mostly already gone, and delete_prefix LISTs four tiers even
    for an empty prefix. Sweeping that on every cold start would pound MinIO
    for hours to reclaim nothing. Anything older is the general retention
    pass's job."""
    monkeypatch.setattr(
        _reaper, "_FAILED_ASSET_EXPIRY_COLD_START_H", 6.0, raising=False
    )
    rows = _rows(3, age_h=4.0) + _rows(4, age_h=20.0, start=300)
    store = _FakeStore(rows, redis=_FakeRedis())
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=2,
        failed_asset_expiry_dry_run=False,
    )
    out = await _reaper._run_failed_asset_expiry_once()
    assert out["purged"] == 3
    assert sorted(purges) == ["j0000", "j0001", "j0002"]


@pytest.mark.asyncio
async def test_watermark_overrides_cold_start_floor(monkeypatch, purges):
    """A stored watermark NEWER than the cold-start floor must win, so a pass
    that is keeping up never re-reads rows it already handled."""
    redis = _FakeRedis()
    rows = _rows(6, age_h=4.0)
    redis.kv[_reaper._FAILED_ASSET_EXPIRY_WM_KEY] = (
        rows[3][1].strftime(_reaper._WM_FMT)
    )
    store = _FakeStore(rows, redis=redis)
    _install(
        monkeypatch, store,
        failed_asset_expiry_enabled=True,
        failed_asset_expiry_hours=2,
        failed_asset_expiry_dry_run=False,
    )
    out = await _reaper._run_failed_asset_expiry_once()
    assert out["purged"] == 3
    assert sorted(purges) == ["j0003", "j0004", "j0005"]
