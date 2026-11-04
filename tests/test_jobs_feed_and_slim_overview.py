"""The pieces the append-only #jobs console stands on.

The live jobs view used to be a paginated, sorted, counted TABLE that was
rebuilt from scratch every 2 s. At prod scale that shape cannot work: 16.4 M
rows growing by ~88 k/hour (24 jobs/s) means page 1 is a different set every
second and the total is meaningless. The console instead *appends* whatever
finished since its watermark -- keyset, no COUNT, no OFFSET -- and trims the
top once the buffer is full, like ``tail -f``.

Pinned here:
  * ``/jobs/completes?slim=1`` returns the compact row the console renders,
    not the ~1.2 KB JobInfo (a 500-row batch is ~600 KB otherwise)
  * a slim row never leaks the heavy fields
  * ``/overview?slim=1`` keeps the counts and drops the arrays (~240 KB/poll
    at 253 workers, fetched twice a second by every tab)
  * ``lanes`` is summed server-side so a slim poll can still show it
"""

from datetime import datetime

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
import server.scheduler as scheduler
from server.hub._state import state
from server.hub.routes import system
from server.hub.routes.jobs import lifecycle as lc
from server.protocol import JobInfo, JobOptions, JobStatus


def _info(**kw) -> JobInfo:
    base = dict(
        job_id="j-1",
        status=JobStatus.completed,
        url="https://example.tld/watch/1",
        options=JobOptions(mode="fetch"),
        created_at=datetime(2026, 9, 11, 10, 0, 0),
        started_at=datetime(2026, 9, 11, 10, 0, 1),
        completed_at=datetime(2026, 9, 11, 10, 0, 4),
        worker_id="w51123",
        lane_idx=1,
    )
    base.update(kw)
    return JobInfo(**base)


def test_slim_row_carries_what_a_console_line_shows():
    row = lc._slim_feed_row(_info())
    assert row["job_id"] == "j-1"
    assert row["status"] == "completed"
    assert row["url"].endswith("/watch/1")
    assert row["mode"] == "fetch"
    assert row["worker_id"] == "w51123"
    assert row["lane_idx"] == 1
    # Timestamps are strings the browser can parse, not datetimes.
    assert row["started_at"].startswith("2026-09-11T10:00:01")
    assert row["completed_at"].startswith("2026-09-11T10:00:04")


def test_slim_row_drops_the_heavy_fields():
    """The whole point is the byte count: 500 full rows are ~600 KB."""
    row = lc._slim_feed_row(_info())
    for heavy in ("options", "progress", "goal", "novnc_url", "session_id"):
        assert heavy not in row, heavy


def test_slim_row_clips_a_runaway_error():
    row = lc._slim_feed_row(_info(status=JobStatus.failed, error="x" * 9000))
    assert len(row["error"]) == 200


def test_slim_row_survives_a_missing_completed_at():
    row = lc._slim_feed_row(_info(completed_at=None, worker_id=None))
    assert row["completed_at"] is None
    assert row["worker_id"] == ""


def test_sum_lanes_counts_every_row_not_just_alive_ones():
    """`alive` flaps a few percent per poll; a lane total gated on it would
    jitter against the worker count printed next to it."""
    rows = [
        {"capacity": 2, "alive": True},
        {"capacity": 2, "alive": False},
        {"capacity": 2, "alive": True},
    ]
    assert scheduler._sum_lanes(rows) == 6


def test_sum_lanes_treats_an_unknown_capacity_as_zero_not_one():
    assert scheduler._sum_lanes([{"alive": True}, {"capacity": None}]) == 0
    assert scheduler._sum_lanes([{"capacity": "junk"}, {"capacity": 2}]) == 2


class _Registry:
    def stats(self):
        return {"count": 0, "lanes": 0, "workers": []}

    async def stats_async(self):
        workers = [
            {"worker_id": "w1", "capacity": 2, "alive": True},
            {"worker_id": "w2", "capacity": 2, "alive": False},
        ]
        return {
            "count": len(workers),
            "lanes": scheduler._sum_lanes(workers),
            "workers": workers,
        }


@pytest.mark.asyncio
async def test_overview_slim_keeps_the_counts_and_drops_the_arrays(monkeypatch):
    monkeypatch.setattr(state, "registry", _Registry(), raising=False)
    monkeypatch.setattr(state, "sessions", None, raising=False)

    full = await system.overview()
    slim = await system.overview(slim=1)

    assert full["workers"]["count"] == 2
    assert len(full["workers"]["workers"]) == 2

    assert slim["workers"]["count"] == 2
    assert slim["workers"]["lanes"] == 4
    assert slim["workers"]["workers"] == []
    assert slim["sessions"]["sessions"] == []
    # health / job count are what the header renders -- they must survive.
    assert "health" in slim and "jobs" in slim
