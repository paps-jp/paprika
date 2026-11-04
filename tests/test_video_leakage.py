"""``/stats/video-leakage``: download_video jobs that saw a stream and still
completed with no video.

A job counts as completed whether or not the deferred yt-dlp produced a file,
so the loss was invisible. Pinned here:
  * the SQL filters on (status, created_at) -- the indexed pair -- in HOURS,
    tests download_video via JSON_EXTRACT (MariaDB's JSON_VALUE renders a
    boolean as '1'/'0'; ``= 'true'`` matched nothing on prod 2026-11-04),
    classifies a job by a text LIKE on its assets JSON (not JSON_SEARCH), and
    is bounded by max_statement_time
  * a host's jobs split into with_video / missed (stream seen, no asset) /
    no_stream (nothing seen: not a loss), and miss_rate ignores no_stream
  * the host's video_probe_stats histogram rides along (www. folded)
  * ordering is by missed desc; totals cover every host, not the cut
  * the route caches per (hours, limit) and ``refresh=1`` bypasses it
"""

import asyncio

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub import mariadb
from server.hub.routes import videostats


class _Cur:
    def __init__(self, log, leak_rows, probe_rows):
        self.log = log
        self._leak = leak_rows
        self._probe = probe_rows
        self._last = None

    async def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.log.append((flat, params))
        self._last = flat

    async def fetchall(self):
        if "FROM video_probe_stats" in self._last:
            return self._probe
        if "FROM jobs j JOIN job_results r" in self._last:
            return self._leak
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, leak_rows, probe_rows):
        self.log = []
        self._cur = _Cur(self.log, leak_rows, probe_rows)

    def acquire(self):
        return _Conn(self._cur)


# (host, jobs, with_video, missed) as the SQL returns them (SUMs are Decimal
# on aiomysql; ints here are fine, the loader casts).
_LEAK = [
    ("example.com", 10, 2, 6),      # 2 no_stream
    ("cdnvideo.tld", 4, 4, 0),
    ("gated.tld", 6, 0, 2),         # 4 no_stream
    ("gallery.tld", 300, 0, 0),     # pictures only: never a loss
]
_PROBE = [
    ("www.gated.tld", "gated", 30),
    ("gated.tld", "tokened", 10),
    ("example.com", "decouplable", 5),
]


def test_rows_are_shaped_and_ordered_by_misses_not_by_no_stream():
    pool = _Pool(_LEAK, _PROBE)
    out = asyncio.run(mariadb.load_video_leakage(pool, hours=1, limit=40))
    hosts = [h["host"] for h in out["hosts"]]
    # gallery.tld has 300 jobs and nothing seen -> last, not first.
    assert hosts == ["example.com", "gated.tld", "gallery.tld", "cdnvideo.tld"]
    ex = out["hosts"][0]
    assert (ex["jobs"], ex["with_video"], ex["missed"], ex["no_stream"]) == (10, 2, 6, 2)
    assert ex["miss_rate"] == 0.75          # 6 / (2 + 6), no_stream excluded
    assert ex["probe"] == {"decouplable": 5}
    assert ex["probe_verdict"] == "decouplable"
    g = out["hosts"][1]
    # www. on the probe side is folded onto the job-side host.
    assert g["probe"] == {"gated": 30, "tokened": 10}
    assert g["probe_verdict"] == "gated"
    assert g["probe_total"] == 40
    assert g["miss_rate"] == 1.0
    gal = out["hosts"][2]
    assert gal["miss_rate"] == 0.0 and gal["no_stream"] == 300
    assert out["totals"] == {
        "jobs": 320, "with_video": 6, "missed": 8, "no_stream": 306,
        "miss_rate": round(8 / 14, 3),
    }
    assert out["host_count"] == 4
    assert out["hours"] == 1


def test_limit_cuts_the_list_but_not_the_totals():
    out = asyncio.run(mariadb.load_video_leakage(_Pool(_LEAK, _PROBE), hours=1, limit=1))
    assert len(out["hosts"]) == 1
    assert out["host_count"] == 4
    assert out["totals"]["jobs"] == 320


def test_sql_uses_the_indexed_pair_and_the_right_predicates():
    pool = _Pool([], [])
    asyncio.run(mariadb.load_video_leakage(pool, hours=6, limit=10))
    sql, params = pool.log[0]
    assert sql.startswith("SET STATEMENT max_statement_time=")
    assert "j.status = 'completed'" in sql
    assert "j.created_at >= (UTC_TIMESTAMP(3) - INTERVAL %s HOUR)" in sql
    assert "completed_at" not in sql
    # The trap: JSON_VALUE gives '1'/'0' for a JSON boolean on MariaDB.
    assert "JSON_VALUE" not in sql
    assert "JSON_EXTRACT(j.options, '$.download_video') = 'true'" in sql
    assert "j.options LIKE '%%\"download_video\": true%%'" in sql
    # has_video is a text LIKE, not JSON_SEARCH (cost on 21 k rows/hour).
    assert "JSON_SEARCH" not in sql
    assert "r.assets LIKE '%%\"mime\": \"video/%%'" in sql
    # "seen" is video_urls_seen, the list the deferred targets are picked from.
    assert "r.video_urls_seen NOT IN ('[]', 'null')" in sql
    assert "SUM(NOT has_video AND seen)" in sql
    assert params == (6,)


def test_hours_and_limit_are_clamped():
    pool = _Pool([], [])
    out = asyncio.run(mariadb.load_video_leakage(pool, hours=0, limit=10**6))
    assert out["hours"] == 1
    assert pool.log[0][1] == (1,)
    out = asyncio.run(mariadb.load_video_leakage(_Pool([], []), hours=999, limit=0))
    assert out["hours"] == mariadb.VIDEO_LEAK_MAX_HOURS


def _fake_loader(calls):
    async def _fake(pool, hours=1, limit=40):
        calls.append((hours, limit))
        return {"hours": hours, "hosts": [], "host_count": 0,
                "totals": {"jobs": 0, "with_video": 0, "missed": 0,
                           "no_stream": 0, "miss_rate": 0.0}}
    return _fake


def test_route_without_redis_computes_inline_and_caches(monkeypatch):
    from server.hub._state import state

    calls = []
    monkeypatch.setattr(mariadb, "load_video_leakage", _fake_loader(calls))
    monkeypatch.setattr(state, "mariadb_pool", object(), raising=False)
    monkeypatch.setattr(state, "store", None, raising=False)
    videostats._cache.clear()

    a = asyncio.run(videostats.get_video_leakage(hours=1, limit=40, refresh=0))
    b = asyncio.run(videostats.get_video_leakage(hours=1, limit=40, refresh=0))
    c = asyncio.run(videostats.get_video_leakage(hours=6, limit=40, refresh=0))
    d = asyncio.run(videostats.get_video_leakage(hours=1, limit=40, refresh=1))
    assert a["cached"] is False and "query_ms" in a
    assert b["cached"] is True
    assert c["cached"] is False
    assert d["cached"] is False
    assert calls == [(1, 40), (6, 40), (1, 40)]


class _FakeRedis:
    """set(nx=True) / get / delete -- what the route uses."""

    def __init__(self):
        self.kv = {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True

    async def delete(self, k):
        self.kv.pop(k, None)


class _Store:
    def __init__(self, r):
        self._r = r


def test_route_with_redis_answers_202_and_computes_once_in_background(monkeypatch):
    """The front cuts long responses at ~30 s and round-robins across ~20 hubs:
    the scan must run off the request path, once, and land in shared Redis."""
    from server.hub._state import state

    calls = []
    monkeypatch.setattr(mariadb, "load_video_leakage", _fake_loader(calls))
    monkeypatch.setattr(state, "mariadb_pool", object(), raising=False)
    r = _FakeRedis()
    monkeypatch.setattr(state, "store", _Store(r), raising=False)
    videostats._cache.clear()

    async def scenario():
        first = await videostats.get_video_leakage(hours=1, limit=40, refresh=0)
        # A second hub (cold L1) asks while the scan is running: also 202,
        # and it must NOT start a second scan (lock held).
        videostats._cache.clear()
        second = await videostats.get_video_leakage(hours=1, limit=40, refresh=0)
        await asyncio.gather(*list(videostats._tasks))
        videostats._cache.clear()           # a third hub, reading Redis only
        third = await videostats.get_video_leakage(hours=1, limit=40, refresh=0)
        # refresh drops the stored result and starts a new scan.
        fourth = await videostats.get_video_leakage(hours=1, limit=40, refresh=1)
        await asyncio.gather(*list(videostats._tasks))
        fifth = await videostats.get_video_leakage(hours=1, limit=40, refresh=0)
        return first, second, third, fourth, fifth

    first, second, third, fourth, fifth = asyncio.run(scenario())
    assert first.status_code == 202 and second.status_code == 202
    assert first.headers["retry-after"] == "5"
    assert third["cached"] is True and third["hours"] == 1
    assert fourth.status_code == 202
    assert fifth["cached"] is True
    assert calls == [(1, 40), (1, 40)]
    # The lock is released after each scan.
    assert not any(k.startswith("paprika:video_leak:lock:") for k in r.kv)


def test_query_failure_becomes_a_payload_not_a_hang(monkeypatch):
    from server.hub._state import state

    async def _boom(pool, hours=1, limit=40):
        raise RuntimeError("table gone")

    monkeypatch.setattr(mariadb, "load_video_leakage", _boom)
    monkeypatch.setattr(state, "mariadb_pool", object(), raising=False)
    monkeypatch.setattr(state, "store", None, raising=False)
    videostats._cache.clear()
    out = asyncio.run(videostats.get_video_leakage(hours=1, limit=40, refresh=0))
    assert out["error"].startswith("query failed: RuntimeError")
    assert out["hosts"] == []


def test_route_503s_without_mariadb(monkeypatch):
    from fastapi import HTTPException
    from server.hub._state import state

    monkeypatch.setattr(state, "mariadb_pool", None, raising=False)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(videostats.get_video_leakage(hours=1, limit=40, refresh=0))
    assert ei.value.status_code == 503
