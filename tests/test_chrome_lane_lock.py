"""Regression: a Chrome lane must not be wedged by a previous container's
profile lock.

2026-08-05. Chrome refuses a profile whose ``SingletonLock`` symlink names a
different host ("The profile appears to be in use by another Google Chrome
process (N) on another computer (HOST)") and the refusal never expires. While
lane dirs lived on the CT disk this was impossible -- the dir died with the
container. On the node-shared ramdisk (``/var/paprika/chrome/<worker_id>/``,
docs/ramdisk-chrome-lane.md) the dir OUTLIVES the container, and a container's
hostname is its docker id, so every ``docker compose up -d`` leaves a lock
Chrome will reject forever: lane start throws ``RuntimeError: lane 0: Chrome
:9223 failed to respond`` and the worker crash-loops at ~40s per cycle.

Fix under test: before launching Chrome, drop a lock written by a FOREIGN
host. Our own lock (a crashed process in this same container) is left alone --
Chrome recovers from that case by itself.

See memory: worker-chrome-profile-lock-restart-loop, ramdisk-chrome-lane.
"""

import pytest

from server.worker import lanes


class _Lane:
    """Just enough of a Lane to call the method under test."""

    lane_idx = 0

    _clear_foreign_singleton_lock = lanes.Lane._clear_foreign_singleton_lock


def _lane_for(tmp_path, monkeypatch, hostname):
    monkeypatch.setattr(lanes, "lane_user_data_dir", lambda idx: tmp_path)
    monkeypatch.setattr(lanes.socket, "gethostname", lambda: hostname)
    return _Lane()


def _symlink(path, target):
    try:
        path.symlink_to(target)
    except OSError as e:  # Windows without developer mode
        pytest.skip(f"cannot create symlinks here: {e}")


def test_foreign_lock_is_cleared(tmp_path, monkeypatch):
    lock = tmp_path / "SingletonLock"
    _symlink(lock, "261b261d1932-60991")     # a dead container's id
    cookie = tmp_path / "SingletonCookie"
    _symlink(cookie, "1044539126578771350")

    lane = _lane_for(tmp_path, monkeypatch, "bcdddcb7f55d")
    lane._clear_foreign_singleton_lock()

    assert not lock.is_symlink(), "stale lock left -> Chrome wedges forever"
    assert not cookie.is_symlink()


def test_our_own_lock_is_left_for_chrome(tmp_path, monkeypatch):
    """Same container, dead pid: Chrome clears that itself. Deleting it here
    would also delete a LIVE sibling process's lock."""
    lock = tmp_path / "SingletonLock"
    _symlink(lock, "bcdddcb7f55d-4242")
    cookie = tmp_path / "SingletonCookie"
    _symlink(cookie, "999")

    lane = _lane_for(tmp_path, monkeypatch, "bcdddcb7f55d")
    lane._clear_foreign_singleton_lock()

    assert lock.is_symlink()
    assert cookie.is_symlink()


def test_no_lock_is_a_noop(tmp_path, monkeypatch):
    lane = _lane_for(tmp_path, monkeypatch, "bcdddcb7f55d")
    lane._clear_foreign_singleton_lock()  # must not raise
    assert list(tmp_path.iterdir()) == []


def test_unparseable_lock_is_left_alone(tmp_path, monkeypatch):
    lock = tmp_path / "SingletonLock"
    _symlink(lock, "weird-target-without-a-pid-shape")
    lane = _lane_for(tmp_path, monkeypatch, "bcdddcb7f55d")
    lane._clear_foreign_singleton_lock()
    # "weird-target-without-a-pid" != our host -> foreign -> cleared. The point
    # of this test is only that we never raise on an odd target.
    assert True


def test_regular_file_is_not_touched(tmp_path, monkeypatch):
    """Only symlinks are Chrome's singleton markers; never unlink a real file."""
    lock = tmp_path / "SingletonLock"
    lock.write_text("not a symlink")
    lane = _lane_for(tmp_path, monkeypatch, "bcdddcb7f55d")
    lane._clear_foreign_singleton_lock()
    assert lock.exists()
