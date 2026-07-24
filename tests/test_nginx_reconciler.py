"""Regression tests for the nginx upstream reconciler's membership rule.

Anchored on the 2026-07-24 full-502 outage: presence and reachability failed in
OPPOSITE directions (``.40/.41`` registered but fd-exhausted, ``.35-.39``
serving /health but de-registered), and a presence-only reconciler kept exactly
the wrong two hubs.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "nginx_reconciler.py"


@pytest.fixture()
def recon(monkeypatch):
    """Import nginx_reconciler.py standalone with a stub ``redis`` module
    (the real redis-py is only present in the hub image)."""
    monkeypatch.setitem(sys.modules, "redis", types.ModuleType("redis"))
    spec = importlib.util.spec_from_file_location("_nginx_reconciler", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._STREAKS.clear()
    return mod


ALL = {f"10.10.50.{n}" for n in range(35, 42)}
DEAD = {"10.10.50.40", "10.10.50.41"}
LIVE = ALL - DEAD


def _settle(mod, registered, candidates, healthy, ticks):
    for _ in range(ticks):
        members = mod.decide_members(registered, candidates)
    return members


def test_outage_shape_drops_dead_registered_and_rescues_live_unregistered(
    recon, monkeypatch
):
    """The exact incident: only the two fd-exhausted hubs are registered, the
    five healthy ones are not. Membership must invert that."""
    monkeypatch.setattr(recon, "probe_health", lambda ip: ip in LIVE)
    # Tick 1: dead hubs not yet at DROP_STREAK, live ones not yet at RESCUE.
    assert recon.decide_members(DEAD, ALL) == DEAD
    # Tick 2: rescue streak (2) reached -> the live five join.
    assert recon.decide_members(DEAD, ALL) == DEAD | LIVE
    # Tick 3: drop streak (3) reached -> the dead two finally leave.
    assert recon.decide_members(DEAD, ALL) == LIVE


def test_single_probe_blip_does_not_evict_a_registered_hub(recon, monkeypatch):
    """A one-tick timeout on a busy hub must not pull it out of the pool."""
    flaky = "10.10.50.37"
    monkeypatch.setattr(recon, "probe_health", lambda ip: True)
    assert recon.decide_members(ALL, ALL) == ALL
    monkeypatch.setattr(recon, "probe_health", lambda ip: ip != flaky)
    assert recon.decide_members(ALL, ALL) == ALL  # 1 failure < DROP_STREAK
    assert recon.decide_members(ALL, ALL) == ALL  # 2 failures < DROP_STREAK
    assert recon.decide_members(ALL, ALL) == ALL - {flaky}  # 3rd -> dropped
    monkeypatch.setattr(recon, "probe_health", lambda ip: True)
    assert recon.decide_members(ALL, ALL) == ALL  # recovers on first success


def test_unhealthy_unregistered_candidate_is_never_adopted(recon, monkeypatch):
    """Index/conf candidates that answer nothing stay out however many ticks."""
    monkeypatch.setattr(recon, "probe_health", lambda ip: ip in LIVE)
    assert _settle(recon, LIVE, ALL, LIVE, ticks=5) == LIVE


def test_probe_disabled_falls_back_to_presence_only(recon, monkeypatch):
    """Kill switch restores the old behaviour verbatim."""
    monkeypatch.setattr(recon, "HEALTH_PROBE", False)
    monkeypatch.setattr(
        recon, "probe_health", lambda ip: pytest.fail("must not probe")
    )
    assert recon.decide_members(DEAD, ALL) == DEAD


def test_conf_ips_parses_current_upstream_block(recon):
    conf = """
    upstream hubs {
        server 10.10.50.35:8100 max_fails=3 fail_timeout=10s;
        server 10.10.50.36:8100 max_fails=3 fail_timeout=10s down;
        keepalive 64;
    }
    """
    assert recon.conf_ips(conf) == {"10.10.50.35", "10.10.50.36"}


def test_indexed_ips_skips_random_hostname_hub_ids(recon):
    class _R:
        def zrange(self, *_a, **_k):
            return [b"hub-35", b"a3b51cd4c697", b"hub-41"]

    assert recon.indexed_ips(_R()) == {"10.10.50.35", "10.10.50.41"}


def test_probe_health_rejects_non_paprika_200(recon, monkeypatch):
    """A stranger listening on the hub port must not be adopted."""

    class _Resp:
        status = 200

        def __init__(self, body):
            self._b = body

        def read(self, _n=None):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _Resp(b"<html>nginx</html>")
    )
    assert recon.probe_health("10.10.50.99") is False
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _Resp(b'{"status":"ok"}')
    )
    assert recon.probe_health("10.10.50.35") is True
