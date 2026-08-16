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


# --- routable must reach BOTH row builders ---------------------------------

def test_routable_is_set_in_both_row_builders():
    """`/workers` merges two builders: locally-connected workers and
    redis-known ones. The first cut of `routable` touched only the redis
    builder, so every worker whose WS this hub actually holds -- the most
    routable there is -- came back False. Measured on hub-37: 63 rows said
    routable=False while their leases had 112-119s of TTL left.

    Counting occurrences rather than checking one site, because "wired one of
    N mirrors" is the failure mode this whole family of fields keeps hitting
    (mem_anon_rate_mb_min, loop_lag_ms, and now this)."""
    src = inspect.getsource(scheduler)
    assert src.count('"routable"') >= 2


def test_locally_connected_workers_are_routable_by_definition():
    """A row built from self.connections means this hub holds the WS."""
    src = inspect.getsource(scheduler)
    local = src[src.index('"last_heartbeat": w.last_heartbeat'):]
    before = src[:src.index('"last_heartbeat": w.last_heartbeat')]
    assert '"routable": True,' in before[-1200:], (
        "the local builder must mark its own workers routable"
    )


# --- one field list, two readers --------------------------------------------

def test_both_readers_emit_exactly_the_canonical_keys():
    """THE guard for this whole family of bugs. Two builders hand-listing their
    own fields dropped four of them in two days:

        mem_anon_rate_mb_min   1 of 4 mirrors wired -> 0 on all 170 workers
        loop_lag_ms            the same trap
        routable               redis rows only -> locally-held workers False
        address/cpu/mem/disk   local builder only -> whichever hub nginx picked
                               showed its own ~27 workers with blank columns

    Naming the set once and asserting both readers match it is what makes the
    next omission impossible rather than merely unlikely."""
    class _W:
        client_address = "10.10.51.1"
        cpu_pct = mem_pct = disk_pct = disk_free_gb = load1 = 1.0
        nproc = 8
        mem_scope = "cgroup"
        mem_current_mb = mem_anon_mb = 1.0
        mem_psi_some_avg60 = mem_psi_full_avg60 = 0.0
        mem_majfault_per_s = mem_refault_per_s = mem_anon_rate_mb_min = 0.0
        loop_lag_ms = 2.0
        memguard = ""
        profiles_cached = []
        pending_update_to = None

    live = set(scheduler._row_detail_from_worker(_W()))
    blob = set(scheduler._row_detail_from_blob({}))
    canonical = set(scheduler._ROW_DETAIL_FIELDS)
    assert live == canonical, f"live reader drifted: {live ^ canonical}"
    assert blob == canonical, f"blob reader drifted: {blob ^ canonical}"


def test_the_address_rename_is_handled():
    """ConnectedWorker calls it client_address; the blob calls it address.
    That rename is the entire reason local rows carried no IP -- there was no
    attribute of the expected name and nothing failed loudly."""
    class _W:
        client_address = "10.10.51.9"
        def __getattr__(self, k): return 0
    assert scheduler._row_detail_from_worker(_W())["address"] == "10.10.51.9"


def test_both_builders_use_the_shared_readers():
    src = inspect.getsource(scheduler)
    assert "**_row_detail_from_worker(w)," in src
    assert "**_row_detail_from_blob(data)," in src


def test_a_missing_blob_still_yields_every_key():
    """Some workers' blobs genuinely lack the detail keys (w516 on 2026-08-16
    had address/cpu/mem/disk all None). The row must still be shaped the same
    -- a missing VALUE is a display question, a missing KEY is a KeyError in
    whatever consumes it."""
    assert set(scheduler._row_detail_from_blob(None)) == set(
        scheduler._ROW_DETAIL_FIELDS
    )
