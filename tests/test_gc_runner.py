"""server/hub/tools/gc_runner.py: the resident, sharded jobs-table GC.

Pins the contracts the 2026-11-03 drain relied on:

* shard i of N owns exactly the rows with ``CRC32(job_id) % N == i`` and only
  shard 0 holds the fleet lock key (so the in-hub loops stay out);
* the runner KEEPS its lock across passes (refresh if ours, SET NX otherwise)
  and the stock release becomes a no-op -- the gap the stock cooldown opened
  let a hub loop take the key for a 10-minute pass while the runner spun on 0;
* shutdown deletes only OUR key, never one a hub has since taken.
"""

from datetime import datetime

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


# ── heartbeat (2026-11-04) ──────────────────────────────────────────────────
# "alive but deleting nothing" was invisible: a rolled-back _reaper.py made
# every pass raise, the container stayed Up, and only the lag tank eventually
# turned red. The row each shard upserts is what the control plane reads.


class _HbCur:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params=None):
        if self.sink.get("raise"):
            raise RuntimeError("db down")
        self.sink.setdefault("calls", []).append((sql, params))


class _HbConn:
    def __init__(self, sink):
        self.sink = sink
        self.committed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def cursor(self):
        return _HbCur(self.sink)

    async def commit(self):
        self.sink["commits"] = self.sink.get("commits", 0) + 1


class _HbPool:
    def __init__(self):
        self.sink: dict = {}

    def acquire(self):
        return _HbConn(self.sink)


def test_heartbeat_ddl_and_upsert_use_utc_only():
    """MariaDB NOW() on .20 is JST; every column here is compared to UTC
    ``jobs.completed_at`` and read by a UTC-based control plane."""
    assert "NOW()" not in gc_runner.HEARTBEAT_DDL
    assert "NOW()" not in gc_runner.HEARTBEAT_UPSERT
    assert "UTC_TIMESTAMP()" in gc_runner.HEARTBEAT_UPSERT
    assert "CREATE TABLE IF NOT EXISTS" in gc_runner.HEARTBEAT_DDL
    assert "PRIMARY KEY (shard)" in gc_runner.HEARTBEAT_DDL
    for col in ("shard", "shards_total", "hub_id", "state", "state_since",
                "last_pass_at", "beat_at", "rows_total", "rate_per_min", "cutoff_at"):
        assert col in gc_runner.HEARTBEAT_DDL
    # state_since must be assigned before state so the IF() sees the OLD state
    up = gc_runner.HEARTBEAT_UPSERT
    assert up.index("state_since  = IF(state <> VALUES(state)") < up.index("state        = VALUES(state)")


@pytest.mark.asyncio
async def test_heartbeat_row_tracks_progress_and_cutoff(monkeypatch):
    monkeypatch.setattr(_reaper, "_last_retention_cutoff", None)
    pool = _HbPool()
    hb = gc_runner.Heartbeat(pool, shard=1, n_shards=3, hub_id="gc-runner-s1")
    assert await hb.beat("starting") is True
    sql, params = pool.sink["calls"][-1]
    assert sql == gc_runner.HEARTBEAT_UPSERT
    assert params[:4] == [1, 3, "gc-runner-s1", "starting"]
    assert params[4] is None            # no pass yet
    assert params[5] == 0 and params[7] is None
    assert pool.sink["commits"] == 1    # aiomysql pool is not autocommit

    cutoff = datetime(2026, 11, 2, 3, 4, 5)
    monkeypatch.setattr(_reaper, "_last_retention_cutoff", cutoff)
    hb.progressed(5000)
    assert await hb.beat("pass") is True
    _, params = pool.sink["calls"][-1]
    assert params[3] == "pass"
    assert isinstance(params[4], datetime)      # last_pass_at moved
    assert params[5] == 5000 and params[6] >= 0
    assert params[7] == cutoff                  # the cutoff actually used
    assert hb.last_state == "pass" and hb.writes == 2

    # idle keeps last_pass_at (progress time), only beat_at (server-side) moves
    before = params[4]
    assert await hb.beat("idle") is True
    _, params = pool.sink["calls"][-1]
    assert params[3] == "idle" and params[4] == before and params[5] == 5000


@pytest.mark.asyncio
async def test_heartbeat_failure_never_raises():
    pool = _HbPool()
    pool.sink["raise"] = True
    hb = gc_runner.Heartbeat(pool, 0, 3, "gc-runner")
    assert await hb.beat("idle") is False
    assert hb.failures == 1 and hb.writes == 0
    assert await gc_runner.ensure_heartbeat_table(pool) is False
    with pytest.raises(ValueError):
        hb.params("bogus")


def test_state_after_pass_distinguishes_fail_closed(monkeypatch):
    monkeypatch.setattr(_reaper, "_last_retention_skip_reason", None)
    assert gc_runner.state_after_pass(5000) == "pass"
    assert gc_runner.state_after_pass(0) == "idle"
    monkeypatch.setattr(_reaper, "_last_retention_skip_reason", "lock_held")
    assert gc_runner.state_after_pass(0) == "idle"
    monkeypatch.setattr(_reaper, "_last_retention_skip_reason", "fail_closed")
    assert gc_runner.state_after_pass(0) == "fail_closed"
    # progress wins even if a stale reason is lying around
    assert gc_runner.state_after_pass(1) == "pass"


@pytest.mark.asyncio
async def test_reaper_exposes_skip_reason_and_cutoff(monkeypatch):
    """The runner can't tell a drained 0 from a fail-closed 0 by return value;
    _reaper sets the module-level reason on every early return and records
    the cutoff it actually used on a normal pass."""
    from datetime import timedelta

    from server.hub._state import state

    class _Settings(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    class _Store:
        _r = None

        async def list_deletable_job_ids(self, cutoff, statuses, limit):
            return []

    monkeypatch.setattr(state, "store", _Store())
    monkeypatch.setattr(state, "settings", _Settings(job_retention_enabled=False))
    monkeypatch.setattr(_reaper, "_last_retention_skip_reason", "stale")
    monkeypatch.setattr(_reaper, "_last_retention_cutoff", None)
    assert await _reaper._run_job_retention_once() == 0
    assert _reaper._last_retention_skip_reason == "disabled"

    monkeypatch.setattr(state, "settings",
                        _Settings(job_retention_enabled=True, job_retention_days=2))

    async def _no_cutoff(cutoff, who):
        return None

    monkeypatch.setattr(_reaper, "_watermark_clamp_cutoff", _no_cutoff)
    assert await _reaper._run_job_retention_once() == 0
    assert _reaper._last_retention_skip_reason == "fail_closed"
    assert _reaper._last_retention_cutoff is None

    async def _pass_through(cutoff, who):
        return cutoff

    monkeypatch.setattr(_reaper, "_watermark_clamp_cutoff", _pass_through)
    assert await _reaper._run_job_retention_once() == 0
    assert _reaper._last_retention_skip_reason is None       # ran normally, drained
    c = _reaper._last_retention_cutoff
    assert c is not None
    assert abs((datetime.utcnow() - timedelta(days=2)) - c) < timedelta(minutes=1)
