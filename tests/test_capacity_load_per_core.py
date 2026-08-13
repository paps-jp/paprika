"""Regression: /workers/capacity must judge worker load PER CORE.

2026-08-09 incident: ``load1`` is a HOST number -- an LXC CT shares its
Proxmox node's ``getloadavg`` -- but ``_compute_capacity`` compared it to a
flat ``fetch_load_ref`` (24). On the 128-thread nodes reading load1 ~70
(0.55/core, 18% CPU, zero PSI) that pinned 64 of 151 workers at the 0.3
health floor, erasing 88 of 302 lanes. ``recommended`` fell under
``running + dl_occupancy`` so ``accept_new`` was false 85% of the time with
120-190 lanes actually idle, throttling the whole crawl pipeline to
~250 jobs/min while the fleet sat at ~50% utilisation.

Two halves must hold together, so both are pinned here:
  * the health formula normalises by ``nproc`` (this file), and
  * ``nproc`` reaches every hub's view via the Redis mirror
    (test_worker_snapshot_nproc_mirror.py) -- without that, the aggregated
    rows carry nproc=0 and the fallback path silently reinstates the bug.
"""

import asyncio
import types

import pytest

from server.hub import _state as _state_mod
from server.hub.routes import workers as workers_route


def _worker(wid, *, load1, nproc, capacity=2, in_flight=0, mem_pct=20.0):
    return {
        "worker_id": wid,
        "alive": True,
        "status": "active",
        "capacity": capacity,
        "in_flight": in_flight,
        "load1": load1,
        "nproc": nproc,
        "mem_pct": mem_pct,
        "disk_pct": 20.0,
        # _dispatchable() needs a lane preview URL, else the worker is
        # counted in max_concurrent but never in healthy_lanes.
        "lane_novnc_urls": ["http://x/1", "http://x/2"],
    }


class _Registry:
    def __init__(self, workers):
        self._workers = workers

    async def stats_async(self):
        return {"workers": self._workers}


class _Settings:
    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


class _Store:
    async def list_job_infos(self, status=None, limit=1):
        return [], 0


def _capacity(workers, **settings):
    """Run _compute_capacity against a synthetic fleet."""
    values = {
        # Isolate the load axis: no global haircut, no queue backoff.
        "fetch_load_factor": 1.0,
        "fetch_mem_ref": 75.0,
        "fetch_downloading_weight": 0.0,
    }
    values.update(settings)
    st = _state_mod.state
    saved = (st.registry, st.settings, st.store)
    st.registry = _Registry(workers)
    st.settings = _Settings(values)
    st.store = _Store()
    try:
        return asyncio.run(workers_route._compute_capacity())
    finally:
        st.registry, st.settings, st.store = saved


def test_busy_128_thread_node_is_not_discounted():
    """The exact incident signature: load1=70 on a 128-thread node.

    0.55 load/core with the default 1.0 ref -> fully healthy. Under the old
    flat ref=24 this scored 1 - (70-24)/24 = -0.9 -> clamped to 0.3.
    """
    caps = _capacity([_worker("w1", load1=70.0, nproc=128)])
    assert caps["healthy_lanes"] == 2.0
    assert caps["recommended_concurrency"] == 2
    assert caps["load_axis"] == {"per_core": 1, "fallback_abs": 0}


def test_genuinely_saturated_host_still_discounts():
    """The guard must survive the fix: 2.0 load/core hits the 0.3 floor."""
    caps = _capacity([_worker("w1", load1=256.0, nproc=128)])
    assert caps["healthy_lanes"] == pytest.approx(0.6, abs=0.05)


def test_partial_discount_scales_with_load_per_core():
    """1.5 load/core with ref 1.0 -> health 0.5 -> 1.0 of 2 lanes."""
    caps = _capacity([_worker("w1", load1=192.0, nproc=128)])
    assert caps["healthy_lanes"] == pytest.approx(1.0, abs=0.05)


def test_small_node_is_judged_on_its_own_core_count():
    """Same load1 as the 128-thread case, but a 32-thread node: 2.2/core.

    Normalising by nproc is what lets one threshold serve a heterogeneous
    fleet -- the flat ref could only ever be right for one node size.
    """
    caps = _capacity([_worker("w1", load1=70.0, nproc=32)])
    assert caps["healthy_lanes"] == pytest.approx(0.6, abs=0.05)


def test_worker_without_nproc_falls_back_to_absolute_ref():
    """A pre-nproc agent mid-rolling-update keeps the legacy behaviour."""
    caps = _capacity([_worker("w1", load1=70.0, nproc=0)], fetch_load_ref=24.0)
    assert caps["healthy_lanes"] == pytest.approx(0.6, abs=0.05)
    assert caps["load_axis"] == {"per_core": 0, "fallback_abs": 1}


def test_fallback_ref_does_not_gate_a_worker_that_reports_nproc():
    """The two refs are alternatives, not a two-stage gate.

    load1=70 blows past fetch_load_ref=24, but the worker reports nproc so
    only the per-core axis applies. Stacking them would have re-introduced
    the incident for every worker on a big node.
    """
    caps = _capacity([_worker("w1", load1=70.0, nproc=128)], fetch_load_ref=24.0)
    assert caps["healthy_lanes"] == 2.0


def test_idle_fleet_admits_work():
    """End-to-end shape: 10 idle workers on a busy 128-thread node.

    20 lanes, none running -> accept_new. This is the assertion that was
    false in production for 85% of capacity polls.
    """
    fleet = [_worker(f"w{i}", load1=70.0, nproc=128) for i in range(10)]
    caps = _capacity(fleet)
    assert caps["lanes"]["total"] == 20
    assert caps["recommended_free"] == 20
    assert caps["accept_new"] is True
