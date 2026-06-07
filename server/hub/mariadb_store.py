"""MariaDB-backed JobStore implementation.

When the operator configures MariaDB connection in the Settings tab,
the hub uses this store instead of the Redis-backed one.  Live log
pub/sub still uses Redis (MariaDB has no native pub/sub); job info /
results go to MariaDB.

Log lines go to **disk** (``{storage_dir}/{job_id}/log.txt``) rather
than MariaDB. Logs are append-only telemetry that's either replayed
sequentially (full log dump for ``GET /jobs/{id}/log.txt``) or
streamed live via Redis pub/sub — never queried by range/filter — so
a flat file matches the access pattern at constant cost regardless
of total size. Pushing them out of MariaDB also unloads the largest
table by far (at ~3K jobs the ``job_logs`` table was already 365 MB
of ~2M rows — at 200K jobs it would be tens of GB).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


class MariaDBJobStore:
    """Persistent job store backed by MariaDB.

    ``pool`` is an ``aiomysql.Pool``.
    ``redis_url`` is optional; when given, live log pub/sub uses Redis.
    ``storage_dir_fn`` is a zero-arg callable returning the current
    storage root (resolved late so storage_dir changes are picked up). When
    provided, log lines persist to ``{root}/{job_id}/log.txt`` instead
    of the MariaDB ``job_logs`` table.
    """

    def __init__(
        self,
        pool: Any,
        redis_url: str | None = None,
        storage_dir_fn: Callable[[], Path] | None = None,
    ) -> None:
        self._pool = pool
        self._redis_url = redis_url
        self._storage_dir_fn = storage_dir_fn
        self._r: Any = None          # redis.asyncio.Redis (for pub/sub)
        self._pubsub_r: Any = None   # separate client for subscribe
        # Per-job asyncio.Lock so concurrent appenders to the same
        # file serialise their writes (POSIX append is line-atomic only
        # below PIPE_BUF; long log lines from codegen scripts can exceed
        # that). Locks evict naturally when the job is deleted.
        self._log_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        # Test MariaDB connectivity
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")

        # Init Redis for pub/sub if available. make_redis_client handles both
        # plain redis:// and Sentinel (redis+sentinel://) URLs transparently
        # -- control-plane phase 4 (Redis HA); plain URLs are unchanged.
        if self._redis_url:
            try:
                from server.store import make_redis_client
                self._r = make_redis_client(self._redis_url, decode_responses=True)
                self._pubsub_r = make_redis_client(self._redis_url, decode_responses=True)
                await self._r.ping()
            except Exception as e:
                log.warning("Redis pub/sub unavailable: %s (live logs disabled)", e)
                self._r = None
                self._pubsub_r = None

    async def close(self) -> None:
        if self._r is not None:
            try:
                await self._r.aclose()
            except Exception:
                pass
        if self._pubsub_r is not None:
            try:
                await self._pubsub_r.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Job info
    # ------------------------------------------------------------------ #

    async def save_job_info(self, info: Any) -> None:
        payload = info.model_dump_json()
        ts = info.created_at.timestamp() if info.created_at else 0.0
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO jobs
                       (job_id, status, url, mode, goal, options,
                        worker_id, lane_idx, session_id, owner_id,
                        created_at, started_at, completed_at,
                        error, progress)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE
                         status=VALUES(status), url=VALUES(url),
                         mode=VALUES(mode), goal=VALUES(goal),
                         options=VALUES(options),
                         worker_id=VALUES(worker_id),
                         lane_idx=VALUES(lane_idx),
                         session_id=VALUES(session_id),
                         started_at=VALUES(started_at),
                         completed_at=VALUES(completed_at),
                         error=VALUES(error),
                         progress=VALUES(progress)""",
                    (
                        info.job_id,
                        info.status.value if hasattr(info.status, "value") else str(info.status),
                        info.url,
                        info.options.mode if info.options else "fetch",
                        info.options.goal if info.options else None,
                        _json_dumps(info.options.model_dump() if info.options else None),
                        info.worker_id,
                        info.lane_idx,
                        info.session_id,
                        getattr(info, "owner_id", None) or "default",
                        _parse_dt(info.created_at),
                        _parse_dt(info.started_at),
                        _parse_dt(info.completed_at),
                        info.error,
                        _json_dumps(info.progress.model_dump() if info.progress else None),
                    ),
                )

    async def get_job_info(self, job_id: str) -> Any:
        from server.protocol import JobInfo, JobOptions, JobProgress

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT job_id, status, url, mode, goal, options, "
                    "worker_id, lane_idx, session_id, "
                    "created_at, started_at, completed_at, error, progress, "
                    "owner_id "
                    "FROM jobs WHERE job_id=%s", (job_id,))
                row = await cur.fetchone()
        if not row:
            return None
        return _row_to_job_info(row)

    async def claim_queued_job(
        self, job_id: str, worker_id: str, started_at: Any
    ) -> bool:
        """Atomically transition a job ``queued`` -> ``running`` for redrive
        dispatch. Returns True iff THIS call won the claim (the UPDATE matched
        a still-``queued``, still-UNASSIGNED row).

        Cross-hub safe: when several hubs' redrive loops race for the same
        queued job, only ONE hub's UPDATE matches and flips it -- the losers
        get rowcount 0 and skip. This is the dispatch mutex for
        server/hub/_redrive.py (no Redis lease needed; the SoT row IS the lock).
        The ``worker_id IS NULL`` guard ALSO makes it safe against a live
        ``POST /jobs`` handler: POST records a ``worker_id`` the instant it
        hands a job to a worker (even while status is still ``queued``, before
        the worker reports ``running``), so this claim can never steal a job
        that POST already dispatched. The caller sends the worker assignment
        only on a True return, and calls :meth:`release_claimed_job` to revert
        if that send fails."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE jobs SET status='running', worker_id=%s, "
                    "started_at=%s WHERE job_id=%s AND status='queued' "
                    "AND worker_id IS NULL",
                    (worker_id, _parse_dt(started_at), job_id),
                )
                return cur.rowcount == 1

    async def release_claimed_job(self, job_id: str) -> bool:
        """Revert a redrive claim (``running`` -> ``queued``) when the worker
        send failed, so a later pass can retry it. Only flips a row that is
        still ``running`` -- if the worker already picked it up and the job
        moved on (completed/failed), this is a no-op (rowcount 0)."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE jobs SET status='queued', worker_id=NULL, "
                    "started_at=NULL WHERE job_id=%s AND status='running'",
                    (job_id,),
                )
                return cur.rowcount == 1

    async def list_job_ids(
        self, offset: int = 0, limit: int = 0
    ) -> list[str]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                if limit > 0:
                    await cur.execute(
                        "SELECT job_id FROM jobs "
                        "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                        (limit, offset))
                else:
                    await cur.execute(
                        "SELECT job_id FROM jobs "
                        "ORDER BY created_at DESC")
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def count_jobs(self) -> int:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM jobs")
                row = await cur.fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------ #
    # Bulk hydration — SQL-pushdown shortcut for the admin UI
    # ------------------------------------------------------------------ #
    #
    # Without these, the admin UI's "filter by status" path does an N+1
    # walk: list_job_ids() returns every ID in the DB, then list_jobs()
    # hits get_job_info() for each ID (one round-trip per row). At
    # 2,000+ jobs this is ~2 seconds per /jobs?status=... call and the
    # /jobs/counts poll repeats the same scan every 2 seconds. These
    # helpers push the filter + projection into a single SELECT so the
    # admin UI sees <50 ms responses.

    async def list_job_infos(
        self,
        *,
        offset: int = 0,
        limit: int = 0,
        status: list[str] | None = None,
        mode: list[str] | None = None,
        url_substr: str | None = None,
        owner_id: str | None = None,
    ) -> tuple[list[Any], int]:
        """Return (infos, total_matching) in a single hydration query.

        * ``status`` / ``mode`` — case-insensitive IN-filter lists.
          Empty/None means no filter on that column.
        * ``url_substr`` — case-insensitive substring match on url.
        * ``limit=0`` — return everything matching.

        Result rows are sorted ``created_at DESC`` (newest first), which
        matches the admin UI's expectation.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            placeholders = ",".join(["%s"] * len(status))
            clauses.append(f"status IN ({placeholders})")
            params.extend(s.lower() for s in status)
        if mode:
            placeholders = ",".join(["%s"] * len(mode))
            clauses.append(f"mode IN ({placeholders})")
            params.extend(m.lower() for m in mode)
        if url_substr:
            clauses.append("url LIKE %s")
            params.append(f"%{url_substr}%")
        if owner_id:
            clauses.append("owner_id = %s")
            params.append(owner_id)

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Total count for the matching set (separate query so the
                # paged SELECT only carries `limit` rows back over the
                # wire instead of the entire match set).
                count_sql = f"SELECT COUNT(*) FROM jobs {where_sql}"
                await cur.execute(count_sql, tuple(params))
                row = await cur.fetchone()
                total = row[0] if row else 0

                # Paged SELECT.
                select_cols = (
                    "job_id, status, url, mode, goal, options, "
                    "worker_id, lane_idx, session_id, "
                    "created_at, started_at, completed_at, error, progress, "
                    "owner_id"
                )
                if limit > 0:
                    page_sql = (
                        f"SELECT {select_cols} FROM jobs {where_sql} "
                        f"ORDER BY created_at DESC LIMIT %s OFFSET %s"
                    )
                    await cur.execute(
                        page_sql, tuple(params) + (limit, offset),
                    )
                else:
                    page_sql = (
                        f"SELECT {select_cols} FROM jobs {where_sql} "
                        f"ORDER BY created_at DESC"
                    )
                    await cur.execute(page_sql, tuple(params))
                rows = await cur.fetchall()

        infos = [_row_to_job_info(r) for r in rows]
        return infos, total

    async def count_by_status_and_mode(
        self,
        *,
        created_after_ts: float | None = None,
    ) -> tuple[dict[str, int], dict[str, int], int]:
        """Return ``(by_status, by_mode, total)`` for jobs.

        When ``created_after_ts`` is set, restrict to rows whose
        ``created_at >= FROM_UNIXTIME(ts)`` -- used by /jobs/summary
        to compute the ``recent_<window>h`` deltas without hydrating
        any JobInfo. Three GROUP BY queries instead of the N+1
        hydration walk; at 100k rows each is <50ms with the
        ``idx_status_created`` / ``idx_mode_created`` composite
        indexes (created as part of the same migration that added
        the ``status`` / ``mode`` columns).

        Used by ``GET /jobs/summary``.
        """
        by_status: dict[str, int] = {}
        by_mode: dict[str, int] = {}
        total = 0
        where_sql = ""
        where_params: tuple = ()
        if created_after_ts is not None:
            where_sql = " WHERE created_at >= FROM_UNIXTIME(%s)"
            where_params = (float(created_after_ts),)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT COUNT(*) FROM jobs{where_sql}",
                    where_params,
                )
                row = await cur.fetchone()
                total = row[0] if row else 0
                await cur.execute(
                    f"SELECT status, COUNT(*) FROM jobs{where_sql} "
                    "GROUP BY status",
                    where_params,
                )
                for s, n in await cur.fetchall():
                    if s:
                        by_status[s] = int(n)
                await cur.execute(
                    f"SELECT mode, COUNT(*) FROM jobs{where_sql} "
                    "GROUP BY mode",
                    where_params,
                )
                for m, n in await cur.fetchall():
                    by_mode[m or "fetch"] = int(n)
        return by_status, by_mode, total

    async def summary_counts(
        self, *, window_ts: list[float] | None = None,
    ) -> tuple[dict[str, int], dict[str, int], int, list[tuple[dict[str, int], int]]]:
        """One-acquire job summary for ``GET /jobs/summary``.

        Returns ``(by_status, by_mode, total, windows)`` where ``windows[i]`` is
        ``(by_status, total)`` for jobs with ``created_at >=
        FROM_UNIXTIME(window_ts[i])``. Conditional aggregation folds every recent
        window into the ONE GROUP-BY-status query (plus one GROUP-BY-mode), so
        the whole summary is 2 queries on 1 connection instead of the old
        3-queries-per-window (9 queries, ~2 s of round-trips at ~8.5k rows --
        which made the admin "最近のジョブ" tab block until it returned)."""
        wins = [float(t) for t in (window_ts or [])]
        win_sel = "".join(
            f", SUM(created_at >= FROM_UNIXTIME(%s)) AS w{i}"
            for i in range(len(wins))
        )
        by_status: dict[str, int] = {}
        by_mode: dict[str, int] = {}
        total = 0
        windows: list[list] = [[{}, 0] for _ in wins]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT status, COUNT(*) AS c{win_sel} "
                    "FROM jobs GROUP BY status",
                    tuple(wins),
                )
                for row in await cur.fetchall():
                    s = row[0]
                    if not s:
                        continue
                    c = int(row[1] or 0)
                    by_status[s] = c
                    total += c
                    for i in range(len(wins)):
                        wc = int(row[2 + i] or 0)
                        windows[i][0][s] = wc
                        windows[i][1] += wc
                await cur.execute("SELECT mode, COUNT(*) FROM jobs GROUP BY mode")
                for m, n in await cur.fetchall():
                    by_mode[m or "fetch"] = int(n)
        return by_status, by_mode, total, [(w[0], w[1]) for w in windows]

    async def delete_job(self, job_id: str) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                # CASCADE deletes job_results + (any remaining legacy)
                # job_logs rows. The disk-backed log.txt is cleaned up
                # separately below.
                await cur.execute(
                    "DELETE FROM jobs WHERE job_id=%s", (job_id,))
                deleted = cur.rowcount > 0
        # Best-effort: drop the per-job log file and evict its lock.
        # Failure to unlink is logged but not raised — the rest of the
        # job directory (assets, page.html, ...) is the operator's
        # responsibility to GC via the storage-side tooling.
        path = self._log_path(job_id)
        if path is not None:
            try:
                await asyncio.to_thread(path.unlink, True)  # missing_ok=True
            except TypeError:
                # Python <3.8 fallback
                try:
                    await asyncio.to_thread(path.unlink)
                except FileNotFoundError:
                    pass
                except Exception as e:
                    log.debug("unlink %s: %s", path, e)
            except Exception as e:
                log.debug("unlink %s: %s", path, e)
        self._log_locks.pop(job_id, None)
        return deleted

    # ------------------------------------------------------------------ #
    # Job result
    # ------------------------------------------------------------------ #

    async def save_job_result(self, result: Any) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO job_results
                       (job_id, status, html_href, log_href,
                        assets, assets_failed, video_detection,
                        video_urls_seen, iframe_srcs,
                        ytdlp_results, visited_urls, error)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE
                         status=VALUES(status),
                         html_href=VALUES(html_href),
                         log_href=VALUES(log_href),
                         assets=VALUES(assets),
                         assets_failed=VALUES(assets_failed),
                         video_detection=VALUES(video_detection),
                         video_urls_seen=VALUES(video_urls_seen),
                         iframe_srcs=VALUES(iframe_srcs),
                         ytdlp_results=VALUES(ytdlp_results),
                         visited_urls=VALUES(visited_urls),
                         error=VALUES(error)""",
                    (
                        result.job_id,
                        result.status.value if hasattr(result.status, "value") else str(result.status),
                        result.html_href,
                        result.log_href,
                        _json_dumps([a.model_dump() for a in result.assets] if result.assets else []),
                        result.assets_failed,
                        _json_dumps(result.video_detection),
                        _json_dumps(result.video_urls_seen),
                        _json_dumps(result.iframe_srcs),
                        _json_dumps([y.model_dump() for y in result.ytdlp_results] if result.ytdlp_results else []),
                        _json_dumps(result.visited_urls),
                        result.error,
                    ),
                )

    async def get_job_result(self, job_id: str) -> Any:
        from server.protocol import AssetInfo, JobResult, YtdlpResult

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status, html_href, log_href, assets, "
                    "assets_failed, video_detection, video_urls_seen, "
                    "iframe_srcs, ytdlp_results, visited_urls, error "
                    "FROM job_results WHERE job_id=%s", (job_id,))
                rr = await cur.fetchone()
        if not rr:
            return None

        assets = []
        for a in (json.loads(rr[3]) if rr[3] else []):
            try:
                assets.append(AssetInfo(**a))
            except Exception:
                pass
        ytdlp = []
        for y in (json.loads(rr[8]) if rr[8] else []):
            try:
                ytdlp.append(YtdlpResult(**y))
            except Exception:
                pass

        return JobResult(
            job_id=job_id, status=rr[0],
            html_href=rr[1], log_href=rr[2],
            assets=assets, assets_failed=rr[4] or 0,
            video_detection=json.loads(rr[5]) if rr[5] else {},
            video_urls_seen=json.loads(rr[6]) if rr[6] else [],
            iframe_srcs=json.loads(rr[7]) if rr[7] else [],
            ytdlp_results=ytdlp,
            visited_urls=json.loads(rr[9]) if rr[9] else [],
            error=rr[10],
        )

    # ------------------------------------------------------------------ #
    # Log lines
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Log persistence — disk-backed when storage_dir_fn is configured,
    # falls back to the legacy MariaDB job_logs table otherwise.
    # ------------------------------------------------------------------ #

    def _log_path(self, job_id: str) -> Path | None:
        """Return ``{storage_dir}/{job_id}/log.txt`` or None if no
        storage root is configured (caller falls back to MariaDB)."""
        if self._storage_dir_fn is None:
            return None
        try:
            return Path(self._storage_dir_fn()) / job_id / "log.txt"
        except Exception:
            return None

    def _log_lock(self, job_id: str) -> asyncio.Lock:
        lock = self._log_locks.get(job_id)
        if lock is None:
            lock = asyncio.Lock()
            self._log_locks[job_id] = lock
        return lock

    @staticmethod
    def _sync_append(path: Path, lines: list[str]) -> None:
        """Blocking append — runs inside ``asyncio.to_thread`` so slow
        storage I/O doesn't stall the event loop. Opens with line buffering
        so partial writes flush even when many short lines arrive."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", buffering=1) as f:
            for line in lines:
                if not line.endswith("\n"):
                    line = line + "\n"
                f.write(line)

    @staticmethod
    def _sync_read(path: Path) -> list[str]:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f]

    async def append_log_line(self, job_id: str, line: str) -> None:
        path = self._log_path(job_id)
        if path is not None:
            async with self._log_lock(job_id):
                await asyncio.to_thread(self._sync_append, path, [line])
            return
        # Legacy fallback: MariaDB job_logs table (in-memory / test only).
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COALESCE(MAX(line_num), -1) + 1 "
                    "FROM job_logs WHERE job_id=%s", (job_id,))
                row = await cur.fetchone()
                next_num = row[0] if row else 0
                await cur.execute(
                    "INSERT INTO job_logs (job_id, line_num, line) "
                    "VALUES (%s, %s, %s)", (job_id, next_num, line))

    async def append_log_lines(self, job_id: str, lines: list[str]) -> None:
        """Batch-append log lines.

        Used by the LogBatcher so the worker WS receive loop is not blocked
        on a per-line round-trip. With disk storage this becomes a single
        ``write()`` call per batch (POSIX coalesces buffered IO); with the
        MariaDB fallback the batch becomes one multi-row INSERT.
        """
        if not lines:
            return
        path = self._log_path(job_id)
        if path is not None:
            async with self._log_lock(job_id):
                await asyncio.to_thread(self._sync_append, path, lines)
            return
        # Legacy fallback: MariaDB job_logs table.
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COALESCE(MAX(line_num), -1) + 1 "
                    "FROM job_logs WHERE job_id=%s", (job_id,))
                row = await cur.fetchone()
                base = row[0] if row else 0
                await cur.executemany(
                    "INSERT INTO job_logs (job_id, line_num, line) "
                    "VALUES (%s, %s, %s)",
                    [(job_id, base + i, ln) for i, ln in enumerate(lines)])

    async def get_log_lines(self, job_id: str) -> list[str]:
        """Return the full log for a job, newest-format first then
        legacy MariaDB rows. Disk is the primary store; rows still
        present in ``job_logs`` (jobs that ran before this migration)
        are concatenated AFTER the file content. The migration helper
        ``server.hub.log_migrate.migrate_logs_to_disk()`` flushes the
        table; once that's run on all live jobs the table can be
        truncated."""
        # Disk first.
        path = self._log_path(job_id)
        disk_lines: list[str] = []
        if path is not None and path.exists():
            try:
                disk_lines = await asyncio.to_thread(self._sync_read, path)
            except Exception as e:
                log.warning("read log file %s failed: %s", path, e)
        # MariaDB legacy rows (empty after migration).
        db_lines: list[str] = []
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT line FROM job_logs WHERE job_id=%s "
                        "ORDER BY line_num", (job_id,))
                    rows = await cur.fetchall()
            db_lines = [r[0] for r in rows]
        except Exception as e:
            log.debug("read job_logs %s failed: %s", job_id, e)
        # Disk wins when both have content (post-migration, MariaDB
        # should be empty for any job whose file exists).
        if disk_lines:
            return disk_lines
        return db_lines

    # ------------------------------------------------------------------ #
    # Pub/Sub  (delegated to Redis — MariaDB has no native pub/sub)
    # ------------------------------------------------------------------ #

    _CHAN_LOG = "paprika:job:{}:log:chan"

    async def publish_log(self, job_id: str, line: str) -> None:
        if self._r is not None:
            await self._r.publish(self._CHAN_LOG.format(job_id), line)

    async def subscribe_log(self, job_id: str) -> AsyncIterator[str]:
        if self._pubsub_r is None:
            return
        chan = self._CHAN_LOG.format(job_id)
        pubsub = self._pubsub_r.pubsub()
        await pubsub.subscribe(chan)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                for line in data.split("\n"):
                    if not line:
                        continue
                    yield line
                    if line == "__JOB_DONE__":
                        return
        finally:
            try:
                await pubsub.unsubscribe(chan)
                await pubsub.aclose()
            except Exception:
                pass


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _json_dumps(v: Any) -> str | None:
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, default=str)


def _parse_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.strip().rstrip("Z")
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _row_to_job_info(row: tuple) -> Any:
    """Convert a MariaDB row tuple to a JobInfo pydantic model."""
    from server.protocol import JobInfo, JobOptions, JobProgress

    opts_raw = json.loads(row[5]) if row[5] else {}
    try:
        opts = JobOptions(**opts_raw) if opts_raw else JobOptions(url=row[2])
    except Exception:
        opts = JobOptions(url=row[2])

    prog_raw = json.loads(row[13]) if row[13] else {}
    try:
        progress = JobProgress(**prog_raw) if prog_raw else JobProgress()
    except Exception:
        progress = JobProgress()

    return JobInfo(
        job_id=row[0],
        status=row[1],
        url=row[2],
        options=opts,
        created_at=row[9] or datetime.utcnow(),
        started_at=row[10],
        completed_at=row[11],
        error=row[12],
        progress=progress,
        worker_id=row[6],
        lane_idx=row[7],
        session_id=row[8],
        owner_id=(row[14] if len(row) > 14 else None) or "default",
    )
