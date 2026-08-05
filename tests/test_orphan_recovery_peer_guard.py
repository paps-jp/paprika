"""Regression: the startup orphan-job recovery must not blanket-fail a live
fleet's jobs, and must not block the lifespan.

2026-08-03 incident: all 7 hubs had booted while Redis was still LOADING, so
none of them wrote ``paprika:hubs:{id}`` presence. Restarting one hub made it
see zero *alive* peers, conclude it was alone, and mark every ``status=running``
job in the shared MariaDB failed -- 10 of the 12 it killed were running on the
six healthy peers. The same scan (a full walk of ~860k rows) was awaited inline
in the lifespan, so uvicorn never bound its port and ``/health`` refused
connections for 10+ minutes.

Fix under test:
  * peer liveness is decided by a real /health probe, with Redis presence
    demoted to a mere candidate source (mirrors the nginx reconciler, 74b760e);
  * any error in that check means "skip", never "proceed";
  * the scan runs as a background task so startup can complete.

See memory: startup-orphan-recovery-blanket-fail-trap.
"""

import asyncio

import pytest

from server.hub import _reaper
from server.hub._state import config, state


class _FakeHubs:
    """Stands in for HubRegistry.list_all()."""

    def __init__(self, rows, exc=None):
        self._rows = rows
        self._exc = exc

    async def list_all(self):
        if self._exc is not None:
            raise self._exc
        return self._rows


@pytest.fixture(autouse=True)
def _restore_state():
    orig_hubs, orig_id = state.hubs, config.hub_id
    config.hub_id = "hub-35"
    yield
    state.hubs, config.hub_id = orig_hubs, orig_id


def _probe(monkeypatch, responder):
    """Patch the /health probe: responder(hub_id) -> status code, or raises."""
    seen = []

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen.append(url)
            return _Resp(responder(url))

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


def test_presence_alive_peer_short_circuits(monkeypatch):
    """A live presence row is proof enough -- no probe needed."""
    state.hubs = _FakeHubs([
        {"hub_id": "hub-35", "alive": True, "local": True},
        {"hub_id": "hub-36", "alive": True, "local": False},
    ])
    seen = _probe(monkeypatch, lambda url: 200)
    assert asyncio.run(_reaper._any_peer_hub_alive()) is True
    assert seen == []  # short-circuited before probing


def test_presenceless_but_healthy_peer_is_alive(monkeypatch):
    """THE incident: peers have no presence row but are serving fine.

    Presence says alive=False for all of them; the probe says otherwise, and
    the probe wins.
    """
    state.hubs = _FakeHubs([
        {"hub_id": "hub-35", "alive": False, "local": True},
        {"hub_id": "hub-36", "alive": False, "local": False},
        {"hub_id": "hub-37", "alive": False, "local": False},
    ])
    seen = _probe(monkeypatch, lambda url: 200)
    assert asyncio.run(_reaper._any_peer_hub_alive()) is True
    assert any("10.10.50.36" in u for u in seen)
    assert all(u.endswith("/health") for u in seen)


def test_genuinely_dead_peers_report_not_alive(monkeypatch):
    """Single-hub / all-peers-down: recovery is legitimately allowed."""
    state.hubs = _FakeHubs([
        {"hub_id": "hub-35", "alive": False, "local": True},
        {"hub_id": "hub-36", "alive": False, "local": False},
    ])
    _probe(monkeypatch, lambda url: 502)
    assert asyncio.run(_reaper._any_peer_hub_alive()) is False


def test_no_peers_at_all_reports_not_alive(monkeypatch):
    state.hubs = _FakeHubs([{"hub_id": "hub-35", "alive": True, "local": True}])
    seen = _probe(monkeypatch, lambda url: 200)
    assert asyncio.run(_reaper._any_peer_hub_alive()) is False
    assert seen == []


def test_enumeration_failure_fails_safe(monkeypatch):
    """If we cannot enumerate peers we cannot prove we're alone -> assume a
    live cluster. Skipping recovery is recoverable; blanket-failing is not."""
    state.hubs = _FakeHubs([], exc=RuntimeError("redis down"))
    assert asyncio.run(_reaper._any_peer_hub_alive()) is True


def test_no_registry_reports_not_alive():
    state.hubs = None
    assert asyncio.run(_reaper._any_peer_hub_alive()) is False


def test_liveness_error_skips_the_scan(monkeypatch):
    """An exception out of the liveness check must abort recovery, not fall
    through into the blanket scan."""
    async def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(_reaper, "_any_peer_hub_alive", _boom)

    scanned = []

    class _Store:
        async def list_job_ids(self):
            scanned.append(True)
            return ["j1"]

    monkeypatch.setattr(state, "store", _Store(), raising=False)
    assert asyncio.run(_reaper._recover_orphan_running_jobs_scan()) == 0
    assert scanned == []  # never reached the full-table walk


def test_entrypoint_returns_immediately_and_defers_scan(monkeypatch):
    """The lifespan must not wait on the full-table walk."""
    started = asyncio.Event()

    async def _slow_scan():
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(_reaper, "_recover_orphan_running_jobs_scan", _slow_scan)

    async def _drive():
        n = await asyncio.wait_for(_reaper._recover_orphan_running_jobs(), 1.0)
        await asyncio.wait_for(started.wait(), 1.0)  # it really was scheduled
        return n

    assert asyncio.run(_drive()) == 0
