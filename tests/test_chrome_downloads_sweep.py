"""Regression: the worker must reclaim Chrome's own download directory.

yt-dlp writes into the ramdisk scratch pool, so the video path was believed
to be fully off-disk. It is not. A *browser-native* download -- a click, an
auto-download link, a Content-Disposition response -- is saved by Chrome to
its configured download dir, and paprika configures nothing, so it stays the
default ``$HOME/Downloads``. In the worker container that is /root/Downloads
on the container's writable layer = the node's LVM thin pool.

Measured 2026-08-17 on depot: 21.1 GB across 34 CTs (9% of a 234 GB pool),
growing ~350 MB/day/CT with no ceiling. About half was abandoned
``Unconfirmed NNNNNN.crdownload`` partials that Chrome never cleans up
because the session died mid-transfer. It propagates too -- a vzdump seed
taken from a running CT bakes its Downloads into every CT restored from it.

The dangerous direction is over-deletion: unlinking a transfer that is still
running. The mtime guard is what prevents that, so it is pinned here
alongside the reclaim itself.
"""

import os
import time
from pathlib import Path

import pytest

from server.worker.agent import WorkerAgent


HALF_HOUR = 1800.0


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A WorkerAgent skeleton sweeping ``tmp_path`` instead of ~/Downloads.

    __init__ opens sockets and reads the fleet environment, so the object is
    built unbound and given only the attributes the sweep touches.
    """
    monkeypatch.setenv("PAPRIKA_DOWNLOADS_SWEEP_ROOT", str(tmp_path))
    a = object.__new__(WorkerAgent)
    a.worker_id = "w50150"
    return a


def _write(path, size, age_s):
    """Create a file of ``size`` bytes, backdated by ``age_s`` seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def test_removes_stale_downloads_and_partials(agent, tmp_path):
    """Finished downloads AND abandoned .crdownload partials both go."""
    _write(tmp_path / "cos-ru2_720p.mp4", 2048, 2 * HALF_HOUR)
    _write(tmp_path / "Unconfirmed 760263.crdownload", 4096, 2 * HALF_HOUR)

    removed, freed = agent._sweep_chrome_downloads(HALF_HOUR)

    assert removed == 2
    assert freed == 2048 + 4096
    assert list(tmp_path.iterdir()) == []


def test_in_flight_download_is_kept(agent, tmp_path):
    """An active transfer keeps its mtime fresh -- never unlink it.

    This is the whole reason the sweep is age-gated rather than a plain
    'empty the directory'.
    """
    live = _write(tmp_path / "Unconfirmed 111111.crdownload", 999, 5.0)
    stale = _write(tmp_path / "old.mp4", 100, 2 * HALF_HOUR)

    removed, freed = agent._sweep_chrome_downloads(HALF_HOUR)

    assert removed == 1
    assert freed == 100
    assert live.exists()
    assert not stale.exists()


def test_sweeps_nested_files_but_never_removes_directories(agent, tmp_path):
    """Chrome nests a download when the site sends a directory-ish name.

    Those must be reclaimed, but the directories themselves must survive:
    rmdir'ing the root out from under a live Chrome would send it somewhere
    we do not sweep at all.
    """
    nested = _write(tmp_path / "album" / "track.mp3", 512, 2 * HALF_HOUR)

    removed, freed = agent._sweep_chrome_downloads(HALF_HOUR)

    assert removed == 1
    assert freed == 512
    assert not nested.exists()
    assert (tmp_path / "album").is_dir()


def test_missing_root_is_not_an_error(agent, tmp_path, monkeypatch):
    """A lane that never downloaded anything has no Downloads dir."""
    monkeypatch.setenv(
        "PAPRIKA_DOWNLOADS_SWEEP_ROOT", str(tmp_path / "does-not-exist"),
    )

    assert agent._sweep_chrome_downloads(HALF_HOUR) == (0, 0)


def test_root_defaults_to_home_downloads(agent, tmp_path, monkeypatch):
    """Without the override the sweep targets ``<home>/Downloads``.

    Pins the container-path assumption (/root/Downloads when HOME=/root): if
    this ever silently resolved elsewhere the sweep would run happily and
    reclaim nothing, which is exactly how the leak went unnoticed.

    Patches ``Path.home`` rather than $HOME so the test means the same thing
    on Windows, where home comes from %USERPROFILE% instead.
    """
    monkeypatch.delenv("PAPRIKA_DOWNLOADS_SWEEP_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Downloads").mkdir()

    assert agent._download_sweep_roots() == [tmp_path / "Downloads"]
