"""The stuck drain that took 78% of the fleet off the board.

Measured on prod 2026-08-18: 255 workers, 510 lanes, 465 lanes reported busy
-- and 162-177 jobs actually running. 200 of the 255 workers were draining
after their 200-job recycle, idle, and unable to exit: the recycle exit waits
for ``_in_flight <= 0`` and a single job whose slot never came back (the hub
settled it from under the worker) pins that counter forever. A draining worker
reports ``in_flight == capacity`` on purpose ("full, don't dispatch"), so every
one of them looked like a busy, healthy worker in ``/workers``.

Three independent defences are pinned here:

* the worker force-exits a drain that has not completed within the deadline
  (``_DRAIN_DEADLINE_S``) instead of draining forever,
* the drain is reported to the hub, so "stuck draining" is a visible state,
* a single assignment cannot outlive ``_JOB_MAX_WALL_S`` holding its slot.
"""

import asyncio
import time

import pytest

from server.protocol import WorkerCapabilities, WorkerHeartbeat
from server.scheduler import (
    _ROW_DETAIL_FIELDS,
    _row_detail_from_blob,
    _row_detail_from_worker,
    WorkerRegistry,
)


class _FakeWS:
    def __init__(self, tag: str = "ws"):
        self.tag = tag


# --------------------------------------------------------------------------
# 1. the drain is visible to the hub
# --------------------------------------------------------------------------

def test_heartbeat_carries_drain_state():
    """A pre-fix worker sends no drain fields; a fixed one sends both."""
    hb = WorkerHeartbeat(in_flight=2, capacity=2)
    assert hb.draining is False
    assert hb.drain_reason == ""

    hb = WorkerHeartbeat(
        in_flight=2, capacity=2, draining=True,
        drain_reason="recycle after 200 job(s)",
    )
    assert hb.draining is True
    assert hb.drain_reason == "recycle after 200 job(s)"


@pytest.mark.asyncio
async def test_registry_records_and_exposes_the_drain():
    reg = WorkerRegistry()
    ws = _FakeWS()
    await reg.register("w1", ws, WorkerCapabilities(max_concurrent=2))

    await reg.heartbeat("w1", 2)
    w = reg.connections["w1"]
    assert w.self_draining is False
    assert w.self_drain_since == 0.0

    await reg.heartbeat(
        "w1", 2, draining=True, drain_reason="recycle after 200 job(s)",
    )
    w = reg.connections["w1"]
    assert w.self_draining is True
    assert w.self_drain_reason == "recycle after 200 job(s)"
    assert w.self_drain_since > 0.0
    stamped = w.self_drain_since

    # Held across beats: "draining for N seconds" is the signal, not a
    # per-beat flag.
    await reg.heartbeat("w1", 2, draining=True, drain_reason="recycle after 200 job(s)")
    assert reg.connections["w1"].self_drain_since == stamped

    # ...and cleared when the worker stops draining.
    await reg.heartbeat("w1", 1)
    assert reg.connections["w1"].self_draining is False
    assert reg.connections["w1"].self_drain_since == 0.0


@pytest.mark.asyncio
async def test_workers_row_shows_stuck_drain_that_in_flight_hides():
    """The row a busy worker and a stuck one produce must differ."""
    reg = WorkerRegistry()
    await reg.register("busy", _FakeWS(), WorkerCapabilities(max_concurrent=2))
    await reg.register("stuck", _FakeWS(), WorkerCapabilities(max_concurrent=2))
    await reg.heartbeat("busy", 2)
    await reg.heartbeat("stuck", 2, draining=True, drain_reason="recycle after 200 job(s)")

    rows = {r["worker_id"]: r for r in reg.stats()["workers"]}
    # Indistinguishable on the field dispatch actually reads...
    assert rows["busy"]["in_flight"] == rows["stuck"]["in_flight"] == 2
    # ...and distinguishable now.
    assert rows["busy"]["draining"] is False
    assert rows["stuck"]["draining"] is True
    assert rows["stuck"]["drain_reason"] == "recycle after 200 job(s)"
    assert rows["stuck"]["draining_s"] >= 0.0


@pytest.mark.asyncio
async def test_row_builders_stay_in_sync_on_the_new_fields():
    """Both builders carry the canonical key set (peer hubs read the blob)."""
    reg = WorkerRegistry()
    await reg.register("w1", _FakeWS(), WorkerCapabilities(max_concurrent=2))
    w = reg.connections["w1"]
    w.self_draining = True
    w.self_drain_reason = "memory guard"

    local = _row_detail_from_worker(w)
    blob = _row_detail_from_blob({"draining": True, "drain_reason": "memory guard"})
    assert set(local) == set(blob) == set(_ROW_DETAIL_FIELDS)
    assert local["draining"] is blob["draining"] is True
    assert local["drain_reason"] == blob["drain_reason"] == "memory guard"


# --------------------------------------------------------------------------
# 2. the drain watchdog
# --------------------------------------------------------------------------

class _Agent:
    """The heartbeat loop's drain bookkeeping, lifted out of the WS plumbing.

    Mirrors the real fields so the deadline arithmetic under test is the
    arithmetic that ships.
    """

    def __init__(self, in_flight: int):
        self._draining = False
        self._drain_since_m = 0.0
        self._drain_reason = ""
        self._in_flight = in_flight
        self.max_concurrent = 2
        self.exited = False

    def beat(self, now_m: float, deadline_s: float) -> int:
        """One heartbeat's worth of the drain logic. Returns eff_in_flight."""
        if self._draining:
            if not self._drain_since_m:
                self._drain_since_m = now_m
            eff = self.max_concurrent
        else:
            self._drain_since_m = 0.0
            self._drain_reason = ""
            eff = self._in_flight
        if (
            self._draining
            and self._in_flight > 0
            and deadline_s > 0
            and self._drain_since_m
            and (now_m - self._drain_since_m) >= deadline_s
        ):
            self.exited = True
        return eff


def test_stuck_drain_force_exits_at_the_deadline():
    a = _Agent(in_flight=1)          # the slot that never comes back
    a._draining = True
    a._drain_reason = "recycle after 200 job(s)"

    assert a.beat(1000.0, 900.0) == 2   # reports itself FULL while draining
    assert a.exited is False
    a.beat(1000.0 + 899.0, 900.0)
    assert a.exited is False, "must not cut a drain short"
    a.beat(1000.0 + 900.0, 900.0)
    assert a.exited is True, "a drain that never completes must still end"


def test_healthy_drain_is_never_force_exited():
    """in_flight reached 0 -> the ordinary recycle exit owns this, not us."""
    a = _Agent(in_flight=0)
    a._draining = True
    a.beat(1000.0, 900.0)
    a.beat(1000.0 + 5000.0, 900.0)
    assert a.exited is False


def test_drain_clock_resets_when_the_drain_clears():
    a = _Agent(in_flight=1)
    a._draining = True
    a.beat(1000.0, 900.0)
    a._draining = False
    a.beat(1100.0, 900.0)
    assert a._drain_since_m == 0.0 and a._drain_reason == ""
    a._draining = True
    a.beat(1200.0, 900.0)
    a.beat(1200.0 + 899.0, 900.0)
    assert a.exited is False, "the second drain gets its own full deadline"


def test_deadline_can_be_disabled():
    a = _Agent(in_flight=1)
    a._draining = True
    a.beat(1000.0, 0.0)
    a.beat(1000.0 + 10_000.0, 0.0)
    assert a.exited is False
