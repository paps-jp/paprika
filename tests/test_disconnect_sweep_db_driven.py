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


def test_sweep_queries_the_store_for_in_flight_rows():
    body = _sweep_source()
    assert "list_job_infos" in body, "the sweep must ask the authoritative store"
    assert "IN_FLIGHT_STATUSES" in body


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


def test_in_flight_statuses_cover_the_states_a_dead_worker_can_hold():
    """queued (claimed but not started), running, and downloading. If a new
    in-flight state appears, the sweep picks it up automatically -- this test
    exists so that stays true."""
    assert {s.value for s in IN_FLIGHT_STATUSES} == {
        "queued", "running", "downloading",
    }


@pytest.mark.parametrize("marker", ["pull", "claim", "_STALE_RUNNING_GRACE_S"])
def test_the_reason_is_written_down(marker):
    """The next reader needs to know why a store query sits in a disconnect
    handler; without it this looks like a redundant lookup to delete."""
    assert marker in _sweep_source()
