"""Regression: a worker must derive its id from its CT's REAL LAN IP.

2026-08-05 incident. The worker container runs on a docker bridge, so the
kernel's route to the hub has source ``172.18.0.2`` on EVERY worker in the
fleet. ``worker_id_from_ip`` concatenates the 3rd+4th octets, so all ~100
workers booted as ``w02``. Harmless until 08-04, when Chrome's user-data-dir
moved to a node-shared tmpfs partitioned by worker_id
(``/var/paprika/chrome/<worker_id>/``, docs/ramdisk-chrome-lane.md): every CT
on a node then shared ONE profile dir, each tripping over the previous
container's ``SingletonLock`` ("profile appears to be in use ... on another
computer") -> ``RuntimeError: lane 0: Chrome :9223 failed to respond`` -> exit
-> restart. 63 of 88 reachable workers were crash-looping (median container age
42s) and only ~25 stayed connected.

Fix under test:
  * the hub echoes the caller's post-NAT address on the endpoint every worker
    already reaches at init (``GET /health`` -> ``client_ip``);
  * the worker asks for it FIRST and derives ``w51145`` from it;
  * a container-private address is never accepted as an identity, so the
    fallbacks (persisted id, then hostname+random) produce something unique
    per container rather than one id shared by the fleet.

See memory: worker-chrome-profile-lock-restart-loop, stable-worker-id.
"""

import pytest

from server.hub.routes.system import caller_ip
from server.worker.agent import workerid


# --------------------------------------------------------------------------
# hub side: /health echoes the caller's real address
# --------------------------------------------------------------------------


class _Req:
    def __init__(self, headers=None, client_host=None):
        self.headers = headers or {}

        class _C:
            host = client_host

        self.client = _C() if client_host is not None else None


def test_caller_ip_prefers_x_real_ip():
    """Behind the nginx front request.client.host is the proxy for EVERY
    worker -- the forwarded header is the only thing that distinguishes them."""
    r = _Req({"x-real-ip": "10.10.51.145"}, client_host="10.10.50.34")
    assert caller_ip(r) == "10.10.51.145"


def test_caller_ip_uses_first_forwarded_hop():
    r = _Req({"x-forwarded-for": "10.10.51.145, 10.10.50.34"}, client_host="10.10.50.34")
    assert caller_ip(r) == "10.10.51.145"


def test_caller_ip_falls_back_to_peer():
    assert caller_ip(_Req({}, client_host="10.10.51.145")) == "10.10.51.145"


def test_caller_ip_handles_no_request():
    assert caller_ip(None) == ""


# --------------------------------------------------------------------------
# worker side: what counts as an identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ip", [
    "172.18.0.2",   # docker compose bridge -- THE bug: identical fleet-wide
    "172.17.0.2",   # default docker bridge
    "172.31.255.9",
    "127.0.0.1",
    "169.254.1.2",
    "0.0.0.0",
    "not-an-ip",
    "",
    None,
])
def test_container_private_addresses_are_not_identities(ip):
    assert workerid.usable_lan_ip(ip) == ""


@pytest.mark.parametrize("ip", ["10.10.51.145", "10.10.5.14", "192.168.50.150", "172.15.0.1", "172.32.0.1"])
def test_real_lan_addresses_pass(ip):
    assert workerid.usable_lan_ip(ip) == ip


def test_worker_id_from_ip_shape():
    assert workerid.worker_id_from_ip("10.10.51.145") == "w51145"
    assert workerid.worker_id_from_ip("172.18.0.2") == "w02"  # why filtering matters


# --------------------------------------------------------------------------
# worker side: asking the hub
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_lan_ip_via_hub_reads_client_ip(monkeypatch):
    seen = {}

    def _get(url, timeout=None):
        seen["url"] = url
        return _Resp({"status": "ok", "client_ip": "10.10.51.145"})

    monkeypatch.setattr(workerid.httpx, "get", _get)
    monkeypatch.delenv("PAPRIKA_WORKER_ID_HUB_PROBE_DISABLE", raising=False)

    assert workerid.lan_ip_via_hub("ws://10.10.50.34:8000") == "10.10.51.145"
    # Existing endpoint, http scheme derived from the ws URL.
    assert seen["url"] == "http://10.10.50.34:8000/health"


def test_lan_ip_via_hub_rejects_a_bridge_answer(monkeypatch):
    """Dev stack: hub and worker share one bridge, so the hub's view is ALSO
    172.x. That is not an identity either -- fall through to the local paths."""
    monkeypatch.setattr(
        workerid.httpx, "get",
        lambda url, timeout=None: _Resp({"client_ip": "172.18.0.5"}),
    )
    assert workerid.lan_ip_via_hub("ws://hub:8000") == ""


def test_lan_ip_via_hub_gives_up_quietly_when_the_hub_is_down(monkeypatch):
    """A hub mid-restart must not block worker startup."""
    calls = []

    def _boom(url, timeout=None):
        calls.append(url)
        raise ConnectionError("connection refused")

    monkeypatch.setattr(workerid.httpx, "get", _boom)
    monkeypatch.setattr(workerid.time, "sleep", lambda s: None)
    assert workerid.lan_ip_via_hub("ws://10.10.50.34:8000", attempts=3) == ""
    assert len(calls) == 3  # retried, then gave up


def test_lan_ip_via_hub_disabled_by_env(monkeypatch):
    monkeypatch.setenv("PAPRIKA_WORKER_ID_HUB_PROBE_DISABLE", "1")
    monkeypatch.setattr(
        workerid.httpx, "get",
        lambda *a, **k: pytest.fail("probe must not run when disabled"),
    )
    assert workerid.lan_ip_via_hub("ws://10.10.50.34:8000") == ""


def test_no_hub_url_means_no_probe(monkeypatch):
    monkeypatch.delenv("PAPRIKA_WORKER_ID_HUB_PROBE_DISABLE", raising=False)
    monkeypatch.delenv("HUB_URL", raising=False)
    monkeypatch.setattr(
        workerid.httpx, "get",
        lambda *a, **k: pytest.fail("nothing to probe"),
    )
    assert workerid.lan_ip_via_hub("") == ""


# --------------------------------------------------------------------------
# worker side: the whole resolution order
# --------------------------------------------------------------------------


def test_default_worker_id_uses_the_hub_answer(monkeypatch):
    monkeypatch.setattr(workerid, "lan_ip_via_hub", lambda hub_url="": "10.10.51.145")
    monkeypatch.setattr(workerid, "lan_ip", lambda: "172.18.0.2")
    assert workerid.default_worker_id("ws://10.10.50.34:8000") == "w51145"


def test_bridge_ip_never_becomes_the_id(monkeypatch, tmp_path):
    """THE regression: hub unreachable + bridge-only container must NOT yield
    the fleet-wide ``w02``. It falls through to the persisted id."""
    monkeypatch.setattr(workerid, "lan_ip_via_hub", lambda hub_url="": "")
    monkeypatch.setattr(workerid, "lan_ip", lambda: "172.18.0.2")
    idfile = tmp_path / "worker_id"
    idfile.write_text("w51145")
    monkeypatch.setattr(workerid, "WORKER_ID_FILE", idfile)
    assert workerid.default_worker_id() == "w51145"


def test_last_resort_id_is_unique_per_container(monkeypatch, tmp_path):
    """With every probe dead and nothing persisted, the id must still be
    per-container -- a random suffix churns, a shared one corrupts profiles."""
    monkeypatch.setattr(workerid, "lan_ip_via_hub", lambda hub_url="": "")
    monkeypatch.setattr(workerid, "lan_ip", lambda: "172.18.0.2")
    monkeypatch.setattr(workerid.socket, "gethostname", lambda: "61827cf7fb79")
    idfile = tmp_path / "sub" / "worker_id"
    monkeypatch.setattr(workerid, "WORKER_ID_FILE", idfile)

    wid = workerid.default_worker_id()
    assert wid.startswith("61827cf7fb79-") and len(wid) > len("61827cf7fb79-")
    assert idfile.read_text() == wid  # persisted so it survives a restart


def test_local_lan_ip_still_works_without_a_hub(monkeypatch):
    """A worker on host networking (or native) keeps deriving locally."""
    monkeypatch.setattr(workerid, "lan_ip_via_hub", lambda hub_url="": "")
    monkeypatch.setattr(workerid, "lan_ip", lambda: "10.10.51.145")
    assert workerid.default_worker_id() == "w51145"
