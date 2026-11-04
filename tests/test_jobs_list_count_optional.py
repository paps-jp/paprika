"""Regression: the admin jobs list must not pay for COUNT(*) it never reads.

Measured on prod 2026-09-11 (16.4 M rows in ``jobs``):

    GET /jobs?limit=1     3.36 s     <- one row, still 3.4 s
    GET /jobs?limit=20    3.53 s
    GET /jobs?limit=500   5.79 s
    GET /jobs?status=running  0.03-0.39 s   <- filtered COUNT is cheap

i.e. ~3.4 s of FIXED cost that scales with the TABLE, not the page: the
unfiltered ``SELECT COUNT(*) FROM jobs`` walks the whole index. The admin
polls this every 2 s and then renders the per-status badge count from
``/jobs/summary`` instead (``_badgeTotalForFilter ?? jobs.total``), so the
expensive number was computed and thrown away -- while the 2 s poll interval
against a 3.5 s response piled requests up until the browser's 6-connection
limit stalled the whole UI.

Pinned here:
  * ``count=0`` emits NO COUNT query and reports ``total: None``
  * the default still counts, so external API clients keep their total
  * the two answers can't collide in the response memo
  * a store that predates the kwarg still works (no 500)
"""

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub import mariadb_store
from server.hub._state import state
from server.hub.routes.jobs import lifecycle as lc


class _Cur:
    def __init__(self, holder):
        self.holder = holder

    async def execute(self, sql, params=None):
        self.holder.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return (7,)

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, holder):
        self.holder = holder

    def cursor(self):
        return _Cur(self.holder)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self):
        self.sql: list = []

    def acquire(self):
        return _Conn(self.sql)


def _store():
    s = object.__new__(mariadb_store.MariaDBJobStore)
    s._pool = _Pool()
    return s


def _counts(pool) -> list:
    return [sql for sql, _p in pool.sql if sql.upper().startswith("SELECT COUNT(")]


@pytest.mark.asyncio
async def test_with_total_false_emits_no_count_query():
    s = _store()
    infos, total = await s.list_job_infos(limit=20, with_total=False)
    assert _counts(s._pool) == [], s._pool.sql
    # The paged SELECT still ran -- we dropped the count, not the page.
    assert any("FROM JOBS" in sql.upper() for sql, _ in s._pool.sql), s._pool.sql
    assert total is None


@pytest.mark.asyncio
async def test_default_still_counts():
    """External clients (the SDK, pipeline consumers) read ``total``."""
    s = _store()
    _infos, total = await s.list_job_infos(limit=20)
    assert len(_counts(s._pool)) == 1, s._pool.sql
    assert total == 7


class _FakeStore:
    """Records the with_total it was called with; returns one page."""

    def __init__(self):
        self.calls: list = []

    async def list_job_infos(self, **kw):
        self.calls.append(kw)
        return [], (0 if kw.get("with_total", True) else None)


@pytest.fixture
def store(monkeypatch):
    st = _FakeStore()
    monkeypatch.setattr(state, "store", st, raising=False)
    # The route memoises responses for ~1.5s; start every test from empty so
    # one case can't be served the other's cached answer.
    lc._JOBS_LIST_CACHE.clear()
    yield st
    lc._JOBS_LIST_CACHE.clear()


class _Req:
    headers: dict = {}
    query_params: dict = {}


@pytest.mark.asyncio
async def test_route_count_zero_propagates_and_reports_null_total(store):
    out = await lc.list_jobs(_Req(), limit=20, count=0)
    assert store.calls[-1]["with_total"] is False
    assert out["total"] is None


@pytest.mark.asyncio
async def test_route_default_asks_for_the_total(store):
    out = await lc.list_jobs(_Req(), limit=20)
    assert store.calls[-1]["with_total"] is True
    assert out["total"] == 0


@pytest.mark.asyncio
async def test_counted_and_uncounted_do_not_share_a_cache_entry(store):
    """Same page, different question -- a shared memo key would let the
    ``count=0`` poll poison the answer an API client gets (and vice versa)."""
    counted = await lc.list_jobs(_Req(), limit=20)
    uncounted = await lc.list_jobs(_Req(), limit=20, count=0)
    assert counted["total"] == 0
    assert uncounted["total"] is None
    assert len(store.calls) == 2, store.calls


@pytest.mark.asyncio
async def test_store_without_the_kwarg_still_answers(monkeypatch):
    """A store predating with_total (or an old test double) must not 500."""

    class _Old:
        async def list_job_infos(
            self, *, offset=0, limit=0, status=None, mode=None,
            url_substr=None, owner_id=None,
        ):
            return [], 3

    monkeypatch.setattr(state, "store", _Old(), raising=False)
    lc._JOBS_LIST_CACHE.clear()
    out = await lc.list_jobs(_Req(), limit=20, count=0)
    assert out["total"] == 3
    lc._JOBS_LIST_CACHE.clear()
