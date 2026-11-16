"""A job does not re-send a host's cookies the hub already has.

Each save (``PUT /hosts/{host}``) makes every hub rewrite that host's whole
record on its disk. Measured 2026-11-16: ~900 saves a minute fleet-wide, 895
host files rewritten a minute on each of 20 hubs, ~1.2 TB/day of SSD writes --
for ~200 different hosts a minute. So the same cookie set is saved at most
once per interval; a cookie that appears or disappears is saved at once.
"""
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from server.worker import cookie_save
from server.worker.agent import _mix_jobexec

NOW = datetime(2026, 11, 16, 8, 0, 0, tzinfo=timezone.utc)


def _c(name, value="v", domain="example.com", path="/", **kw):
    return {"name": name, "value": value, "domain": domain, "path": path, **kw}


def _rec(cookies, age_s, z=True):
    t = (NOW - timedelta(seconds=age_s)).replace(tzinfo=None).isoformat()
    return {"host": "example.com", "cookies": cookies, "updated_at": t + ("Z" if z else "")}


@pytest.fixture(autouse=True)
def _default_interval(monkeypatch):
    monkeypatch.delenv("PAPRIKA_COOKIE_SAVE_MIN_INTERVAL_S", raising=False)


def _age(existing, new):
    return cookie_save.redundant_age_s(existing, new, now=NOW)


# --- left out ---------------------------------------------------------------------


def test_the_same_cookies_saved_a_minute_ago_are_not_sent_again():
    jar = [_c("sid", "abc"), _c("lang", "ja")]
    assert _age(_rec(jar, 60), list(reversed(jar))) == pytest.approx(60)


def test_a_value_that_only_rotated_waits_for_the_interval():
    # 55% of consecutive saves: same cookies, a different value
    assert _age(_rec([_c("sid", "old")], 120), [_c("sid", "new")]) == pytest.approx(120)


def test_expiry_and_flags_do_not_make_it_a_different_cookie():
    # 37% of consecutive saves
    old = [_c("sid", expires=1794800000, httpOnly=True)]
    new = [_c("sid", expires=1794900000, httpOnly=False)]
    assert _age(_rec(old, 30), new) is not None


def test_a_leading_dot_and_case_in_the_domain_are_the_same_domain():
    assert _age(_rec([_c("sid", domain=".Example.com")], 30), [_c("sid", domain="example.com")]) is not None


def test_a_host_with_no_cookies_is_not_re_marked_every_job():
    # "marker created (0 cookies)" used to be rewritten by every job on that host
    assert _age(_rec([], 30), []) is not None


def test_the_hubs_timestamp_is_read_with_or_without_z():
    assert _age(_rec([_c("sid")], 45, z=False), [_c("sid")]) == pytest.approx(45)


# --- sent --------------------------------------------------------------------------


def test_a_new_cookie_is_saved_at_once():
    # a login, a challenge clearance
    assert _age(_rec([_c("lang")], 5), [_c("lang"), _c("cf_clearance")]) is None


def test_a_cookie_that_went_away_is_saved_at_once():
    assert _age(_rec([_c("lang"), _c("sid")], 5), [_c("lang")]) is None


def test_the_same_name_on_another_path_or_domain_is_a_different_cookie():
    assert _age(_rec([_c("sid", path="/")], 5), [_c("sid", path="/app")]) is None
    assert _age(_rec([_c("sid", domain="a.example.com")], 5), [_c("sid", domain="b.example.com")]) is None


def test_after_the_interval_the_cookies_are_saved_again():
    jar = [_c("sid")]
    assert _age(_rec(jar, 599), jar) is not None
    assert _age(_rec(jar, 600), jar) is None                       # default: 600 s
    assert _age(_rec(jar, 86400), jar) is None


def test_a_host_the_hub_does_not_know_is_saved():
    assert _age(None, [_c("sid")]) is None
    assert _age(None, []) is None                                   # the first "I visited this" marker


def test_a_record_without_a_usable_timestamp_is_saved():
    jar = [_c("sid")]
    for bad in (None, "", "yesterday", 1794800000):
        assert _age({"cookies": jar, "updated_at": bad}, jar) is None
    assert _age({"cookies": jar}, jar) is None


def test_a_timestamp_from_the_future_is_not_a_recent_save():
    jar = [_c("sid")]
    assert _age(_rec(jar, -120), jar) is None                       # clock skew


def test_the_interval_is_a_setting_and_zero_turns_it_off(monkeypatch):
    jar = [_c("sid")]
    monkeypatch.setenv("PAPRIKA_COOKIE_SAVE_MIN_INTERVAL_S", "60")
    assert _age(_rec(jar, 30), jar) is not None
    assert _age(_rec(jar, 90), jar) is None
    monkeypatch.setenv("PAPRIKA_COOKIE_SAVE_MIN_INTERVAL_S", "0")
    assert _age(_rec(jar, 1), jar) is None                          # every job saves, as before
    monkeypatch.setenv("PAPRIKA_COOKIE_SAVE_MIN_INTERVAL_S", "not a number")
    assert _age(_rec(jar, 30), jar) is not None                     # falls back to the default


def test_junk_in_the_jar_does_not_raise():
    assert _age(_rec([_c("sid"), "x", None], 30), [_c("sid"), 7]) is not None


# --- the job runner asks before it sends -------------------------------------------


def test_the_cookie_save_asks_before_the_put():
    src = inspect.getsource(_mix_jobexec)
    start = src.index("def _make_cookie_save_callback")
    body = src[start: src.index("return _save_cb", start)]
    ask = body.index("cookie_save.redundant_age_s(existing_rec, filtered or existing_cookies)")
    assert body.index("g = await cli.get(url)") < ask < body.index("await cli.put(")
    assert "cookie save skipped: the hub has the same cookies" in body


# --- end to end: what the job actually sends ---------------------------------------


class _Reply:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.text = ""

    def json(self):
        return self._body


class _Hub:
    """Stands in for the hub: one GET reply, and a list of what was PUT."""

    def __init__(self, existing):
        self.existing = existing
        self.puts = []

    def client(self, **_kw):
        hub = self

        class _Cli:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return _Reply(200, hub.existing) if hub.existing is not None else _Reply(404)

            async def put(self, url, json=None):
                hub.puts.append((url, json))
                return _Reply(200, {})

        return _Cli()


class _Assign:
    job_id = "0a1b2c3d4e5f"
    asset_upload_base = "http://hub:8000/jobs/0a1b2c3d4e5f/assets"


def _run_save(monkeypatch, existing, jar):
    import asyncio

    hub = _Hub(existing)
    monkeypatch.setattr(_mix_jobexec, "make_async_client", hub.client)
    lines = []
    cb = _mix_jobexec._JobExecMixin._make_cookie_save_callback(None, _Assign(), "www.Example.com", lines.append)
    asyncio.run(cb(jar))
    return hub.puts, lines


def _fresh(cookies, age_s):
    t = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).replace(tzinfo=None).isoformat()
    return {"host": "example.com", "cookies": cookies, "notes": "kept", "updated_at": t}


def test_a_job_sends_nothing_when_the_hub_was_just_given_the_same_cookies(monkeypatch):
    puts, lines = _run_save(monkeypatch, _fresh([_c("sid", "old")], 30), [_c("sid", "new"), _c("ad", domain="tracker.net")])
    assert puts == []
    assert any("cookie save skipped: the hub has the same cookies for example.com" in l for l in lines)


def test_a_job_sends_a_new_cookie_even_right_after_a_save(monkeypatch):
    puts, _ = _run_save(monkeypatch, _fresh([_c("sid")], 30), [_c("sid"), _c("cf_clearance")])
    assert len(puts) == 1
    url, body = puts[0]
    assert url == "http://hub:8000/hosts/example.com"
    assert [c["name"] for c in body["cookies"]] == ["sid", "cf_clearance"] and body["notes"] == "kept"


def test_a_job_sends_the_cookies_again_once_the_record_is_old(monkeypatch):
    puts, _ = _run_save(monkeypatch, _fresh([_c("sid", "old")], 3600), [_c("sid", "new")])
    assert len(puts) == 1 and puts[0][1]["cookies"][0]["value"] == "new"


def test_a_job_registers_a_host_the_hub_has_never_seen(monkeypatch):
    puts, _ = _run_save(monkeypatch, None, [])
    assert len(puts) == 1 and puts[0][1]["cookies"] == []


def test_a_visit_that_matched_no_cookie_does_not_rewrite_the_saved_ones(monkeypatch):
    # "refreshed timestamp only": the existing jar was PUT back by every job
    puts, _ = _run_save(monkeypatch, _fresh([_c("sid")], 30), [_c("ad", domain="tracker.net")])
    assert puts == []


def test_with_the_interval_off_every_job_saves_as_before(monkeypatch):
    monkeypatch.setenv("PAPRIKA_COOKIE_SAVE_MIN_INTERVAL_S", "0")
    puts, _ = _run_save(monkeypatch, _fresh([_c("sid")], 30), [_c("sid")])
    assert len(puts) == 1
