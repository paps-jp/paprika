"""Regression: the worker-disconnect sweep must ask the store, not just memory.

The sweep that settles a dead worker's in-flight jobs used to enumerate them
from ``state.sessions.by_worker()`` -- this hub's own in-memory session
registry. That holds only the jobs THIS hub dispatched, which was fine while
"the hub that assigns" and "the hub that holds the worker's WebSocket" were
always the same hub.

Two cases break that assumption:

  * the dispatch path registers the session best-effort and carries on with
    ``fetch_sid = None`` when it fails -- those jobs were already invisible to
    the sweep;
  * under pull dispatch (design D9) a worker's WS is pinned to one hub by
    consistent hash while its claim is answered by whichever hub nginx picked,
    so the disconnecting hub has no session for the job at all.

When the sweep finds nothing, the job is not settled immediately; it waits out
``_STALE_RUNNING_GRACE_S`` (300s) in the stale-job reconciler instead. Nothing
is lost, but recovery goes from seconds to five minutes.

So the sweep now unions the session registry with a store query for in-flight
rows carrying this worker_id. This file pins that the store is consulted, that
its rows are filtered to the disconnecting worker, and that a store failure
degrades to the old behaviour rather than breaking disconnect handling.
"""

import inspect

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub.routes import workers as workers_route
from server.protocol import IN_FLIGHT_STATUSES


def _sweep_source() -> str:
    """The disconnect handler's source, sliced around the orphan sweep."""
    src = inspect.getsource(workers_route)
    start = src.index("orphan_job_ids: list[str] = []")
    end = src.index("log.info(\"worker disconnected: %s\", worker_id)")
    return src[start:end]


def test_sweep_queries_the_store_for_queued_rows():
    body = _sweep_source()
    assert "list_job_infos" in body, "the sweep must ask the authoritative store"


def test_store_query_is_queued_only():
    """THE regression. Pulling every in-flight status fed `running` rows to
    the sweep, which settles them as FAILED immediately -- skipping the 300s
    grace the stale-job reconciler exists to give a worker that dropped for a
    self-update and comes back with the same id. Every rolling update then
    failed that worker's whole in-flight set, taking the hourly success rate
    from 97.3% to 74.7% (2,813 failures/hour).

    `queued` is the safe half: the worker provably never started those, so
    there is no partial work and nothing to wait for."""
    body = _sweep_source()
    q = body[body.index("list_job_infos"):body.index("list_job_infos") + 260]
    assert "JobStatus.queued.value" in q
    assert "IN_FLIGHT_STATUSES" not in q


def test_sweep_filters_store_rows_to_the_disconnecting_worker():
    """Without the worker_id filter a disconnect would settle the whole
    fleet's in-flight jobs -- the blast radius that made the 2026-08-10
    blanket-fail incident so damaging."""
    body = _sweep_source()
    assert 'worker_id", None) == worker_id' in body


def test_sweep_still_uses_the_session_registry():
    """The store query is an addition, not a replacement: the registry still
    covers sessions whose job row may already be terminal."""
    body = _sweep_source()
    assert "by_worker(worker_id)" in body


def test_store_failure_does_not_break_disconnect_handling():
    """A DB blip during a disconnect must not take out the handler -- it
    should log and fall back to what the registry knows."""
    body = _sweep_source()
    store_call = body[body.index("list_job_infos"):]
    assert "except Exception" in store_call


def test_running_and_downloading_stay_with_the_reconciler():
    """They are in-flight but must NOT be swept on disconnect -- see above."""
    assert {s.value for s in IN_FLIGHT_STATUSES} == {
        "queued", "running", "downloading",
    }
    body = _sweep_source()
    assert "_STALE_RUNNING_GRACE_S" in body, "the reason must stay written down"


def test_session_derived_running_jobs_are_left_alone_too():
    """The other half of the same regression.

    Narrowing the STORE query to `queued` only fixed the rows the store
    contributed. The session registry (``by_worker``) also feeds ids into this
    sweep, and an ordinary fetch job registers a session -- so every running job
    on this hub was still failed the instant its worker's WS blipped, with no
    grace window at all.

    A blip is usually not a death. Measured 2026-08-16 on w51183: job
    16d15a39a6b4 was failed 5s after it started and 3dd90b37b487 15s in, while
    the worker process kept running and went on completing jobs -- it had only
    reconnected. ~39 killed jobs/min hub-wide, each stranding the images it had
    already uploaded, because image-pull only reads /jobs/completes.
    """
    body = _sweep_source()
    running = body[body.index("elif jinfo.status == JobStatus.running:"):]
    # It must fall through to the reconciler, NOT to the failed branch below.
    assert "continue" in running[:running.index("else:")]
    fail_at = body.index("disconnected before the job ")
    assert body.index("elif jinfo.status == JobStatus.running:") < fail_at, (
        "the running branch must be reached before the blanket-fail branch"
    )


def test_keepalive_completion_survives_the_running_carve_out():
    """A keepalive job whose worker vanished is COMPLETED, not left hanging --
    its capture is already saved. That branch must stay ahead of the new one."""
    body = _sweep_source()
    assert body.index('phase == "keepalive"') < body.index(
        "elif jinfo.status == JobStatus.running:"
    )


@pytest.mark.parametrize("marker", ["pull", "claim", "_STALE_RUNNING_GRACE_S"])
def test_the_reason_is_written_down(marker):
    """The next reader needs to know why a store query sits in a disconnect
    handler; without it this looks like a redundant lookup to delete."""
    assert marker in _sweep_source()
