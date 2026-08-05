"""Regression: the worker's periodic /tmp sweep must reclaim Chrome's own
abandoned scratch, not just paprika-* dirs.

Chrome leaks two families into the system temp dir every time a parent
SIGKILLs it -- which in this pipeline is every lane swap, every Xvfb restart
and every container SIGTERM:

    scoped_dir*             base::ScopedTempDir that outlived its owner
    .com.google.Chrome.*    shm segments (on disk due to
                            --disable-dev-shm-usage)
    .org.chromium.*         renderer scratch

Nothing in the worker owned them. Measured 2026-08-05 across 11 fleet CTs:
the six lacking the hand-installed CT-side daily timer held 6.6-10.9 GB of
container /tmp at 0-1d uptime, versus 0.46-1.8 GB on the five that had it.

Two properties made the old sweep miss them, and both are pinned here:

  1. the prefix list only matched ``paprika-*``
  2. the loop skipped anything that wasn't a directory -- and the shm
     segments, the most numerous family by far, are plain files

The dangerous direction is over-deletion (removing scratch out from under a
live Chrome), so the age guard and the protection of our own live dirs are
pinned too.
"""

import os
import time

import pytest

from server.worker.agent import WorkerAgent


HOUR = 3600.0


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A WorkerAgent skeleton sweeping ``tmp_path`` instead of the real /tmp.

    __init__ opens sockets and reads the fleet environment, so the object is
    built unbound and given only the attributes the sweep touches.
    """
    monkeypatch.setenv("PAPRIKA_TMP_SWEEP_ROOT", str(tmp_path))
    a = object.__new__(WorkerAgent)
    a.worker_id = "w50150"
    a._sessions = {}
    a._bg_video_tasks = {}
    return a


def _age(path, seconds):
    """Backdate an entry so the sweep's mtime guard sees it as stale."""
    old = time.time() - seconds
    os.utime(path, (old, old))


def _mkdir(root, name, age_s):
    d = root / name
    d.mkdir()
    (d / "payload").write_bytes(b"x" * 1024)
    _age(d, age_s)
    return d


def _mkfile(root, name, age_s):
    f = root / name
    f.write_bytes(b"x" * 1024)
    _age(f, age_s)
    return f


# --- 1. the two families that used to survive ------------------------------


def test_sweeps_chrome_scoped_dirs(agent, tmp_path):
    d = _mkdir(tmp_path, "scoped_dir1234_5678", 2 * HOUR)
    removed, _, chrome = agent._sweep_tmp_orphans(1800.0, HOUR)
    assert not d.exists()
    assert (removed, chrome) == (1, 1)


def test_sweeps_undotted_singleton_socket_dirs(agent, tmp_path):
    """The numerous family, and the one the CT-side timer never matched.
    Counted on a prod worker 2026-08-05: 6704 undotted DIRECTORIES against
    1427 dotted files. A prefix list carrying only the dotted spelling --
    which is what scripts/worker-housekeep.sh greps for -- reclaims a small
    minority of the entries."""
    d = _mkdir(tmp_path, "com.google.Chrome.JO1sV1", 2 * HOUR)
    removed, _, chrome = agent._sweep_tmp_orphans(1800.0, HOUR)
    assert not d.exists()
    assert (removed, chrome) == (1, 1)


def test_live_singleton_socket_dir_is_spared(agent, tmp_path, monkeypatch):
    """The one entry in that family that must NOT go.

    A running Chrome keeps its socket dir for its whole lifetime, and the
    directory's mtime is its creation time -- observed a full day stale on a
    live browser -- so the age guard offers no protection at all here. The
    profile's SingletonSocket symlink is the evidence used instead.
    """
    from server.worker import lanes

    ram = tmp_path / "ram"
    profile = ram / "chrome-lane-0"
    profile.mkdir(parents=True)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: ram)

    live = _mkdir(tmp_path, "com.google.Chrome.LIVE01", 24 * HOUR)
    dead = _mkdir(tmp_path, "com.google.Chrome.DEAD02", 24 * HOUR)
    try:
        (profile / "SingletonSocket").symlink_to(live / "SingletonSocket")
    except OSError as e:  # Windows without developer mode
        pytest.skip(f"cannot create symlinks here: {e}")

    removed, _, chrome = agent._sweep_tmp_orphans(1800.0, HOUR)

    assert live.exists(), "deleted the socket dir of a running Chrome"
    assert not dead.exists()
    assert (removed, chrome) == (1, 1)


def test_chrome_half_sits_out_when_liveness_is_unknowable(
    agent, tmp_path, monkeypatch,
):
    """If the live set cannot be resolved, guessing costs a lane -- so the
    Chrome half of the sweep skips the pass entirely. Our own dirs, whose
    keep-set is independent, still get reclaimed."""
    import server.worker.lanes as lanes

    def _boom():
        raise RuntimeError("lanes unavailable")

    monkeypatch.setattr(lanes, "chrome_live_socket_dirs", _boom)

    socket_dir = _mkdir(tmp_path, "com.google.Chrome.Unknown", 24 * HOUR)
    scoped = _mkdir(tmp_path, "scoped_dir_old", 24 * HOUR)
    ours = _mkdir(tmp_path, "paprika-job9-xyz", 24 * HOUR)

    removed, _, chrome = agent._sweep_tmp_orphans(1800.0, HOUR)

    assert socket_dir.exists() and scoped.exists()
    assert not ours.exists()
    assert (removed, chrome) == (1, 0)


def test_sweeps_chrome_shm_files(agent, tmp_path):
    """The shm segments are FILES. The pre-fix sweep bailed on
    ``if not entry.is_dir(): continue``, which is why ~1300 of them per CT
    accumulated while the dir-shaped leaks were at least occasionally
    reclaimed by the emergency path."""
    f = _mkfile(tmp_path, ".com.google.Chrome.AbCdEf", 2 * HOUR)
    removed, freed, chrome = agent._sweep_tmp_orphans(1800.0, HOUR)
    assert not f.exists()
    assert (removed, chrome) == (1, 1)
    assert freed == 1024


def test_sweeps_chromium_renderer_scratch(agent, tmp_path):
    d = _mkdir(tmp_path, ".org.chromium.Chromium.XyZ123", 2 * HOUR)
    agent._sweep_tmp_orphans(1800.0, HOUR)
    assert not d.exists()


# --- 2. over-deletion guards ------------------------------------------------


def test_young_chrome_entries_are_kept(agent, tmp_path):
    """Age is the ONLY evidence available for Chrome's entries -- they carry
    no job or session id to match against the live sets -- so a too-short
    window would delete scratch out from under a running browser."""
    d = _mkdir(tmp_path, "scoped_dir_live", 10 * 60)
    f = _mkfile(tmp_path, ".com.google.Chrome.Live", 10 * 60)
    removed, _, chrome = agent._sweep_tmp_orphans(1800.0, HOUR)
    assert d.exists() and f.exists()
    assert (removed, chrome) == (0, 0)


def test_chrome_sweep_disabled_by_zero(agent, tmp_path):
    """Kill switch: PAPRIKA_TMP_SWEEP_CHROME_MIN_AGE_S=0 reverts to the
    pre-fix behaviour without a redeploy."""
    d = _mkdir(tmp_path, "scoped_dir_old", 24 * HOUR)
    f = _mkfile(tmp_path, ".com.google.Chrome.Old", 24 * HOUR)
    removed, _, chrome = agent._sweep_tmp_orphans(1800.0, 0.0)
    assert d.exists() and f.exists()
    assert (removed, chrome) == (0, 0)


def test_unrelated_tmp_entries_are_never_touched(agent, tmp_path):
    """The sweep runs in a shared /tmp. Anything outside the two prefix
    lists is not ours to delete, however old it is."""
    keep_d = _mkdir(tmp_path, "systemd-private-abc", 24 * HOUR)
    keep_f = _mkfile(tmp_path, "some-other-tool.sock", 24 * HOUR)
    removed, _, _ = agent._sweep_tmp_orphans(1800.0, HOUR)
    assert keep_d.exists() and keep_f.exists()
    assert removed == 0


# --- 3. the paprika-* half still behaves ------------------------------------


def test_live_session_dir_survives(agent, tmp_path):
    """The keep-set must still win for our own dirs: a live session's
    scratch is old enough to trip the age guard on any long job."""
    agent._sessions = {"sess-abc": object()}
    live = _mkdir(tmp_path, "paprika-ses-sess-abc-xyz", 24 * HOUR)
    dead = _mkdir(tmp_path, "paprika-ses-sess-gone-xyz", 24 * HOUR)
    agent._sweep_tmp_orphans(1800.0, HOUR)
    assert live.exists()
    assert not dead.exists()


def test_protected_caches_survive(agent, tmp_path):
    cache = _mkdir(tmp_path, "paprika-profile-cache", 24 * HOUR)
    exts = _mkdir(tmp_path, "paprika-extensions", 24 * HOUR)
    agent._sweep_tmp_orphans(1800.0, HOUR)
    assert cache.exists() and exts.exists()


def test_paprika_files_are_still_ignored(agent, tmp_path):
    """Only Chrome's family gained file handling. A stray paprika-* FILE is
    not a known leak shape, so the sweep leaves it alone rather than
    guessing."""
    f = _mkfile(tmp_path, "paprika-something.txt", 24 * HOUR)
    agent._sweep_tmp_orphans(1800.0, HOUR)
    assert f.exists()


# --- 4. mixed pass ----------------------------------------------------------


def test_sweep_follows_redirected_lane_tmpdirs(agent, tmp_path, monkeypatch):
    """Once Chrome's TMPDIR is redirected onto the ramdisk
    (lanes._prepare_lane_tmpdir) its leftovers stop arriving in the system
    tmp dir. The spawn-time wipe bounds them per Chrome lifetime, but a lane
    that runs for days without a respawn still accumulates -- and on a
    node-shared tmpfs with no per-directory quota that is worse than the
    disk leak it replaced."""
    from server.worker import lanes

    ram = tmp_path / "ram"
    lane_tmp = ram / "chrome-lane-0.tmp"
    lane_tmp.mkdir(parents=True)
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: True)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: ram)

    stale = _mkdir(lane_tmp, "scoped_dir_stale", 2 * HOUR)
    shm = _mkfile(lane_tmp, ".com.google.Chrome.Stale", 2 * HOUR)
    young = _mkdir(lane_tmp, "scoped_dir_live", 10 * 60)

    removed, _, chrome = agent._sweep_tmp_orphans(1800.0, HOUR)

    assert not stale.exists() and not shm.exists()
    assert young.exists()
    assert (removed, chrome) == (2, 2)


def test_sweep_still_covers_system_tmp_with_a_ramdisk(agent, tmp_path, monkeypatch):
    """Both roots, not either/or: a lane whose tmpdir prep failed falls back
    to the inherited TMPDIR, so the system dir never stops being a source."""
    from server.worker import lanes

    ram = tmp_path / "ram"
    (ram / "chrome-lane-0.tmp").mkdir(parents=True)
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: True)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: ram)

    system = _mkdir(tmp_path, "scoped_dir_in_system_tmp", 2 * HOUR)

    agent._sweep_tmp_orphans(1800.0, HOUR)

    assert not system.exists()


def test_mixed_pass_counts_chrome_separately(agent, tmp_path):
    """The log line splits the two so an operator can see which half is
    doing the reclaiming."""
    _mkdir(tmp_path, "scoped_dir_a", 2 * HOUR)
    _mkfile(tmp_path, ".com.google.Chrome.B", 2 * HOUR)
    _mkdir(tmp_path, "paprika-job1-xyz", 2 * HOUR)
    removed, freed, chrome = agent._sweep_tmp_orphans(1800.0, HOUR)
    assert (removed, chrome) == (3, 2)
    assert freed == 3 * 1024
