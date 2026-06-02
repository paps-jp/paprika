"""MariaDB-backed JobStore implementation.

When the operator configures MariaDB connection in the Settings tab,
the hub uses this store instead of the Redis-backed one.  Live log
pub/sub still uses Redis (MariaDB has no native pub/sub); all
persistence (job info, results, logs) goes to MariaDB.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


class MariaDBJobStore:
    """Persistent job store backed by MariaDB.

    ``pool`` is an ``aiomysql.Pool``.
    ``redis_url`` is optional; when given, live log pub/sub uses Redis.
    """

    def __init__(self, pool: Any, redis_url: str | None = None) -> None:
        self._pool = pool
        self._redis_url = redis_url
        self._r: Any = None          # redis.asyncio.Redis (for pub/sub)
        self._pubsub_r: Any = None   # separate client for subscribe

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
                        worker_id, lane_idx, session_id,
                        created_at, started_at, completed_at,
                        error, progress)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                    "created_at, started_at, completed_at, error, progress "
                    "FROM jobs WHERE job_id=%s", (job_id,))
                row = await cur.fetchone()
        if not row:
            return None
        return _row_to_job_info(row)

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

    async def delete_job(self, job_id: str) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                # CASCADE deletes job_results + job_logs
                await cur.execute(
                    "DELETE FROM jobs WHERE job_id=%s", (job_id,))
                return cur.rowcount > 0

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

    async def append_log_line(self, job_id: str, line: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                # Get next line_num
                await cur.execute(
                    "SELECT COALESCE(MAX(line_num), -1) + 1 "
                    "FROM job_logs WHERE job_id=%s", (job_id,))
                row = await cur.fetchone()
                next_num = row[0] if row else 0
                await cur.execute(
                    "INSERT INTO job_logs (job_id, line_num, line) "
                    "VALUES (%s, %s, %s)", (job_id, next_num, line))

    async def get_log_lines(self, job_id: str) -> list[str]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT line FROM job_logs WHERE job_id=%s "
                    "ORDER BY line_num", (job_id,))
                rows = await cur.fetchall()
        return [r[0] for r in rows]

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
    )
