"""Node-side worker health scan: parsing and the trip conditions.

The detection rules matter more than usual here because this is the layer that
acts on containers NOBODY else can see -- by the time it fires, the worker's
own guard is gone. A false positive recycles a healthy worker; a false negative
leaves a container thrashing until an operator notices.
"""
from __future__ import annotations

import pytest

from server.hub import _ct_scan

_MB = 1024 * 1024
_GB = 1024 * _MB


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def test_parse_scan_line():
    line = ("ct=133 host=paprika-worker187 cur=6394085376 max=6442450944 "
            "anon=446464 file=6307475456 refault=1409128593 "
            "majfault=76381491 psi60=26.02")
    r = _ct_scan.parse_scan_line(line)
    assert r["ct"] == "133"
    assert r["host"] == "paprika-worker187"
    assert r["max"] == "6442450944"      # kept as text: may be the word "max"
    assert r["cur"] == 6394085376
    assert r["psi60"] == 26.02


def test_parse_handles_unlimited_and_junk():
    r = _ct_scan.parse_scan_line("ct=7 host=paprika-worker7 max=max cur=100 bogus")
    assert r["max"] == "max" and r["cur"] == 100


def test_parse_rejects_a_line_without_a_ct():
    assert _ct_scan.parse_scan_line("hello world") is None
    assert _ct_scan.parse_scan_line("") is None


# --------------------------------------------------------------------------
# trip conditions
# --------------------------------------------------------------------------

def _row(**kw):
    r = {"ct": "1", "host": "paprika-worker1", "cur": 3.0 * _GB,
         "max": str(8 * _GB), "anon": 2.0 * _GB, "file": 1.0 * _GB,
         "refault": 0.0, "majfault": 0.0, "psi60": 0.0}
    r.update(kw)
    return r


def test_healthy_worker_does_not_trip():
    prev = (0.0, 0, 0)
    assert _ct_scan.evaluate(_row(), prev, 30.0) == []


def test_busy_container_full_of_clean_cache_does_not_trip():
    """Measured on garage CT351: 6174MB of an 8192MB limit, refault 0.0/s,
    PSI 0.02 -- and completely healthy. A memcg fills with reclaimable cache by
    design, so "nearly full" must never be a trip condition on its own."""
    row = _row(cur=6174.0 * _MB, max=str(8192 * _MB), anon=2378.0 * _MB,
               file=3567.0 * _MB, psi60=0.02)
    assert _ct_scan.evaluate(row, (0.0, 0, 0), 30.0) == []


def test_ct133_fingerprint_trips():
    """The real thing, from hall CT133 on 2026-08-03: the cgroup is at 99% of
    its limit and essentially none of it is the worker's own memory, because
    the worker process is already dead. No rate is needed to see this."""
    row = _row(cur=6098.0 * _MB, max=str(6144 * _MB), anon=0.4 * _MB,
               file=6014.0 * _MB, psi60=20.57)
    reasons = _ct_scan.evaluate(row, None, 30.0)
    assert any("anon" in r for r in reasons)


def test_leak_at_the_wall_trips():
    """balcony CT337, 2026-08-03: 8191MB of an 8192MB limit with anon at 93%
    OF THE LIMIT. The mirror image of CT133 -- and the thrash rule cannot see
    it, because that one requires anon to be LOW. The CT was wedged
    (exec=hang) and every salvage stage failed, so missing it means missing a
    box that needs a human."""
    row = _row(cur=8191.0 * _MB, max=str(8192 * _MB), anon=7652.0 * _MB,
               file=400.0 * _MB, psi60=3.03)
    reasons = _ct_scan.evaluate(row, None, 30.0)
    assert any("leak at the wall" in r for r in reasons)


def test_healthy_balcony_cts_do_not_trip_the_leak_rule():
    """The busiest HEALTHY balcony CTs measured the same minute: 88% full with
    anon at 53% of the limit, and 87% full with anon at 40%. Neither may trip
    -- they were serving jobs normally."""
    for cur_mb, anon_mb in ((7221, 4310), (7203, 3289), (7503, 3526)):
        row = _row(cur=cur_mb * _MB, max=str(8192 * _MB), anon=anon_mb * _MB,
                   file=(cur_mb - anon_mb) * _MB, psi60=0.0)
        assert _ct_scan.evaluate(row, (0.0, 0, 0), 30.0) == [], f"{cur_mb}/{anon_mb}"


def test_the_two_wall_shapes_are_opposites():
    """Sanity: each rule must catch what the other cannot."""
    thrash = _row(cur=6098.0 * _MB, max=str(6144 * _MB), anon=0.4 * _MB, psi60=0.0)
    leak = _row(cur=8191.0 * _MB, max=str(8192 * _MB), anon=7652.0 * _MB, psi60=0.0)
    t = " ".join(_ct_scan.evaluate(thrash, None, 30.0))
    l = " ".join(_ct_scan.evaluate(leak, None, 30.0))
    assert "all cache" in t and "leak at the wall" not in t
    assert "leak at the wall" in l and "all cache" not in l


def test_leak_is_a_fast_signal_and_thrash_is_not():
    """Per-signal sustain. anon is monotonic -- balcony CT337 ran from the
    worker guard's threshold to a dead process faster than a 300s window, so
    waiting the full window only costs the worker. refault is bursty --
    w51175 hit 1938/s and cleared by itself in 94s -- so it MUST wait, or the
    scan recycles workers that were about to recover on their own."""
    assert _ct_scan.is_fast(["anon 93% of the cgroup limit (leak at the wall, ...)"])
    assert not _ct_scan.is_fast(["refault 2000/s >= 500/s"])
    assert not _ct_scan.is_fast(["memcg 99% full but only 0% anon (all cache)"])
    assert not _ct_scan.is_fast([])


def test_refault_storm_trips_on_rate():
    prev = (0.0, 0, 0)
    row = _row(refault=60_000.0)          # 2000/s over 30s
    reasons = _ct_scan.evaluate(row, prev, 30.0)
    assert any("refault" in r for r in reasons)


def test_cumulative_counters_alone_never_trip():
    """A healthy host carries millions of lifetime faults -- .34 measured
    14,534,336 at a rate of 0.3/s. Only the delta may be judged."""
    row = _row(refault=1_409_128_593.0, majfault=76_381_491.0)
    assert _ct_scan.evaluate(row, None, 30.0) == []


def test_counter_reset_is_not_a_storm():
    """The CT restarted between polls, so the counter went backwards. That must
    read as 'no information', not as a huge negative or absolute rate."""
    prev = (0.0, 9_000_000, 0)
    assert _ct_scan.evaluate(_row(refault=12.0), prev, 30.0) == []


def test_percentages_adapt_to_the_ct_limit():
    """hall runs 6GB CTs and boiler 8GB ones. The worker-side guard cannot see
    its own limit so it uses absolute thresholds; from the node the limit is
    readable, so the same numbers must work on both."""
    small = _row(cur=5.9 * _GB, max=str(6 * _GB), anon=1.0 * _MB, file=5.8 * _GB)
    large = _row(cur=5.9 * _GB, max=str(8 * _GB), anon=1.0 * _MB, file=5.8 * _GB)
    assert _ct_scan.evaluate(small, None, 30.0)      # 98% of 6GB -> trips
    assert _ct_scan.evaluate(large, None, 30.0) == []  # 74% of 8GB -> fine


def test_unlimited_cgroup_skips_the_percentage_rule():
    row = _row(cur=100.0 * _GB, max="max", anon=1.0 * _MB)
    assert _ct_scan.evaluate(row, None, 30.0) == []


def test_psi_trips_on_its_own():
    assert any("PSI" in r for r in _ct_scan.evaluate(_row(psi60=25.0), None, 30.0))


def test_thresholds_are_env_tunable(monkeypatch):
    monkeypatch.setenv("PAPRIKA_CTSCAN_PSI_PCT", "1")
    assert _ct_scan.evaluate(_row(psi60=5.0), None, 30.0)


def test_zero_threshold_disables_that_rule(monkeypatch):
    monkeypatch.setenv("PAPRIKA_CTSCAN_PSI_PCT", "0")
    assert _ct_scan.evaluate(_row(psi60=99.0), None, 30.0) == []


# --------------------------------------------------------------------------
# pass-level behaviour
# --------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self):
        self.keys: dict[str, str] = {}

    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.keys:
            return None
        self.keys[k] = v
        return True


@pytest.mark.asyncio
async def test_only_one_hub_runs_a_pass(monkeypatch):
    """All 7 hubs run this loop. Without a lease they each SSH every node every
    interval for identical data -- measured after the first deploy: six hubs
    hitting one node 3-4x per 10 minutes each -- and each would independently
    decide to restart the same sick container."""
    from server.hub import _salvage
    monkeypatch.setattr(_salvage, "_proxmox_nodes", lambda: [("n", "1.2.3.4")])
    monkeypatch.setattr(_salvage, "_proxmox_ssh", lambda: ("root", "22", "/k"))
    r = _FakeRedis()
    monkeypatch.setattr(_ct_scan, "_redis", lambda: r)

    async def fake_scan(name, addr):
        return []

    monkeypatch.setattr(_ct_scan, "_scan_node", fake_scan)
    first = await _ct_scan.scan_pass()
    second = await _ct_scan.scan_pass()
    assert "skipped" not in first
    assert second.get("skipped") == "lease"


@pytest.mark.asyncio
async def test_no_redis_still_scans(monkeypatch):
    """Dev / single hub: nobody to race, so the lease must not block."""
    from server.hub import _salvage
    monkeypatch.setattr(_salvage, "_proxmox_nodes", lambda: [("n", "1.2.3.4")])
    monkeypatch.setattr(_salvage, "_proxmox_ssh", lambda: ("root", "22", "/k"))
    monkeypatch.setattr(_ct_scan, "_redis", lambda: None)

    async def fake_scan(name, addr):
        return []

    monkeypatch.setattr(_ct_scan, "_scan_node", fake_scan)
    assert "skipped" not in await _ct_scan.scan_pass()


@pytest.mark.asyncio
async def test_scan_pass_is_inert_without_credentials(monkeypatch):
    """No proxmox key/nodes -> do nothing at all, and above all do not SSH."""
    from server.hub import _salvage
    monkeypatch.setattr(_salvage, "_proxmox_nodes", lambda: [])

    async def boom(*a, **kw):
        raise AssertionError("must not touch a node without credentials")

    monkeypatch.setattr(_ct_scan, "_scan_node", boom)
    assert (await _ct_scan.scan_pass())["cts"] == 0
