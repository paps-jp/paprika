"""Regression: ``downloading`` rows whose download died with a worker restart.

2026-11-16: 673 of 1244 ``downloading`` rows (54%) had no yt-dlp behind them
anywhere in the fleet. A worker restart (self-update exit 42, recycle,
compose recreate) kills its detached downloads, the new process comes back
under the same worker id and has never heard of those jobs. The hub's
capacity endpoint subtracts ``downloading x 0.2`` from ``available``, so the
ghosts kept job-submit from filling ~130 idle lanes. Nothing removed them:

  1. the reaper listed ``downloading`` newest-first with ``limit=500`` and
     768 rows were younger than the 1h timeout, so it never reached one old
     enough to act on;
  2. for a worker connected to this hub it only sent ``HubForceCompleteJob``,
     which the restarted worker silently ignored -- every minute, forever;
  3. its "is the worker alive on another hub" check raised TypeError, so it
     would have failed every peer-owned row outright once (1) was fixed.
"""

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from server.hub import _reaper
from server.hub._state import state
from server.protocol import (
    HubForceCompleteJob,
    JobInfo,
    JobOptions,
    JobStatus,
    WorkerJobFailed,
)


HOUR = 3600.0


def _row(job_id, worker_id, age_s, status=JobStatus.downloading):
    now = datetime.utcnow()
    return JobInfo(
        job_id=job_id,
        status=status,
        url=f"https://example.test/{job_id}",
        options=JobOptions(),
        created_at=now - timedelta(seconds=age_s + 20),
        started_at=now - timedelta(seconds=age_s),
        worker_id=worker_id,
    )


class _Store:
    def __init__(self, rows, *, with_lister=True):
        self.rows = {r.job_id: r for r in rows}
        self.lister_calls = []
        self.saved_results = []
        if not with_lister:
            self.list_stale_downloading = None

    async def list_stale_downloading(self, *, started_before, limit):
        self.lister_calls.append((started_before, limit))
        out = [
            r.model_copy(deep=True) for r in self.rows.values()
            if r.status == JobStatus.downloading and r.started_at < started_before
        ]
        return sorted(out, key=lambda r: r.started_at)[:limit]

    async def list_job_infos(self, *, offset=0, limit=0, status=None):
        out = [
            r.model_copy(deep=True) for r in self.rows.values()
            if status is None or r.status.value in status
        ]
        out.sort(key=lambda r: r.created_at, reverse=True)
        return (out[:limit] if limit else out), len(out)

    async def get_job_info(self, job_id):
        r = self.rows.get(job_id)
        return r.model_copy(deep=True) if r is not None else None

    async def save_job_info(self, info):
        self.rows[info.job_id] = info.model_copy(deep=True)

    async def save_job_result(self, result):
        self.saved_results.append(result)


class _Conn:
    """Stands in for scheduler.ConnectedWorker: the message goes out through
    the CONNECTION. WorkerRegistry itself has no send() -- the reaper calling
    one is exactly the bug these fakes must not paper over."""

    def __init__(self, sent):
        self._sent = sent

    async def send(self, msg):
        self._sent.append(msg)


class _Registry:
    def __init__(self, local=(), alive=(), stats_exc=None):
        self.sent = []
        self.connections = {w: _Conn(self.sent) for w in local}
        self._alive = set(alive) | set(local)
        self._stats_exc = stats_exc
        self.stats_calls = 0

    async def stats_async(self):
        self.stats_calls += 1
        if self._stats_exc is not None:
            raise self._stats_exc
        return {"workers": [
            {"worker_id": w, "alive": True} for w in sorted(self._alive)
        ]}


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(state, "store", None)
    monkeypatch.setattr(state, "registry", None)
    _reaper._force_complete_sent.clear()
    _reaper._worker_absent_since.clear()
    # Path C's absence grace is covered by its own tests below; elsewhere a
    # worker missing from the alive set is gone at once.
    monkeypatch.setattr(_reaper, "_DOWNLOADING_GONE_GRACE_S", 0.0)
    yield
    _reaper._force_complete_sent.clear()
    _reaper._worker_absent_since.clear()


def _install(monkeypatch, store, registry):
    monkeypatch.setattr(state, "store", store)
    monkeypatch.setattr(state, "registry", registry)


# --- 1. the listing reaches the old rows --------------------------------


async def test_old_rows_are_reached_behind_many_young_ones(monkeypatch):
    """THE incident shape: a ghost older than the timeout behind more young
    rows than the old page size. It must be actioned on the first pass."""
    rows = [_row(f"young{i:04d}", "w1", 60 + i) for i in range(800)]
    rows.append(_row("ghost", "w-gone", 5 * HOUR))
    store = _Store(rows)
    _install(monkeypatch, store, _Registry(alive={"w1"}))

    await _reaper._reap_stuck_downloading()

    (started_before, limit), = store.lister_calls
    assert started_before <= datetime.utcnow() - timedelta(seconds=HOUR - 5)
    assert limit == _reaper._DOWNLOADING_REAPER_BATCH
    assert store.rows["ghost"].status == JobStatus.failed
    assert all(
        store.rows[f"young{i:04d}"].status == JobStatus.downloading
        for i in range(800)
    )


async def test_store_without_the_oldest_first_lister_falls_back(monkeypatch):
    store = _Store([_row("ghost", "w-gone", 2 * HOUR)], with_lister=False)
    _install(monkeypatch, store, _Registry(alive={"w1"}))
    await _reaper._reap_stuck_downloading()
    assert store.rows["ghost"].status == JobStatus.failed


# --- 2. a connected worker that ignores force-complete ------------------


async def test_local_worker_gets_force_complete_first_not_a_failure(monkeypatch):
    store = _Store([_row("j1", "w1", 2 * HOUR)])
    reg = _Registry(local={"w1"})
    _install(monkeypatch, store, reg)

    await _reaper._reap_stuck_downloading()

    assert [type(m) for m in reg.sent] == [HubForceCompleteJob]
    assert store.rows["j1"].status == JobStatus.downloading
    assert "j1" in _reaper._force_complete_sent


async def test_still_downloading_after_the_grace_is_failed(monkeypatch):
    store = _Store([_row("j1", "w1", 2 * HOUR)])
    reg = _Registry(local={"w1"})
    _install(monkeypatch, store, reg)
    _reaper._force_complete_sent["j1"] = (
        time.monotonic() - _reaper._FORCE_COMPLETE_GRACE_S - 1
    )

    assert await _reaper._reap_stuck_downloading() == 1

    row = store.rows["j1"]
    assert row.status == JobStatus.failed
    assert "force-complete" in row.error
    assert row.completed_at is not None
    assert [r.job_id for r in store.saved_results] == ["j1"]
    assert reg.sent == []  # no pointless re-send on the failing pass
    assert "j1" not in _reaper._force_complete_sent


async def test_within_the_grace_it_is_only_re_sent(monkeypatch):
    store = _Store([_row("j1", "w1", 2 * HOUR)])
    reg = _Registry(local={"w1"})
    _install(monkeypatch, store, reg)
    first = time.monotonic() - 60
    _reaper._force_complete_sent["j1"] = first

    await _reaper._reap_stuck_downloading()

    assert store.rows["j1"].status == JobStatus.downloading
    assert [type(m) for m in reg.sent] == [HubForceCompleteJob]
    assert _reaper._force_complete_sent["j1"] == first  # clock not restarted


async def test_a_job_that_completed_meanwhile_is_not_overwritten(monkeypatch):
    """The listing and the write are separate reads: a wrap-up that lands in
    between must win."""
    store = _Store([_row("j1", "w1", 2 * HOUR)])
    reg = _Registry(local={"w1"})
    _install(monkeypatch, store, reg)
    _reaper._force_complete_sent["j1"] = time.monotonic() - 10 * HOUR

    real_get = store.get_job_info

    async def _get(job_id):
        info = await real_get(job_id)
        info.status = JobStatus.completed
        return info

    monkeypatch.setattr(store, "get_job_info", _get)
    assert await _reaper._reap_stuck_downloading() == 0
    assert store.rows["j1"].status == JobStatus.downloading  # untouched
    assert store.saved_results == []


async def test_clock_is_forgotten_once_the_job_leaves_the_stale_set(monkeypatch):
    store = _Store([_row("j1", "w1", 2 * HOUR)])
    _install(monkeypatch, store, _Registry(local={"w1"}))
    await _reaper._reap_stuck_downloading()
    assert "j1" in _reaper._force_complete_sent

    store.rows["j1"].status = JobStatus.completed
    await _reaper._reap_stuck_downloading()
    assert "j1" not in _reaper._force_complete_sent


# --- 3. peer-owned and gone workers -------------------------------------


async def test_worker_alive_on_a_peer_hub_is_left_to_that_hub(monkeypatch):
    store = _Store([
        _row("p1", "w-peer", 2 * HOUR),
        _row("p2", "w-peer", 3 * HOUR),
    ])
    reg = _Registry(alive={"w-peer"})
    _install(monkeypatch, store, reg)

    assert await _reaper._reap_stuck_downloading() == 0
    assert store.rows["p1"].status == JobStatus.downloading
    assert store.rows["p2"].status == JobStatus.downloading
    assert reg.sent == []
    assert reg.stats_calls == 1  # one fleet view per pass, not per row


async def test_worker_gone_fleet_wide_is_failed(monkeypatch):
    store = _Store([_row("g1", "w-gone", 2 * HOUR)])
    _install(monkeypatch, store, _Registry(alive={"w-other"}))
    assert await _reaper._reap_stuck_downloading() == 1
    assert store.rows["g1"].status == JobStatus.failed
    assert "gone fleet-wide" in store.rows["g1"].error


async def test_registry_has_no_send_so_the_connection_is_used():
    """Guard the real class, not just the fake: Path A must not go through
    ``state.registry.send``."""
    from server.scheduler import WorkerRegistry

    assert not hasattr(WorkerRegistry, "send")


async def test_worker_briefly_absent_is_not_failed_as_gone(monkeypatch):
    """A worker re-homing during a rolling hub restart drops out of the alive
    set for seconds; its download is still running."""
    monkeypatch.setattr(_reaper, "_DOWNLOADING_GONE_GRACE_S", 300.0)
    store = _Store([_row("g1", "w-flap", 2 * HOUR)])
    reg = _Registry(alive={"w-other"})
    _install(monkeypatch, store, reg)

    assert await _reaper._reap_stuck_downloading() == 0
    assert store.rows["g1"].status == JobStatus.downloading
    assert "w-flap" in _reaper._worker_absent_since

    # back in the alive set -> the absence clock is dropped
    reg._alive.add("w-flap")
    await _reaper._reap_stuck_downloading()
    assert "w-flap" not in _reaper._worker_absent_since
    assert store.rows["g1"].status == JobStatus.downloading


async def test_worker_absent_past_the_grace_is_failed_as_gone(monkeypatch):
    monkeypatch.setattr(_reaper, "_DOWNLOADING_GONE_GRACE_S", 300.0)
    store = _Store([_row("g1", "w-gone", 2 * HOUR)])
    _install(monkeypatch, store, _Registry(alive={"w-other"}))
    _reaper._worker_absent_since["w-gone"] = time.monotonic() - 301
    assert await _reaper._reap_stuck_downloading() == 1
    assert store.rows["g1"].status == JobStatus.failed


@pytest.mark.parametrize("registry", [
    _Registry(stats_exc=RuntimeError("redis down")),
    _Registry(),  # empty alive set: a blip, not a dead fleet
])
async def test_no_fleet_view_fails_nothing(monkeypatch, registry):
    store = _Store([_row("g1", "w-gone", 2 * HOUR)])
    _install(monkeypatch, store, registry)
    assert await _reaper._reap_stuck_downloading() == 0
    assert store.rows["g1"].status == JobStatus.downloading


async def test_young_rows_are_never_touched(monkeypatch):
    store = _Store([_row("y1", "w-gone", HOUR - 120)])
    reg = _Registry(local={"w1"})
    _install(monkeypatch, store, reg)
    assert await _reaper._reap_stuck_downloading() == 0
    assert store.rows["y1"].status == JobStatus.downloading
    assert reg.sent == []


# --- 4. the worker answers a force-complete it cannot act on -------------


from server.worker.agent import WorkerAgent  # noqa: E402


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPRIKA_RESUME_DIR", str(tmp_path / "resume"))
    a = object.__new__(WorkerAgent)
    a.worker_id = "w50150"
    a._force_complete_job_ids = set()
    a._abandoned_job_ids = set()
    a._bg_video_tasks = {}
    a._job_tasks = {}
    a.sent = []

    async def _send(msg):
        a.sent.append(msg)

    a._send = _send
    return a


def _sigterm(monkeypatch, result):
    def _fake(job_id):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "server.worker.agent.video._terminate_ytdlp_descendants_for_job", _fake,
    )


def _failed(agent):
    return [m for m in agent.sent if isinstance(m, WorkerJobFailed)]


async def test_unknown_job_is_reported_failed(agent, monkeypatch):
    _sigterm(monkeypatch, 0)
    await agent._force_complete_video_job("ghost", "downloading exceeded 3600s")
    (msg,) = _failed(agent)
    assert msg.job_id == "ghost"
    assert "downloading lost" in msg.error and "w50150" in msg.error
    assert "ghost" not in agent._force_complete_job_ids


async def test_deferred_download_in_flight_is_wrapped_up(agent, monkeypatch):
    _sigterm(monkeypatch, 0)
    agent._bg_video_tasks = {object(): "j1"}
    await agent._force_complete_video_job("j1", "r")
    assert _failed(agent) == []
    assert "j1" in agent._force_complete_job_ids  # the finally block salvages


async def test_job_still_in_its_lane_is_left_alone(agent, monkeypatch):
    """A fetch-mode yt-dlp runs inside the lane; the hub flips the row to
    ``downloading`` on its first progress line."""
    _sigterm(monkeypatch, 0)
    agent._job_tasks = {"j1": object()}
    await agent._force_complete_video_job("j1", "r")
    assert _failed(agent) == []


async def test_handed_over_download_not_resumed_yet_is_left_alone(agent, monkeypatch):
    from server.worker import resume as _resume

    _sigterm(monkeypatch, 0)
    assert _resume.save("j1", {"worker_id": "w50150", "dirs": []})
    await agent._force_complete_video_job("j1", "r")
    assert _failed(agent) == []


@pytest.mark.parametrize("result", [2, RuntimeError("no /proc")])
async def test_a_process_to_signal_or_no_way_to_look_is_not_a_ghost(
    agent, monkeypatch, result,
):
    _sigterm(monkeypatch, result)
    await agent._force_complete_video_job("j1", "r")
    assert _failed(agent) == []


# --- 5. the disconnect sweep hands the worker's downloads to the watch ----
#
# 2026-11-16: 1180 downloads were lost to worker restarts in 3h, 1014 of them
# to memory-guard exits. The deferred watch (_settle_abandoned_downloading)
# that should fail them 240s after the disconnect was fed only from this
# hub's session registry, and a deferred download has no session -- so each
# row stayed `downloading` for the reaper's 1h + 20min instead.


import inspect  # noqa: E402

from server.hub import mariadb_store  # noqa: E402
from server.hub.routes import workers as workers_route  # noqa: E402


class _PoolCur:
    def __init__(self, rows, seen):
        self._rows, self._seen = rows, seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, args=None):
        self._seen.append((sql, args))

    async def fetchall(self):
        return self._rows


class _PoolConn:
    def __init__(self, rows, seen):
        self._rows, self._seen = rows, seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def cursor(self):
        return _PoolCur(self._rows, self._seen)


class _Pool:
    def __init__(self, rows):
        self.rows, self.seen = rows, []

    def acquire(self):
        return _PoolConn(self.rows, self.seen)


async def test_store_lists_only_this_workers_downloading_ids():
    pool = _Pool([("j1",), ("j2",)])
    store = mariadb_store.MariaDBJobStore(pool)
    assert await store.list_downloading_ids_for_worker("w1") == ["j1", "j2"]
    (sql, args), = pool.seen
    assert "status = %s" in sql and "worker_id = %s" in sql
    assert args[:2] == ("downloading", "w1")


async def test_store_lookup_without_a_worker_id_asks_nothing():
    pool = _Pool([("j1",)])
    store = mariadb_store.MariaDBJobStore(pool)
    assert await store.list_downloading_ids_for_worker("") == []
    assert pool.seen == []


def test_the_disconnect_sweep_feeds_downloading_rows_to_the_watch():
    """The lookup must land in ``orphan_job_ids`` BEFORE the settle loop,
    whose downloading branch parks the ids for the deferred watch."""
    src = inspect.getsource(workers_route)
    body = src[src.index("orphan_job_ids: list[str] = []"):
               src.index('log.info("worker disconnected: %s", worker_id)')]
    lookup = body.index("list_downloading_ids_for_worker")
    assert lookup < body.index("for jid in set(orphan_job_ids):")
    assert "orphan_job_ids.extend(await _dl_lister(worker_id))" in body
    park = body.index("elif jinfo.status == JobStatus.downloading:")
    assert "deferred_downloading.append(jid)" in body[park:park + 1200]


# --- 6. no force-complete before the register ack -----------------------
# The hub registers a link, then awaits other work before sending
# HubRegistered. A force-complete in that window was the first frame the
# worker read; it raised "unexpected ack" out of run() and exited, dropping
# every download it held (4790419, found 2026-11-16).

from server.protocol import HubRegistered, encode_msg  # noqa: E402
from server.scheduler import ConnectedWorker  # noqa: E402
from server.worker.agent._mix_run import _RunMixin  # noqa: E402


async def test_a_worker_mid_handshake_gets_no_force_complete(monkeypatch):
    store = _Store([_row("j1", "w1", 2 * HOUR)])
    reg = _Registry(local={"w1"})
    reg.connections["w1"].registered_acked = False
    _install(monkeypatch, store, reg)

    assert await _reaper._reap_stuck_downloading() == 0

    assert reg.sent == []
    assert store.rows["j1"].status == JobStatus.downloading
    assert "j1" not in _reaper._force_complete_sent  # grace clock not started
    assert reg.stats_calls == 0  # nor treated as absent (Path B/C)


async def test_once_acked_the_same_worker_gets_it(monkeypatch):
    store = _Store([_row("j1", "w1", 2 * HOUR)])
    reg = _Registry(local={"w1"})
    reg.connections["w1"].registered_acked = True
    _install(monkeypatch, store, reg)

    await _reaper._reap_stuck_downloading()

    assert [type(m) for m in reg.sent] == [HubForceCompleteJob]


def test_a_new_connection_starts_unacked():
    import dataclasses
    f = {x.name: x for x in dataclasses.fields(ConnectedWorker)}
    assert f["registered_acked"].default is False


def test_the_hub_marks_the_link_acked_right_after_sending_the_ack():
    src = inspect.getsource(workers_route)
    send = src.index("await worker.send(\n            HubRegistered(")
    mark = src.index("worker.registered_acked = True")
    assert send < mark
    # Nothing else awaited in between: the flag must not lag the ack.
    assert src[send:mark].count("await") == 1


class _HandshakeWs:
    def __init__(self, frames):
        self._frames = list(frames)
        self.recv_calls = 0

    async def recv(self):
        self.recv_calls += 1
        return self._frames.pop(0)


class _PastTheAck(Exception):
    pass


class _HandshakeAgent:
    """Just enough of the worker to run the handshake. Setting
    ``_last_inbound_ok`` is the first thing done after the ack, so it stops
    the run there."""

    worker_id = "w1"
    capabilities = None
    worker_secret = None

    def __init__(self, ws):
        object.__setattr__(self, "_ws", ws)

    async def _send(self, msg):
        pass

    def __setattr__(self, name, value):
        if name == "_last_inbound_ok":
            raise _PastTheAck()
        object.__setattr__(self, name, value)


async def test_the_worker_handshake_survives_a_frame_before_the_ack(monkeypatch):
    monkeypatch.setattr(
        "server.worker.agent._mix_run.WorkerRegister", lambda **kw: None,
    )
    ws = _HandshakeWs([
        encode_msg(HubForceCompleteJob(job_id="j1", reason="r")),
        encode_msg(HubRegistered(worker_id="w1", expected_worker_version=None)),
    ])
    with pytest.raises(_PastTheAck):
        await _RunMixin._handshake_and_loop(_HandshakeAgent(ws))
    assert ws.recv_calls == 2


def test_frames_before_the_ack_are_handled_before_the_main_loop():
    src = inspect.getsource(_RunMixin._handshake_and_loop)
    assert "early.append(ack)" in src
    replay = src.index("for msg in early:")
    assert src.index("_handle_hub_message(msg)", replay) < src.index("async for raw in self._ws:")
    assert 'raise RuntimeError(f"unexpected ack' not in src
