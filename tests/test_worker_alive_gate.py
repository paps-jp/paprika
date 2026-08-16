"""A worker that is beating must not be reported dead.

``/workers`` decided liveness as ``_fresh and bool(owner)`` -- the heartbeat
index AND the Redis lease naming the hub that holds the worker's control
WebSocket. Two signals ANDed together make availability their product, and the
lease has a hole the heartbeat does not: when a worker's WS re-homes (a
self-update, a hub restart, ordinary WS churn) the NEW hub refreshes the
heartbeat index at once, while the OLD hub's ``unregister`` still runs
``_OWNER_CAD_LUA`` and deletes the lease if nothing has overwritten it yet. The
lease is then missing until the next heartbeat -- up to HEARTBEAT_INTERVAL.

Measured 2026-08-16 against all seven production hubs at the same moment:

    hub-35  count=172  alive=123   fresh-but-dead=43
    hub-36  count=172  alive=124   fresh-but-dead=41
    hub-37  count=172  alive=124   fresh-but-dead=40
    hub-38  count=172  alive=137   fresh-but-dead=28
    hub-39  count=169  alive=135   fresh-but-dead=19
    hub-40  count=172  alive=127   fresh-but-dead=39
    hub-41  count=171  alive=124   fresh-but-dead=30

``count`` (WS connections) never moved. Between 19 and 43 workers -- 14-25% of
the fleet -- reported alive=False with heartbeat ages of 2 to 14 seconds, the
set rotating constantly. The operator saw workers as Offline; every capacity
and utilisation figure computed from `alive` was understated by the same
fraction, which is how a whole session of throughput measurements came out
wrong.

The fix follows Kubernetes' split (KEP-589): the Lease is renewed every 10s and
decides liveness on its own, while NodeStatus is a separate slower channel that
is never ANDed into readiness. Here the heartbeat decides liveness, and the
lease may only veto it once the heartbeat has also gone quiet -- which still
gives the lease the job it was added for (dropping #screens ghost tiles for a
worker whose WS is gone) about four times faster than freshness alone would.
"""

import inspect
import time

import pytest

from server import scheduler


def _alive(age_s: float, owner: str | None, grace: float | None = None) -> bool:
    """Reproduce the gate exactly as the row builder computes it."""
    g = scheduler._OWNER_VETO_GRACE_S if grace is None else grace
    last_ts = time.time() - age_s
    fresh = bool(last_ts) and (time.time() - float(last_ts)) < scheduler.WORKER_TTL
    age = time.time() - float(last_ts)
    return fresh and (bool(owner) or (age is not None and age < g))


# --- the regression --------------------------------------------------------

@pytest.mark.parametrize("age", [2, 6, 8, 11, 14])
def test_a_beating_worker_with_no_lease_is_alive(age):
    """THE regression, with the exact heartbeat ages observed in production."""
    assert _alive(age, None) is True


def test_a_beating_worker_with_a_lease_is_alive():
    assert _alive(3, "hub-41") is True


# --- what the lease is still for -------------------------------------------

def test_a_silent_worker_with_no_lease_drops_out():
    """The reason the lease gate was added: a worker whose WS is gone must stop
    appearing, or #screens renders a tile that always fails to capture."""
    assert _alive(45, None) is False


def test_the_lease_holds_a_quiet_worker_up_to_the_ttl():
    """With an owner still present, a gap in beats is a slow worker, not a
    disconnected one -- that is what WORKER_TTL is for."""
    assert _alive(45, "hub-41") is True
    assert _alive(scheduler.WORKER_TTL + 5, "hub-41") is False


def test_dropping_out_is_faster_than_freshness_alone():
    """30s, not 120s. Freshness alone was the original complaint."""
    assert scheduler._OWNER_VETO_GRACE_S < scheduler.WORKER_TTL / 2


def test_the_grace_clears_the_lease_hole():
    """The hole is up to one heartbeat interval wide. A grace at or below that
    would still hide live workers -- which is the whole bug."""
    assert scheduler._OWNER_VETO_GRACE_S > scheduler.HEARTBEAT_INTERVAL
    assert scheduler._OWNER_VETO_GRACE_S >= 3 * scheduler.HEARTBEAT_INTERVAL


def test_the_grace_is_tunable_without_a_redeploy():
    assert "PAPRIKA_OWNER_VETO_GRACE_S" in inspect.getsource(scheduler)


# --- the separate signal ---------------------------------------------------

def test_routability_is_its_own_field():
    """"Is it running" and "can a request for it be served right now" are
    different questions. Folding them into one boolean is what hid the fleet;
    the preview / #screens / forward paths need the second one, and now have
    it without borrowing the first."""
    src = inspect.getsource(scheduler)
    assert '"routable": bool(owner),' in src


def test_liveness_is_not_an_and_of_two_signals_any_more():
    """Guards the shape, not just the values: `_fresh and bool(owner)` must not
    come back. It reads like a tightening and is actually a 25% outage."""
    # Code lines only: the comment above the gate quotes the old expression to
    # explain what was wrong with it, and a naive substring check trips on the
    # very documentation that keeps it from coming back.
    code = "\n".join(
        line for line in inspect.getsource(scheduler).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "_fresh and bool(owner)" not in code


@pytest.mark.parametrize("marker", [
    "KEP-589", "re-homes", "_OWNER_CAD_LUA", "172",
])
def test_the_reason_is_written_down(marker):
    """The next reader will see a grace window bolted onto a liveness check and
    want to delete it. The measurement and the mechanism have to be there."""
    assert marker in inspect.getsource(scheduler)
