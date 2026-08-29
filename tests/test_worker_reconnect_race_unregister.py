"""The reconnect race that turns a healthy worker into a /workers ghost.

A hub event-loop stall (see the ``complete_latency`` split in ``/health``: p50
transit was 16-38s on the three slow hubs and 0.25-1.5s on the healthy ones)
makes the hub miss its WS keepalive pong. Every worker on that hub sees
``1011 keepalive ping timeout`` at once and reconnects -- hub-38 logged 45
``worker connected`` in one minute for a fleet of 36. In that storm the same
worker_id gets two live ``worker_link`` coroutines seconds apart, and the older
one's ``finally`` runs AFTER the newer one has registered.

``Scheduler.unregister`` has carried a compare-and-delete guard for exactly
this since 2a395ff, but its only call site passed no ``ws``, so the guard --
``if ws is not None and ...`` -- never once ran in production. The stale
finally evicted the live registration and the worker kept an echoing WS with
no registry entry: ``POST /workers/{id}/claim`` 409'd forever
("worker '...' is not connected to this hub"), ``/workers`` showed it dead,
and because this hub's heartbeat echoes still arrived the worker's own
inbound-liveness watchdog could not notice.

These tests pin the guard from the outside: register, re-register, then let
the stale connection clean up, and assert the live one survives.
"""

import pytest

from server.protocol import WorkerCapabilities
from server.scheduler import WorkerRegistry


class _FakeWS:
    """Stands in for a Starlette WebSocket. Identity is all that matters --
    ``unregister`` compares ``cur.ws is not ws``."""

    def __init__(self, tag: str):
        self.tag = tag

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<_FakeWS {self.tag}>"


def _caps() -> WorkerCapabilities:
    return WorkerCapabilities(max_concurrent=2)


@pytest.mark.asyncio
async def test_stale_finally_does_not_evict_the_reconnected_worker():
    """THE regression. Old ws cleans up after the new ws registered -- the
    worker must stay connected. Before the fix the worker vanished from
    ``connections`` while its new WS stayed live, and every claim 409'd."""
    reg = WorkerRegistry()
    old, new = _FakeWS("old"), _FakeWS("new")

    await reg.register("w51141", old, _caps())
    await reg.register("w51141", new, _caps())  # reconnect storm: second link

    await reg.unregister("w51141", old)  # stale worker_link's finally

    assert "w51141" in reg.connections, "stale finally evicted the live link"
    assert reg.connections["w51141"].ws is new


@pytest.mark.asyncio
async def test_live_connection_still_unregisters_itself():
    """The guard must not make disconnects sticky: when the ws doing the
    cleanup IS the registered one, the entry goes away as before."""
    reg = WorkerRegistry()
    ws = _FakeWS("only")

    await reg.register("w51131", ws, _caps())
    await reg.unregister("w51131", ws)

    assert "w51131" not in reg.connections
    assert "w51131" not in reg.assignments


@pytest.mark.asyncio
async def test_unregister_without_ws_stays_unconditional():
    """``ws=None`` keeps the legacy force-drop for any non-worker_link caller
    (the guard is opt-in, so callers that genuinely mean "drop it" still can)."""
    reg = WorkerRegistry()
    await reg.register("w5178", _FakeWS("a"), _caps())

    await reg.unregister("w5178")

    assert "w5178" not in reg.connections


@pytest.mark.asyncio
async def test_worker_link_finally_passes_its_own_ws():
    """Pin the call site, not just the guard. The bug was never in
    ``unregister`` -- it was the single caller dropping the argument, which no
    test of ``unregister`` alone could have caught."""
    import inspect

    from server.hub.routes import workers as workers_route

    src = inspect.getsource(workers_route.worker_link)
    assert "state.registry.unregister(worker_id, ws)" in src, (
        "worker_link's finally must pass its own ws so the compare-and-delete "
        "guard in Scheduler.unregister can protect a reconnected worker"
    )
