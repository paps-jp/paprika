"""Regression: the heartbeat's recycle exit must not steal a self-update's drain.

``_draining`` has two owners. The fd-budget gate and the drain-after-N counter
set it meaning "exit as soon as in-flight is empty", and the heartbeat loop
obliges with ``os._exit(0)``. The rolling self-update sets the SAME flag meaning
something else entirely: hold still while I wait for the hub's update slot,
sleep my assigned jitter, fetch the new source, and only then ``exit(42)``.

The heartbeat fires every 10s. The update's gate + jitter is 8-30s and can queue
behind the hub's update budget. So the recycle almost always won, and it exits
BEFORE the fetch -- docker restarts the worker on the same old code, the hub
advertises the same mismatch, and the worker loops.

Measured 2026-08-16 across 18 worker CTs on boiler / foyer / garage, uniformly:
8-37 ``begin rolling self-update`` per hour each, but exactly 2 reaching
``drain-for-update: fetching source``. A mean of 13.8 pointless full restarts per
worker per hour, fleet-wide ~2,600/hour. On w51183 the loop ran every 15s. Each
restart is a WS disconnect, and a disconnect is how a running job dies.

The guard mirrors the one that already exists for the memory guard
(``_memguard_owns_recycle``), including its bounded-deferral shape: a self-update
task that dies without exiting must not leave the worker draining forever.
"""

import inspect

import pytest

from server.worker.agent import _mix_run, _mix_selfupdate
from server.worker.agent._core import WorkerAgent


def _heartbeat_source() -> str:
    """The heartbeat loop's source, sliced around the recycle exit."""
    src = inspect.getsource(_mix_run)
    start = src.index("# Recycle: once the drain has emptied in-flight")
    return src[start:start + 1400]


def test_recycle_exit_consults_the_self_update_guard():
    """THE regression. Without this the exit is unconditional on
    ``_draining and _in_flight <= 0`` and races the update's fetch."""
    body = _heartbeat_source()
    assert "_selfupdate_owns_recycle()" in body
    exit_at = body.index("os._exit(0)")
    guard_at = body.index("_selfupdate_owns_recycle()")
    assert guard_at < exit_at, "the guard must be consulted before exiting"


def test_guard_holds_while_an_update_is_pending():
    agent = WorkerAgent.__new__(WorkerAgent)
    agent._pending_update_to = "a7a181f598f4"
    agent._update_drain_m = _mix_run.time.monotonic()
    assert agent._selfupdate_owns_recycle() != ""


def test_guard_stands_aside_when_no_update_is_pending():
    """The fd-budget gate and drain-after-N counter must still recycle
    promptly -- they are the reason the exit exists at all."""
    agent = WorkerAgent.__new__(WorkerAgent)
    agent._pending_update_to = None
    agent._update_drain_m = 0.0
    assert agent._selfupdate_owns_recycle() == ""


def test_deferral_is_bounded():
    """A self-update task that dies without exiting must not strand the worker
    in drain forever -- it reports itself full, so it would just sit idle."""
    agent = WorkerAgent.__new__(WorkerAgent)
    agent._pending_update_to = "a7a181f598f4"
    agent._update_drain_m = (
        _mix_run.time.monotonic() - WorkerAgent._SELFUPDATE_RECYCLE_DEFER_S - 1
    )
    assert agent._selfupdate_owns_recycle() == ""


def test_bound_clears_the_self_updates_own_worst_case():
    """600s drain deadline + 900s gate timeout + jitter + fetch. A deferral cap
    below that sum would re-introduce the race on the slow path."""
    assert WorkerAgent._SELFUPDATE_RECYCLE_DEFER_S >= 600 + 900


@pytest.mark.parametrize("env", ["PAPRIKA_SELFUPDATE_RECYCLE_DEFER_S"])
def test_guard_has_a_kill_switch(env):
    """Every other guard in this file is env-tunable; this one must be too, so
    the behaviour can be reverted without a fleet-wide code push."""
    assert env in inspect.getsource(WorkerAgent._selfupdate_owns_recycle)


def test_drain_ownership_is_stamped_before_draining_goes_up():
    """Ordering matters: a heartbeat landing between ``_draining = True`` and
    the stamp would see a drained worker with no owner and exit(0)."""
    src = inspect.getsource(_mix_selfupdate._SelfUpdateMixin._maybe_begin_self_update)
    assert src.index("_update_drain_m") < src.index("self._draining = True")
