"""A job's own files -- the page, its images, the log -- are never written to
the CT disk.

Operator, 2026-11-16, after the same rule was set for video: "画像も同様にして
ほしい。SSDではなく ramdisk".

Before this, the per-job directory was on the ramdisk only as an opt-in
(``PAPRIKA_SCRATCH_JOB``) and only when the pool's claim arithmetic had 256 MB
to spare. In production it almost never had: the pools' outstanding video
promises matched their free space, so 150 workers that had opted in fell back
to ``tempfile.mkdtemp()`` like the 108 that had not. Measured fleet-wide: 252
job directories on CT disks, 11 on the ramdisk -- while every pool had 16-87 GB
actually free, and a job directory is 0.3-3 MB.

So a job directory is now handed out on what is really free, not on what has
been promised, and there is no way back to the disk: no room means the job
(or the session) does not start.
"""
import asyncio
import inspect
import tempfile

import pytest

from server.worker import scratch_pool
from server.worker.agent import _mix_jobexec, _mix_sessions, _mix_videodl

MB = 1024 * 1024
GB = 1024 * MB


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """A mounted ramdisk with 20 GB free and every byte of it promised to
    videos -- the state production pools are in."""
    monkeypatch.delenv("PAPRIKA_VIDEO_ALLOW_DISK", raising=False)
    monkeypatch.delenv("PAPRIKA_SCRATCH_DISABLE", raising=False)
    monkeypatch.delenv("PAPRIKA_SCRATCH_JOB", raising=False)
    monkeypatch.setenv("PAPRIKA_SCRATCH_POOL_DIR", str(tmp_path))
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: True)
    state = {"free": 20 * GB}
    monkeypatch.setattr(scratch_pool, "free_bytes", lambda p: state["free"])
    monkeypatch.setattr(scratch_pool, "outstanding_bytes", lambda p, owner=None: 20 * GB)
    state["dir"] = tmp_path
    return state


@pytest.fixture
def no_disk(monkeypatch):
    """A temp directory outside the pool fails the test."""
    real = tempfile.mkdtemp

    def _only_in_pool(*a, **kw):
        if not kw.get("dir"):
            raise AssertionError(f"a temp directory on disk was asked for: {a} {kw}")
        return real(*a, **kw)

    monkeypatch.setattr(tempfile, "mkdtemp", _only_in_pool)


@pytest.fixture
def fast(monkeypatch):
    naps = []
    clock = [1000.0]

    async def _sleep(n):
        naps.append(n)
        clock[0] += n

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    monkeypatch.setattr(scratch_pool.time, "monotonic", lambda: clock[0])
    return naps


# --- handing out a directory ----------------------------------------------------


def test_a_job_directory_is_on_the_ramdisk_even_when_every_byte_is_promised(pool, no_disk):
    # the old path: a 256 MB claim against a fully promised pool
    assert scratch_pool.acquire("paprika-0a1b2c3d4e5f-", "w5101", 256 * MB) is None
    d = scratch_pool.acquire_small("paprika-0a1b2c3d4e5f-", "w5101")
    assert d is not None and d.parent == pool["dir"]
    assert d.name.startswith("paprika-0a1b2c3d4e5f-w5101-")     # owner in the name


def test_it_promises_nothing_so_videos_lose_nothing(pool, monkeypatch):
    d = scratch_pool.acquire_small("paprika-0a1b2c3d4e5f-", "w5101")
    claim = pool["dir"] / scratch_pool._CLAIMS_DIRNAME / f"{d.name}.claim"
    assert claim.read_text() == "0"
    monkeypatch.undo()                                           # the real arithmetic
    monkeypatch.setenv("PAPRIKA_SCRATCH_POOL_DIR", str(pool["dir"]))
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: True)
    assert scratch_pool.outstanding_bytes(pool["dir"]) == 0


def test_the_opt_in_switch_no_longer_matters(pool, no_disk, monkeypatch):
    monkeypatch.setenv("PAPRIKA_SCRATCH_JOB", "0")              # 108 workers run like this
    assert scratch_pool.acquire_small("paprika-ses-abc-", "w5101") is not None


def test_a_truly_full_ramdisk_gives_nothing(pool, no_disk):
    pool["free"] = scratch_pool.min_free_bytes() + 10 * MB      # under floor + 64 MB
    assert scratch_pool.acquire_small("paprika-0a1b2c3d4e5f-", "w5101") is None
    assert "MB free" in scratch_pool.why_small_refused()


def test_no_ramdisk_gives_nothing(pool, no_disk, monkeypatch):
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: False)
    assert scratch_pool.acquire_small("paprika-0a1b2c3d4e5f-", "w5101") is None
    assert scratch_pool.why_small_refused() == "no ramdisk is mounted"


# --- waiting, and what happens when no room comes --------------------------------


def test_room_is_used_at_once(pool, no_disk, fast):
    d = asyncio.run(scratch_pool.acquire_small_wait("paprika-0a1b2c3d4e5f-", "w5101"))
    assert d is not None and fast == []


def test_a_full_ramdisk_is_waited_out_then_given_up_never_the_disk(pool, no_disk, fast):
    pool["free"] = 0
    d = asyncio.run(scratch_pool.acquire_small_wait("paprika-0a1b2c3d4e5f-", "w5101"))
    assert d is None
    assert 59 <= sum(fast) <= 61                                 # PAPRIKA_JOB_RAM_WAIT_S


def test_room_that_appears_while_waiting_is_taken(pool, no_disk, fast, monkeypatch):
    pool["free"] = 0
    real_sleep = asyncio.sleep

    async def _sleep(n):
        await real_sleep(n)
        if len(fast) >= 3:
            pool["free"] = 5 * GB

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    d = asyncio.run(scratch_pool.acquire_small_wait("paprika-0a1b2c3d4e5f-", "w5101"))
    assert d is not None and len(fast) == 3


def test_a_development_box_gets_a_plain_directory(pool, monkeypatch):
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: False)
    monkeypatch.setenv("PAPRIKA_VIDEO_ALLOW_DISK", "1")
    d = asyncio.run(scratch_pool.acquire_small_wait("paprika-0a1b2c3d4e5f-", "w-dev"))
    assert d is not None and d.parent != pool["dir"]
    d.rmdir()


# --- the callers have no other way to get a directory ---------------------------


def test_the_job_runner_has_no_way_back_to_the_disk():
    src = inspect.getsource(_mix_jobexec)
    at = src.index('f"paprika-{job_id}-", self.worker_id,')
    block = src[at - 400: at + 500]
    assert "acquire_small_wait" in block
    assert "mkdtemp" not in block
    assert "raise RuntimeError" in block                         # no room = the job fails
    assert "job_workdirs_enabled" not in src


def test_a_session_has_no_way_back_to_the_disk():
    src = inspect.getsource(_mix_sessions)
    at = src.index('f"paprika-ses-{sid}-", self.worker_id,')
    block = src[at - 400: at + 500]
    assert "acquire_small_wait" in block
    assert "raise RuntimeError" in block
    assert "tempfile.mkdtemp(" not in src                        # nowhere in the module
    assert "job_workdirs_enabled" not in src


def test_the_handoff_downloader_refuses_a_plain_directory():
    src = inspect.getsource(_mix_videodl)
    guard = src.index("video_dir_ok(root)")
    assert guard < src.index('tempfile.mkdtemp(prefix=f"paprika-vid-')


def test_a_failed_job_says_why(pool, no_disk, fast):
    """What the job runner raises is what the hub records as the job's error."""
    pool["free"] = 0

    async def _start():
        workdir = await scratch_pool.acquire_small_wait("paprika-0a1b2c3d4e5f-", "w5101")
        if workdir is None:
            raise RuntimeError(
                "no room on the ramdisk for this job's files "
                "(they are never written to disk): " + scratch_pool.why_small_refused()
            )

    with pytest.raises(RuntimeError, match="never written to disk.*0 MB free"):
        asyncio.run(_start())


# --- a video downloaded into a session's directory --------------------------------
# The directory is on the ramdisk but was handed out with no reservation, so
# the download is admitted where it starts: real space and real memory.


def test_a_session_video_needs_real_space_not_unpromised_space(pool):
    d = scratch_pool.acquire_small("paprika-ses-abc-", "w5101")
    assert scratch_pool.why_no_video_room(d, "w5101") == ""      # 20 GB free, all promised
    pool["free"] = scratch_pool.min_free_bytes() + 100 * MB
    assert "MB needed" in scratch_pool.why_no_video_room(d, "w5101")


def test_a_session_video_needs_room_in_the_workers_memory(pool, monkeypatch):
    d = scratch_pool.acquire_small("paprika-ses-abc-", "w5101")
    monkeypatch.setattr(scratch_pool, "cgroup_headroom_bytes", lambda p, w: 100 * MB)
    assert "memory budget" in scratch_pool.why_no_video_room(d, "w5101")
    monkeypatch.setattr(scratch_pool, "cgroup_headroom_bytes", lambda p, w: None)
    assert scratch_pool.why_no_video_room(d, "w5101") == ""      # axis not configured


def test_a_session_video_is_refused_outside_the_ramdisk(pool, tmp_path, monkeypatch):
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: False)
    assert "not on the ramdisk" in scratch_pool.why_no_video_room(tmp_path, "w5101")
    monkeypatch.setenv("PAPRIKA_VIDEO_ALLOW_DISK", "1")          # a development box
    assert scratch_pool.why_no_video_room(tmp_path, "w5101") == ""


def test_both_session_download_paths_ask_first():
    from server.worker.agent import video
    from server.worker.session_actions.handlers import media

    v = inspect.getsource(video._make_video_downloader)
    assert v.index("why_no_video_room(assets_dir, worker_id)") < v.index("asyncio.create_task(_run(")
    m = inspect.getsource(media._act_download_video)
    assert m.index("why_no_video_room(") < m.index("from core.fetcher import run_ytdlp")
    s = inspect.getsource(_mix_sessions)
    assert s.count("worker_id=self.worker_id,") >= 2             # both downloaders know whose memory
