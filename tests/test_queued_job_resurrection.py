"""Regression: a deleted or retired job must not come back from the dead.

2026-08-30. The deletion frontier walked BACKWARDS -- MIN(created_at) went from
2026-08-22 to 2026-05-30 -- while retention was running at full budget, and the
5-day backlog grew from 913,718 to 1,028,671 rows. 6,468 May/June rows were
resurrecting, 33 of them re-terminated every ~30 seconds:

    redrive:    re-queued 9338a308e6f4 onto the pull list
    reconciler: failed stuck queued job 9338a308e6f4 (queued 97216 min)
    lifecycle:  [hub] job 9338a308e6f4 -> worker w51238 (in_flight=1/2)

97,216 minutes = 67 days. Two gaps kept the cycle alive:

  1. the redrive had a MINIMUM age gate and no maximum, so it re-dispatched
     rows the reconciler had already retired -- forever, and always first,
     since it sorts oldest-first;
  2. nothing evicted the id from the redis pull list when a job was retired or
     its row deleted. The list outlives the row, so a worker would BLPOP a
     dead id, the assign path would ``save_job_info`` (an upsert), and the row
     came back with its ORIGINAL created_at -- straight back to the head of
     retention's oldest-first queue.

Each turn of the cycle also burned a real worker lane and uploaded assets for
a job nobody wanted, minting MinIO objects with no ledger row behind them.

Pinned here: the age bound, that it skips rather than stops, and that both the
retire path and the purge path drop the id.
"""

import inspect
import types

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub import _reaper, _redrive


def _patch_pull_queue(monkeypatch, fake):
    """Swap the module the call sites actually resolve.

    They do ``from server.hub import _pull_queue``, which reads the attribute
    off the already-imported package -- putting a fake in ``sys.modules`` does
    NOT intercept that, it just sits there unused."""
    import server.hub

    monkeypatch.setattr(server.hub, "_pull_queue", fake, raising=False)


# --------------------------------------------------------------------------- #
# 1. redrive's upper age bound
# --------------------------------------------------------------------------- #

def test_redrive_has_an_upper_age_bound():
    """Without one, redrive re-dispatches a row forever -- including rows the
    reconciler already failed."""
    assert hasattr(_redrive, "_MAX_AGE_S")
    assert _redrive._MAX_AGE_S > _redrive._MIN_AGE_S


def test_upper_bound_reads_the_reconcilers_own_env_var():
    """The two thresholds describe the same deadline from opposite sides. If
    they drift apart, the gap between them is the resurrection window."""
    src = inspect.getsource(_redrive)
    assert '_MAX_AGE_S = _num("PAPRIKA_QUEUE_TIMEOUT_S"' in src
    reaper_src = inspect.getsource(_reaper)
    assert '_STALE_QUEUED_TIMEOUT_S = float(os.environ.get("PAPRIKA_QUEUE_TIMEOUT_S"' in reaper_src


def test_over_age_rows_are_skipped_not_broken_on():
    """The candidate list is sorted OLDEST-first, so the over-age rows sit at
    the FRONT. ``break`` there would stop the pass before reaching a single
    placeable job -- 6,468 stale rows would have halted dispatch entirely."""
    src = inspect.getsource(_redrive._redrive_pass)
    gate = src[src.index("if age >= _MAX_AGE_S:"):]
    body = gate[:gate.index("if info.worker_id:")]
    # Comments in this branch mention "break" on purpose; judge the code only.
    code_lines = [
        ln for ln in body.splitlines() if not ln.strip().startswith("#")
    ]
    assert any("continue" in ln for ln in code_lines)
    assert not any("break" in ln for ln in code_lines)


def _info(job_id, age_s, now):
    """Minimal stand-in for JobInfo: _redrive_pass Phase 1 only reads these."""
    import datetime

    return types.SimpleNamespace(
        job_id=job_id,
        created_at=now - datetime.timedelta(seconds=age_s),
        worker_id=None,
        started_at=None,
        options=types.SimpleNamespace(mode="fetch"),
    )


@pytest.mark.asyncio
async def test_redrive_skips_expired_but_still_places_the_ones_behind_them(
    monkeypatch,
):
    """The whole point of ``continue``: a placeable job queued behind a pile of
    ancient ones must still get dispatched."""
    import datetime

    now = datetime.datetime.utcnow()
    old_a = _info("ancient-a", 90 * 86400, now)   # 90 days
    old_b = _info("ancient-b", 67 * 86400, now)   # 67 days, the observed case
    fresh = _info("fresh", 60, now)               # inside the deadline

    class _Store:
        async def list_job_infos(self, **kw):
            assert kw.get("status") == ["queued"]
            return [fresh, old_b, old_a], 3  # newest-first, as the real one is

    class _Registry:
        def pick_worker(self, **kw):
            return object()

    dispatched: list[str] = []
    evicted: list[str] = []

    async def _fake_dispatch(info):
        dispatched.append(info.job_id)
        return True

    class _FakePQ:
        ENABLED = True

        @staticmethod
        async def remove(jid):
            evicted.append(jid)
            return 1

    monkeypatch.setattr(_redrive.state, "store", _Store(), raising=False)
    monkeypatch.setattr(_redrive.state, "registry", _Registry(), raising=False)
    monkeypatch.setattr(_redrive, "_redrive_dispatch_one", _fake_dispatch, raising=False)
    _patch_pull_queue(monkeypatch, _FakePQ)

    placed = await _redrive._redrive_pass()

    assert dispatched == ["fresh"], "only the in-deadline job may be dispatched"
    assert sorted(evicted) == ["ancient-a", "ancient-b"]
    assert placed == 1


# --------------------------------------------------------------------------- #
# 2. eviction on retire / on purge
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_evict_helper_is_inert_when_pull_dispatch_is_off(monkeypatch):
    calls = []

    class _FakePQ:
        ENABLED = False

        @staticmethod
        async def remove(jid):
            calls.append(jid)
            return 1

    _patch_pull_queue(monkeypatch, _FakePQ)
    await _reaper._evict_from_pull_queue("j1", "test")
    assert calls == []


@pytest.mark.asyncio
async def test_evict_helper_never_raises(monkeypatch):
    """It is called from delete paths; a redis hiccup must not fail the
    delete it is attached to."""

    class _FakePQ:
        ENABLED = True

        @staticmethod
        async def remove(jid):
            raise RuntimeError("redis down")

    _patch_pull_queue(monkeypatch, _FakePQ)
    await _reaper._evict_from_pull_queue("j1", "test")  # must not raise


@pytest.mark.asyncio
async def test_purge_job_evicts_the_id(monkeypatch, tmp_path):
    """Deleting the row without evicting the id is not a delete, it is a
    pause: a worker pops the id and the upsert puts the row back with its
    original created_at."""
    import asyncio

    evicted = []

    class _FakePQ:
        ENABLED = True

        @staticmethod
        async def remove(jid):
            evicted.append(jid)
            return 1

    class _Store:
        def __init__(self):
            self.deleted = []

        async def delete_job(self, jid):
            self.deleted.append(jid)
            return True

    import server.hub.objstore as objstore

    monkeypatch.setattr(objstore, "enabled", lambda: False, raising=False)
    _patch_pull_queue(monkeypatch, _FakePQ)
    monkeypatch.setattr(
        _reaper, "get_storage_dir", lambda: tmp_path, raising=False
    )

    store = _Store()
    ok = await _reaper._purge_job(store, "j1", asyncio.Semaphore(1), 5.0)
    assert ok is True
    assert store.deleted == ["j1"]
    assert evicted == ["j1"], "the pull-list id must go with the row"


def test_reconciler_evicts_when_it_retires_a_stuck_queued_job():
    """Retiring the row is not enough -- the id may still be poppable."""
    src = inspect.getsource(_reaper)
    branch = src[src.index('"  !! [reconciler] "'):]
    branch = branch[:branch.index("queued_failed += 1")]
    assert "_evict_from_pull_queue" in branch
