"""Regression: dispatch-time job log writes must not block POST /jobs.

2026-08-14 throughput investigation. Stage timing over 135 slow submits put
this one block at **45.8% of all time spent inside POST /jobs** -- the single
largest cost in job creation, p50 2.17s / p90 3.32s. The code was:

    for ln in _hk_consultation:
        await state.store.append_log_line(job_id, ln)   # per-line
        await state.store.publish_log(job_id, ln)       # per-line

and ``append_log_line`` takes a per-job lock then does its own
``asyncio.to_thread`` hop into a filesystem append -- on a thread pool shared
with (and starved by) S3 asset uploads. So N lines meant 2N awaits and N
thread-pool hops, inline, while the crawl submitter's 48 POST slots waited.

The call site's own comment already claimed these writes "never block job
dispatch". They now actually don't. What this file pins:

  1. the flush is detached -- create_job's caller does not await the writes;
  2. it batches into ``append_log_lines`` when the store has it (one write
     instead of N), and still works on stores that only have the per-line API;
  3. HostKnowledge lines stay ahead of pre-flight lines (they are collected
     into ONE task precisely because two tasks have no ordering guarantee);
  4. a store that throws can never surface as a job failure.
"""

import asyncio

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub.routes.jobs import lifecycle as lc


class _PerLineStore:
    """Store with only the per-line API -- server/store.py implementations."""

    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.delay = delay
        self.fail = fail
        self.batches: list[list[str]] = []
        self.published: list[str] = []
        self.per_line: list[str] = []

    async def append_log_line(self, job_id, line):
        self.per_line.append(line)

    async def publish_log(self, job_id, line):
        self.published.append(line)


class _BatchStore(_PerLineStore):
    """Store that also offers the batch API -- the MariaDB store."""

    async def append_log_lines(self, job_id, lines):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("store down")
        self.batches.append(list(lines))


@pytest.fixture
def store(monkeypatch):
    def _install(s):
        monkeypatch.setattr(lc.state, "store", s, raising=False)
        return s
    return _install


@pytest.mark.asyncio
async def test_flush_is_detached_from_the_request(store, monkeypatch):
    """The spawn call must return immediately even when the store is slow --
    this is the whole point: 2.17s of store time used to sit in the request."""
    s = store(_BatchStore(delay=0.25))
    started = asyncio.get_running_loop().time()
    lc._spawn_dispatch_log_flush("job1", ["a", "b", "c"])
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.05, f"spawn blocked for {elapsed:.3f}s"
    assert s.batches == []          # nothing written yet -- it's detached
    await asyncio.sleep(0.4)        # let the task finish
    assert s.batches == [["a", "b", "c"]]


@pytest.mark.asyncio
async def test_batches_into_one_store_call(store):
    s = store(_BatchStore())
    lc._spawn_dispatch_log_flush("job2", ["l1", "l2", "l3", "l4"])
    await asyncio.sleep(0.05)
    assert s.batches == [["l1", "l2", "l3", "l4"]], "must be ONE append, not N"
    assert s.per_line == []
    assert s.published == ["l1", "l2", "l3", "l4"]


@pytest.mark.asyncio
async def test_falls_back_when_store_lacks_the_batch_api(store):
    s = store(_PerLineStore())
    lc._spawn_dispatch_log_flush("job3", ["x", "y"])
    await asyncio.sleep(0.05)
    assert s.per_line == ["x", "y"]
    assert s.published == ["x", "y"]


@pytest.mark.asyncio
async def test_store_failure_never_escapes(store):
    s = store(_BatchStore(fail=True))
    lc._spawn_dispatch_log_flush("job4", ["boom"])
    await asyncio.sleep(0.05)
    assert s.batches == []
    # A failed append must not go on to publish a line that isn't persisted,
    # and must not raise out of the task.
    assert s.published == []


@pytest.mark.asyncio
async def test_hostknowledge_lines_precede_preflight_lines(store):
    """create_job collects both sources into one list and flushes once, so
    ordering is guaranteed. Two separate tasks would not be."""
    s = store(_BatchStore())
    hk = ["==> HostKnowledge: applied popup_policy=kill"]
    preflight = ["==> pre-flight plugin: paprika-flare for cloudflare/managed"]
    lines = list(hk)
    lines.extend(preflight)
    lc._spawn_dispatch_log_flush("job5", lines)
    await asyncio.sleep(0.05)
    assert s.batches == [hk + preflight]


@pytest.mark.asyncio
async def test_empty_input_spawns_nothing(store):
    s = store(_BatchStore())
    lc._spawn_dispatch_log_flush("job6", [])
    await asyncio.sleep(0.05)
    assert s.batches == [] and s.published == []


@pytest.mark.asyncio
async def test_task_is_referenced_so_gc_cannot_drop_it(store):
    """asyncio only holds a weak reference to a bare create_task result; the
    module parks it in a set until done."""
    s = store(_BatchStore(delay=0.1))
    lc._spawn_dispatch_log_flush("job7", ["kept"])
    assert len(lc._JOB_LOG_FLUSH_TASKS) == 1
    await asyncio.sleep(0.2)
    assert lc._JOB_LOG_FLUSH_TASKS == set()   # discarded on completion
    assert s.batches == [["kept"]]
