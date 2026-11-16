"""The 200-job recycle must not cut the video downloads running behind it.

A deferred video download runs after its lane is released and holds no
in-flight slot, so the recycle exit -- ``_draining and _in_flight <= 0`` --
never saw it: the worker exited about ten seconds after its 200th job and
took every running download with it.

Measured 2026-11-14 over 5.5 hours: 1,130 worker restarts cut 2,368 downloads,
a third of all failed jobs. A 1GiB download died at 98.6%, 51 minutes in.

Waiting for them at the exit alone is not affordable: a draining worker takes
no jobs, and the same data says a 10-minute wait would have idled about 10% of
the fleet. So the recycle is put off -- the worker keeps working -- while a
download has long to go, and only the short rest is waited for at the exit.
"""
import inspect

import pytest

from server.worker.agent import _mix_run
from server.worker.agent._core import WorkerAgent


def _agent(jobs_done=200, downloads=None):
    """A worker at its recycle threshold. ``downloads`` maps a job id to the
    seconds yt-dlp says are left (None: no estimate)."""
    a = WorkerAgent.__new__(WorkerAgent)
    a.worker_id = "w-test"
    a._recycle_after = 200
    a._jobs_done = jobs_done
    a._draining = False
    a._drain_reason = ""
    a._in_flight = 0
    a._pending_update_to = None
    a._update_drain_m = 0.0
    a._recycle_hold_logged = False
    a._bg_video_tasks = {}
    a._bg_video_left = {}
    now = _mix_run.time.monotonic()
    for i, (job_id, left) in enumerate((downloads or {}).items()):
        a._bg_video_tasks[f"task-{i}"] = job_id
        if left is not None:
            a._bg_video_left[job_id] = ((now, float(left)),)
    return a


def test_a_recycle_with_nothing_downloading_starts_at_once():
    a = _agent()
    a._maybe_begin_recycle()
    assert a._draining and a._drain_reason == "recycle after 200 job(s)"
    assert a._downloads_own_recycle() == ""             # and nothing holds the exit


def test_below_the_threshold_nothing_happens():
    a = _agent(jobs_done=199, downloads={"j1": 1800})
    a._maybe_begin_recycle()
    assert not a._draining and not getattr(a, "_recycle_deferred_m", 0.0)


def test_a_long_download_puts_the_recycle_off_and_the_worker_keeps_working():
    a = _agent(downloads={"j1": 37 * 60})
    a._maybe_begin_recycle()
    assert not a._draining                              # still taking jobs
    assert a._recycle_deferred_m > 0
    a._jobs_done += 30                                  # ... which it goes on finishing
    a._maybe_begin_recycle()
    assert not a._draining


def test_the_recycle_is_taken_up_when_the_download_is_nearly_done():
    a = _agent(downloads={"j1": 37 * 60})
    a._maybe_begin_recycle()
    a._bg_video_left["j1"] = ((_mix_run.time.monotonic(), 90.0),)      # 1.5 minutes left
    a._maybe_begin_recycle()
    assert a._draining and a._recycle_deferred_m == 0.0
    # The exit then waits for that last stretch (and the upload after it) ...
    assert "still finishing" in a._downloads_own_recycle()
    a._bg_video_tasks.clear()
    assert a._downloads_own_recycle() == ""             # ... and goes once it is done


def test_short_or_unknown_downloads_do_not_put_it_off_but_are_waited_for_at_the_exit():
    a = _agent(downloads={"short": 60, "unknown": None})
    a._maybe_begin_recycle()
    assert a._draining
    assert "2 video download(s) still finishing" in a._downloads_own_recycle()


def test_one_wild_estimate_does_not_decide():
    # 2026-11-14, first hour live: a 6GiB download read 44, 64 and 47 minutes
    # within ten seconds. The 64 crossed the put-off limit of an hour, the
    # recycle went ahead and the download was cut with 50 minutes to go.
    a = _agent(downloads={"j1": 50 * 60})
    now = _mix_run.time.monotonic()
    readings = [45, 46, 55, 55, 54, 52, 48, 47, 46, 47, 47, 53, 64, 61, 55, 51, 47, 44, 47, 50, 64]
    a._bg_video_left["j1"] = tuple(
        (now - (len(readings) - i), m * 60.0) for i, m in enumerate(readings)
    )
    (_, left), = a._downloads_left()
    assert 45 * 60 <= left <= 52 * 60                   # the middle, not the last (64)
    a._maybe_begin_recycle()
    assert not a._draining
    # Only so many are kept, at most one a second.
    for _ in range(50):
        a._note_download_left("j1", "50:00")
    assert len(a._bg_video_left["j1"]) <= WorkerAgent._DL_LEFT_SAMPLES


def test_a_stalled_download_is_not_believed():
    a = _agent(downloads={"j1": 20 * 60})
    stale = _mix_run.time.monotonic() - WorkerAgent._DL_LEFT_STALE_S - 1
    a._bg_video_left["j1"] = ((stale, 20 * 60.0),)
    assert a._downloads_left() == [("j1", None)]
    a._maybe_begin_recycle()
    assert a._draining


def test_a_download_that_cannot_finish_in_time_does_not_put_it_off():
    # It would be cut at the end of the wait anyway: waiting an hour for it
    # buys nothing.
    a = _agent(downloads={"j1": WorkerAgent._RECYCLE_DEFER_MAX_S + 600})
    a._maybe_begin_recycle()
    assert a._draining


def test_putting_off_is_bounded():
    a = _agent(downloads={"j1": 30 * 60})
    a._maybe_begin_recycle()
    assert not a._draining
    # 45 minutes later another 30-minute download is running: 15 minutes of
    # the hour are left, it cannot finish, the recycle goes ahead.
    a._recycle_since_m = _mix_run.time.monotonic() - 45 * 60
    a._bg_video_left["j1"] = ((_mix_run.time.monotonic(), 30 * 60.0),)
    a._maybe_begin_recycle()
    assert a._draining


def test_a_job_with_more_files_to_fetch_is_not_near_its_end():
    # 2026-11-15, live: a job's first file read "46 seconds left", the recycle
    # started, the second file began at 0% with 24 minutes to go and was cut.
    a = _agent(downloads={"j1": 46})
    a._note_download_stage("j1", more=1)
    assert a._downloads_left() == [("j1", None)]        # a new file: the old estimate is dropped
    a._note_download_left("j1", "00:46")
    a._maybe_begin_recycle()
    assert not a._draining
    # The last file, far from done: put off on its own estimate.
    a._note_download_stage("j1", more=0)
    a._bg_video_left["j1"] = ((_mix_run.time.monotonic(), 24 * 60.0),)
    a._maybe_begin_recycle()
    assert not a._draining
    # The last file, nearly done: now the recycle goes ahead.
    a._bg_video_left["j1"] = ((_mix_run.time.monotonic(), 46.0),)
    a._maybe_begin_recycle()
    assert a._draining


def test_a_download_that_has_only_just_started_counts_until_it_has_an_estimate():
    a = _agent(downloads={"j1": None})
    a._note_download_stage("j1", more=0)
    a._maybe_begin_recycle()
    assert not a._draining
    # ... but one that never prints an estimate (ffmpeg, a live stream) is not
    # waited for beyond that: nobody can say when it ends.
    stage, more, _ = a._bg_video_stage["j1"]
    a._bg_video_stage["j1"] = (
        stage, more, _mix_run.time.monotonic() - WorkerAgent._DL_LEFT_STALE_S - 1)
    a._maybe_begin_recycle()
    assert a._draining


def test_a_fetched_video_waiting_to_be_uploaded_puts_the_recycle_off():
    # The store answers 507 while it is full and the upload retries for up to
    # 15 minutes: 6 of 62 downloads cut in one 20-minute window were finished
    # files waiting for that.
    a = _agent(downloads={"j1": None})
    a._note_download_stage("j1", stage="upload")
    a._maybe_begin_recycle()
    assert not a._draining
    # Bounded like every other reason.
    a._recycle_since_m = _mix_run.time.monotonic() - WorkerAgent._RECYCLE_DEFER_MAX_S
    a._maybe_begin_recycle()
    assert a._draining


def test_a_drained_recycle_goes_back_to_work_for_a_download_that_started_meanwhile():
    a = _agent()
    a._maybe_begin_recycle()
    assert a._draining                                  # nothing was downloading
    assert a._recycle_back_to_work() == ""              # ... and still nothing is
    # A job that was in flight when the drain began starts a long download.
    a._bg_video_tasks["t"] = "late"
    a._bg_video_left["late"] = ((_mix_run.time.monotonic(), 20 * 60.0),)
    assert "long to go" in a._recycle_back_to_work()
    assert not a._draining                              # taking jobs again
    a._maybe_begin_recycle()
    assert not a._draining                              # and put off as usual
    # The overall limit still counts from the first time, not from here.
    a._recycle_since_m = _mix_run.time.monotonic() - 50 * 60
    a._maybe_begin_recycle()
    assert a._draining
    assert a._recycle_back_to_work() == ""


def test_only_a_recycle_goes_back_to_work():
    a = _agent(downloads={"j1": 20 * 60})
    a._draining, a._drain_reason = True, "memory guard: anon 7100MB"
    assert a._recycle_back_to_work() == "" and a._draining
    a._drain_reason = "recycle after 200 job(s)"
    a._pending_update_to = "abc123"                     # a self-update wants this exit
    assert a._recycle_back_to_work() == "" and a._draining


def test_the_wait_at_the_exit_is_bounded():
    a = _agent(downloads={"j1": 60})
    a._maybe_begin_recycle()
    assert a._downloads_own_recycle() != ""
    a._recycle_dl_wait_m = _mix_run.time.monotonic() - WorkerAgent._RECYCLE_DL_WAIT_S - 1
    assert a._downloads_own_recycle() == ""


def test_the_exit_does_not_wait_for_a_download_that_cannot_make_it():
    # 2026-11-14, first hour live: a worker idled the whole 300s for one
    # download that still had 197 minutes to go, then cut it anyway.
    a = _agent(downloads={"hopeless": 197 * 60})
    a._draining, a._drain_reason = True, "recycle after 200 job(s)"
    assert a._downloads_own_recycle() == ""
    # Next to one that can make it, the wait is for that one only.
    a = _agent(downloads={"hopeless": 197 * 60, "nearly": 100})
    a._draining, a._drain_reason = True, "recycle after 200 job(s)"
    assert "1 video download(s) still finishing" in a._downloads_own_recycle()
    # ... and only while it still can: 250s into the wait, 100s more is too long.
    a._recycle_dl_wait_m = _mix_run.time.monotonic() - 250
    a._bg_video_left["nearly"] = ((_mix_run.time.monotonic(), 100.0),)
    assert a._downloads_own_recycle() == ""


def test_only_the_recycle_waits():
    # The memory guard and the fd-budget gate drain because the worker is in
    # trouble now; they must exit as promptly as before.
    a = _agent(downloads={"j1": 60})
    a._draining = True
    a._drain_reason = "memory guard: anon 7100MB"
    assert a._downloads_own_recycle() == ""


def test_the_exit_consults_the_downloads_and_the_heartbeat_takes_a_put_off_recycle_up():
    src = inspect.getsource(_mix_run)
    start = src.index("# Recycle: once the drain has emptied in-flight")
    body = src[start:start + 1600]
    assert body.index("_downloads_own_recycle()") < body.index("os._exit(0)")
    assert body.index("_note_cut_downloads()") < body.index("os._exit(0)")
    # ... and that same question first asks whether to go back to work.
    own = inspect.getsource(WorkerAgent._downloads_own_recycle)
    assert own.index("_recycle_back_to_work()") < own.index("_downloads_left()")
    # An idle worker finishes no jobs, so the heartbeat has to look as well.
    hb = src[:start]
    assert "self._maybe_begin_recycle()" in hb


def test_the_estimate_is_read_from_yt_dlp_and_forgotten_with_the_download():
    assert _mix_run._eta_seconds("37:23") == 37 * 60 + 23
    assert _mix_run._eta_seconds("01:04:50") == 3600 + 4 * 60 + 50
    for bad in ("Unknown", "", None, "1:2:3:4", "-1:00"):
        assert _mix_run._eta_seconds(bad) is None
    a = _agent()
    a._bg_video_tasks["t"] = "j1"
    a._note_download_left("j1", "10:00")
    (job_id, left), = a._downloads_left()
    assert job_id == "j1" and 590 <= left <= 600
    a._note_download_left("j1", "Unknown")              # keeps the last real estimate
    assert a._downloads_left()[0][1] is not None
    from server.worker.agent import _mix_jobexec
    spawn = inspect.getsource(_mix_jobexec)
    assert "self._note_download_left(job_id, _prog[\"eta\"])" in spawn
    assert "_bg_video_left" in spawn                     # dropped when the task ends
    # Each file of a job, and the upload after them, is announced.
    assert "self._note_download_stage(job_id, more=_more)" in spawn
    assert "self._note_download_stage(job_id, stage=\"upload\")" in spawn
    assert "_bg_video_stage" in spawn


@pytest.mark.parametrize("env,method", [
    ("PAPRIKA_RECYCLE_DL_LEFT_S", "_recycle_should_wait"),
    ("PAPRIKA_RECYCLE_DEFER_MAX_S", "_recycle_should_wait"),
    ("PAPRIKA_RECYCLE_DL_WAIT_S", "_downloads_own_recycle"),
])
def test_each_wait_has_a_kill_switch(env, method, monkeypatch):
    assert env in inspect.getsource(getattr(WorkerAgent, method))
    monkeypatch.setenv(env, "0")
    a = _agent(downloads={"j1": 20 * 60})
    if method == "_recycle_should_wait":
        a._maybe_begin_recycle()
        assert a._draining                              # not put off
    else:
        a._draining, a._drain_reason = True, "recycle after 200 job(s)"
        assert a._downloads_own_recycle() == ""          # exits at once, as before
