"""A recycle hands its video downloads over instead of throwing them away.

Putting the recycle off (test_recycle_waits_for_downloads.py) turned out to
change only WHICH downloads are cut: the worker keeps taking jobs while it
waits, so there are always a few in flight when it finally exits. Measured
2026-11-15 over 150 minutes, fleet-wide: 686 downloads cut and 310 GiB of
already-fetched video discarded, 82% of them of a kind yt-dlp resumes by
itself when run again on the same directory.

So the exiting process now leaves a note per download, the next one keeps the
directory and runs yt-dlp again, and the hub waits for that instead of failing
the job the moment the socket drops.

What must never happen, and is pinned here:

  * resuming after anything but a recycle -- above all a memory-guard exit,
    where the kept file is the very memory the worker was short of;
  * resuming for a job the hub no longer wants;
  * a `downloading` job left parked forever because nobody came back for it.
"""
import asyncio
import inspect
import time

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub.routes import workers as workers_route
from server.protocol import (
    HubAssignJob,
    JobInfo,
    JobOptions,
    JobProgress,
    JobResult,
    JobStatus,
    WorkerJobProgress,
)
from server.worker import resume, scratch_pool
from server.worker.agent import _mix_jobexec, _mix_run
from server.worker.agent._core import WorkerAgent


@pytest.fixture
def rdir(tmp_path, monkeypatch):
    """Manifests under tmp_path, with the defaults in force."""
    d = tmp_path / "paprika-resume"
    monkeypatch.setenv("PAPRIKA_RESUME_DIR", str(d))
    for k in ("PAPRIKA_RESUME_DOWNLOADS", "PAPRIKA_RESUME_MAX_AGE_S", "PAPRIKA_RESUME_MAX"):
        monkeypatch.delenv(k, raising=False)
    # The download directories these tests make stand for ramdisk ones: a
    # download on a disk is never resumed (test_video_never_on_disk.py).
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: True)
    return d


def _assign(job_id="j1"):
    return HubAssignJob(
        job_id=job_id, url="https://example.test/v/1", options=JobOptions(),
        asset_upload_base=f"http://hub/jobs/{job_id}/assets",
    )


def _manifest(tmp_path, job_id="j1", *, worker_id="w-test", with_bytes=True, **extra):
    """Save the manifest of a download whose directory is ``tmp_path/vid-<job>``."""
    d = tmp_path / f"paprika-vid-{job_id}-w-test-abc"
    d.mkdir(exist_ok=True)
    if with_bytes:
        (d / "video [x].mp4.part").write_bytes(b"x" * 1024)
    data = {
        "worker_id": worker_id,
        "created": time.time(),
        "resumes": 0,
        "assign": _assign(job_id).model_dump(mode="json"),
        "base_result": JobResult(job_id=job_id, status=JobStatus.completed).model_dump(mode="json"),
        "targets": [{"url": "https://cdn.test/a.m3u8", "referer": "https://example.test/"}],
        "page_url": "https://example.test/v/1",
        "dl_dirs": [str(d)],
        "on_pool": False,
        "cookies_file": None,
    }
    data.update(extra)
    assert resume.save(job_id, data)
    return d


# --- the note ---------------------------------------------------------------


def test_a_note_alone_is_not_enough_to_resume(rdir, tmp_path):
    """A manifest exists for EVERY running download, so a crash or an OOM kill
    leaves one behind too. Only the stamp of an orderly recycle counts."""
    _manifest(tmp_path)
    m = resume.load("j1")
    assert m and m["targets"][0]["url"].endswith("a.m3u8")
    assert resume.resumable("w-test") == []
    assert "did not hand it over" in resume.why_not(m, "w-test")


def test_a_handed_over_download_is_resumable(rdir, tmp_path):
    d = _manifest(tmp_path)
    assert resume.mark_ok("j1")
    assert [m["job_id"] for m in resume.resumable("w-test")] == ["j1"]
    assert resume.keep_dirs("w-test") == {str(d.resolve())}


def test_marking_a_download_that_has_no_note_does_nothing(rdir):
    assert resume.mark_ok("nope") is False


@pytest.mark.parametrize("change,reason", [
    (dict(with_bytes=False), "nothing left on disk"),
    (dict(worker_id="w-other"), "belongs to worker w-other"),
    (dict(resumes=3), "already resumed 3"),
    (dict(targets=[]), "incomplete"),
])
def test_what_is_not_resumed(rdir, tmp_path, change, reason):
    _manifest(tmp_path, **change)
    resume.mark_ok("j1")
    assert resume.resumable("w-test") == []
    assert reason in resume.why_not(resume.load("j1"), "w-test")


def test_a_stale_handover_is_not_resumed(rdir, tmp_path):
    """A cloned or restored CT carries somebody else's /tmp, and a worker
    that stayed down for an hour has no job left to resume for."""
    _manifest(tmp_path)
    resume.mark_ok("j1")
    later = time.time() + resume.max_age_s() + 60
    assert resume.resumable("w-test", now=later) == []
    assert resume.keep_dirs("w-test", now=later) == set()


def test_the_kill_switch(rdir, tmp_path, monkeypatch):
    _manifest(tmp_path)
    resume.mark_ok("j1")
    monkeypatch.setenv("PAPRIKA_RESUME_DOWNLOADS", "0")
    assert resume.resumable("w-test") == []


def test_startup_prune_drops_the_notes_nobody_will_use(rdir, tmp_path):
    """...with their cookies file, which nothing else would collect. The
    directories are left to the wipes that have always owned them."""
    ck = tmp_path / "ytdlp_cookies_deferred_x.txt"
    ck.write_text("# Netscape HTTP Cookie File\n")
    stale = _manifest(tmp_path, "old", cookies_file=str(ck))      # never stamped
    _manifest(tmp_path, "new")
    resume.mark_ok("new")
    (rdir / "junk.json").write_text("{not json")
    kept, dropped = resume.prune("w-test")
    assert (kept, dropped) == (1, 2)
    assert resume.load("old") is None and not ck.exists()
    assert not (rdir / "junk.json").exists()
    assert stale.is_dir(), "prune must not remove bytes itself"
    assert resume.load("new") is not None


# --- startup: the purges leave a handed-over download alone -------------------


def test_the_scratch_pool_purge_spares_what_is_being_resumed(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPRIKA_SCRATCH_POOL_DIR", str(tmp_path))
    monkeypatch.delenv("PAPRIKA_SCRATCH_DISABLE", raising=False)
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: True)
    monkeypatch.setattr(scratch_pool, "free_bytes", lambda pool: 64 * 1024 ** 3)
    keep = scratch_pool.acquire("paprika-vid-j1-", "w51175", 1024)
    drop = scratch_pool.acquire("paprika-vid-j2-", "w51175", 1024)
    assert keep is not None and drop is not None
    n, _freed = scratch_pool.purge_own("w51175", keep={str(keep.resolve())})
    assert n == 1
    assert keep.is_dir() and not drop.exists()
    # Without a keep-set it is the wholesale purge it always was.
    n, _freed = scratch_pool.purge_own("w51175")
    assert n == 1 and not keep.exists()


def test_the_tmp_wipe_spares_the_manifests_and_what_is_being_resumed(tmp_path, monkeypatch):
    """The startup wipe matches ``paprika-*``; the manifest directory and the
    directory of a handed-over download must survive it, and nothing else."""
    import server.__main__ as main

    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    names = ["paprika-resume", "paprika-vid-j1-abc", "paprika-vid-j2-abc",
             "paprika-profile-cache", "paprika-extensions", "paprika-0a1b2c-xyz"]
    for n in names:
        (tmp_path / n).mkdir()
    keep = {str((tmp_path / "paprika-vid-j1-abc").resolve())}
    assert main._wipe_job_tmpdirs(keep) == 2
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["paprika-extensions", "paprika-profile-cache",
                    "paprika-resume", "paprika-vid-j1-abc"]
    # With nothing handed over it is the wholesale wipe it always was.
    assert main._wipe_job_tmpdirs(set()) == 1
    assert not (tmp_path / "paprika-vid-j1-abc").exists()


def test_startup_asks_the_manifests_before_either_purge():
    import server.__main__ as main

    src = inspect.getsource(main._run_worker)
    assert src.index("_resume.keep_dirs()") < src.index("_wipe_job_tmpdirs(_resume_keep)")
    assert "purge_own(worker_id, keep=_keep)" in src
    assert src.index("_resume.prune(worker_id)") < src.index("purge_own(worker_id, keep=_keep)")
    assert resume.DIRNAME in WorkerAgent._TMP_SWEEP_PROTECTED


# --- the exit: only a recycle hands over --------------------------------------


def _agent(reason="recycle after 200 job(s)", downloads=("j1",)):
    a = WorkerAgent.__new__(WorkerAgent)
    a.worker_id = "w-test"
    a._drain_reason = reason
    a._draining = True
    a._in_flight = 0
    a._bg_video_tasks = {f"task-{i}": j for i, j in enumerate(downloads)}
    a._bg_video_left = {}
    a._abandoned_job_ids = set()
    a._force_complete_job_ids = set()
    return a


def test_a_recycle_hands_its_downloads_over(rdir, tmp_path):
    _manifest(tmp_path, "j1")
    _manifest(tmp_path, "j2")
    a = _agent(downloads=("j1", "j2"))
    a._note_cut_downloads()
    assert {m["job_id"] for m in resume.resumable("w-test")} == {"j1", "j2"}


@pytest.mark.parametrize("reason", [
    "memory guard: refault 973/s >= 500/s",
    "self-update to 285afea0f108",
    "",
])
def test_no_other_exit_does(rdir, tmp_path, reason):
    """THE safety property. A download on the node ramdisk is charged to this
    CT's memory for as long as its file exists; resuming it after a
    memory-guard exit would bring the pressure straight back."""
    _manifest(tmp_path, "j1")
    a = _agent(reason=reason)
    a._note_cut_downloads()
    assert resume.resumable("w-test") == []


def test_what_a_recycle_does_not_hand_over(rdir, tmp_path):
    """ffmpeg keeps nothing to carry on from, and a job the hub has ended or
    asked us to wrap up is not waiting for the rest."""
    for j in ("ok", "ffmpeg", "gone", "wrapup"):
        _manifest(tmp_path, j)
    a = _agent(downloads=("ok", "ffmpeg", "gone", "wrapup"))
    a._bg_video_noresume = {"ffmpeg"}
    a._abandoned_job_ids = {"gone"}
    a._force_complete_job_ids = {"wrapup"}
    assert a._hand_over_downloads(["ok", "ffmpeg", "gone", "wrapup"]) == 1
    assert [m["job_id"] for m in resume.resumable("w-test")] == ["ok"]


def test_every_recycle_exit_goes_through_the_handover():
    """The clean exit and the drain-deadline force-exit both call
    _note_cut_downloads right before os._exit(0)."""
    src = inspect.getsource(_mix_run._RunMixin._heartbeat_loop) \
        if hasattr(_mix_run, "_RunMixin") else inspect.getsource(_mix_run)
    forced = src[src.index("drain did not complete"):]
    forced = forced[:forced.index("os._exit(0)")]
    assert "self._note_cut_downloads()" in forced
    clean = src[src.index("# Recycle: once the drain has emptied in-flight"):]
    clean = clean[:clean.index("os._exit(0)")]
    assert "self._note_cut_downloads()" in clean
    assert "_hand_over_downloads" in inspect.getsource(WorkerAgent._note_cut_downloads)


def test_ffmpeg_downloads_are_recognised():
    src = inspect.getsource(WorkerAgent._spawn_deferred_video_download)
    assert '"frame=" in line and "fps=" in line' in src
    assert "_bg_video_noresume" in src


# --- the next process: ask the hub, then carry on -----------------------------


class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Http:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.urls = []

    async def get(self, url, **kw):
        self.urls.append(url)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def _asker(*answers, monkeypatch=None):
    a = WorkerAgent.__new__(WorkerAgent)
    a.worker_id = "w-test"
    a.hub_http_url = "http://hub:8000"
    a._http = _Http(*answers)
    return a


@pytest.fixture
def no_sleep(monkeypatch):
    async def _s(_n):
        return None
    monkeypatch.setattr(_mix_jobexec.asyncio, "sleep", _s)


@pytest.mark.parametrize("status", ["downloading", "running", "completed"])
def test_the_hub_still_wants_it(status, no_sleep):
    """``completed`` included: the hub closes the fetch first and takes the
    video afterwards."""
    a = _asker(_Resp(200, {"status": status, "worker_id": "w-test"}))
    assert asyncio.run(a._hub_job_wants_download("j1")) == ""
    assert a._http.urls == ["http://hub:8000/jobs/j1"]


@pytest.mark.parametrize("answer,reason", [
    (_Resp(404), "gone"),
    (_Resp(200, {"status": "failed", "worker_id": "w-test"}), "failed"),
    (_Resp(200, {"status": "cancelled", "worker_id": "w-test"}), "cancelled"),
    (_Resp(200, {"status": "downloading", "worker_id": "w-other"}), "belongs to w-other"),
    (_Resp(503), "503"),
    (ConnectionError("down"), "unreachable"),
    (_Resp(200, ValueError("bad json")), "unreadable"),
])
def test_no_clear_yes_means_do_not_resume(answer, reason, no_sleep):
    """The opposite bias from the abandon loop: there, silence means "keep
    downloading"; here it means "do not start" -- an hour of ramdisk for an
    asset nobody can accept is the cost of guessing wrong."""
    a = _asker(answer)
    assert reason in asyncio.run(a._hub_job_wants_download("j1"))


def _resumer(wants, monkeypatch):
    a = WorkerAgent.__new__(WorkerAgent)
    a.worker_id = "w-test"
    a.sent, a.spawned = [], []

    async def _send(msg):
        a.sent.append(msg)

    async def _wants(job_id):
        return wants.get(job_id, "")

    a._send = _send
    a._hub_job_wants_download = _wants
    a._spawn_deferred_video_download = (
        lambda assign, targets, base_result, page_url, cookie_jar=None, resume=None:
        a.spawned.append((assign, targets, base_result, page_url, resume))
    )
    return a


def test_a_wanted_download_is_picked_up_in_its_own_directory(rdir, tmp_path, monkeypatch):
    d = _manifest(tmp_path, "j1")
    resume.mark_ok("j1")
    a = _resumer({}, monkeypatch)
    assert asyncio.run(a._resume_cut_downloads()) == 1

    (assign, targets, base_result, page_url, m), = a.spawned
    assert isinstance(assign, HubAssignJob) and assign.job_id == "j1"
    assert assign.asset_upload_base == "http://hub/jobs/j1/assets"
    assert isinstance(base_result, JobResult)
    assert targets == [{"url": "https://cdn.test/a.m3u8", "referer": "https://example.test/"}]
    assert page_url == "https://example.test/v/1"
    assert m["dl_dirs"] == [str(d)]
    # ...and the hub is told BEFORE the download starts, so it stops waiting
    # to fail the job.
    (msg,) = a.sent
    assert isinstance(msg, WorkerJobProgress)
    assert (msg.job_id, msg.phase, msg.resumed) == ("j1", "downloading", True)


def test_an_unwanted_download_is_dropped_bytes_and_all(rdir, tmp_path, monkeypatch):
    d = _manifest(tmp_path, "j1")
    resume.mark_ok("j1")
    a = _resumer({"j1": "the job is failed"}, monkeypatch)
    assert asyncio.run(a._resume_cut_downloads()) == 0
    assert a.spawned == [] and a.sent == []
    assert not d.exists(), "its bytes must not outlive the decision"
    assert resume.load("j1") is None


def test_only_handed_over_downloads_are_looked_at(rdir, tmp_path, monkeypatch):
    _manifest(tmp_path, "crashed")                 # never stamped
    a = _resumer({}, monkeypatch)
    assert asyncio.run(a._resume_cut_downloads()) == 0
    assert a.spawned == []


def test_the_resume_runs_once_per_process_and_a_flap_re_announces():
    """First connection: pick the handed-over downloads up. Any later one:
    tell the hub which downloads are STILL running here, or the hub -- which
    parked them when the old socket dropped -- fails them after its grace."""
    src = inspect.getsource(WorkerAgent._handshake_and_loop)
    hook = src[src.index("_resume_started"):src.index("hb_task = asyncio.create_task")]
    assert "self._resume_cut_downloads()" in hook
    again = hook[hook.index("else:"):]
    assert "_bg_video_tasks" in again and "resumed=True" in again


def test_the_download_keeps_its_note_up_to_date_and_drops_it_at_the_end():
    src = inspect.getsource(WorkerAgent._spawn_deferred_video_download)
    assert src.count("_save_manifest()") >= 2, "at the start and when a dir is added"
    body = src[src.index("async def _run()"):]
    fin = body[body.index("finally:"):]
    assert "_resume.remove(job_id)" in fin[:600], (
        "a download seen through to its end leaves nothing to resume"
    )
    # A resumed download runs in the directories it was handed, not new ones.
    assert src.index("_resumed_dirs") < src.index("self._acquire_video_ram(")
    assert "kept from before the restart" in src


# --- the hub: wait for the worker to come back --------------------------------


class _Store:
    def __init__(self, *infos):
        self.rows = {i.job_id: i for i in infos}
        self.saved, self.logs = [], []

    async def get_job_info(self, job_id):
        return self.rows.get(job_id)

    async def save_job_info(self, info):
        self.rows[info.job_id] = info
        self.saved.append(info.job_id)

    async def publish_log(self, job_id, line):
        self.logs.append((job_id, line))


def _info(job_id, status=JobStatus.downloading, worker_id="w1", resumed_at=None,
          phase="downloading"):
    from datetime import datetime

    return JobInfo(
        job_id=job_id, status=status, url="https://example.test/",
        options=JobOptions(), created_at=datetime(2026, 11, 15, 12, 0, 0), worker_id=worker_id,
        progress=JobProgress(phase=phase, resumed_at=resumed_at),
    )


@pytest.fixture
def hub(monkeypatch):
    def _make(*infos):
        store = _Store(*infos)
        monkeypatch.setattr(workers_route.state, "store", store, raising=False)
        monkeypatch.setattr(workers_route, "_ABANDONED_DOWNLOADING_GRACE_S", 0.0)
        return store
    return _make


def test_a_download_nobody_came_back_for_is_failed_after_the_grace(hub):
    """The same outcome and the same message as before -- only later."""
    store = hub(_info("j1"))
    asyncio.run(workers_route._settle_abandoned_downloading("w1", ["j1"], time.time()))
    row = store.rows["j1"]
    assert row.status == JobStatus.failed
    assert "disconnected before the job finished" in row.error
    assert row.progress.phase == "failed" and row.completed_at is not None
    assert store.logs, "viewers must be told the job is over"


def test_a_resumed_download_is_left_alone(hub):
    since = time.time()
    store = hub(_info("j1", resumed_at=since + 30))
    asyncio.run(workers_route._settle_abandoned_downloading("w1", ["j1"], since))
    assert store.rows["j1"].status == JobStatus.downloading and store.saved == []


def test_a_resume_from_before_this_disconnect_does_not_count(hub):
    since = time.time()
    store = hub(_info("j1", resumed_at=since - 600))
    asyncio.run(workers_route._settle_abandoned_downloading("w1", ["j1"], since))
    assert store.rows["j1"].status == JobStatus.failed


@pytest.mark.parametrize("info", [
    _info("j1", status=JobStatus.completed, phase="completed"),
    _info("j1", status=JobStatus.failed, phase="failed"),
    _info("j1", worker_id="w2"),
])
def test_only_this_workers_still_downloading_jobs_are_settled(hub, info):
    store = hub(info)
    before = info.status
    asyncio.run(workers_route._settle_abandoned_downloading("w1", ["j1", "missing"], time.time()))
    assert store.rows["j1"].status == before and store.saved == []


def test_the_disconnect_sweep_parks_downloading_jobs_instead_of_failing_them():
    src = inspect.getsource(workers_route)
    body = src[src.index("orphan_job_ids: list[str] = []"):
               src.index('log.info("worker disconnected: %s", worker_id)')]
    park = body.index("elif jinfo.status == JobStatus.downloading:")
    assert park < body.index("disconnected before the job "), (
        "the downloading branch must be reached before the blanket-fail branch"
    )
    assert "continue" in body[park:body.index("else:", park)]
    assert "deferred_downloading" in body and "_settle_abandoned_downloading" in body
    assert workers_route._ABANDONED_DOWNLOADING_GRACE_S >= 120, (
        "a recycle takes about half a minute to be back; leave room"
    )


def test_the_session_announce_parks_them_too():
    """A restarted worker announces no session, and "no session" is exactly
    what a resumed download looks like. Failing on that would undo the sweep."""
    src = inspect.getsource(workers_route._reconcile_worker_sessions)
    assert src.index("jinfo.status == JobStatus.downloading") < src.index("JobStatus.failed")
    assert "_settle_abandoned_downloading" in src


class _Worker:
    worker_id = "w1"


class _Registry:
    def __init__(self):
        self.marked = []

    def mark_downloading(self, worker_id, job_id):
        self.marked.append((worker_id, job_id))


@pytest.fixture
def no_registry(monkeypatch):
    """The scheduler side of the handler, reduced to what this message uses."""
    reg = _Registry()
    monkeypatch.setattr(workers_route.state, "registry", reg, raising=False)
    return reg


def test_the_resumed_message_stamps_the_row_and_nothing_else(hub, no_registry):
    info = _info("j1")
    info.progress.assets_saved = 41
    store = hub(info)
    t0 = time.time()
    asyncio.run(workers_route._handle_worker_message(
        _Worker(), WorkerJobProgress(job_id="j1", phase="downloading", resumed=True),
    ))
    row = store.rows["j1"]
    assert row.progress.resumed_at >= t0
    assert row.progress.assets_saved == 41, "this message carries no counters"
    assert row.status == JobStatus.downloading


def test_the_resumed_message_does_not_reopen_a_closed_job(hub, no_registry):
    store = hub(_info("j1", status=JobStatus.failed, phase="failed"))
    asyncio.run(workers_route._handle_worker_message(
        _Worker(), WorkerJobProgress(job_id="j1", phase="downloading", resumed=True),
    ))
    row = store.rows["j1"]
    assert row.status == JobStatus.failed and row.progress.phase == "failed"
    assert row.progress.resumed_at is None and store.saved == []


def test_a_running_job_whose_download_was_resumed_is_not_settled(hub, monkeypatch):
    """Phase is `downloading` but the row has not seen a progress line to move
    the STATUS yet -- the running watch must not fail it at 600s."""
    monkeypatch.setattr(workers_route, "_ABANDONED_RUNNING_GRACE_S", 0.0)
    monkeypatch.setattr(workers_route.state, "local_tasks", {}, raising=False)
    store = hub(_info("j1", status=JobStatus.running, resumed_at=time.time()))
    asyncio.run(workers_route._settle_abandoned_running("w1", ["j1"]))
    assert store.rows["j1"].status == JobStatus.running


def test_an_older_hub_or_worker_is_not_confused():
    """Rolling out hub-first or worker-first must both be safe: the new field
    defaults off, and unknown fields are ignored on the other side."""
    assert WorkerJobProgress(job_id="j").resumed is False
    assert JobProgress().resumed_at is None
    assert JobProgress(**{"phase": "x", "some_future_field": 1}).phase == "x"
