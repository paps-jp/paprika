"""Giving back the in-flight slot when the hub has already ended the job.

The hub can finish a job out from under the worker still executing it -- the
stale-job reconciler settles anything whose worker it cannot see in the live
fleet, and a WS flap is enough to trigger that. Nothing told the worker, so
the assignment kept its lane and its ``_in_flight`` slot for the life of the
process. Prod evidence, 2026-08-18: job 491c36f2eebe was marked
``failed`` ("worker w51111 no longer in the live fleet") at 04:42 while the
worker ran it; seven hours later the worker still held both lanes and had run
nothing since 05:34.

Two reclaim paths are pinned here -- the specific one (ask the hub whether the
job is over, cancel if it is) and the blunt one (no assignment may outlive the
wall-clock cap holding a slot) -- plus the hub-side session equivalent.
"""

import asyncio

import pytest

from server.hub import _reaper
from server.hub._state import state
from server.protocol import JobStatus
from server.worker.agent._mix_maintenance import _MaintenanceMixin
from server.worker.agent._mix_run import _RunMixin


# --------------------------------------------------------------------------
# worker: is the hub done with this job?
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _HTTP:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def get(self, url, timeout=None):
        self.calls.append(url)
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


class _Worker(_MaintenanceMixin):
    def __init__(self, resp):
        self.worker_id = "w1"
        self.hub_http_url = "http://hub:8000"
        self._http = _HTTP(resp)
        self._job_tasks = {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"status": "failed"}, "failed"),
        ({"status": "JobStatus.cancelled"}, "cancelled"),
        ({"status": "review"}, "review"),
        # ``completed`` IS terminal here, unlike the deferred-download probe:
        # the job body is still executing and the hub has already accepted a
        # result, so nothing this task produces can land.
        ({"status": "completed"}, "completed"),
        ({"status": "running"}, None),
        ({"status": "downloading"}, None),
    ],
)
async def test_hub_job_is_over_verdicts(payload, expected):
    w = _Worker(_Resp(200, payload))
    assert await w._hub_job_is_over("j1") == expected


@pytest.mark.asyncio
async def test_deleted_job_counts_as_over():
    w = _Worker(_Resp(404))
    assert await w._hub_job_is_over("j1") == "deleted"


@pytest.mark.asyncio
async def test_unreachable_hub_is_never_evidence():
    """A network blip must not cancel a job that is running fine."""
    assert await _Worker(RuntimeError("boom"))._hub_job_is_over("j1") is None
    assert await _Worker(_Resp(503))._hub_job_is_over("j1") is None


# --------------------------------------------------------------------------
# worker: the wall-clock cap returns the slot
# --------------------------------------------------------------------------

class _GuardWorker(_RunMixin):
    """Just enough agent for _run_job_guarded: a job body that hangs the way
    the stranded workers' did (parked after its work, never returning)."""

    def __init__(self, hang: bool):
        self.worker_id = "w1"
        self._job_tasks = {}
        self._in_flight = 0
        self._hang = hang
        self.sent = []
        self.finally_ran = False

    async def _send(self, msg):
        self.sent.append(msg)

    async def _run_assigned_job(self, msg):
        self._in_flight += 1
        try:
            if self._hang:
                await asyncio.Event().wait()   # never returns on its own
        finally:
            # The real job body decrements + releases its lane in a finally,
            # which is exactly why cancelling recovers the slot.
            self._in_flight -= 1
            self.finally_ran = True


class _Assign:
    job_id = "j1"


@pytest.mark.asyncio
async def test_wall_clock_cap_cancels_and_returns_the_slot(monkeypatch):
    monkeypatch.setenv("PAPRIKA_JOB_MAX_WALL_S", "0.05")
    w = _GuardWorker(hang=True)
    w._job_tasks["j1"] = asyncio.current_task()

    await asyncio.wait_for(w._run_job_guarded(_Assign()), timeout=5)

    assert w.finally_ran is True, "the job body's finally must run"
    assert w._in_flight == 0, "the slot has to come back"
    assert w._job_tasks == {}, "and the task registration with it"
    assert w.sent and w.sent[0].job_id == "j1", "hub is told, not left guessing"
    assert "wall-clock cap" in (w.sent[0].error or "")


@pytest.mark.asyncio
async def test_normal_job_is_untouched_by_the_cap(monkeypatch):
    monkeypatch.setenv("PAPRIKA_JOB_MAX_WALL_S", "30")
    w = _GuardWorker(hang=False)
    w._job_tasks["j1"] = asyncio.current_task()
    await w._run_job_guarded(_Assign())
    assert w._in_flight == 0
    assert w.sent == [], "a job that finishes sends no failure"
    assert w._job_tasks == {}


# --------------------------------------------------------------------------
# hub: the session reaper collects sessions pinned by a finished job
# --------------------------------------------------------------------------

class _Info:
    def __init__(self, status):
        self.status = status


class _Store:
    def __init__(self, jobs):
        self.jobs = jobs
        self.reads = 0

    async def get_job_info(self, job_id):
        self.reads += 1
        if job_id not in self.jobs:
            return None
        return _Info(self.jobs[job_id])


class _Sess:
    def __init__(self, job_id):
        self.job_id = job_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (JobStatus.failed, "failed"),
        (JobStatus.completed, "completed"),
        (JobStatus.cancelled, "cancelled"),
        # Still working: the b79ab7d5e813 protection -- a 652s
        # download_video must never be reaped mid-action.
        (JobStatus.running, None),
    ],
)
async def test_session_job_is_over(monkeypatch, status, expected):
    monkeypatch.setattr(state, "store", _Store({"j1": status}), raising=False)
    assert await _reaper._session_job_is_over(_Sess("j1")) == expected


@pytest.mark.asyncio
async def test_session_with_a_deleted_job_is_collectable(monkeypatch):
    monkeypatch.setattr(state, "store", _Store({}), raising=False)
    assert await _reaper._session_job_is_over(_Sess("j1")) == "gone"


@pytest.mark.asyncio
async def test_unprovable_sessions_are_left_alone(monkeypatch):
    """No job id, no store, or a store that errors -> never evict."""
    monkeypatch.setattr(state, "store", _Store({"j1": JobStatus.failed}), raising=False)
    assert await _reaper._session_job_is_over(_Sess(None)) is None

    monkeypatch.setattr(state, "store", None, raising=False)
    assert await _reaper._session_job_is_over(_Sess("j1")) is None

    class _Boom(_Store):
        async def get_job_info(self, job_id):
            raise RuntimeError("store down")

    monkeypatch.setattr(state, "store", _Boom({}), raising=False)
    assert await _reaper._session_job_is_over(_Sess("j1")) is None
