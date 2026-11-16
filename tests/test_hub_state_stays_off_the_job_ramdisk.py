"""The hub's own state stays in ``config.data_dir`` when job files move away.

Job files (pages, images, logs) can be pointed at a ramdisk with the
``storage_dir`` setting, so that nothing a crawl produces is written to the
hub's SSD (operator, 2026-11-16: "画像も同様にしてほしい。SSDではなく ramdisk").
That directory is bounded, evicted after a few minutes and empty after a
reboot.

Four things used to be kept under the same path without being a job's file:
per-host knowledge (written by the distillers, 1.1 GB on a production hub),
the AI call log (only grows, nothing evicts it), its offloaded blobs, and
operator recordings. On a ramdisk they would have been lost at the next
reboot, or would have filled the space job files need. The routes that read
per-host knowledge already looked in ``config.data_dir``, so with the two
directories apart the writers and the readers would also have disagreed.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from server.hub import _state

HUB = Path(__file__).resolve().parents[1] / "server" / "hub"


class _Settings:
    def __init__(self, storage_dir):
        self._sd = str(storage_dir)

    def get(self, key, default=None):
        return self._sd if key == "storage_dir" else default


@pytest.fixture
def split(tmp_path, monkeypatch):
    """Hub state on ``disk``, job files on ``ram`` -- two different places."""
    disk = tmp_path / "disk"
    ram = tmp_path / "ram"
    disk.mkdir()
    ram.mkdir()
    monkeypatch.setattr(_state.config, "data_dir", disk)
    monkeypatch.setattr(_state.state, "settings", _Settings(ram))
    assert _state.get_storage_dir() == ram
    return disk, ram


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# --- the rule ---------------------------------------------------------------------


def test_the_job_files_directory_maps_to_the_hubs_own(split):
    disk, ram = split
    assert _state.state_dir_for(ram) == disk
    assert _state.state_dir_for(str(ram)) == disk


def test_any_other_directory_is_left_alone(split, tmp_path):
    other = tmp_path / "a-tool-or-a-test"
    assert _state.state_dir_for(other) == other


def test_one_directory_for_both_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(_state.config, "data_dir", tmp_path)
    monkeypatch.setattr(_state.state, "settings", _Settings(tmp_path))
    assert _state.state_dir_for(tmp_path) == tmp_path


# --- per-host knowledge -----------------------------------------------------------


def test_the_light_distiller_writes_knowledge_to_disk(split):
    from server.hub import distiller_light

    disk, ram = split
    # called the way the hub calls it: data_dir=get_storage_dir()
    assert distiller_light._path_for("example.com", ram) == disk / "host_knowledge" / "example.com.json"
    assert distiller_light._history_path_for("example.com", ram) == (
        disk / "host_knowledge" / "example.com" / "history.jsonl"
    )


def test_the_reasoning_distiller_does_not_build_the_path_itself():
    src = (HUB / "distiller_r1.py").read_text(encoding="utf-8")
    assert 'data_dir / "host_knowledge"' not in src
    assert src.count('state_dir_for(data_dir) / "host_knowledge"') == 2


def test_escalation_reads_knowledge_where_it_is_written(split):
    from server.hub import _escalate

    disk, ram = split
    barrier = {"per_page": {"barriers": {"age_gate": {"present": True}}}}
    (disk / "host_knowledge").mkdir()
    (disk / "host_knowledge" / "example.com.json").write_text(json.dumps(barrier), "utf-8")
    assert _escalate._host_barriers("example.com") == {"age_gate"}
    # a copy among the job files is not what is read
    other = {"per_page": {"barriers": {"captcha": {"present": True}}}}
    (ram / "host_knowledge").mkdir()
    (ram / "host_knowledge" / "other.com.json").write_text(json.dumps(other), "utf-8")
    assert _escalate._host_barriers("other.com") == set()


# --- operator recordings ----------------------------------------------------------


def test_recordings_are_kept_on_disk(split, monkeypatch):
    from server.hub import oprec_store

    disk, ram = split
    monkeypatch.setattr(oprec_store, "_STORE", None)
    assert oprec_store.get_store().root == disk / "oprec"
    assert _files(ram) == [] and not (ram / "oprec").exists()


# --- the AI call log --------------------------------------------------------------


def test_the_ai_log_is_written_to_disk(split):
    from server.hub import _ai_io_log

    disk, ram = split
    p = _ai_io_log._jsonl_path("0a1b2c3d4e5f")
    assert p.parent.parent == disk / "ai_io" and p.name == "0a1b2c3d4e5f.jsonl"
    assert not (ram / "ai_io").exists()


def test_a_long_ai_message_is_staged_on_disk_and_keeps_its_key(split, monkeypatch):
    from server.hub import _ai_io_log, objstore

    disk, ram = split
    sent = []

    async def _put(key, local_path):
        sent.append((key, Path(local_path)))
        return True

    async def _mirror(*a, **kw):
        raise AssertionError("mirror_file derives the key from the job-files directory")

    monkeypatch.setattr(objstore, "put_object", _put)
    monkeypatch.setattr(objstore, "mirror_file", _mirror)
    monkeypatch.setattr(objstore, "_prefix", lambda: "jobs")
    sha = "ab" * 20
    asyncio.run(_ai_io_log._offload_to_minio(sha, "x" * 100))
    staged = disk / "ai_io" / "_blobs" / f"{sha}.bin"
    assert sent == [(f"jobs/ai_io/_blobs/{sha}.bin", staged)]     # the key it always had
    assert staged.read_text("utf-8") == "x" * 100
    assert _files(ram) == []


def test_the_key_has_no_leading_slash_without_a_prefix(split, monkeypatch):
    from server.hub import _ai_io_log, objstore

    sent = []

    async def _put(key, local_path):
        sent.append(key)
        return True

    monkeypatch.setattr(objstore, "put_object", _put)
    monkeypatch.setattr(objstore, "_prefix", lambda: "")
    asyncio.run(_ai_io_log._offload_to_minio("cd" * 20, "y"))
    assert sent == ["ai_io/_blobs/" + "cd" * 20 + ".bin"]


# --- nothing new slips in ---------------------------------------------------------


def test_no_hub_module_keeps_a_named_folder_among_the_job_files():
    """``get_storage_dir() / "<name>"`` is how the four above were written.
    A job's files are reached by job id, never by a fixed name."""
    rx = re.compile(r'get_storage_dir\(\)\s*/\s*f?"[A-Za-z_.]')
    found = []
    for py in sorted(HUB.rglob("*.py")):
        for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line) and not line.lstrip().startswith("#"):
                found.append(f"{py.relative_to(HUB).as_posix()}:{n}: {line.strip()}")
    assert found == []
