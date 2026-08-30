"""Regression: hub startup must not re-write / re-push the whole registry.

At prod scale (8,133 hosts, ~66k Redis job records) the MariaDB bootstrap in
``app.lifespan`` cost every hub 4-7 minutes with /health unanswered:

  * ``restore_hosts`` routed each row through ``HostRegistry.upsert``, which
    re-stamps ``updated_at`` -- so all 8k files were fsync-rewritten on every
    start, synchronously on the event loop.
  * ``auto_migrate_all`` then re-pushed the same rows (plus every job still in
    Redis) one INSERT IGNORE at a time. aiomysql answers each duplicate-key
    warning with an extra ``SHOW WARNINGS`` round-trip, so ~74k collisions
    became ~148k serial round-trips against .20.

Pinned here:
  * a restore whose rows already match the disk mirror writes nothing
  * MariaDB's timestamps land on disk (that is what makes the mirror stable)
  * rows dropped from MariaDB still get deleted from disk
  * migrate_* asks which keys exist first and only inserts the rest
"""

from datetime import datetime

import pytest

from server.hub import mariadb
from server.hub.hosts import HostRegistry


def _row(host, *, updated="2026-08-01 10:00:00", notes=None, recipes=None):
    return (
        host, None, notes, None, "kill", None, None, None, 900, None,
        recipes, "2026-01-01 00:00:00", updated, None, "default", 1, 0, 0,
    )


@pytest.fixture()
def registry(tmp_path):
    return HostRegistry(tmp_path)


@pytest.fixture()
def spy_writes(monkeypatch):
    """Record every atomic_write_json the restore performs (still writing)."""
    import server.hub._jsonstore as js

    seen: list = []
    original = js.atomic_write_json

    def _spy(path, data, **kw):
        seen.append(path)
        return original(path, data, **kw)

    monkeypatch.setattr(js, "atomic_write_json", _spy)
    return seen


def test_restore_is_a_noop_when_disk_already_matches(registry, spy_writes):
    rows = [_row(f"h{i}.example.com") for i in range(20)]

    assert mariadb._restore_hosts_to_disk(rows, registry) == 20
    assert len(spy_writes) == 20
    spy_writes.clear()

    assert mariadb._restore_hosts_to_disk(rows, registry) == 20
    assert spy_writes == [], f"second restore rewrote {len(spy_writes)} files"


def test_restore_mirrors_mariadb_timestamps(registry):
    mariadb._restore_hosts_to_disk([_row("a.example.com")], registry)
    rec = registry.get("a.example.com")
    assert rec.updated_at.startswith("2026-08-01T10:00:00")
    assert rec.created_at.startswith("2026-01-01T00:00:00")


def test_restore_rewrites_changed_rows_only(registry, spy_writes):
    mariadb._restore_hosts_to_disk(
        [_row("a.example.com"), _row("b.example.com")], registry
    )
    spy_writes.clear()

    mariadb._restore_hosts_to_disk(
        [_row("a.example.com"), _row("b.example.com", notes="edited")], registry
    )
    assert len(spy_writes) == 1
    assert registry.get("b.example.com").notes == "edited"


def test_restore_deletes_hosts_dropped_from_mariadb(registry):
    mariadb._restore_hosts_to_disk(
        [_row("a.example.com"), _row("gone.example.com")], registry
    )
    assert registry.get("gone.example.com") is not None

    mariadb._restore_hosts_to_disk([_row("a.example.com")], registry)
    assert registry.get("gone.example.com") is None
    assert registry.get("a.example.com") is not None


def test_restore_accepts_datetime_columns(registry):
    row = list(_row("a.example.com"))
    row[12] = datetime(2026, 8, 1, 10, 0, 0)
    mariadb._restore_hosts_to_disk([tuple(row)], registry)
    assert registry.get("a.example.com").updated_at == "2026-08-01T10:00:00Z"


# ---------------------------------------------------------------------------
# migrate_* prefilter
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, present, log):
        self._present = present
        self._log = log
        self._rows = []
        self.rowcount = 0

    async def execute(self, sql, args=None):
        self._log.append(sql)
        if sql.lstrip().upper().startswith("SELECT"):
            self._rows = [(k,) for k in args if k in self._present]
            self.rowcount = len(self._rows)
        else:
            self.rowcount = 1
        return self.rowcount

    async def fetchall(self):
        return self._rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, present, log):
        self._present, self._log = present, log

    def cursor(self):
        return _FakeCursor(self._present, self._log)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, present):
        self.present = set(present)
        self.sql = []

    def acquire(self):
        return _FakeConn(self.present, self.sql)


@pytest.mark.asyncio
async def test_existing_keys_chunks_the_lookup():
    pool = _FakePool({"k3", "k7"})
    found = await mariadb._existing_keys(
        pool, "jobs", "job_id", [f"k{i}" for i in range(10)], chunk=4
    )
    assert found == {"k3", "k7"}
    assert len(pool.sql) == 3


@pytest.mark.asyncio
async def test_migrate_hosts_skips_rows_already_in_mariadb(registry):
    registry.upsert(host="a.example.com", cookies=[])
    registry.upsert(host="b.example.com", cookies=[])
    pool = _FakePool({"a.example.com"})

    result = await mariadb.migrate_hosts(registry, pool, purge=False)

    assert result["skipped"] == 1
    assert result["migrated"] == 1
    inserts = [s for s in pool.sql if "INSERT IGNORE INTO hosts" in s]
    assert len(inserts) == 1
