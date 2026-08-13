"""Regression: Chrome core dumps must not accumulate on the CT rootfs.

The nodes run the kernel default ``core_pattern`` ("core" = the crashing
process's CWD, /app in the worker container) with RLIMIT_CORE unlimited, so
every Chrome crash wrote a 0.2-8 GB core file straight into the LVM thin pool.
Measured on foyer 2026-08-13: 51-101 files / 3.8-8.0 GB per CT, ~105 GB across
22 CTs in five days -- pve/data went 41% -> 100% and seven CTs dropped into
ext4 emergency read-only (not recoverable from inside the CT).

Two layers, both pinned here:

  1. ``server/__main__.py`` drops RLIMIT_CORE's soft limit to 0 before any
     lane spawns, so children never dump in the first place.
  2. this sweep clears what earlier worker versions already wrote, and bounds
     the footprint if the rlimit is opted out of (PAPRIKA_ALLOW_CORE_DUMPS=1).

The dangerous direction is over-deletion: ``/app/core`` is ALSO the paprika
``core/`` package directory, and a name-only match would delete the running
worker's own source tree. That case gets its own test.
"""

import os
import re
import time
from pathlib import Path

import pytest

from server.worker.agent import WorkerAgent


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A WorkerAgent skeleton sweeping ``tmp_path`` instead of the real CWD."""
    monkeypatch.setenv("PAPRIKA_CORE_SWEEP_ROOT", str(tmp_path))
    a = object.__new__(WorkerAgent)
    a.worker_id = "w51027"
    return a


def _core(root: Path, name: str, age_s: float, size: int = 4096) -> Path:
    p = root / name
    p.write_bytes(b"\0" * size)
    old = time.time() - age_s
    os.utime(p, (old, old))
    return p


def test_removes_stale_core_dumps(agent, tmp_path):
    """core.<pid> older than the window goes, and the bytes are reported."""
    _core(tmp_path, "core.11142", 600, size=8192)
    _core(tmp_path, "core.42887", 600, size=4096)

    removed, freed = agent._sweep_core_dumps(120.0)

    assert removed == 2
    assert freed == 12288
    assert not list(tmp_path.glob("core.*"))


def test_removes_suffixless_core_file(agent, tmp_path):
    """Without kernel.core_uses_pid the dump is just ``core``."""
    _core(tmp_path, "core", 600)

    removed, _ = agent._sweep_core_dumps(120.0)

    assert removed == 1


def test_never_deletes_the_core_package_directory(agent, tmp_path):
    """/app/core is paprika's own source tree -- deleting it kills the worker.

    Same name as a dump, so only the is_file() check separates them.
    """
    pkg = tmp_path / "core"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "fetcher.py").write_text("# paprika source\n")
    old = time.time() - 86400
    os.utime(pkg, (old, old))

    removed, freed = agent._sweep_core_dumps(120.0)

    assert removed == 0
    assert freed == 0
    assert (pkg / "fetcher.py").exists()


def test_spares_a_dump_still_being_written(agent, tmp_path):
    """A fresh core may still be landing; the kernel holds the fd anyway."""
    _core(tmp_path, "core.99999", 5)

    removed, _ = agent._sweep_core_dumps(120.0)

    assert removed == 0
    assert (tmp_path / "core.99999").exists()


def test_ignores_unrelated_names(agent, tmp_path):
    """Only ``core`` / ``core.<digits>``. Nothing else in /app is fair game."""
    for name in ("core.py", "corefile", "core.abc", "scorecard", "core.1.bak"):
        _core(tmp_path, name, 600)

    removed, _ = agent._sweep_core_dumps(120.0)

    assert removed == 0
    assert len(list(tmp_path.iterdir())) == 5


def test_missing_root_is_not_an_error(agent, monkeypatch, tmp_path):
    """A worker whose CWD vanished must not crash the maintenance loop."""
    monkeypatch.setenv("PAPRIKA_CORE_SWEEP_ROOT", str(tmp_path / "gone"))

    assert agent._sweep_core_dumps(120.0) == (0, 0)


def test_startup_disables_core_dumps():
    """__main__ must drop RLIMIT_CORE's soft limit, not just sweep after.

    Pinned as source, not behaviour: importing the worker entrypoint runs
    argparse and the fleet environment probe.
    """
    src = Path(__file__).resolve().parents[1] / "server" / "__main__.py"
    text = src.read_text(encoding="utf-8")

    assert "RLIMIT_CORE" in text
    assert re.search(r"setrlimit\(\s*_resource\.RLIMIT_CORE,\s*\(0,", text)
    # The opt-out must exist, and the hard limit must be preserved so a
    # debugging session can raise it back.
    assert "PAPRIKA_ALLOW_CORE_DUMPS" in text
    assert "(0, _hard)" in text
