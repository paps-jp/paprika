"""Regression: /fleet/egress-allow must list EVERY object-store tier.

The worker egress firewall DROPs RFC1918 traffic except the IPs this endpoint
returns. When PAPRIKA_WORKER_MINIO_DIRECT is on, the worker PUTs asset bytes
straight to whichever MinIO the hub's presigned URL names -- so an endpoint
missing from this list isn't a permission error, it's a silent DROP: the PUT
sits in SYN_SENT for the full connect timeout, leaking fds (incident
2026-06-24, canary workers .150/.151/.157 piled up ~50 half-open sockets each).

The endpoint was written when there was one store, and derived only
``s3_endpoint``. The tiering added afterwards means objstore._write_cb can sign
a URL for any of four hosts -- primary, non-video hot, and each tier's spill
target -- and the COMMON case (an image asset -> the non-video hot tier) was
not among the allowed ones. Enabling MinIO-direct on 2026-08-16 would have
reproduced the incident on the majority of assets, so the list is pinned here
against the same accessors objstore signs with.
"""

import pytest

from server.hub.routes import system as system_route


class _Hubs:
    async def list_all(self):
        return [{"hub_id": "hub-35", "ip": "10.10.50.35"}]


@pytest.fixture
def _endpoints(monkeypatch):
    """Point every tier at a distinguishable IP, mirroring the prod layout on
    2026-08-16 (primary .48, non-video hot .47, spills .8 and .17)."""
    from server.hub import objstore

    monkeypatch.setattr(
        objstore, "_s3cfg",
        lambda key, env, default="": (
            "http://10.10.50.48:9000" if key == "s3_endpoint" else default
        ),
    )
    monkeypatch.setattr(objstore, "_nonvideo_endpoint", lambda: "http://10.10.50.47:9000")
    monkeypatch.setattr(objstore, "_spill_endpoint", lambda: "http://10.10.50.8:9100")
    monkeypatch.setattr(objstore, "_nv_spill_endpoint", lambda: "http://10.10.50.17:9000")


async def _allowlist(monkeypatch) -> set[str]:
    monkeypatch.setattr(system_route.state, "hubs", _Hubs(), raising=False)
    resp = await system_route.fleet_egress_allow()
    return {ln.strip() for ln in resp.body.decode().splitlines() if ln.strip()}


@pytest.mark.asyncio
async def test_every_tier_endpoint_is_allowed(monkeypatch, _endpoints):
    allowed = await _allowlist(monkeypatch)
    for ip in ("10.10.50.48", "10.10.50.47", "10.10.50.8", "10.10.50.17"):
        assert ip in allowed, f"{ip} missing -- direct PUTs there would be DROPped"


@pytest.mark.asyncio
async def test_hub_ips_still_included(monkeypatch, _endpoints):
    """The tier fix must not displace what this endpoint already did."""
    assert "10.10.50.35" in await _allowlist(monkeypatch)


@pytest.mark.asyncio
async def test_unconfigured_tier_is_skipped_not_fatal(monkeypatch):
    """Single-tier deployments leave the extra endpoints blank; that must yield
    a shorter list, not an exception or a bogus entry."""
    from server.hub import objstore

    monkeypatch.setattr(
        objstore, "_s3cfg",
        lambda key, env, default="": (
            "http://10.10.50.48:9000" if key == "s3_endpoint" else default
        ),
    )
    monkeypatch.setattr(objstore, "_nonvideo_endpoint", lambda: "")
    monkeypatch.setattr(objstore, "_spill_endpoint", lambda: "")
    monkeypatch.setattr(objstore, "_nv_spill_endpoint", lambda: "")
    allowed = await _allowlist(monkeypatch)
    assert "10.10.50.48" in allowed
    assert "" not in allowed


@pytest.mark.asyncio
async def test_hostname_endpoint_is_skipped(monkeypatch):
    """The firewall rules take addresses; a DNS name can't be turned into one
    here (and DNS egress is already broadly allowed), so it's dropped rather
    than emitted as a rule the worker can't apply."""
    from server.hub import objstore

    monkeypatch.setattr(
        objstore, "_s3cfg",
        lambda key, env, default="": (
            "http://minio.internal:9000" if key == "s3_endpoint" else default
        ),
    )
    monkeypatch.setattr(objstore, "_nonvideo_endpoint", lambda: "http://10.10.50.47:9000")
    monkeypatch.setattr(objstore, "_spill_endpoint", lambda: "")
    monkeypatch.setattr(objstore, "_nv_spill_endpoint", lambda: "")
    allowed = await _allowlist(monkeypatch)
    assert "minio.internal" not in allowed
    assert "10.10.50.47" in allowed
