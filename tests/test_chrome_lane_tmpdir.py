"""Regression: Chrome's scratch must follow its profile onto the ramdisk.

docs/ramdisk-chrome-lane.md moved every lane's *user-data-dir* to a node
tmpfs, which cut CT rootfs writes 42% per unit of work. It left Chrome's
*scratch* behind: ScopedTempDirs (``scoped_dir*``) and -- because
--disable-dev-shm-usage is set -- its shm segments (``.com.google.Chrome.*``)
go to TMPDIR, which defaulted to the container's /tmp on the CT's LVM-thin
rootfs. That is the same write path the ramdisk work exists to avoid, and it
also grew without bound: measured across 11 fleet CTs on 2026-08-05, the six
without the hand-installed CT-side timer held 6.6-10.9 GB at 0-1d uptime.

Fix under test: a lane-private TMPDIR on the ramdisk, emptied on every
spawn. The wipe is what makes RAM a safe place to put this -- the mount is
shared by every CT on the node and has no per-directory quota, so an
unbounded leak there is worse than the disk leak it replaces (a full mount
kills every lane at once; Chrome cannot degrade gracefully out of ENOSPC).

The two directions that must never break:
  * inert without a ramdisk -- source rolls out ahead of the infra
  * never touch the profile -- that dir carries login state
"""

import pytest

from server.worker import lanes


class _Lane:
    """Just enough of a Lane to call the method under test."""

    lane_idx = 0

    _prepare_lane_tmpdir = lanes.Lane._prepare_lane_tmpdir

    def __init__(self):
        self._env = {}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """A lane whose 'ramdisk' is tmp_path. The tmpfs check is the one thing
    a test box cannot reproduce, so it is the one thing faked."""
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: True)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: tmp_path)
    # The free-space gate is exercised on its own below; a test box's disk
    # would otherwise decide the outcome of every other case here.
    monkeypatch.setenv("PAPRIKA_CHROME_TMP_MIN_FREE_MB", "0")
    return _Lane()


def _litter(d):
    """What Chrome leaves behind when its parent SIGKILLs it."""
    d.mkdir(parents=True, exist_ok=True)
    scoped = d / "scoped_dir4567_890"
    scoped.mkdir()
    (scoped / "payload").write_bytes(b"x" * 512)
    (d / ".com.google.Chrome.AbCdEf").write_bytes(b"x" * 512)
    return scoped


# --- inert without a ramdisk ------------------------------------------------


def test_no_ramdisk_leaves_tmpdir_alone(tmp_path, monkeypatch):
    """On the default /tmp this must be a no-op: the periodic sweep already
    covers that case, and changing Chrome's TMPDIR there would be a
    behaviour change with no write-path gain."""
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: False)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: tmp_path)
    lane = _Lane()
    lane._env["TMPDIR"] = "/inherited"

    lane._prepare_lane_tmpdir()

    assert lane._env["TMPDIR"] == "/inherited"
    assert not (tmp_path / "chrome-lane-0.tmp").exists()


def test_tmp_roots_empty_without_ramdisk(tmp_path, monkeypatch):
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: False)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: tmp_path)
    (tmp_path / "chrome-lane-0.tmp").mkdir()

    assert lanes.chrome_lane_tmp_roots() == []


# --- the redirect itself ----------------------------------------------------


def test_tmpdir_is_created_and_exported(lane, tmp_path):
    lane._prepare_lane_tmpdir()

    want = tmp_path / "chrome-lane-0.tmp"
    assert want.is_dir()
    assert lane._env["TMPDIR"] == str(want)


def test_scratch_is_wiped_on_every_spawn(lane, tmp_path):
    """The whole point: Chrome is dead at this instant, so everything in
    here is garbage by construction -- no age heuristic needed, unlike the
    shared /tmp where live and dead scratch are indistinguishable."""
    scoped = _litter(tmp_path / "chrome-lane-0.tmp")

    lane._prepare_lane_tmpdir()

    assert not scoped.exists()
    assert list((tmp_path / "chrome-lane-0.tmp").iterdir()) == []


def test_profile_dir_is_never_touched(lane, tmp_path):
    """The scratch dir is deliberately a SIBLING of the user-data-dir, not a
    child: this wipe runs on every spawn and the profile carries login
    state (cookies / Preferences)."""
    profile = tmp_path / "chrome-lane-0"
    profile.mkdir()
    (profile / "Default").mkdir()
    (profile / "Default" / "Cookies").write_bytes(b"secret")
    backup = tmp_path / "chrome-lane-0.lane-default"
    backup.mkdir()
    (backup / "keep").write_bytes(b"x")

    lane._prepare_lane_tmpdir()

    assert (profile / "Default" / "Cookies").read_bytes() == b"secret"
    assert (backup / "keep").exists()


def test_lanes_get_separate_dirs(tmp_path, monkeypatch):
    """Lane 1's respawn must not wipe lane 0's live scratch."""
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: True)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: tmp_path)
    monkeypatch.setenv("PAPRIKA_CHROME_TMP_MIN_FREE_MB", "0")
    assert lanes.lane_tmp_dir(0) != lanes.lane_tmp_dir(1)

    lane0 = _Lane()
    lane0._prepare_lane_tmpdir()
    live = _litter(lanes.lane_tmp_dir(0))

    lane1 = _Lane()
    lane1.lane_idx = 1
    lane1._prepare_lane_tmpdir()

    assert live.exists()


# --- failure never costs the lane -------------------------------------------


def test_unwritable_root_falls_back_to_default_tmpdir(tmp_path, monkeypatch):
    """A lane that cannot start is far worse than a lane writing scratch to
    disk, so any error here degrades to the pre-existing behaviour."""
    blocker = tmp_path / "chrome-lane-0.tmp"
    blocker.write_bytes(b"not a directory")
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: True)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: tmp_path)
    monkeypatch.setenv("PAPRIKA_CHROME_TMP_MIN_FREE_MB", "0")
    monkeypatch.setattr(
        lanes, "lane_tmp_dir", lambda idx: blocker / "nested",
    )
    lane = _Lane()
    lane._env["TMPDIR"] = "/stale/from/previous/spawn"

    lane._prepare_lane_tmpdir()

    assert "TMPDIR" not in lane._env


def test_full_ramdisk_degrades_to_disk_instead_of_killing_the_lane(
    tmp_path, monkeypatch,
):
    """/ram/chrome is shared by every CT on the node and Chrome cannot
    survive ENOSPC, so scratch must yield the moment the mount gets tight.
    The startup-time root check cannot cover this -- it runs once, and the
    mount fills hours later."""
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: True)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: tmp_path)
    monkeypatch.setenv("PAPRIKA_CHROME_TMP_MIN_FREE_MB", "1024")

    class _FullFS:
        f_bavail = 100
        f_frsize = 1024 * 1024      # 100 MB free, gate wants 1024

    # raising=False: os.statvfs is POSIX-only and absent on a Windows dev box.
    monkeypatch.setattr(lanes.os, "statvfs", lambda p: _FullFS(), raising=False)
    lane = _Lane()
    lane._env["TMPDIR"] = "/stale/from/previous/spawn"

    lane._prepare_lane_tmpdir()

    assert "TMPDIR" not in lane._env
    assert not (tmp_path / "chrome-lane-0.tmp").exists()


# --- the sweeper can find them ----------------------------------------------


def test_tmp_roots_lists_every_lane(tmp_path, monkeypatch):
    """The spawn-time wipe only bounds the leak per Chrome lifetime; a lane
    running for days without a respawn still needs the periodic sweep."""
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: True)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: tmp_path)
    (tmp_path / "chrome-lane-0.tmp").mkdir()
    (tmp_path / "chrome-lane-1.tmp").mkdir()
    # Neither the profile nor its swap backup is scratch.
    (tmp_path / "chrome-lane-0").mkdir()
    (tmp_path / "chrome-lane-0.lane-default").mkdir()

    roots = sorted(p.name for p in lanes.chrome_lane_tmp_roots())

    assert roots == ["chrome-lane-0.tmp", "chrome-lane-1.tmp"]
