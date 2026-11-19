"""A finished job does not make the hub rewrite the host's knowledge file.

Every job outcome bumped the counters in ``host_knowledge/{host}.json`` by
rewriting the whole file with an fsync, and appended a line to a
``history.jsonl`` that nothing reads (over 1 GB on one hub). After the host
records stopped being rewritten per cookie save, this was most of what a hub
still wrote to its disk: 37 rewrites a minute on the hub measured.

Pinned here:
  * off (the default until the hub's loop sets an interval): every outcome is
    written at once, as before
  * on: outcomes are counted in memory and added to the file at most once per
    host per interval -- and the file ends up exactly as if they had been
    applied one by one
  * a host's first outcome is written at once, so its file exists
  * no per-outcome history line
  * the confidence tier is computed for hosts whose per_page section predates
    the schema (it used to fail validation and never refresh)
  * what another writer put in the file meanwhile is kept
"""

import json
import logging
import threading
from datetime import datetime, timedelta

import pytest

from server.hub import _host_cache, distiller_light as dl

HOST = "example.com"


@pytest.fixture(autouse=True)
def _clean():
    dl._held.clear()
    dl._interval_s = 0.0
    yield
    dl._held.clear()
    dl._interval_s = 0.0


@pytest.fixture()
def writes(monkeypatch):
    """Every knowledge file written, as (file name, fsync-ed?)."""
    seen: list = []
    original = dl._atomic_write

    def _spy(path, content, **kw):
        seen.append((path.name, kw.get("durable", True)))
        return original(path, content, **kw)

    monkeypatch.setattr(dl, "_atomic_write", _spy)
    return seen


@pytest.fixture()
def clock(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(dl.time, "monotonic", lambda: t["now"])
    return t


@pytest.fixture()
def wall(monkeypatch):
    """Job completion times: one minute apart, starting now."""
    t = {"now": datetime.utcnow().replace(microsecond=0)}

    class _Datetime:
        @staticmethod
        def utcnow():
            t["now"] += timedelta(minutes=1)
            return t["now"]

    monkeypatch.setattr(dl, "datetime", _Datetime)
    return t


def _record(d, success=True, reason="", host=HOST):
    return dl.record_job_outcome(
        host=host, success=success, job_id="j1", reason=reason, data_dir=d,
    )


def _file(d, host=HOST):
    return d / "host_knowledge" / f"{host}.json"


def _on_disk(d, host=HOST):
    return json.loads(_file(d, host).read_text("utf-8"))


@pytest.fixture()
def known(tmp_path, writes):
    """A host with one job on file, counting in memory switched on (1 h)."""
    _record(tmp_path)
    dl.set_write_interval(3600)
    writes.clear()
    return tmp_path


# --- off: as before ---------------------------------------------------------------


def test_until_an_interval_is_set_every_outcome_is_written(tmp_path, writes):
    first = _record(tmp_path)
    second = _record(tmp_path, success=False, reason="timeout")

    assert writes == [(f"{HOST}.json", True), (f"{HOST}.json", True)]
    assert first["stats"]["total_jobs"] == 1
    assert second["stats"]["total_jobs"] == 2
    s = _on_disk(tmp_path)["stats"]
    assert (s["total_jobs"], s["successful_jobs"], s["success_rate"]) == (2, 1, 0.5)
    assert s["last_failure_reason"] == "timeout"


# --- on ---------------------------------------------------------------------------


def test_an_outcome_for_a_known_host_is_not_written(known, writes):
    assert _record(known) is None

    assert writes == []
    assert dl.pending_count() == 1
    assert _on_disk(known)["stats"]["total_jobs"] == 1


def test_a_hosts_first_outcome_is_written_at_once(known, writes):
    d = _record(known, host="new.example.com")

    assert writes == [("new.example.com.json", True)]
    assert d["stats"]["total_jobs"] == 1
    assert dl.pending_count() == 0


def test_the_file_is_written_once_per_interval(known, writes, clock):
    for _ in range(50):
        _record(known)
    assert dl.flush(only_due=True) == 0  # nothing has waited an hour yet
    assert writes == []

    clock["now"] += 3601
    assert dl.flush(only_due=True) == 1
    assert writes == [(f"{HOST}.json", True)]
    assert _on_disk(known)["stats"]["total_jobs"] == 51
    assert dl.pending_count() == 0

    _record(known)                       # the next hour starts with this one
    clock["now"] += 1800
    assert dl.flush(only_due=True) == 0
    clock["now"] += 1801
    assert dl.flush(only_due=True) == 1
    assert _on_disk(known)["stats"]["total_jobs"] == 52


OUTCOMES = [
    (True, ""), (False, "timeout"), (True, ""), (False, ""), (True, ""),
    (False, "x" * 400), (False, ""), (True, ""), (True, ""), (True, ""),
]


def test_counting_in_memory_gives_the_same_file_as_one_by_one(tmp_path, wall):
    one_by_one, counted = tmp_path / "a", tmp_path / "b"
    start = wall["now"]

    for ok, why in OUTCOMES:
        _record(one_by_one, ok, why)

    wall["now"] = start
    _record(counted, *OUTCOMES[0])       # first outcome: written at once
    dl.set_write_interval(3600)
    for ok, why in OUTCOMES[1:]:
        assert _record(counted, ok, why) is None
    assert dl.flush() == 1

    a, b = _on_disk(one_by_one), _on_disk(counted)
    a.pop("created_at"), b.pop("created_at")     # when the skeleton was made
    assert a == b
    assert a["stats"]["total_jobs"] == 10
    assert a["stats"]["last_failure_reason"] == "x" * 300


def test_no_history_line_is_written_per_outcome(known):
    for _ in range(3):
        _record(known)
    dl.flush()
    assert not (known / "host_knowledge" / HOST).exists()


def test_switching_it_off_writes_what_is_waiting(known, writes):
    _record(known)
    _record(known, host="other.example.com")     # new host: written at once
    _record(known, host="other.example.com")
    writes.clear()

    assert dl.set_write_interval(0) == 2
    assert dl.pending_count() == 0
    assert _on_disk(known)["stats"]["total_jobs"] == 2
    assert _on_disk(known, "other.example.com")["stats"]["total_jobs"] == 2

    _record(known)                               # and back to writing each one
    assert writes[-1] == (f"{HOST}.json", True)


def test_counts_survive_a_write_that_fails(known, clock, monkeypatch):
    _record(known)
    _record(known, success=False, reason="blocked")
    original = dl._atomic_write

    def _full(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(dl, "_atomic_write", _full)
    clock["now"] += 3601
    assert dl.flush(only_due=True) == 0
    assert dl.pending_count() == 1

    monkeypatch.setattr(dl, "_atomic_write", original)
    _record(known)                               # arrives while it is retried later
    assert dl.flush(only_due=True) == 0          # not on every tick ...
    clock["now"] += 3601
    assert dl.flush(only_due=True) == 1          # ... after another interval
    s = _on_disk(known)["stats"]
    assert (s["total_jobs"], s["successful_jobs"]) == (4, 3)
    assert s["last_failure_reason"] == "blocked"


def test_what_another_writer_put_in_the_file_is_kept(known):
    _record(known)
    _record(known)
    # the reasoning distiller rewrites the file while outcomes are waiting
    d = _on_disk(known)
    d["per_page"]["barriers"] = {"age_gate": {"present": True}}
    _file(known).write_text(json.dumps(d), "utf-8")

    dl.flush()
    after = _on_disk(known)
    assert after["per_page"]["barriers"] == {"age_gate": {"present": True}}
    assert after["stats"]["total_jobs"] == 3


def test_outcomes_from_many_threads_are_all_counted(known):
    def _many():
        for _ in range(100):
            _record(known)

    threads = [threading.Thread(target=_many) for _ in range(8)]
    for t in threads:
        t.start()
    dl.flush()                                   # a flush in the middle of it
    for t in threads:
        t.join()
    dl.flush()
    assert _on_disk(known)["stats"]["total_jobs"] == 801


def test_written_at_once_from_many_threads_loses_nothing(tmp_path):
    def _many():
        for _ in range(25):
            _record(tmp_path)

    threads = [threading.Thread(target=_many) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _on_disk(tmp_path)["stats"]["total_jobs"] == 200


# --- the confidence tier ----------------------------------------------------------


def test_the_tier_is_computed_for_a_host_with_an_old_style_per_page(tmp_path, caplog):
    recent = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    old_style = {
        "host": HOST,
        "per_page": {
            # shapes the current schema rejects; written by an earlier distiller
            "navigation_hints": {"common_observations": [{"note": "lazy images"}]},
            "content_extraction": [{"page_kind": 3, "strategy": {"kind": ["x"]}}],
        },
        "stats": {
            "total_jobs": 30, "successful_jobs": 30, "success_rate": 1.0,
            "last_success_at": recent, "overall_confidence": "low",
        },
    }
    _file(tmp_path).parent.mkdir(parents=True)
    _file(tmp_path).write_text(json.dumps(old_style), "utf-8")

    with caplog.at_level(logging.INFO, logger=dl.__name__):
        _record(tmp_path)

    after = _on_disk(tmp_path)
    assert after["stats"]["overall_confidence"] == "high"
    assert after["per_page"] == old_style["per_page"]        # untouched
    assert "maturity evaluation failed" not in caplog.text


def test_a_file_that_is_not_json_any_more_starts_over(tmp_path):
    _file(tmp_path).parent.mkdir(parents=True)
    _file(tmp_path).write_bytes(b"\x00\xff\x00 not json")

    _record(tmp_path)
    after = _on_disk(tmp_path)
    assert after["host"] == HOST
    assert after["stats"]["total_jobs"] == 1


# --- the hub's loop and shutdown --------------------------------------------------


class _Settings:
    def __init__(self, v):
        self.v = v

    def get(self, key, default=None):
        return self.v if key == "host_stats_flush_s" else default


def test_the_setting_exists_and_defaults_to_an_hour():
    from server.hub.settings import _SCHEMA

    assert _SCHEMA["host_stats_flush_s"] == (3600, "int")


def test_it_does_not_depend_on_mariadb(monkeypatch):
    from server.hub._state import state

    monkeypatch.setattr(state, "mariadb_pool", None)
    monkeypatch.setattr(state, "settings", _Settings(3600))
    assert _host_cache.wanted_stats_interval_s() == 3600

    monkeypatch.setattr(state, "settings", _Settings(0))     # operator's off switch
    assert _host_cache.wanted_stats_interval_s() == 0

    monkeypatch.setattr(state, "settings", _Settings("soon"))
    assert _host_cache.wanted_stats_interval_s() == 3600


async def test_the_tick_follows_the_setting_and_writes_what_is_due(
    tmp_path, writes, clock, monkeypatch
):
    from server.hub._state import state

    monkeypatch.setattr(state, "hosts", None)
    monkeypatch.setattr(state, "settings", _Settings(3600))
    _record(tmp_path)                            # host on file
    writes.clear()

    await _host_cache.tick()
    assert dl.write_interval_s() == 3600
    _record(tmp_path)
    await _host_cache.tick()
    assert writes == []

    clock["now"] += 3601
    await _host_cache.tick()
    assert len(writes) == 1

    _record(tmp_path)
    monkeypatch.setattr(state, "settings", _Settings(0))
    await _host_cache.tick()
    assert dl.write_interval_s() == 0
    assert _on_disk(tmp_path)["stats"]["total_jobs"] == 3


def test_shutdown_writes_what_is_waiting_without_fsync(known, writes, monkeypatch):
    from server.hub._state import state

    monkeypatch.setattr(state, "hosts", None)
    _record(known)

    assert _host_cache.flush_on_shutdown() == 0          # counts host records only
    assert writes == [(f"{HOST}.json", False)]
    assert _on_disk(known)["stats"]["total_jobs"] == 2


def test_shutdown_stops_writing_when_it_takes_too_long(known, clock, monkeypatch):
    for h in ("a.example.com", "b.example.com", "c.example.com"):
        _record(known, host=h)                   # first: written at once
        _record(known, host=h)                   # second: waits
    original = dl._apply

    def _slow(t, **kw):
        clock["now"] += 2.0
        return original(t, **kw)

    monkeypatch.setattr(dl, "_apply", _slow)
    assert dl.flush(durable=False, budget_s=3.0) == 2
    assert dl.pending_count() == 1
