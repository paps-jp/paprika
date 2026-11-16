"""Video files are never written to a disk.

Operator rule, 2026-11-16: "SSDには動画ファイルを書き込まない。これは絶対。"
The node ramdisk exists to spare the SSDs under the worker CTs.

Before the rule, a deferred download the ramdisk pool turned down went to the
container's temp dir instead -- "a needless disk write is cheap" -- and that
temp dir is the CT rootfs, i.e. the node's NVMe. Measured over one hour across
259 workers: 5,977 of 10,964 downloads, with 80 GiB of video sitting on CT
disks at that moment.

So a download now gets a directory on the ramdisk or none at all: it waits for
room, and when none comes the job finishes without its video and says so.
What is pinned here is the absence of every path back to the disk.
"""
import asyncio
import inspect
import tempfile
from pathlib import Path

import pytest

from server.protocol import (
    HubAssignJob,
    JobOptions,
    JobResult,
    JobStatus,
    WorkerJobComplete,
    WorkerJobLog,
)
from server.worker import resume, scratch_pool
from server.worker.agent import _mix_jobexec
from server.worker.agent._core import WorkerAgent

MB = 1024 * 1024
JOB = "0a1b2c3d4e5f"


@pytest.fixture
def strict(monkeypatch):
    """Production settings: no development switch."""
    monkeypatch.delenv("PAPRIKA_VIDEO_ALLOW_DISK", raising=False)
    monkeypatch.delenv("PAPRIKA_SCRATCH_DISABLE", raising=False)


@pytest.fixture
def no_disk(monkeypatch):
    """Any attempt to make a plain temp directory fails the test."""
    def _boom(*a, **kw):
        raise AssertionError(f"a temp directory on disk was asked for: {a} {kw}")
    monkeypatch.setattr(tempfile, "mkdtemp", _boom)


@pytest.fixture
def fast(monkeypatch):
    """Waiting costs no wall-clock time, and is counted."""
    naps = []

    async def _sleep(n):
        naps.append(n)
        fast_clock[0] += n

    fast_clock = [1000.0]
    monkeypatch.setattr(_mix_jobexec.asyncio, "sleep", _sleep)
    monkeypatch.setattr(_mix_jobexec.time, "monotonic", lambda: fast_clock[0])
    return naps


def _agent():
    a = WorkerAgent.__new__(WorkerAgent)
    a.worker_id = "w-test"
    a.sent, a.uploaded = [], []

    async def _send(m):
        a.sent.append(m)

    async def _upload(assign, path, name, **kw):
        a.uploaded.append(name)
        return True

    async def _ck(assign, targets, page_url, jar):
        return None, "none (test)"

    a._send = _send
    a._upload_asset = _upload
    a._deferred_cookies_file = _ck
    a._abandoned_job_ids = set()
    a._force_complete_job_ids = set()
    a._bg_video_tasks = {}
    a._bg_video_left = {}
    return a


def _lines(a):
    return [m.line for m in a.sent if isinstance(m, WorkerJobLog)]


# --- where a video may be written ---------------------------------------------


def test_only_a_ram_backed_directory_is_acceptable(tmp_path, strict, monkeypatch):
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: False)
    assert not scratch_pool.video_dir_ok(tmp_path)
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: True)
    assert scratch_pool.video_dir_ok(tmp_path)


def test_a_directory_that_cannot_be_judged_is_not_ram(tmp_path, strict, monkeypatch):
    def _boom(p):
        raise OSError("mounts unreadable")
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", _boom)
    assert scratch_pool.is_ram(tmp_path) is False
    assert scratch_pool.video_dir_ok(tmp_path) is False


def test_the_development_switch_is_off_unless_asked_for(tmp_path, strict, monkeypatch):
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: False)
    assert scratch_pool.video_disk_allowed() is False
    monkeypatch.setenv("PAPRIKA_VIDEO_ALLOW_DISK", "1")
    assert scratch_pool.video_disk_allowed() and scratch_pool.video_dir_ok(tmp_path)


# --- getting a directory: the ramdisk, or nothing ------------------------------


def test_room_on_the_ramdisk_is_used_at_once(tmp_path, strict, no_disk, fast, monkeypatch):
    monkeypatch.setenv("PAPRIKA_SCRATCH_POOL_DIR", str(tmp_path))
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: True)
    monkeypatch.setattr(scratch_pool, "free_bytes", lambda pool: 64 * 1024 * MB)
    real = tempfile.mkdtemp.__wrapped__ if hasattr(tempfile.mkdtemp, "__wrapped__") else None
    assert real is None            # no_disk replaced it; the pool must not need it
    made = tmp_path / f"paprika-vid-{JOB}-w-test-abc"

    def _acquire(prefix, worker_id, need):
        made.mkdir()
        return made
    monkeypatch.setattr(scratch_pool, "acquire", _acquire)
    a = _agent()
    d = asyncio.run(a._acquire_video_ram(JOB, f"paprika-vid-{JOB}-"))
    assert d == made and fast == [] and a.sent == []


def test_no_room_means_waiting_and_then_nothing_never_the_disk(strict, no_disk, fast, monkeypatch):
    """THE rule. The old code returned ``tempfile.mkdtemp()`` here."""
    calls = []
    monkeypatch.setattr(scratch_pool, "acquire", lambda *a: calls.append(a) or None)
    monkeypatch.setattr(scratch_pool, "why_refused", lambda need, wid: "ramdisk has 100 MB free")
    monkeypatch.setenv("PAPRIKA_VIDEO_RAM_WAIT_S", "60")
    a = _agent()
    assert asyncio.run(a._acquire_video_ram(JOB, f"paprika-vid-{JOB}-")) is None
    assert len(calls) >= 6, "it must keep asking while it waits"
    assert sum(fast) == pytest.approx(60, abs=10)
    (line,) = _lines(a)
    assert "waiting for room on the ramdisk" in line and "100 MB free" in line


def test_room_that_appears_while_waiting_is_taken(tmp_path, strict, no_disk, fast, monkeypatch):
    answers = [None, None, tmp_path]
    monkeypatch.setattr(scratch_pool, "acquire", lambda *a: answers.pop(0))
    monkeypatch.setattr(scratch_pool, "why_refused", lambda need, wid: "busy")
    a = _agent()
    assert asyncio.run(a._acquire_video_ram(JOB, f"paprika-vid-{JOB}-")) == tmp_path
    assert len(fast) == 2


def test_a_job_the_hub_gave_up_on_stops_waiting(strict, no_disk, fast, monkeypatch):
    monkeypatch.setattr(scratch_pool, "acquire", lambda *a: None)
    a = _agent()
    a._abandoned_job_ids = {JOB}
    assert asyncio.run(a._acquire_video_ram(JOB, f"paprika-vid-{JOB}-")) is None
    assert fast == []


def test_a_development_box_may_use_its_disk(tmp_path, strict, fast, monkeypatch):
    monkeypatch.setattr(scratch_pool, "acquire", lambda *a: None)
    monkeypatch.setenv("PAPRIKA_VIDEO_ALLOW_DISK", "1")
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(tmp_path / (prefix + "x")))
    a = _agent()
    d = asyncio.run(a._acquire_video_ram(JOB, f"paprika-vid-{JOB}-"))
    assert d == tmp_path / f"paprika-vid-{JOB}-x" and fast == []


# --- the download itself -----------------------------------------------------


def _run_deferred(a, monkeypatch, ytdlp):
    import core.fetcher as fetcher

    monkeypatch.setattr(fetcher, "run_ytdlp", ytdlp)
    assign = HubAssignJob(
        job_id=JOB, url="https://example.test/v/1", options=JobOptions(),
        asset_upload_base=f"http://hub/jobs/{JOB}/assets",
    )

    async def _go():
        a._spawn_deferred_video_download(
            assign, [{"url": "https://cdn.test/a.m3u8", "referer": None}],
            JobResult(job_id=JOB, status=JobStatus.completed),
            "https://example.test/v/1",
        )
        await asyncio.gather(*list(a._bg_video_tasks), return_exceptions=True)

    asyncio.run(_go())
    return [m for m in a.sent if isinstance(m, WorkerJobComplete)]


def test_without_ram_the_job_finishes_without_its_video(tmp_path, strict, no_disk, monkeypatch):
    """...and says why, in the log an operator reads and in the facts a
    program reads -- "no RAM" is not "the download failed"."""
    monkeypatch.setenv("PAPRIKA_RESUME_DIR", str(tmp_path / "resume"))

    def _ytdlp(*a, **kw):
        raise AssertionError("yt-dlp must not be started without a ramdisk directory")

    a = _agent()

    async def _none(job_id, prefix):
        return None
    a._acquire_video_ram = _none
    (done,) = _run_deferred(a, monkeypatch, _ytdlp)
    assert done.result.assets == [] and a.uploaded == []
    assert done.result.facts["deferred"] == {"targets": 1, "files": 0, "upload_failed": 0, "no_ram": True}
    assert any("NOT downloaded" in ln and "never written to disk" in ln for ln in _lines(a))
    assert resume.load(JOB) is None, "there is nothing to resume"


def test_a_full_ramdisk_mid_download_is_retried_in_place(tmp_path, strict, no_disk, monkeypatch):
    """The old code retried on a NEW directory on the CT disk. Now the same
    directory is used again after a pause: yt-dlp carries on from its .part."""
    monkeypatch.setenv("PAPRIKA_RESUME_DIR", str(tmp_path / "resume"))
    monkeypatch.setenv("PAPRIKA_VIDEO_ENOSPC_WAIT_S", "0")
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: True)
    monkeypatch.setattr(scratch_pool, "release", lambda p: None)
    ram = tmp_path / f"paprika-vid-{JOB}-w-test-abc"
    ram.mkdir()
    dirs = []

    def _ytdlp(url, out_dir, **kw):
        dirs.append(Path(out_dir))
        if len(dirs) == 1:
            return False, "ERROR: unable to write data: [Errno 28] No space left on device"
        (Path(out_dir) / "video [x].mp4").write_bytes(b"x" * 2048)
        return True, "ok"

    a = _agent()

    async def _ram(job_id, prefix):
        return ram
    a._acquire_video_ram = _ram
    (done,) = _run_deferred(a, monkeypatch, _ytdlp)
    assert dirs == [ram, ram], "the retry must stay in the ramdisk directory"
    assert a.uploaded == ["video [x].mp4"]
    assert [x.name for x in done.result.assets] == ["video [x].mp4"]
    assert "no_ram" not in done.result.facts["deferred"]


def test_the_deferred_download_has_no_way_back_to_the_disk():
    src = inspect.getsource(WorkerAgent._spawn_deferred_video_download)
    assert "mkdtemp" not in src, "the deferred download must not create plain temp dirs"
    assert "_acquire_video_ram" in src
    assert "retrying on local disk" not in src
    helper = inspect.getsource(WorkerAgent._acquire_video_ram)
    assert helper.index("video_disk_allowed()") < helper.index("mkdtemp"), (
        "the only plain temp dir is behind the development switch"
    )


# --- what an older version left on the disk is not carried on with -------------


def _manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPRIKA_RESUME_DIR", str(tmp_path / "resume"))
    d = tmp_path / f"paprika-vid-{JOB}-abc"
    d.mkdir()
    (d / "video [x].mp4.part").write_bytes(b"x" * 1024)
    assign = HubAssignJob(
        job_id=JOB, url="https://example.test/v/1", options=JobOptions(),
        asset_upload_base=f"http://hub/jobs/{JOB}/assets",
    )
    assert resume.save(JOB, {
        "worker_id": "w-test", "resumes": 0,
        "assign": assign.model_dump(mode="json"),
        "base_result": JobResult(job_id=JOB, status=JobStatus.completed).model_dump(mode="json"),
        "targets": [{"url": "https://cdn.test/a.m3u8"}],
        "dl_dirs": [str(d)], "on_pool": False, "cookies_file": None,
    })
    assert resume.mark_ok(JOB)
    return d


def test_a_download_left_on_a_disk_is_not_resumed(tmp_path, strict, monkeypatch):
    """The first start after this change finds downloads the old version put
    on the CT disk, handed over for resuming. They are dropped -- and, being
    out of keep_dirs(), their bytes go with the startup wipe."""
    _manifest(tmp_path, monkeypatch)
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: False)
    assert resume.resumable("w-test") == []
    assert "on a disk" in resume.why_not(resume.load(JOB), "w-test")
    assert resume.keep_dirs("w-test") == set()


def test_a_download_on_the_ramdisk_still_is(tmp_path, strict, monkeypatch):
    d = _manifest(tmp_path, monkeypatch)
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: True)
    assert [m["job_id"] for m in resume.resumable("w-test")] == [JOB]
    assert resume.keep_dirs("w-test") == {str(d.resolve())}


# --- the reason a download is kept waiting is said ----------------------------


def test_why_refused_names_the_axis(tmp_path, strict, monkeypatch):
    monkeypatch.setenv("PAPRIKA_SCRATCH_POOL_DIR", str(tmp_path))
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: True)
    monkeypatch.setattr(scratch_pool, "outstanding_bytes", lambda pool: 3000 * MB)
    monkeypatch.setattr(scratch_pool, "min_free_bytes", lambda: 512 * MB)
    monkeypatch.setattr(scratch_pool, "cgroup_headroom_bytes", lambda pool, wid: None)
    monkeypatch.setattr(scratch_pool, "free_bytes", lambda pool: 4000 * MB)
    why = scratch_pool.why_refused(768 * MB, "w1")
    assert "4000 MB free" in why and "3000 MB" in why and "768 MB needed" in why
    # Room on the node, none in this worker's own memory budget.
    monkeypatch.setattr(scratch_pool, "free_bytes", lambda pool: 64 * 1024 * MB)
    monkeypatch.setattr(scratch_pool, "cgroup_headroom_bytes", lambda pool, wid: 200 * MB)
    assert "memory budget has 200 MB left" in scratch_pool.why_refused(768 * MB, "w1")
    monkeypatch.setattr(scratch_pool, "cgroup_headroom_bytes", lambda pool, wid: None)
    assert scratch_pool.why_refused(768 * MB, "w1") == ""
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: False)
    assert scratch_pool.why_refused(768 * MB, "w1") == "no ramdisk is mounted"
