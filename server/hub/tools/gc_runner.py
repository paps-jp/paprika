"""Resident job-retention runner: the fleet's jobs-table GC, outside the hub.

Why a separate process (2026-11-03)
-----------------------------------
``_reaper._job_retention_loop`` runs INSIDE the hub, where every purge queues
behind the asset uploads for the S3 thread pool and the event loop: a 20k pass
took 689 s in-hub against 120 s standalone. Capped at 20k per 20-minute tick it
drains ~2.4M rows/day, which is roughly the inflow (2.2M/day measured 11-03),
so any slow day leaves a residue that never gets caught up -- the table reached
52.8M rows / 120 GB before anyone noticed. This runner calls the very same
``_run_job_retention_once`` back to back in its own process: own event loop,
own aiomysql pool, own S3 pool, no per-tick sleep.

Sharding
--------
One runner is GIL-bound (~120% CPU, 7.8k rows/min) long before MinIO or
MariaDB are. ``GC_SHARDS=N GC_SHARD=i`` makes this process own only the rows
with ``CRC32(job_id) % N == i``: run N such processes (one per hub CT) and the
rate scales linearly (12 shards: 87k/min measured). Every residue is owned by
exactly one process, so changing N re-partitions without overlap or gaps.

Lock
----
Shard 0 holds the fleet key ``paprika:retention:lock`` (hub ID ``gc-runner``)
for as long as it runs, refreshing the TTL every pass, so the 20 in-hub loops
skip their ticks: the hub loop is thereby demoted to an automatic FALLBACK that
resumes only when no runner holds the lock. Other shards hold
``paprika:retention:lock:s<i>``. SIGTERM cancels the in-flight pass, deletes
our own key and exits within seconds; the stock release (which leaves a
cooldown TTL that would block our own re-acquire) is replaced by a no-op.

The TTL (``GC_LOCK_TTL_S``, default 600) is the crash safety net: if every
runner dies without running its ``finally`` (host reset, OOM kill) the in-hub
loops stay demoted until the key lapses. A pass is ~40 s, so 10 minutes is
plenty of refresh margin while bounding the "nobody is deleting" window; the
original 3600 would have blocked the fallback for an hour.

Heartbeat (2026-11-04)
----------------------
``restart: unless-stopped`` restarts a runner that exits, but not one that is
alive and deleting nothing: on 2026-11-04 a deploy rolled ``_reaper.py`` back
and every pass died with AttributeError, and the only signal was the Flow
retention-lag tank slowly turning red. Each shard therefore upserts ONE row
into ``paprika.gc_runner_heartbeat`` (created on start if missing) at start
(``starting``), after every pass that deleted rows (``pass``), on every idle
tick (``idle``) and whenever the watermark clamp refused to pick a cutoff
(``fail_closed``). ``beat_at`` moves on every write, ``last_pass_at`` only on
progress, ``state_since`` only on a state change, ``cutoff_at`` is the cutoff
the last pass actually used. The pipeline control plane (``pipeline/api/flow.py``
``_gc_runner_alerts``) reads it and raises: beat older than 3 min = runner
dead/hung; beat fresh but no pass for 15 min while the lag tank is > 60 min =
alive-but-stuck; ``fail_closed`` for 10 min = GC paused waiting for a consumer.
A write failure is a WARNING and never stops the GC.

Config (env; all optional)
--------------------------
GC_SHARDS / GC_SHARD           partition count / this process's index
GC_PASS_CAP                    rows per pass (default 5000: a pass is ~40 s, so
                               a stop request is honoured quickly and the
                               per-pass log line is a usable rate meter)
GC_DELETE_CONCURRENCY          parallel purges within a pass (default 48)
GC_IDLE_S                      sleep when a pass finds nothing (default 30)
GC_LOCK_TTL_S                  crash safety net on the lock (default 600; see
                               "Lock" above)
PAPRIKA_REDIS_URL              default redis://10.10.50.34:6379
PAPRIKA_DATA_DIR               default /data/jobs (hub-data volume with
                               settings.json -> MariaDB credentials)

Run: ``python -m server.hub.tools.gc_runner`` in the paprika-hub image with the
hub's env_file and volumes (deploy/scale/docker-compose.gc-runner.yml).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("gc_runner")

from server.hub import _pull_queue, _reaper  # noqa: E402
from server.hub._invalidate import _BOOTSTRAP_KEYS  # noqa: E402
from server.hub._state import config, get_storage_dir, state  # noqa: E402
from server.hub.mariadb import create_pool, load_settings  # noqa: E402
from server.hub.settings import SettingsRegistry  # noqa: E402
from server.store import make_store  # noqa: E402

FLEET_LOCK_KEY = "paprika:retention:lock"
DEFAULT_LOCK_TTL_S = 600

HEARTBEAT_TABLE = "gc_runner_heartbeat"
# All timestamps are UTC (``UTC_TIMESTAMP()`` / ``datetime.utcnow()``): the
# fleet's MariaDB runs with a JST session zone, so ``NOW()`` would be 9 h off
# the UTC ``jobs.completed_at`` column this GC keys on. ``state_since`` is kept
# server-side so "fail_closed for 10 minutes" needs no reader-side history.
HEARTBEAT_DDL = f"""
CREATE TABLE IF NOT EXISTS {HEARTBEAT_TABLE} (
  shard        TINYINT NOT NULL,
  shards_total TINYINT NOT NULL,
  hub_id       VARCHAR(32) NOT NULL,
  state        ENUM('starting','pass','idle','fail_closed') NOT NULL,
  state_since  DATETIME NOT NULL,
  last_pass_at DATETIME NULL,
  beat_at      DATETIME NOT NULL,
  rows_total   BIGINT NOT NULL DEFAULT 0,
  rate_per_min INT NOT NULL DEFAULT 0,
  cutoff_at    DATETIME NULL,
  PRIMARY KEY (shard)
) ENGINE=InnoDB
"""
# ``state_since`` is assigned BEFORE ``state`` on purpose: ON DUPLICATE KEY
# UPDATE evaluates left to right, so the comparison still sees the OLD state.
HEARTBEAT_UPSERT = f"""
INSERT INTO {HEARTBEAT_TABLE}
  (shard, shards_total, hub_id, state, state_since, last_pass_at, beat_at,
   rows_total, rate_per_min, cutoff_at)
VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(), %s, UTC_TIMESTAMP(), %s, %s, %s)
ON DUPLICATE KEY UPDATE
  state_since  = IF(state <> VALUES(state), UTC_TIMESTAMP(), state_since),
  state        = VALUES(state),
  shards_total = VALUES(shards_total),
  hub_id       = VALUES(hub_id),
  last_pass_at = VALUES(last_pass_at),
  beat_at      = UTC_TIMESTAMP(),
  rows_total   = VALUES(rows_total),
  rate_per_min = VALUES(rate_per_min),
  cutoff_at    = VALUES(cutoff_at)
"""
HEARTBEAT_STATES = ("starting", "pass", "idle", "fail_closed")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def shard_settings() -> tuple[int, int]:
    """(GC_SHARDS, GC_SHARD) validated: 1 <= N, 0 <= i < N."""
    n = max(1, _env_int("GC_SHARDS", 1))
    i = _env_int("GC_SHARD", 0)
    if not 0 <= i < n:
        raise SystemExit(f"GC_SHARD={i} out of range for GC_SHARDS={n}")
    return n, i


def hub_id_for(shard: int) -> str:
    return "gc-runner" if shard == 0 else f"gc-runner-s{shard}"


def lock_key_for(shard: int) -> str:
    return FLEET_LOCK_KEY if shard == 0 else f"{FLEET_LOCK_KEY}:s{shard}"


def sharded_candidate_sql(n_shards: int, status_count: int) -> str:
    ph = ",".join(["%s"] * status_count)
    return (
        f"SELECT job_id FROM jobs WHERE completed_at < %s AND status IN ({ph}) "
        f"AND CRC32(job_id) %% %s = %s ORDER BY completed_at ASC LIMIT %s"
    )


def make_sharded_lister(pool, n_shards: int, shard: int):
    """Drop-in for ``store.list_deletable_job_ids`` restricted to our residue.

    ``job_id`` is the PK and therefore part of ``idx_completed_at_status``, so
    the CRC32 filter is evaluated on the covering index scan -- same cost as
    the unsharded query, just N times fewer rows returned."""

    async def _list(cutoff, status_in: list[str], limit: int) -> list[str]:
        if not status_in or int(limit) <= 0:
            return []
        sql = sharded_candidate_sql(n_shards, len(status_in))
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, [cutoff, *status_in, n_shards, shard, int(limit)])
                return [r[0] for r in await cur.fetchall()]

    return _list


def install_lock_hold(hub_id: str) -> None:
    """Hold the pass lock ACROSS passes instead of handing it back each time.

    The stock release shortens the key to a cooldown TTL; in that window one of
    the hub loops wins the key and runs a slow in-hub pass while we spin on 0
    (observed 2026-11-03 03:39). Here: acquire = "refresh if it is ours, else
    SET NX", release = no-op. The key is deleted explicitly on shutdown."""
    orig_acquire = _reaper._acquire_pass_lock

    async def acquire_or_refresh(r, key: str, ttl_s: float) -> bool:
        try:
            if r is not None:
                holder = await r.get(key)
                if holder is not None and _as_str(holder) == hub_id:
                    await r.expire(key, max(60, int(ttl_s)))
                    return True
        except Exception:
            log.debug("lock refresh failed", exc_info=True)
        return await orig_acquire(r, key, ttl_s)

    async def keep_holding(r, key: str, cooldown_s: float) -> None:
        return None

    _reaper._acquire_pass_lock = acquire_or_refresh
    _reaper._release_pass_lock = keep_holding


def _as_str(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


async def release_own_lock(r, key: str, hub_id: str) -> bool:
    if r is None:
        return False
    try:
        holder = await r.get(key)
        if holder is not None and _as_str(holder) == hub_id:
            await r.delete(key)
            return True
    except Exception:
        log.warning("could not release %s", key, exc_info=True)
    return False


async def ensure_heartbeat_table(pool) -> bool:
    """``CREATE TABLE IF NOT EXISTS`` for the heartbeat row. The paprika DB
    user owns the schema, so this is the whole migration; the control plane
    reads via the ``delian`` user, which needs a one-off
    ``GRANT SELECT ON paprika.gc_runner_heartbeat TO 'delian'@'%'``
    (deploy/scale/gc_runner_heartbeat.sql)."""
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(HEARTBEAT_DDL)
        return True
    except Exception:
        log.warning("heartbeat table could not be created; GC continues", exc_info=True)
        return False


class Heartbeat:
    """Per-shard liveness row writer. Tracks what the row needs between
    writes (total rows, last progress time) so ``main()`` only says WHICH
    state it is in. Never raises: a heartbeat failure must not stop the GC."""

    def __init__(self, pool, shard: int, n_shards: int, hub_id: str):
        self.pool = pool
        self.shard, self.n_shards, self.hub_id = shard, n_shards, hub_id
        self.total = 0
        self.t0 = time.monotonic()
        self.last_pass_at: datetime | None = None
        self.last_state: str | None = None
        self.writes = 0
        self.failures = 0

    def progressed(self, n: int) -> None:
        """Account for a pass that deleted ``n`` rows (n > 0)."""
        self.total += n
        self.last_pass_at = datetime.utcnow()

    def rate_per_min(self) -> int:
        el = time.monotonic() - self.t0
        return int(self.total / el * 60) if el > 0 else 0

    def params(self, state: str) -> list:
        if state not in HEARTBEAT_STATES:
            raise ValueError(f"unknown heartbeat state {state!r}")
        return [
            self.shard,
            self.n_shards,
            self.hub_id,
            state,
            self.last_pass_at,
            self.total,
            self.rate_per_min(),
            _reaper._last_retention_cutoff,
        ]

    async def beat(self, state: str) -> bool:
        try:
            params = self.params(state)
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(HEARTBEAT_UPSERT, params)
                    # aiomysql pools are autocommit=False by default.
                    await conn.commit()
            self.writes += 1
            self.last_state = state
            return True
        except Exception:
            self.failures += 1
            # WARNING, not debug: a silent heartbeat failure is itself the
            # blind spot this exists to remove (the reader sees "no beat" and
            # raises crit, which is the right outcome -- but say why here).
            log.warning("heartbeat write failed (state=%s)", state, exc_info=True)
            return False


def state_after_pass(n: int) -> str:
    """Heartbeat state for a finished pass: progress is ``pass``; a 0 that the
    watermark clamp caused is ``fail_closed`` (operator-visible: GC paused
    waiting for a consumer); any other 0 (drained, lock held by a hub loop,
    retention switched off, pass raised) is ``idle`` -- those are told apart
    by the reader via ``last_pass_at`` going stale while the lag tank grows."""
    if n > 0:
        return "pass"
    if _reaper._last_retention_skip_reason == "fail_closed":
        return "fail_closed"
    return "idle"


async def retention_lag_s(pool, days: int) -> float | None:
    """How far the oldest terminal row sits PAST the cutoff, in seconds.

    0 = drained; growing = the fleet is not keeping up. This is the signal an
    autoscaler or submit gate should watch (not the row count, which changes
    meaning with every inflow change)."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    statuses = list(_reaper._RETENTION_STATUSES)
    ph = ",".join(["%s"] * len(statuses))
    sql = f"SELECT MIN(completed_at) FROM jobs WHERE status IN ({ph})"
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, statuses)
                row = await cur.fetchone()
    except Exception:
        log.debug("lag query failed", exc_info=True)
        return None
    if not row or row[0] is None:
        return 0.0
    return max(0.0, (cutoff - row[0]).total_seconds())


async def bootstrap(n_shards: int, shard: int):
    config.data_dir = Path(os.environ.get("PAPRIKA_DATA_DIR", "/data/jobs"))
    config.redis_url = os.environ.get("PAPRIKA_REDIS_URL", "redis://10.10.50.34:6379")
    config.hub_id = hub_id_for(shard)
    state.settings = s = SettingsRegistry(config.data_dir)
    pool = await create_pool(
        host=s.get("mariadb_host", ""),
        port=int(s.get("mariadb_port", 3306)),
        database=s.get("mariadb_database", "paprika"),
        username=s.get("mariadb_username", ""),
        password=s.get("mariadb_password", ""),
    )
    # DB-backed settings overlay (objstore endpoints etc.); without it the S3
    # client falls back to the dead env endpoint.
    loaded = await load_settings(pool)
    s.update({k: v for k, v in loaded.items() if k not in _BOOTSTRAP_KEYS})
    store, kind = await make_store(
        config.redis_url, mariadb_pool=pool, storage_dir_fn=get_storage_dir
    )
    if kind != "mariadb":
        raise SystemExit(f"store kind {kind!r}, refusing (needs the MariaDB store)")
    state.store, state.mariadb_pool = store, pool
    if n_shards > 1:
        store.list_deletable_job_ids = make_sharded_lister(pool, n_shards, shard)
    _reaper._RETENTION_LOCK_KEY = lock_key_for(shard)
    _reaper._RETENTION_MAX_PER_TICK = max(500, _env_int("GC_PASS_CAP", 5000))
    _reaper._RETENTION_DELETE_CONCURRENCY = max(1, _env_int("GC_DELETE_CONCURRENCY", 48))
    _reaper._RETENTION_LOCK_TTL_S = max(
        60, _env_int("GC_LOCK_TTL_S", DEFAULT_LOCK_TTL_S)
    )
    install_lock_hold(config.hub_id)
    return store, pool


async def main() -> int:
    n_shards, shard = shard_settings()
    idle_s = max(1, _env_int("GC_IDLE_S", 30))
    store, pool = await bootstrap(n_shards, shard)
    s = state.settings
    log.info(
        "ready: hub_id=%s shard=%d/%d lock=%s cap=%d conc=%d days=%s enabled=%s pull_dispatch=%s",
        config.hub_id,
        shard,
        n_shards,
        _reaper._RETENTION_LOCK_KEY,
        _reaper._RETENTION_MAX_PER_TICK,
        _reaper._RETENTION_DELETE_CONCURRENCY,
        s.get("job_retention_days"),
        s.get("job_retention_enabled"),
        _pull_queue.ENABLED,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    r = getattr(store, "_r", None)
    hb = Heartbeat(pool, shard, n_shards, config.hub_id)
    await ensure_heartbeat_table(pool)
    await hb.beat("starting")
    total, t0 = 0, time.monotonic()
    last_lag_log = 0.0
    rc = 0
    try:
        while not stop.is_set():
            t = time.monotonic()
            pass_task = asyncio.ensure_future(_reaper._run_job_retention_once())
            stop_task = asyncio.ensure_future(stop.wait())
            done, _ = await asyncio.wait(
                {pass_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if pass_task not in done:
                pass_task.cancel()
                try:
                    await pass_task
                except (asyncio.CancelledError, Exception):
                    pass
                break
            stop_task.cancel()
            try:
                n = pass_task.result()
            except Exception:
                log.warning("pass failed", exc_info=True)
                n = 0
            total += n
            el = time.monotonic() - t0
            if n:
                hb.progressed(n)
                await hb.beat("pass")
                log.info(
                    "pass: %d in %.0fs | total %d in %.0fs = %.0f/min",
                    n,
                    time.monotonic() - t,
                    total,
                    el,
                    total / el * 60 if el else 0,
                )
                if n >= _reaper._RETENTION_MAX_PER_TICK:
                    continue  # backlog: go straight into the next pass
                # Short pass = caught up; rows cross the cutoff at the inflow
                # rate (~25/s), so a brief nap batches them instead of a
                # candidate SELECT every half second for a handful of rows.
                try:
                    await asyncio.wait_for(stop.wait(), min(10, idle_s))
                except TimeoutError:
                    pass
                continue
            # Nothing to do: either drained for this cutoff window or another
            # holder has the key (a hub loop, or a previous incarnation whose
            # TTL has not lapsed). Poll fast in the latter case.
            holder = None
            if r is not None:
                try:
                    holder = _as_str(await r.get(_reaper._RETENTION_LOCK_KEY) or "") or None
                except Exception:
                    holder = None
            if holder and holder != config.hub_id:
                # Another holder (a hub loop's pass): poll fast to win the
                # release gap, but don't hammer MariaDB with the lag probe
                # (a MIN() range scan, ~2 s on a 10M-row table) every 2 s.
                if time.monotonic() - last_lag_log >= idle_s:
                    last_lag_log = time.monotonic()
                    await hb.beat(state_after_pass(0))
                    log.info("idle: total %d | waiting for lock holder %s", total, holder)
                wait = 2
            else:
                await hb.beat(state_after_pass(0))
                days = int(s.get("job_retention_days", 10) or 10)
                lag = await retention_lag_s(pool, days)
                last_lag_log = time.monotonic()
                log.info(
                    "idle: total %d | lag_s=%s holder=%s",
                    total,
                    "?" if lag is None else f"{lag:.0f}",
                    holder,
                )
                wait = idle_s
            try:
                await asyncio.wait_for(stop.wait(), wait)
            except TimeoutError:
                pass
    except Exception:
        log.exception("runner crashed")
        rc = 1
    finally:
        released = await release_own_lock(r, _reaper._RETENTION_LOCK_KEY, config.hub_id)
        log.info("stopped: total %d, lock released=%s, heartbeats=%d (failed %d)",
                 total, released, hb.writes, hb.failures)
        try:
            pool.close()
            await pool.wait_closed()
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
