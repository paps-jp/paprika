"""Pull dispatch, worker side — phase 1: built, off by default.

A worker only asks for work when it has a lane free, which is what makes a
free-lane registry unnecessary: "which lanes are free" is exactly the set of
workers blocked on BLPOP. This file pins the properties that decide whether
enabling it later is safe.

The gating rules matter more than the loop:

  * counting free lanes instead of acquiring one — acquiring here would
    double-book, because the assignment arrives over the WS and
    ``_run_assigned_job`` acquires the lane itself;
  * a lane-less worker asking for nothing is what makes ``pick_worker``'s
    ``lane_novnc_urls == []`` guard structural rather than a special case;
  * a draining or self-updating worker must not take work it will abandon.
"""

import inspect

import pytest

from server.worker.agent import _mix_pull
from server.worker.agent._core import WorkerAgent
from server.worker.agent._mix_pull import _PullMixin
from server.worker.agent._mix_run import _RunMixin


class _Lane:
    def __init__(self, busy: bool):
        self.busy = busy


class _Pool:
    def __init__(self, *busy: bool):
        self.lanes = [_Lane(b) for b in busy]


class _Agent(_PullMixin):
    def __init__(self, pool=None, draining=False, updating=None, ws=object()):
        self.lane_pool = pool
        self._draining = draining
        self._pending_update_to = updating
        # The assignment comes back over the WS, so a worker without one can
        # only pop an id and discard it. Default to connected; the disconnected
        # case has its own test below.
        self._ws = ws


def test_disabled_by_default():
    assert _mix_pull.PULL_ENABLED is False


def test_mixin_is_composed_into_the_agent():
    assert _PullMixin in WorkerAgent.__mro__
    for m in ("_pull_loop", "_pull_claim", "_pull_free_lanes", "_pull_should_ask"):
        assert hasattr(WorkerAgent, m)


def test_loop_is_started_and_cancelled_with_the_other_loops():
    src = inspect.getsource(_RunMixin._handshake_and_loop)
    assert "_pull_loop()" in src
    # Left running after the WS drops, it would keep claiming jobs this
    # connection can no longer be told about.
    assert "pull_task.cancel()" in src


def test_free_lanes_are_counted_not_acquired():
    """Acquiring here would double-book the lane against
    ``_run_assigned_job``, which acquires it when the assign lands."""
    src = inspect.getsource(_PullMixin._pull_free_lanes)
    # Strip the docstring -- it explains why we do NOT acquire, so a naive
    # substring check on the whole source would match its own explanation.
    code = src.split('"""')[-1]
    assert ".busy" in code
    assert "acquire" not in code


@pytest.mark.parametrize("busy,expected", [
    ((False, False), 2),
    ((True, False), 1),
    ((True, True), 0),
    ((), 0),
])
def test_free_lane_count(busy, expected):
    assert _Agent(_Pool(*busy))._pull_free_lanes() == expected


def test_a_lane_less_worker_counts_zero():
    """No pool at all -- misconfigured or mid-restart. It must not ask."""
    assert _Agent(None)._pull_free_lanes() == 0
    assert _Agent(None)._pull_should_ask() is False


def test_asks_only_with_a_free_lane():
    assert _Agent(_Pool(False, True))._pull_should_ask() is True
    assert _Agent(_Pool(True, True))._pull_should_ask() is False


def test_draining_worker_does_not_ask():
    assert _Agent(_Pool(False), draining=True)._pull_should_ask() is False


def test_self_updating_worker_does_not_ask():
    """It is on its way out; a job taken now is abandoned at exit."""
    assert _Agent(_Pool(False), updating="abc1234")._pull_should_ask() is False


def test_a_broken_pool_never_raises_into_the_loop():
    class _Bad:
        @property
        def lanes(self):
            raise RuntimeError("pool exploded")
    assert _Agent(_Bad())._pull_free_lanes() == 0


def test_loop_returns_immediately_while_disabled():
    """Phase 1 ships it inert -- no Redis connection, no queue traffic."""
    src = inspect.getsource(_PullMixin._pull_loop)
    body = src[:src.index("import redis")]
    assert "if not PULL_ENABLED" in body and "return" in body


def test_claim_goes_to_our_own_hub():
    """Only the hub holding this worker's WS can deliver the assignment."""
    src = inspect.getsource(_PullMixin._pull_claim)
    assert "hub_http_url" in src
    assert "/claim" in src
    assert "worker_id" in src


def test_claim_distinguishes_handled_from_stranded():
    """404 and "already running" mean the work is in hand -- drop the id. Any
    other refusal means WE could not take it and nobody else has it either, so
    the id goes back on the queue instead of waiting out the redrive."""
    src = inspect.getsource(_PullMixin._pull_claim)
    assert '"done" if "already" in _why else "requeue"' in src
    assert 'return "done"' in src and 'return "requeue"' in src


def test_disconnected_worker_does_not_ask():
    """A pop by a worker whose WS is down is pure loss: the id leaves Redis,
    the claim is refused ("not connected to this hub"), and the row waits out
    the redrive. w5110 did this for its whole disconnection on 2026-08-15."""
    assert _Agent(_Pool(False), ws=None)._pull_should_ask() is False


def test_a_won_claim_reserves_its_lane():
    """Between the 200 and the WS assign that marks the lane busy, the lane
    still counts as free -- so a worker popping a non-empty queue claims the
    same lane repeatedly. On 2026-08-15 in_flight hit 5 on a 2-lane worker;
    the extra Chromes starved the event loop, the keepalive ping went
    unanswered, and the WS died with 1011, failing every job that worker had
    running as "disconnected before the job finished"."""
    src = inspect.getsource(_PullMixin._pull_loop)
    assert "_pull_reserve()" in src
    ask = inspect.getsource(_PullMixin._pull_should_ask)
    assert "_pull_pending" in ask


def test_pending_claims_are_subtracted_from_free_lanes():
    a = _Agent(_Pool(False, False))
    assert a._pull_should_ask() is True
    a._pull_pending = 2
    assert a._pull_should_ask() is False


def test_a_reservation_is_released_on_a_deadline():
    """An assign that never arrives (hub restart between CAS and send) must
    not reserve the lane forever."""
    src = inspect.getsource(_PullMixin._pull_settle)
    assert "_ASSIGN_GRACE_S" in src and "finally:" in src
