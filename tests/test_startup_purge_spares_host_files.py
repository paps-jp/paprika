"""The hub's startup sweep removes interrupted downloads, not host files.

At start the hub deletes download partials left under its data dir by a run
that died (``x.mp4.part``). The sweep used to take every file with ``.part``
anywhere in its name, which is also every per-host file of a host whose name
contains it -- ``fr.example.party.json`` under ``hosts/`` and
``host_knowledge/``. Those were deleted on every hub start: the host records
came back from MariaDB, the knowledge files did not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.__main__ import _is_download_partial, _purge_stale_part_files


def _touch(path: Path, size: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.mark.parametrize("name", [
    "video.mp4.part",
    "video.f137.mp4.part",
    "video.mp4.part-Frag0",
    "video.mp4.part-Frag12",
    "video.mp4.part-Frag12.part",
    ".upload-0123456789abcdef.part",
    "clip.webm.part",
])
def test_names_an_interrupted_download_leaves_behind(name):
    assert _is_download_partial(name)


@pytest.mark.parametrize("name", [
    "example.party.json",
    "fr.example.party.json",
    "cdn.partner.example.com.json",
    "www.parts.example.com.json",
    "example.part.json",
    "video.mp4",
    "video.mp4.ytdl",
    "video.part.mp4",
    "video.mp4.part-Frag",
    "video.mp4.part-FragX",
    "video.mp4.partial",
    "history.jsonl",
])
def test_names_that_only_contain_the_word(name):
    assert not _is_download_partial(name)


def test_sweep_removes_partials_and_keeps_host_files(tmp_path):
    job = tmp_path / "0123456789ab" / "assets"
    gone = [
        _touch(job / "video.mp4.part", 2048),
        _touch(job / "video.mp4.part-Frag7", 512),
        _touch(job / "video.mp4.part-Frag7.part", 512),
        _touch(job / ".upload-0123456789abcdef.part", 64),
        _touch(tmp_path / "ramjobs" / "ba9876543210" / "assets" / "clip.webm.part"),
    ]
    kept = [
        _touch(job / "video.mp4"),
        _touch(tmp_path / "hosts" / "example.party.json"),
        _touch(tmp_path / "hosts" / "fr.example.party.json"),
        _touch(tmp_path / "host_knowledge" / "example.party.json"),
        _touch(tmp_path / "host_knowledge" / "fr.example.party.json"),
        _touch(tmp_path / "host_knowledge" / "cdn.partner.example.com.json"),
        _touch(tmp_path / "host_visited" / "www.parts.example.com.json"),
    ]

    assert _purge_stale_part_files(tmp_path) == len(gone)

    assert [p for p in gone if p.exists()] == []
    assert [p for p in kept if not p.exists()] == []


def test_a_folder_named_like_a_partial_is_left_alone(tmp_path):
    inside = _touch(tmp_path / "host_knowledge" / "example.part" / "notes.txt")

    assert _purge_stale_part_files(tmp_path) == 0

    assert inside.exists()


def test_nothing_to_sweep(tmp_path):
    _touch(tmp_path / "hosts" / "example.com.json")

    assert _purge_stale_part_files(tmp_path) == 0
    assert _purge_stale_part_files(tmp_path / "does-not-exist") == 0
