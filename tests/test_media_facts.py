"""Worker-side media facts (server/worker/media_facts.py): measure, never
judge, and never let a bad file or a missing ffprobe break job completion."""
import asyncio
import json
import subprocess
from types import SimpleNamespace as NS

from server.worker import media_facts as MF


def _fake_run(stdout="", rc=0, exc=None):
    def run(cmd, **kw):
        if exc:
            raise exc
        return NS(returncode=rc, stdout=stdout, stderr="")
    return run


def test_missing_and_empty_files(tmp_path):
    assert MF.probe_file(tmp_path / "nope.mp4")["reason"] == "missing"
    p = tmp_path / "e.mp4"
    p.write_bytes(b"")
    r = MF.probe_file(p)
    assert (r["probed"], r["playable"], r["reason"]) == (True, False, "empty")


def test_playable_video(tmp_path, monkeypatch):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x" * 1000)
    out = json.dumps({"format": {"duration": "1234.5", "format_name": "mov,mp4,m4a"},
                      "streams": [{"codec_type": "video", "codec_name": "h264",
                                   "width": 1280, "height": 720},
                                  {"codec_type": "audio"}]})
    monkeypatch.setattr(subprocess, "run", _fake_run(out))
    r = MF.probe_file(p)
    assert r["playable"] and r["has_audio"] and r["duration_s"] == 1234.5
    assert (r["width"], r["height"], r["codec"], r["bytes"]) == (1280, 720, "h264", 1000)


def test_not_media_is_measured_as_unplayable(tmp_path, monkeypatch):
    p = tmp_path / "frag.mp4"
    p.write_bytes(b"x" * 10)
    monkeypatch.setattr(subprocess, "run", _fake_run("", rc=1))
    r = MF.probe_file(p)
    assert (r["probed"], r["playable"], r["reason"]) == (True, False, "not_media")


def test_cover_art_only_is_not_a_video(tmp_path, monkeypatch):
    p = tmp_path / "a.mp4"
    p.write_bytes(b"x" * 10)
    out = json.dumps({"format": {"duration": "200"}, "streams": [
        {"codec_type": "audio"},
        {"codec_type": "video", "disposition": {"attached_pic": 1}}]})
    monkeypatch.setattr(subprocess, "run", _fake_run(out))
    assert MF.probe_file(p)["reason"] == "no_video_stream"


def test_no_verdict_when_ffprobe_is_unavailable(tmp_path, monkeypatch):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x" * 10)
    monkeypatch.setattr(subprocess, "run", _fake_run(exc=FileNotFoundError()))
    r = MF.probe_file(p)
    assert (r["probed"], r["reason"]) == (False, "ffprobe_missing")
    monkeypatch.setattr(subprocess, "run",
                        _fake_run(exc=subprocess.TimeoutExpired("ffprobe", 1)))
    assert MF.probe_file(p)["probed"] is False


def test_probe_paths_caps_and_prefers_largest(tmp_path, monkeypatch):
    for i in range(12):
        (tmp_path / f"v{i}.mp4").write_bytes(b"x" * (i + 1))
    (tmp_path / "img.jpg").write_bytes(b"x" * 999)
    monkeypatch.setattr(subprocess, "run", _fake_run("", rc=1))
    out = asyncio.run(MF.probe_paths(tmp_path.iterdir(), limit=3))
    assert [r["name"] for r in out] == ["v11.mp4", "v10.mp4", "v9.mp4"]
    monkeypatch.setenv("PAPRIKA_MEDIA_FACTS", "0")
    assert asyncio.run(MF.probe_paths(tmp_path.iterdir())) == []
