"""A cookie-only save does not make the hub rewrite the host's file.

Where MariaDB holds the host records, the files under ``hosts/`` are a cache
of it. Yet every cookie save -- made on this hub or replayed from a peer --
rewrote the host's whole file (cookie jar + recipes), fsync-ed. Measured
2026-11-18 on one hub: 139 rewrites a minute, ~100 of them replays, which is
most of the SSD writes the hubs had left.

Pinned here:
  * off (no MariaDB behind the files): every save goes to the file, as before
  * on: a save that changes only cookies / timestamps stays in memory, the hub
    answers from it, and the file is written at most once per interval
  * on: anything else -- a recipe, notes, a login setup, a new host -- is
    written at once, so the file never trails in what cannot be re-harvested
  * the MariaDB mirror wins over a waiting record it has overtaken
"""

import json

import pytest

from server.hub import _host_cache, _invalidate, hosts as hosts_mod, mariadb
from server.hub.hosts import HostRegistry

HOST = "example.com"


def _c(name, value="v"):
    return {"name": name, "value": value, "domain": ".example.com", "path": "/"}


def _recipe(pattern="/watch/*"):
    return {"pattern": pattern, "actions": [{"op": "click", "selector": "#play"}]}


@pytest.fixture()
def writes(monkeypatch):
    """Every file the registry writes, as (file name, fsync-ed?)."""
    seen: list = []
    original = hosts_mod.atomic_write_json

    def _spy(path, data, **kw):
        seen.append((path.name, kw.get("durable", True)))
        return original(path, data, **kw)

    monkeypatch.setattr(hosts_mod, "atomic_write_json", _spy)
    return seen


@pytest.fixture()
def clock(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(hosts_mod.time, "monotonic", lambda: t["now"])
    return t


@pytest.fixture()
def reg(tmp_path):
    return HostRegistry(tmp_path)


@pytest.fixture()
def on(reg, writes):
    """Registry with one host on disk and write-behind on (1 h)."""
    reg.upsert(HOST, [_c("sid", "old")], fetch_recipes=[_recipe()])
    reg.set_write_behind(3600)
    writes.clear()
    return reg


def _on_disk(reg, host=HOST):
    return json.loads((reg.dir / f"{host}.json").read_text(encoding="utf-8"))


# --- off: nothing changes --------------------------------------------------------


def test_off_by_default_every_save_reaches_the_file(reg, writes):
    reg.upsert(HOST, [_c("sid", "a")])
    reg.upsert(HOST, [_c("sid", "b")])
    reg.touch_used(HOST)

    assert len(writes) == 3
    assert _on_disk(reg)["cookies"][0]["value"] == "b"
    assert reg.pending_count() == 0


# --- on: cookies and timestamps wait --------------------------------------------


def test_a_cookie_only_save_is_answered_from_memory_not_written(on, writes):
    on.upsert(HOST, [_c("sid", "new"), _c("cf_clearance", "x")])

    assert writes == []
    assert [c["value"] for c in on.get(HOST).cookies] == ["new", "x"]
    assert _on_disk(on)["cookies"][0]["value"] == "old"
    assert on.pending_count() == 1


def test_the_recipes_survive_a_cookie_only_save(on):
    on.upsert(HOST, [_c("sid", "new")])

    assert len(on.get(HOST).fetch_recipes) == 1
    assert on.get(HOST).pick_recipe("https://example.com/watch/1") is not None


def test_usage_and_login_timestamps_wait_too(on, writes):
    on.touch_used(HOST)
    on.touch_login(HOST)

    assert writes == []
    rec = on.get(HOST)
    assert rec.last_used_at and rec.last_login_at


def test_the_listing_shows_the_waiting_copy(on):
    on.upsert("other.example.com", [_c("a")])  # new host: on disk at once
    on.upsert(HOST, [_c("sid", "new")])

    by_host = {r.host: r for r in on.list_all()}
    assert set(by_host) == {HOST, "other.example.com"}
    assert by_host[HOST].cookies[0]["value"] == "new"


def test_many_saves_of_one_host_cost_one_write_per_interval(on, writes, clock):
    for i in range(50):
        clock["now"] += 30  # 25 minutes of saves
        on.upsert(HOST, [_c("sid", f"v{i}")])
        on.flush(only_due=True)
    assert writes == []

    clock["now"] = 1000.0 + 30 + 3600  # one interval after the first waiting save
    assert on.flush(only_due=True) == 1
    assert writes == [(f"{HOST}.json", False)]  # written without fsync
    assert _on_disk(on)["cookies"][0]["value"] == "v49"
    assert on.pending_count() == 0


def test_a_restarted_hub_does_not_rewrite_on_its_first_save(on, writes, tmp_path):
    fresh = HostRegistry(tmp_path)  # same files, nothing remembered
    fresh.set_write_behind(3600)

    fresh.upsert(HOST, [_c("sid", "after-restart")])

    assert writes == []
    assert fresh.get(HOST).cookies[0]["value"] == "after-restart"


# --- on: everything else is written at once -------------------------------------


def test_a_new_host_is_written_at_once(on, writes):
    on.upsert("new.example.com", [_c("a")])

    assert writes == [("new.example.com.json", True)]


def test_a_new_recipe_is_written_at_once_with_the_newest_cookies(on, writes):
    on.upsert(HOST, [_c("sid", "new")])  # waiting
    on.append_recipe(HOST, _recipe("/embed/*"))

    assert writes == [(f"{HOST}.json", True)]
    disk = _on_disk(on)
    assert len(disk["fetch_recipes"]) == 2
    assert disk["cookies"][0]["value"] == "new"
    assert on.pending_count() == 0


@pytest.mark.parametrize(
    "change",
    [
        {"notes": "operator memo"},
        {"excluded": True},
        {"download_video": True},
        {"popup_policy": "follow"},
        {"recrawl_patterns": ["https://example.com/index*"]},
        {"login_url": "https://example.com/login", "login_goal": "log in"},
        {"fetch_recipes": []},
        {"owner_id": "tenant-a", "shared": False},
    ],
)
def test_any_other_field_is_written_at_once(on, writes, change):
    on.upsert(HOST, [_c("sid", "old")], **change)

    assert len(writes) == 1


def test_deleting_a_host_drops_its_waiting_copy(on):
    on.upsert(HOST, [_c("sid", "new")])

    assert on.delete(HOST) is True
    assert on.get(HOST) is None
    assert on.flush() == 0
    assert not (on.dir / f"{HOST}.json").exists()


# --- switching it off, shutting down --------------------------------------------


def test_switching_off_writes_out_what_was_waiting(on, writes):
    on.upsert(HOST, [_c("sid", "new")])

    assert on.set_write_behind(0) == 1
    assert _on_disk(on)["cookies"][0]["value"] == "new"

    writes.clear()
    on.upsert(HOST, [_c("sid", "newer")])
    assert len(writes) == 1


def test_shutdown_writes_out_everything(on, monkeypatch):
    on.upsert(HOST, [_c("sid", "new")])
    from server.hub._state import state

    monkeypatch.setattr(state, "hosts", on)
    assert _host_cache.flush_on_shutdown() == 1
    assert _on_disk(on)["cookies"][0]["value"] == "new"


# --- when it is on ---------------------------------------------------------------


def test_it_is_only_on_while_mariadb_holds_the_records(monkeypatch):
    from server.hub._state import state

    class _Settings:
        def __init__(self, v):
            self.v = v

        def get(self, key, default=None):
            return self.v if key == "host_cache_flush_s" else default

    monkeypatch.setattr(state, "settings", _Settings(3600))
    monkeypatch.setattr(state, "mariadb_pool", None)
    assert _host_cache.wanted_interval_s() == 0

    monkeypatch.setattr(state, "mariadb_pool", object())
    assert _host_cache.wanted_interval_s() == 3600

    monkeypatch.setattr(state, "settings", _Settings(0))  # operator's off switch
    assert _host_cache.wanted_interval_s() == 0


def test_the_setting_exists_and_defaults_to_an_hour():
    from server.hub.settings import _SCHEMA

    assert _SCHEMA["host_cache_flush_s"] == (3600, "int")


async def test_the_tick_follows_the_setting_and_flushes_what_is_due(
    on, writes, clock, monkeypatch
):
    from server.hub._state import state

    monkeypatch.setattr(state, "hosts", on)
    monkeypatch.setattr(_host_cache, "wanted_interval_s", lambda: 3600.0)
    on.upsert(HOST, [_c("sid", "new")])

    await _host_cache.tick()
    assert writes == []

    clock["now"] += 3601
    await _host_cache.tick()
    assert len(writes) == 1

    on.upsert(HOST, [_c("sid", "newer")])
    monkeypatch.setattr(_host_cache, "wanted_interval_s", lambda: 0.0)
    await _host_cache.tick()  # MariaDB went away / operator switched it off
    assert on.write_behind_s == 0
    assert _on_disk(on)["cookies"][0]["value"] == "newer"


# --- saves replayed from a peer hub ---------------------------------------------


def _peer_event(reg, **change):
    rec = reg.get(HOST)
    data = rec.to_json()
    data.update(change)
    return {"origin": "hub-b", "kind": "hosts", "action": "upsert",
            "key": HOST, "record": data}


def test_a_peers_cookie_save_is_not_written(on, writes, monkeypatch):
    from server.hub._state import state

    monkeypatch.setattr(state, "hosts", on)
    _invalidate._apply_event(
        _peer_event(on, cookies=[_c("sid", "from-peer")],
                    updated_at="2026-11-18T01:00:00Z")
    )

    assert writes == []
    assert on.get(HOST).cookies[0]["value"] == "from-peer"


def test_a_peers_recipe_change_is_written_at_once(on, writes, monkeypatch):
    from server.hub._state import state

    monkeypatch.setattr(state, "hosts", on)
    _invalidate._apply_event(
        _peer_event(on, fetch_recipes=[_recipe(), _recipe("/embed/*")])
    )

    assert len(writes) == 1
    assert len(_on_disk(on)["fetch_recipes"]) == 2


# --- the MariaDB mirror ----------------------------------------------------------


def _row(host, *, cookies=None, recipes=None, updated="2026-11-18 02:00:00"):
    return (
        host, json.dumps(cookies) if cookies is not None else None, None, None,
        "kill", None, None, None, 900, None,
        json.dumps(recipes) if recipes is not None else None,
        "2026-01-01 00:00:00", updated, None, "default", 1, 0, 0,
    )


def test_the_mirror_starts_from_what_the_hub_holds(on):
    on.upsert(HOST, [_c("sid", "new")])  # waiting
    row = _row(HOST, cookies=[_c("sid", "from-db")], recipes=[_recipe()])

    mariadb._restore_hosts_to_disk([row], on)

    assert on.pending_count() == 0
    assert on.get(HOST).cookies[0]["value"] == "from-db"  # MariaDB wins, as before


def test_a_save_made_mid_mirror_cannot_hide_a_recipe_the_mirror_brought(on):
    # The mirror brings a recipe this hub never saw ...
    row = _row(HOST, cookies=[_c("sid", "old")],
               recipes=[_recipe(), _recipe("/embed/*")])
    real_resync = on.resync_with_disk

    def _save_arrives_first():
        # ... while a cookie save, built from the pre-mirror record, lands
        # after the mirror's own flush and before it finishes.
        stale = HostRegistry(on.dir.parent)._from_json(
            {**_on_disk(on), "fetch_recipes": [_recipe()],
             "cookies": [_c("sid", "mid-mirror")]}
        )
        on._wb_pending[HOST] = (json.dumps(stale.to_json()), 0.0)
        real_resync()

    on.resync_with_disk = _save_arrives_first
    mariadb._restore_hosts_to_disk([row], on)

    assert on.pending_count() == 0
    assert len(on.get(HOST).fetch_recipes) == 2
    on.upsert(HOST, [_c("sid", "next")])  # the next save keeps the recipe
    assert len(on.get(HOST).fetch_recipes) == 2


def test_a_host_the_mirror_removed_is_not_brought_back(on):
    on.upsert(HOST, [_c("sid", "new")])
    real_resync = on.resync_with_disk

    def _save_arrives_first():
        on._wb_pending[HOST] = (json.dumps({"host": HOST, "cookies": []}), 0.0)
        real_resync()

    on.resync_with_disk = _save_arrives_first
    mariadb._restore_hosts_to_disk([_row("kept.example.com")], on)

    assert on.get(HOST) is None
    assert on.flush() == 0
    assert not (on.dir / f"{HOST}.json").exists()
