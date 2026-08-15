"""Memory guard: cgroup parsing, rate maths, and the trip thresholds.

Covers the pieces that decide whether a production worker recycles itself, so
they can be exercised without a cgroup, a container, or a hub. The numbers in
the threshold tests are the ones measured on boiler (10.10.50.15) worker CTs
356/365/382 on 2026-08-03 while healthy and running jobs -- if a future tuning
pass makes those look like distress, these fail loudly.
"""
from __future__ import annotations

import textwrap

import pytest

from server.worker import cgroup_mem


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

_STAT = textwrap.dedent(
    """\
    anon 1594638336
    file 1939845120
    shmem 761643008
    slab 99692336
    workingset_refault_file 231516344
    pgmajfault 81309
    """
)

_PRESSURE = textwrap.dedent(
    """\
    some avg10=0.00 avg60=1.25 avg300=0.03 total=2547568
    full avg10=0.00 avg60=0.75 avg300=0.03 total=2547533
    """
)


def _install(tmp_path, monkeypatch, *, current="3645992960", limit="max",
             stat=_STAT, pressure=_PRESSURE):
    """Point cgroup_mem at a fake cgroup directory."""
    (tmp_path / "memory.current").write_text(current, encoding="utf-8")
    if limit is not None:
        (tmp_path / "memory.max").write_text(limit, encoding="utf-8")
    if stat is not None:
        (tmp_path / "memory.stat").write_text(stat, encoding="utf-8")
    if pressure is not None:
        (tmp_path / "memory.pressure").write_text(pressure, encoding="utf-8")
    monkeypatch.setattr(cgroup_mem, "CGROUP_ROOT", str(tmp_path))


def test_sample_reads_stat_and_pressure(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch)
    s = cgroup_mem.sample()
    assert s.ok
    assert s.current == 3645992960
    assert s.anon == 1594638336
    assert s.shmem == 761643008
    assert s.pgmajfault == 81309
    assert s.refault_file == 231516344
    assert s.psi_some_avg60 == 1.25
    assert s.psi_full_avg60 == 0.75


def test_unlimited_cgroup_reports_no_percentage(tmp_path, monkeypatch):
    """``memory.max`` == ``max`` is the PRODUCTION case: the container has no
    limit of its own because the 8GB cap lives on the parent CT cgroup. A
    percentage here would be a fabrication, so limit_pct must be None -- that
    None is what makes the heartbeat label the scope 'host' instead of
    presenting the node's memory as the worker's."""
    _install(tmp_path, monkeypatch, limit="max")
    s = cgroup_mem.sample()
    assert s.limit == 0
    assert s.limit_pct is None


def test_env_declared_limit_enables_percentage(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, limit="max")
    monkeypatch.setenv("PAPRIKA_WORKER_MEM_LIMIT_MB", "8192")
    s = cgroup_mem.sample()
    assert s.limit == 8192 * 1024 * 1024
    assert s.limit_pct == pytest.approx(42.4, abs=0.5)


def test_real_limit_wins_over_env(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, limit=str(4 * 1024 * 1024 * 1024))
    monkeypatch.setenv("PAPRIKA_WORKER_MEM_LIMIT_MB", "8192")
    assert cgroup_mem.sample().limit == 4 * 1024 * 1024 * 1024


def test_missing_controller_is_not_healthy(tmp_path, monkeypatch):
    """cgroup v1 host / no controller must read as 'no signal', never as 0%.
    The guard stays inert on ok=False; treating it as healthy would be fine,
    but treating it as 0 anon + 0 faults would silently disarm every trigger."""
    monkeypatch.setattr(cgroup_mem, "CGROUP_ROOT", str(tmp_path))
    assert cgroup_mem.available() is False
    assert cgroup_mem.sample().ok is False


def test_garbage_fields_are_skipped_not_fatal(tmp_path, monkeypatch):
    _install(
        tmp_path, monkeypatch,
        stat="anon notanumber\npgmajfault 5\n",
        pressure="some avg10=x avg60=2.0 total=1\ngarbage\n",
    )
    s = cgroup_mem.sample()
    assert s.ok
    assert s.anon == 0
    assert s.pgmajfault == 5
    assert s.psi_some_avg60 == 2.0


# --------------------------------------------------------------------------
# rate maths
# --------------------------------------------------------------------------

def _s(**kw) -> cgroup_mem.MemSample:
    kw.setdefault("ok", True)
    return cgroup_mem.MemSample(**kw)


def test_majfault_rate_basic():
    prev, cur = _s(pgmajfault=1000), _s(pgmajfault=4000)
    assert cgroup_mem.majfault_rate(prev, cur, 30.0) == pytest.approx(100.0)


def test_counter_reset_yields_zero_not_negative():
    """The container restarting under us resets the counter. A negative rate
    would be meaningless; a huge positive one (from abs()) would trip the
    guard on a worker that just came up clean."""
    prev, cur = _s(pgmajfault=9_000_000), _s(pgmajfault=12)
    assert cgroup_mem.majfault_rate(prev, cur, 30.0) == 0.0


@pytest.mark.parametrize("dt", [0.0, -1.0])
def test_non_positive_interval_yields_zero(dt):
    assert cgroup_mem.majfault_rate(_s(pgmajfault=0), _s(pgmajfault=99), dt) == 0.0


def test_rate_requires_two_usable_samples():
    assert cgroup_mem.majfault_rate(
        cgroup_mem.MemSample(ok=False), _s(pgmajfault=99), 10.0
    ) == 0.0


def test_refault_rate_tracks_its_own_counter():
    prev, cur = _s(refault_file=0), _s(refault_file=6000)
    assert cgroup_mem.refault_rate(prev, cur, 60.0) == pytest.approx(100.0)


# --------------------------------------------------------------------------
# trip thresholds
# --------------------------------------------------------------------------

from server.worker.agent._mix_run import _RunMixin


class _Agent(_RunMixin):
    """The real mixin, with none of the WorkerAgent around it.

    ``_memguard_breaches`` deliberately touches nothing but its arguments and
    the class-level thresholds, so it is testable without a hub, a websocket
    or a lane pool -- which is the point: this is the code path that decides
    whether a production worker throws away its in-flight work.
    """


_MB = 1024 * 1024


def test_healthy_boiler_baseline_does_not_trip():
    """Measured 2026-08-03 on CT382 while running jobs: anon 2.1GB, 67
    majfaults/s, PSI 0.00. Nothing here may look like distress."""
    prev = _s(pgmajfault=99_067)
    cur = _s(pgmajfault=101_148, anon=2100 * _MB, psi={"some_avg60": 0.0})
    assert _Agent()._memguard_breaches(prev, cur, 31.0) == []


def test_anon_leak_trips():
    cur = _s(anon=7100 * _MB, psi={"some_avg60": 0.0})
    reasons = _Agent()._memguard_breaches(None, cur, 0.0)
    assert len(reasons) == 1 and "anon" in reasons[0]


def test_anon_level_alone_is_quiet_below_the_raised_threshold():
    """The absolute threshold moved 5500 -> 7000MB on 2026-08-14 because the
    CT caps had moved 8192 -> 12288MB without it. 6676MB is the exact level
    w5148 warned at and then recovered from twelve samples later -- a flat
    worker at that level is not distressed and must not be recycled."""
    cur = _s(anon=6676 * _MB, psi={"some_avg60": 0.0})
    assert _Agent()._memguard_breaches(_s(anon=6676 * _MB), cur, 30.0) == []


def test_fast_anon_climb_trips_on_the_rate_before_the_level():
    """w51177, measured 2026-08-14: 5544 -> 8448MB in 2.7 minutes. The level
    axis alone would still be waiting at the first of those samples while the
    kernel OOM-killed the process at 10.4GB. One guard interval of that slope
    (540MB / 30s = 1080MB/min) has to be enough to start counting."""
    prev, cur = _s(anon=6000 * _MB), _s(anon=6540 * _MB, psi={"some_avg60": 0.0})
    reasons = _Agent()._memguard_breaches(prev, cur, 30.0)
    assert len(reasons) == 1 and "climbing" in reasons[0]


def test_startup_ramp_does_not_trip_the_rate_axis():
    """A worker booting its Chrome lanes climbs 400MB -> 2GB in a couple of
    minutes, which is steeper than the leak rate. The floor is what separates
    them: the ramp ends far below it, the leak happens above it. Without this
    gate the rate axis would recycle every worker during its own startup."""
    prev, cur = _s(anon=400 * _MB), _s(anon=700 * _MB, psi={"some_avg60": 0.0})
    assert _Agent()._memguard_breaches(prev, cur, 30.0) == []


def test_slow_drift_above_the_floor_does_not_trip_the_rate_axis():
    """Being above the floor is not itself a breach -- the slope has to be
    there too, or every busy worker in the 3.5-7GB band would recycle."""
    prev, cur = _s(anon=6000 * _MB), _s(anon=6025 * _MB, psi={"some_avg60": 0.0})
    assert _Agent()._memguard_breaches(prev, cur, 30.0) == []


def test_thrash_trips_on_majfault_rate():
    prev = _s(pgmajfault=0)
    cur = _s(pgmajfault=120_000, anon=1000 * _MB, psi={"some_avg60": 0.0})
    reasons = _Agent()._memguard_breaches(prev, cur, 30.0)  # 4000/s
    assert len(reasons) == 1 and "majfault" in reasons[0]


def test_refault_storm_trips():
    """The signal the other three miss. garage 2026-08-03 showed why it was
    needed: workers sat at 6.1GB of an 8GB memcg with anon 2.4GB, PSI 0.02 and
    majfault 143/s -- none of which trips -- so a cache-thrash-shaped failure
    had no trigger at all until this one."""
    prev = _s(refault_file=0)
    cur = _s(refault_file=60_000, anon=2400 * _MB, psi={"some_avg60": 0.02})
    reasons = _Agent()._memguard_breaches(prev, cur, 30.0)  # 2000/s
    assert len(reasons) == 1 and "refault" in reasons[0]


def test_full_but_clean_cache_does_not_trip():
    """THE false positive to avoid. Measured on garage CT351 2026-08-03:
    current 6174MB of an 8192MB limit -- 75% full -- with refault 0.0/s and
    PSI 0.02. That is a healthy busy worker, because a memcg fills with clean
    reclaimable page cache up to its limit by design. A ``current > 80%``
    trigger was proposed after that day's crashes and would have recycled most
    of the fleet; what separates a storm from a full cache is whether the
    cache is being RE-READ."""
    prev = _s(refault_file=1_000_000, pgmajfault=500_000)
    cur = _s(
        refault_file=1_000_000,           # not moving = not thrashing
        pgmajfault=500_000 + 4281,        # 142.7/s, the measured garage value
        anon=2378 * _MB,
        current=6174 * _MB,
        file=3567 * _MB,
        limit=8192 * _MB,
        psi={"some_avg60": 0.02},
    )
    assert _Agent()._memguard_breaches(prev, cur, 30.0) == []


def test_refault_threshold_is_env_tunable(monkeypatch):
    monkeypatch.setenv("PAPRIKA_MEMGUARD_REFAULT_PER_S", "10")
    prev, cur = _s(refault_file=0), _s(
        refault_file=600, anon=100 * _MB, psi={"some_avg60": 0.0})
    assert any("refault" in r for r in _Agent()._memguard_breaches(prev, cur, 30.0))


def test_psi_trips_on_its_own():
    """PSI is the signal a percentage-of-limit gauge structurally cannot show:
    anon is modest and the fault counter isn't sampled yet, but tasks are
    stalled a quarter of the time."""
    cur = _s(anon=1000 * _MB, psi={"some_avg60": 25.0})
    reasons = _Agent()._memguard_breaches(None, cur, 0.0)
    assert len(reasons) == 1 and "PSI" in reasons[0]


def test_first_sample_cannot_trip_on_rate():
    """With no previous sample there is no rate -- a fresh worker whose cgroup
    already shows millions of cumulative faults must not trip instantly."""
    cur = _s(pgmajfault=18_376_645, anon=1000 * _MB, psi={"some_avg60": 0.0})
    assert _Agent()._memguard_breaches(None, cur, 0.0) == []


def test_thresholds_are_env_tunable(monkeypatch):
    monkeypatch.setenv("PAPRIKA_MEMGUARD_ANON_MB", "1500")
    cur = _s(anon=2000 * _MB, psi={"some_avg60": 0.0})
    assert _Agent()._memguard_breaches(None, cur, 0.0)


def test_zero_threshold_disables_that_signal(monkeypatch):
    """A deliberate 0 must turn a trigger OFF -- distinct from 'unset', which
    keeps the shipped default. Operators need to disable one noisy signal
    without disarming the whole guard."""
    monkeypatch.setenv("PAPRIKA_MEMGUARD_ANON_MB", "0")
    cur = _s(anon=99_000 * _MB, psi={"some_avg60": 0.0})
    assert _Agent()._memguard_breaches(None, cur, 0.0) == []


def test_garbage_threshold_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PAPRIKA_MEMGUARD_ANON_MB", "not-a-number")
    cur = _s(anon=2100 * _MB, psi={"some_avg60": 0.0})
    assert _Agent()._memguard_breaches(None, cur, 0.0) == []


def test_multiple_signals_are_all_reported():
    prev = _s(pgmajfault=0)
    cur = _s(pgmajfault=120_000, anon=6000 * _MB, psi={"some_avg60": 40.0})
    assert len(_Agent()._memguard_breaches(prev, cur, 30.0)) == 3


# ---------------------------------------------------------------------------
# The slope has to reach the operator, not just the guard
# ---------------------------------------------------------------------------

def test_anon_growth_rate_reaches_the_workers_view():
    """The rate axis exists because the level thresholds cannot see a fast
    climb in time -- measured on balcony w51177 (2026-08-14), anon went
    5544MB -> 8448MB in 2.7 minutes and the kernel OOM-killed the process
    before the level window could complete.

    Computing it is only half the job: the whole point of stashing it on the
    agent is that the heartbeat carries it, so a building storm is visible in
    the Workers tab BEFORE the guard trips. This pins the whole path --
    protocol field, heartbeat send, scheduler mirror, /workers row -- because
    a break anywhere in it is silent: the guard still works, the operator just
    cannot see the slope coming."""
    import inspect
    from server import protocol, scheduler
    from server.hub.routes import workers as workers_route
    from server.worker.agent import _mix_run

    # protocol: the field exists on the heartbeat
    assert "mem_anon_rate_mb_min" in protocol.WorkerHeartbeat.model_fields

    # worker: the heartbeat sends what the guard stashed
    hb = inspect.getsource(_mix_run._RunMixin._heartbeat_loop)
    assert "mem_anon_rate_mb_min=self._memguard_anon_rate_mb_min" in hb

    # hub: the heartbeat handler passes it on
    assert "mem_anon_rate_mb_min=" in inspect.getsource(workers_route)

    # scheduler: it lands on the worker and is mirrored into the row
    assert "mem_anon_rate_mb_min" in scheduler.ConnectedWorker.__dataclass_fields__
    sched = inspect.getsource(scheduler)
    assert 'worker.mem_anon_rate_mb_min = mem_anon_rate_mb_min' in sched
    # FOUR sites, and the first version of this test only checked one of them
    # -- the field then reached the hub and stopped, invisible on 0/170 rows:
    #   1. the Redis snapshot mirror (tuple form)
    #   2. the local row builder that /workers serves
    #   3. the cross-hub restore that rebuilds a peer's row from Redis
    assert '("mem_anon_rate_mb_min",' in sched, "redis snapshot mirror"
    assert '"mem_anon_rate_mb_min": round(' in sched, "local row builder"
    assert 'data.get("mem_anon_rate_mb_min")' in sched, "cross-hub restore"
