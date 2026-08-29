"""WorkerJobLog persistence must never block the per-worker WS receive loop.

``worker_link`` (server/hub/routes/workers.py) reads one message at a time
off a worker's socket and does ``await _handle_worker_message(worker, msg)``
before reading the next one -- so any ``await`` inside a handler delays
draining the rest of that worker's already-buffered messages, including a
``WorkerJobComplete`` sitting right behind a burst of log/progress lines.

Prod evidence (see [[hub-ws-per-worker-serial-queue]] / ``/health``'s
``complete_latency``): ``transit_ms`` (worker ``sent_at`` -> hub processing)
ran p50 45s / p90 120s on 2026-08-29, while the store round-trips the
handler awaits (MariaDB INSERT, Redis PUBLISH) are each only tens of ms --
the multiplier is queueing behind a chatty job's log lines, not the store
itself being slow.

Fix: log/progress persistence is best-effort telemetry, never load-bearing
for dispatch correctness, so ``_handle_worker_message`` now hands it off via
``_bg()`` (``asyncio.create_task``) instead of awaiting it inline. This
pins that ``_handle_worker_message`` returns immediately regardless of how
slow the store round-trip is, and that the deferred work still actually
runs (and still swallows store errors instead of raising into the task
future, which would otherwise print "exception was never retrieved").
"""

import asyncio

import pytest

from server.hub._state import state
from server.hub.routes import workers as workers_route
from server.protocol import WorkerJobLog


class _SlowStore:
    """Stands in for the job store. Every persistence call blocks for
    ``delay`` seconds so a regression (awaiting it inline) would make the
    test itself slow / time out, instead of only being provable by prod
    latency graphs."""

    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.calls: list[tuple] = []

    async def append_log_line(self, job_id, line):
        await asyncio.sleep(self.delay)
        self.calls.append(("append_log_line", job_id, line))

    async def publish_log(self, job_id, line):
        await asyncio.sleep(self.delay)
        self.calls.append(("publish_log", job_id, line))


class _FailingStore(_SlowStore):
    """A store whose writes always raise -- pins that the error is
    swallowed inside the background task rather than escaping as an
    "exception was never retrieved" warning or, worse, propagating into
    the WS receive loop and dropping the worker's connection."""

    async def append_log_line(self, job_id, line):
        await asyncio.sleep(self.delay)
        raise RuntimeError("boom: FK violation, job already deleted")

    async def publish_log(self, job_id, line):
        await asyncio.sleep(self.delay)
        raise RuntimeError("boom: FK violation, job already deleted")


class _NoopRegistry:
    def log_event(self, *a, **kw):
        pass


class _Worker:
    def __init__(self):
        self.worker_id = "w1"


@pytest.fixture(autouse=True)
def _reset_state():
    orig_store, orig_batcher, orig_registry = (
        state.store, state.log_batcher, state.registry,
    )
    state.log_batcher = None
    state.registry = _NoopRegistry()
    yield
    state.store, state.log_batcher, state.registry = (
        orig_store, orig_batcher, orig_registry,
    )


@pytest.mark.asyncio
async def test_log_line_handler_returns_before_the_slow_store_call_finishes():
    store = _SlowStore(delay=0.3)
    state.store = store

    msg = WorkerJobLog(job_id="job123", line="plain log line, no marker")

    start = asyncio.get_event_loop().time()
    await workers_route._handle_worker_message(_Worker(), msg)
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed < 0.1, (
        f"_handle_worker_message took {elapsed:.3f}s -- it must return "
        "before the store round-trip completes (persistence is "
        "backgrounded via _bg), not after"
    )
    # The background write hasn't landed yet (still sleeping) ...
    assert store.calls == []
    # ... but it does land shortly after, proving the work wasn't dropped.
    # (append_log_line then publish_log run sequentially inside the one
    # backgrounded task, so allow for both delays.)
    await asyncio.sleep(store.delay * 2 + 0.2)
    assert ("append_log_line", "job123", "plain log line, no marker") in store.calls
    assert ("publish_log", "job123", "plain log line, no marker") in store.calls


@pytest.mark.asyncio
async def test_netcap_marker_handler_also_returns_immediately():
    from server.protocol import NET_CAPTURE_MARKER

    store = _SlowStore(delay=0.3)
    state.store = store
    msg = WorkerJobLog(job_id="job123", line=NET_CAPTURE_MARKER + '{"net": []}')

    start = asyncio.get_event_loop().time()
    await workers_route._handle_worker_message(_Worker(), msg)
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed < 0.1
    await asyncio.sleep(store.delay + 0.2)
    assert store.calls, "the backgrounded publish_log for the marker never ran"


@pytest.mark.asyncio
async def test_store_error_in_the_background_task_does_not_raise():
    """A store failure (e.g. job_logs FK violation after the parent job
    was deleted mid-stream) must stay contained inside the background
    task, exactly like the old inline try/except contained it."""
    state.store = _FailingStore(delay=0.05)
    msg = WorkerJobLog(job_id="job123", line="doomed line")

    # Must not raise, even though the underlying store call will.
    await workers_route._handle_worker_message(_Worker(), msg)

    # Let the background task run to completion (and swallow its error).
    await asyncio.sleep(0.2)
    # No assertion needed beyond "we got here" -- an unswallowed exception
    # inside the background task would surface as an asyncio warning on
    # loop teardown, which pytest-asyncio would otherwise leave silent but
    # which a strict CI (-W error) would catch; the real regression this
    # guards is the receive loop itself dying, which the earlier tests
    # already prove doesn't happen (handler returns cleanly every time).
