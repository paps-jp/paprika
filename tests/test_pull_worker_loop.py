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
    def __init__(self, pool=None, draining=False, updating=None):
        self.lane_pool = pool
        self._draining = draining
        self._pending_update_to = updating


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


def test_claim_treats_404_and_409_as_ordinary():
    """A redrive won the CAS, or the job was cancelled between submit and
    pop. Neither is worth retrying -- drop it and pop the next id."""
    src = inspect.getsource(_PullMixin._pull_claim)
    assert "(404, 409)" in src
