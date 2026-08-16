"""Regression: garbage a finished job leaves on the NODE, and who collects it.

Two leaks, both on tmpfs shared by every worker CT on a Proxmox node, both
measured across the fleet on 2026-08-16:

  1. 398 scratch directories under /ram/pdl, of which only 53 belonged to a
     job that was still running. The rest were downloads for jobs the hub had
     already failed / completed elsewhere / deleted -- yt-dlp kept writing to
     them for up to its 2h cap because nothing tells a worker its job is over.

  2. Lane roots under /ram/chrome owned by workers that no longer exist
     (w51180, w5143 and six others). Every sweep in the worker is
     owner-scoped -- correctly, since the CTs cannot see each other's
     processes -- which leaves a decommissioned worker's data with no
     collector at all. The oldest had been resident 76 hours.

The dangerous direction for (2) is over-deletion: reclaiming a live
neighbour's profile costs it its logged-in Chrome. So the negative cases are
the point of the chrome tests below.
"""

import os
import time

import pytest

from server.worker import lanes
from server.worker.agent import WorkerAgent


HOUR = 3600.0


# --- 1. abandoning a download whose job the hub has finished ---------------


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class _Http:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def get(self, url, timeout=None):
        self.calls.append(url)
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


@pytest.fixture
def agent():
    a = object.__new__(WorkerAgent)
    a.worker_id = "w50150"
    a.hub_http_url = "http://hub:8000"
    a._abandoned_job_ids = set()
    a._force_complete_job_ids = set()
    a._bg_video_tasks = {}
    return a


@pytest.mark.parametrize("resp,expected", [
    (_Resp(404), "deleted"),
    (_Resp(200, {"status": "failed"}), "failed"),
    (_Resp(200, {"status": "JobStatus.cancelled"}), "cancelled"),
])
async def test_terminal_states_are_reported(agent, resp, expected):
    agent._http = _Http(resp)
    assert await agent._hub_job_is_finished("j1") == expected


@pytest.mark.parametrize("resp", [
    _Resp(200, {"status": "running"}),
    _Resp(200, {"status": "downloading"}),
    _Resp(200, {"status": "queued"}),
    # The one that looks terminal and is not: the fetch phase completes
    # first and the deferred download uploads its video afterwards. Sampled
    # on the fleet 2026-08-16, 20 of 26 scratch dirs under a completed row
    # had a live yt-dlp writing into them -- treating this as terminal would
    # throw away the video on every ordinary job.
    _Resp(200, {"status": "JobStatus.completed"}),
    # No answer must never read as "finished": a network blip is not a
    # reason to kill a 90%-complete video.
    _Resp(502),
    _Resp(200, None),
    RuntimeError("connection reset"),
])
async def test_anything_short_of_terminal_keeps_the_download(agent, resp):
    agent._http = _Http(resp)
    assert await agent._hub_job_is_finished("j1") is None


async def test_abandon_marks_the_job_and_signals_its_downloads(agent, monkeypatch):
    """The mark is what makes the deferred task's finally skip the uploads and
    the JobComplete; the SIGTERM is what makes it get there now rather than at
    the 2h cap."""
    seen = []
    monkeypatch.setattr(
        "server.worker.agent.video._terminate_ytdlp_descendants_for_job",
        lambda job_id: seen.append(job_id) or 2,
    )

    killed = await agent._abandon_video_job("j9", "failed")

    assert killed == 2
    assert seen == ["j9"]
    assert "j9" in agent._abandoned_job_ids


async def _run_loop(agent, monkeypatch, verdicts, rounds):
    """Drive _abandoned_download_loop for ``rounds`` polls, answering each
    poll from ``verdicts`` (a list, one entry per round)."""
    import asyncio

    monkeypatch.setenv("PAPRIKA_ABANDON_POLL_INTERVAL_S", "1")
    monkeypatch.setenv("PAPRIKA_ABANDON_GRACE_S", "0")
    agent._bg_video_tasks = {object(): "j1"}
    seq = list(verdicts)
    calls = {"n": 0}

    async def _sleep(_s):
        calls["n"] += 1
        if calls["n"] > rounds:
            raise asyncio.CancelledError

    async def _finished(job_id):
        return seq[min(calls["n"] - 1, len(seq) - 1)]

    abandoned = []

    async def _abandon(job_id, reason):
        abandoned.append((job_id, reason))
        agent._abandoned_job_ids.add(job_id)
        return 1

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    agent._hub_job_is_finished = _finished
    agent._abandon_video_job = _abandon
    await agent._abandoned_download_loop()
    return abandoned


async def test_failed_needs_two_readings_before_a_download_dies(agent, monkeypatch):
    """A row can read failed while we are alive and working: the downloading
    reaper marks a job failed the moment it believes the worker is gone
    fleet-wide, which one WS flap produces. One reading is not evidence."""
    abandoned = await _run_loop(agent, monkeypatch, ["failed", None, None], rounds=3)
    assert abandoned == []


async def test_a_persistently_failed_job_is_dropped(agent, monkeypatch):
    abandoned = await _run_loop(agent, monkeypatch, ["failed", "failed"], rounds=3)
    assert abandoned == [("j1", "failed")]


async def test_a_deleted_job_is_dropped_at_once(agent, monkeypatch):
    """Nothing can consume a job that no longer exists, so there is nothing to
    be patient about."""
    abandoned = await _run_loop(agent, monkeypatch, ["deleted"], rounds=2)
    assert abandoned == [("j1", "deleted")]


# --- 2. chrome lane roots whose owner is gone ------------------------------


@pytest.fixture
def ramdisk(tmp_path, monkeypatch):
    """A 'mounted' /ram/chrome with our own lane root at <mount>/w50150."""
    mount = tmp_path / "chrome"
    root = mount / "w50150"
    root.mkdir(parents=True)
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: True)
    monkeypatch.setattr(lanes, "chrome_lane_root", lambda: root)
    return mount


def _lane_root(mount, worker_id, age_s, size=4096):
    d = mount / worker_id
    lane = d / "chrome-lane-0"
    lane.mkdir(parents=True)
    (lane / "Cookies").write_bytes(b"x" * size)
    old = time.time() - age_s
    for p in (lane / "Cookies", lane, d):
        os.utime(p, (old, old))
    return d


def test_vanished_owners_lane_root_is_reclaimed(ramdisk):
    gone = _lane_root(ramdisk, "w51180", 76 * HOUR)

    removed, freed = lanes.sweep_chrome_orphans("w50150", 12 * HOUR)

    assert removed == 1
    assert not gone.exists()
    assert freed >= 4096


def test_fallback_id_lane_roots_are_reclaimed(ramdisk):
    """A worker that cannot reach the hub at startup names itself
    ``<container-hash>-<suffix>`` and abandons that lane root as soon as it
    gets its real id back. Measured on two nodes: 6 GB of them, none of which
    an owner-shaped-id-only match would have collected."""
    gone = _lane_root(ramdisk, "0cfbc8f68d49-ck4x", 76 * HOUR)

    removed, _freed = lanes.sweep_chrome_orphans("w50150", 12 * HOUR)

    assert removed == 1
    assert not gone.exists()


def test_live_neighbour_is_spared(ramdisk):
    """The owner directory's own mtime only moves when a lane dir is created
    or removed, so a worker running the same two lanes for a week looks a week
    idle. Activity INSIDE the lane dirs is the signal."""
    live = _lane_root(ramdisk, "w51176", 76 * HOUR)
    (live / "chrome-lane-0" / "Preferences").write_bytes(b"{}")  # fresh write

    removed, _freed = lanes.sweep_chrome_orphans("w50150", 12 * HOUR)

    assert removed == 0
    assert live.is_dir()


def test_our_own_lane_root_is_never_swept(ramdisk):
    mine = ramdisk / "w50150"
    old = time.time() - 76 * HOUR
    os.utime(mine, (old, old))

    removed, _freed = lanes.sweep_chrome_orphans("w50150", 12 * HOUR)

    assert removed == 0
    assert mine.is_dir()


def test_recent_orphan_is_left_for_a_later_pass(ramdisk):
    recent = _lane_root(ramdisk, "w51180", 2 * HOUR)

    removed, _freed = lanes.sweep_chrome_orphans("w50150", 12 * HOUR)

    assert removed == 0
    assert recent.is_dir()


def test_zero_age_disables_the_sweep(ramdisk):
    gone = _lane_root(ramdisk, "w51180", 76 * HOUR)

    assert lanes.sweep_chrome_orphans("w50150", 0.0) == (0, 0)
    assert gone.is_dir()


def test_no_ramdisk_means_no_sweep(tmp_path, monkeypatch):
    """Without the node mount every lane root is this container's own /tmp;
    there are no neighbours to reclaim and nothing shared to protect."""
    monkeypatch.setattr(lanes, "chrome_on_ramdisk", lambda: False)
    assert lanes.sweep_chrome_orphans("w50150", 12 * HOUR) == (0, 0)


def test_owner_marker_is_stamped(ramdisk):
    lanes.touch_chrome_owner()
    assert (ramdisk / "w50150" / lanes._CHROME_OWNER_MARKER).exists()
