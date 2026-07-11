"""Regression: make_redis_client must build clients with the connection-
resilience knobs added after the 2026-07-09 incident.

Root cause that day: hub Redis clients had no health check, so a pooled
connection that silently died (Redis restart / idle NAT drop / TCP blip) was
never evicted -- every later command reused the dead socket and read empty.
5 of 7 hubs held a stale client for ~12h; their cross-hub fleet view collapsed
to empty, they fell out of nginx, and salvage mass-restarted the fleet.

The fix wires health_check_interval + socket_keepalive into the ONE factory
(server/store.make_redis_client) that both the job store and the pub/sub client
go through. These tests pin that so a future refactor can't silently drop it.
See memory: stale-redis-hub-dereg-salvage-storm.
"""

import server.store as store
from server.store import make_redis_client

_PLAIN_URL = "redis://localhost:6379/0"


def _conn_kwargs(client):
    return client.connection_pool.connection_kwargs


def test_plain_client_has_health_check_and_keepalive():
    ck = _conn_kwargs(make_redis_client(_PLAIN_URL))
    # The two knobs that make a dead pooled socket recoverable.
    assert ck.get("health_check_interval") == 30, "default health check dropped"
    assert ck.get("socket_keepalive") is True, "socket_keepalive dropped"


def test_health_check_interval_env_override(monkeypatch):
    monkeypatch.setenv("PAPRIKA_REDIS_HEALTH_CHECK_INTERVAL", "5")
    ck = _conn_kwargs(make_redis_client(_PLAIN_URL))
    assert ck.get("health_check_interval") == 5


def test_bad_env_falls_back_to_default(monkeypatch):
    # A garbled env value must not crash client construction -- the hub would
    # fail to start. It falls back to the 30s default.
    monkeypatch.setenv("PAPRIKA_REDIS_HEALTH_CHECK_INTERVAL", "not-a-number")
    ck = _conn_kwargs(make_redis_client(_PLAIN_URL))
    assert ck.get("health_check_interval") == 30


def test_sentinel_url_still_carries_resilience():
    # Sentinel (HA) mode must inherit the same knobs; a stale master connection
    # is exactly as dangerous. Building the client does not connect, so this is
    # safe offline.
    client = make_redis_client(
        "redis+sentinel://sa:26379,sb:26379,sc:26379/paprika"
    )
    ck = _conn_kwargs(client)
    assert ck.get("health_check_interval") == 30
    assert ck.get("socket_keepalive") is True


def test_sentinel_url_without_hosts_is_rejected():
    # A malformed sentinel URL must fail loudly, not silently build a client
    # with zero sentinels that can never resolve a master.
    import pytest

    with pytest.raises(ValueError):
        make_redis_client("redis+sentinel:///paprika")


def test_is_sentinel_url_scheme_detection():
    assert store._is_sentinel_url("redis+sentinel://h:26379/svc")
    assert store._is_sentinel_url("sentinel://h:26379/svc")
    assert not store._is_sentinel_url("redis://h:6379/0")
    assert not store._is_sentinel_url("rediss://h:6379/0")
