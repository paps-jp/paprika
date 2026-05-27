"""Job storage abstraction.

Two implementations:

- `InMemoryJobStore`: dict + asyncio.Queue. Single process. Used when no
  --redis-url is configured (dev convenience).
- `RedisJobStore`: redis-py async client + Redis Pub/Sub. Persists across
  process restarts and works across hub/worker processes.

Both expose the same interface so hub/worker code is identical regardless of
which backend is active.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import AsyncIterator, Protocol

from server.protocol import JobInfo, JobResult

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Protocol
# ----------------------------------------------------------------------------


class JobStore(Protocol):
    """The minimal interface hub + worker code uses."""

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...

    # job info (status, progress, etc.)
    async def save_job_info(self, info: JobInfo) -> None: ...
    async def get_job_info(self, job_id: str) -> JobInfo | None: ...
    async def list_job_ids(
        self, offset: int = 0, limit: int = 0
    ) -> list[str]: ...
    async def count_jobs(self) -> int: ...
    async def delete_job(self, job_id: str) -> bool: ...

    # full job result (only after job finishes)
    async def save_job_result(self, result: JobResult) -> None: ...
    async def get_job_result(self, job_id: str) -> JobResult | None: ...

    # log (append-only) and live pub/sub
    async def append_log_line(self, job_id: str, line: str) -> None: ...
    async def get_log_lines(self, job_id: str) -> list[str]: ...
    async def publish_log(self, job_id: str, line: str) -> None: ...
    async def subscribe_log(self, job_id: str) -> AsyncIterator[str]:
        # type: ignore[empty-body]
        yield ""  # for type checkers; real impls override


# ----------------------------------------------------------------------------
# In-memory implementation
# ----------------------------------------------------------------------------


class InMemoryJobStore:
    """Single-process fallback. Lost on restart. No cross-process pub/sub."""

    def __init__(self) -> None:
        self._infos: dict[str, JobInfo] = {}
        self._results: dict[str, JobResult] = {}
        self._logs: dict[str, list[str]] = defaultdict(list)
        # job_id -> list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue[str]]] = defaultdict(list)

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def save_job_info(self, info: JobInfo) -> None:
        self._infos[info.job_id] = info

    async def get_job_info(self, job_id: str) -> JobInfo | None:
        return self._infos.get(job_id)

    async def list_job_ids(
        self, offset: int = 0, limit: int = 0
    ) -> list[str]:
        # Most-recent first by created_at
        items = sorted(
            self._infos.values(),
            key=lambda i: i.created_at,
            reverse=True,
        )
        ids = [i.job_id for i in items]
        if limit > 0:
            return ids[offset : offset + limit]
        return ids[offset:] if offset else ids

    async def count_jobs(self) -> int:
        return len(self._infos)

    async def delete_job(self, job_id: str) -> bool:
        existed = job_id in self._infos
        self._infos.pop(job_id, None)
        self._results.pop(job_id, None)
        self._logs.pop(job_id, None)
        self._subscribers.pop(job_id, None)
        return existed

    async def save_job_result(self, result: JobResult) -> None:
        self._results[result.job_id] = result

    async def get_job_result(self, job_id: str) -> JobResult | None:
        return self._results.get(job_id)

    async def append_log_line(self, job_id: str, line: str) -> None:
        self._logs[job_id].append(line)

    async def get_log_lines(self, job_id: str) -> list[str]:
        return list(self._logs.get(job_id, []))

    async def publish_log(self, job_id: str, line: str) -> None:
        for q in list(self._subscribers.get(job_id, [])):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

    async def subscribe_log(self, job_id: str) -> AsyncIterator[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1024)
        self._subscribers[job_id].append(q)
        try:
            while True:
                line = await q.get()
                yield line
                if line == "__JOB_DONE__":
                    return
        finally:
            try:
                self._subscribers[job_id].remove(q)
            except ValueError:
                pass


# ----------------------------------------------------------------------------
# Redis implementation
# ----------------------------------------------------------------------------

_K_INFO = "paprika:job:{}:info"
_K_RESULT = "paprika:job:{}:result"
_K_LOG = "paprika:job:{}:log"  # LIST of log lines (RPUSH/LRANGE)
_K_INDEX = "paprika:jobs"  # SORTED SET by created_at (ts)
_CHAN_LOG = "paprika:job:{}:log:chan"  # Pub/Sub channel


class RedisJobStore:
    """Persistent multi-process store."""

    def __init__(self, redis_url: str) -> None:
        self.url = redis_url
        self._r = None  # redis.asyncio.Redis
        self._pubsub_r = None  # separate client for pubsub (recommended)

    async def initialize(self) -> None:
        import redis.asyncio as redis  # lazy import (only if used)

        self._r = redis.from_url(self.url, decode_responses=True)
        self._pubsub_r = redis.from_url(self.url, decode_responses=True)
        # quick ping
        await self._r.ping()

    async def close(self) -> None:
        if self._r is not None:
            await self._r.aclose()
        if self._pubsub_r is not None:
            await self._pubsub_r.aclose()

    # job info ----------------------------------------------------------

    async def save_job_info(self, info: JobInfo) -> None:
        payload = info.model_dump_json()
        ts = info.created_at.timestamp() if info.created_at else 0.0
        async with self._r.pipeline(transaction=False) as pipe:
            pipe.set(_K_INFO.format(info.job_id), payload)
            pipe.zadd(_K_INDEX, {info.job_id: ts})
            await pipe.execute()

    async def get_job_info(self, job_id: str) -> JobInfo | None:
        raw = await self._r.get(_K_INFO.format(job_id))
        if not raw:
            return None
        return JobInfo.model_validate_json(raw)

    async def list_job_ids(
        self, offset: int = 0, limit: int = 0
    ) -> list[str]:
        # Most-recent first.  ZREVRANGE uses inclusive stop index.
        start = offset
        stop = (offset + limit - 1) if limit > 0 else -1
        return await self._r.zrevrange(_K_INDEX, start, stop)

    async def count_jobs(self) -> int:
        return await self._r.zcard(_K_INDEX)

    async def delete_job(self, job_id: str) -> bool:
        existed = await self._r.exists(_K_INFO.format(job_id))
        async with self._r.pipeline(transaction=False) as pipe:
            pipe.delete(_K_INFO.format(job_id))
            pipe.delete(_K_RESULT.format(job_id))
            pipe.delete(_K_LOG.format(job_id))
            pipe.zrem(_K_INDEX, job_id)
            await pipe.execute()
        return bool(existed)

    # job result --------------------------------------------------------

    async def save_job_result(self, result: JobResult) -> None:
        await self._r.set(_K_RESULT.format(result.job_id), result.model_dump_json())

    async def get_job_result(self, job_id: str) -> JobResult | None:
        raw = await self._r.get(_K_RESULT.format(job_id))
        if not raw:
            return None
        return JobResult.model_validate_json(raw)

    # log ---------------------------------------------------------------

    async def append_log_line(self, job_id: str, line: str) -> None:
        await self._r.rpush(_K_LOG.format(job_id), line)

    async def get_log_lines(self, job_id: str) -> list[str]:
        return await self._r.lrange(_K_LOG.format(job_id), 0, -1)

    async def publish_log(self, job_id: str, line: str) -> None:
        await self._r.publish(_CHAN_LOG.format(job_id), line)

    async def subscribe_log(self, job_id: str) -> AsyncIterator[str]:
        chan = _CHAN_LOG.format(job_id)
        pubsub = self._pubsub_r.pubsub()
        await pubsub.subscribe(chan)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                yield data
                if data == "__JOB_DONE__":
                    return
        finally:
            try:
                await pubsub.unsubscribe(chan)
                await pubsub.aclose()
            except Exception:
                pass


# ----------------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------------


async def make_store(redis_url: str | None) -> tuple[JobStore, str]:
    """Returns (store, kind). kind is 'redis' or 'in-memory'."""
    if redis_url:
        store = RedisJobStore(redis_url)
        try:
            await store.initialize()
            return store, "redis"
        except Exception as e:
            log.warning(
                "Redis at %s unavailable (%s); falling back to in-memory store.",
                redis_url,
                e,
            )
            try:
                await store.close()
            except Exception:
                pass
    mem = InMemoryJobStore()
    await mem.initialize()
    return mem, "in-memory"
