"""Regression: the POST /jobs duplicate check must match the URL exactly.

2026-08-15. The same-URL dedup passed the WHOLE url as ``url_substr``, which
becomes ``url LIKE '%<url>%'``. A leading wildcard cannot use the url index, so
the optimizer ranged over ``idx_status_created`` instead and filtered each row:

    LIKE  -> type=range  key=idx_status_created  rows≈35,747
    =     -> type=ref    key=idx_url_prefix      rows=1        (index exists)

That was harmless while the pages stayed hot -- stage timing had ``dedup`` at
38-69ms, ~3% of POST /jobs. Once they weren't, the same query went to **p50
24.2s and 98.2% of all POST /jobs time**, and crawl intake fell from 279 to
120 jobs/min while the fleet sat 84% idle.

Switching to equality changes no behaviour: the substring result was filtered
to ``j.url == req.url`` on the very next line. It only removes the rows the
LIKE dragged in (URLs that *contain* this URL).

Pinned here:
  1. the dedup call site asks for an exact match, not a substring;
  2. the store turns url_exact into ``url = %s`` (indexable) and url_substr
     into the LIKE form (still needed by the operator's /jobs?q= search);
  3. exact wins if both are somehow passed -- never silently fall back to the
     scan;
  4. the operator search keeps its substring semantics.
"""

import inspect

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub import mariadb_store
from server.hub.routes.jobs import lifecycle as lc


class _Cur:
    def __init__(self, holder):
        self.holder = holder

    async def execute(self, sql, params=None):
        self.holder.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return (0,)

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


def test_dedup_call_site_uses_exact_match():
    src = inspect.getsource(lc.create_job)
    # The dedup block must not hand the whole URL to the substring filter.
    assert "url_exact=req.url" in src
    assert "url_substr=req.url" not in src


@pytest.mark.asyncio
async def test_url_exact_emits_an_equality_predicate():
    s = _store()
    await s.list_job_infos(status=["queued", "running"], url_exact="https://x.tld/a")
    where = [sql for sql, _p in s._pool.sql if "FROM jobs" in sql]
    assert where, s._pool.sql
    assert all("URL = %S" in w.upper() for w in where), where
    assert not any("LIKE" in w.upper() for w in where), where
    params = [p for sql, p in s._pool.sql if "FROM jobs" in sql][0]
    assert "https://x.tld/a" in params
    assert "%https://x.tld/a%" not in params


@pytest.mark.asyncio
async def test_url_substr_still_emits_like_for_operator_search():
    """GET /jobs?q= is a real substring search -- it must keep working."""
    s = _store()
    await s.list_job_infos(url_substr="example.com")
    where = [sql for sql, _p in s._pool.sql if "FROM jobs" in sql]
    assert any("URL LIKE" in w.upper() for w in where), where
    params = [p for sql, p in s._pool.sql if "FROM jobs" in sql][0]
    assert "%example.com%" in params


@pytest.mark.asyncio
async def test_exact_wins_over_substr_if_both_are_passed():
    """Never silently degrade back to the scan."""
    s = _store()
    await s.list_job_infos(url_exact="https://x.tld/a", url_substr="x.tld")
    where = [sql for sql, _p in s._pool.sql if "FROM jobs" in sql]
    assert all("LIKE" not in w.upper() for w in where), where


@pytest.mark.asyncio
async def test_status_filter_is_kept_alongside_the_exact_match():
    s = _store()
    await s.list_job_infos(
        status=["queued", "running", "downloading"], url_exact="https://x.tld/a")
    where = [sql for sql, _p in s._pool.sql if "FROM jobs" in sql][0]
    assert "STATUS IN" in where.upper()
    assert "URL = %S" in where.upper()
