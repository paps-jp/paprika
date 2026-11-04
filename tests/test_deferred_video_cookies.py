"""The deferred (browser-less) video download gets the session's cookies.

Before 2026-11-04 ``_spawn_deferred_video_download`` ran yt-dlp with URL +
referer only: the lane's Chrome was already gone, and nothing had kept its
cookies. Cookie-gated streams (~32 % of probed hosts) therefore failed after
the handoff even though the browser had just played them.

Pinned here:
  * the end-of-fetch cookie jar is filtered to the hosts the download will
    contact (page / stream / referer), not dumped whole
  * the file is Netscape format yt-dlp accepts; a CDP session cookie
    (expires=-1) becomes 0, not a negative number
  * no match -> no file (yt-dlp runs unauthenticated, as before), and the
    hub host registry is tried as a fallback
  * a cookie problem never raises into the download
"""

import asyncio
from pathlib import Path

import pytest

from server.worker.agent import _mix_jobexec as m


_JAR = [
    {"name": "sess", "value": "abc", "domain": ".cdn.example.net", "path": "/",
     "secure": True, "expires": -1},
    {"name": "login", "value": "x y", "domain": "example.com", "path": "/",
     "secure": False, "expires": 1800000000.5},
    {"name": "tracker", "value": "t", "domain": ".doubleclick.net", "path": "/",
     "secure": True, "expires": 1800000000},
    {"name": "", "value": "no-name", "domain": "example.com"},
    "not-a-dict",
]
_TARGETS = [
    {"url": "https://v3.cdn.example.net/hls/master.m3u8",
     "referer": "https://www.example.com/watch/1", "label": "hls"},
]


def test_hosts_come_from_page_stream_and_referer_without_www():
    hosts = m._deferred_cookie_hosts(_TARGETS, "https://www.example.com/watch/1")
    assert hosts == ["example.com", "v3.cdn.example.net"]


def test_jar_is_cut_to_related_domains_only():
    text, n = m._netscape_cookies_for_hosts(
        _JAR, ["example.com", "v3.cdn.example.net"],
    )
    assert n == 2
    assert "doubleclick" not in text
    assert text.startswith("# Netscape HTTP Cookie File\n")
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert lines == [
        ".cdn.example.net\tTRUE\t/\tTRUE\t0\tsess\tabc",
        "example.com\tFALSE\t/\tFALSE\t1800000000\tlogin\tx y",
    ]


def test_no_match_means_zero_not_a_header_only_file():
    text, n = m._netscape_cookies_for_hosts(_JAR, ["other.tld"])
    assert n == 0


def test_domain_match_is_suffix_both_ways():
    assert m._cookie_domain_matches(".example.com", ["v.example.com"])
    assert m._cookie_domain_matches("a.b.example.com", ["example.com"])
    assert not m._cookie_domain_matches("notexample.com", ["example.com"])
    assert not m._cookie_domain_matches("", ["example.com"])


class _Assign:
    url = "https://www.example.com/watch/1"
    asset_upload_base = "http://hub:8000/jobs/j1/assets"
    job_id = "j1"


class _Agent:
    """Only what _deferred_cookies_file touches."""

    def __init__(self, registry_path=None):
        self._registry_path = registry_path
        self.registry_calls = []

    async def _fetch_cookies_txt_for(self, url, state, log=None):
        self.registry_calls.append((url, state.asset_upload_base))
        return self._registry_path

    _deferred_cookies_file = m._JobExecMixin._deferred_cookies_file


def test_browser_jar_wins_and_is_written_to_a_temp_file():
    ag = _Agent(registry_path=Path("/should/not/be/used"))
    path, note = asyncio.run(
        ag._deferred_cookies_file(_Assign(), _TARGETS, _Assign.url, _JAR),
    )
    try:
        assert path is not None and path.exists()
        body = path.read_text(encoding="utf-8")
        assert "sess\tabc" in body and "doubleclick" not in body
        assert note.startswith("2 browser cookie(s)")
        assert ag.registry_calls == []
    finally:
        if path:
            path.unlink()


def test_registry_is_the_fallback_when_the_jar_has_nothing_relevant():
    ag = _Agent(registry_path=Path("registry.txt"))
    jar = [{"name": "tracker", "value": "t", "domain": ".doubleclick.net"}]
    path, note = asyncio.run(
        ag._deferred_cookies_file(_Assign(), _TARGETS, _Assign.url, jar),
    )
    assert path == Path("registry.txt")
    assert ag.registry_calls == [(_Assign.url, _Assign.asset_upload_base)]
    assert "host registry" in note


def test_nothing_anywhere_means_none_and_a_readable_note():
    ag = _Agent(registry_path=None)
    path, note = asyncio.run(
        ag._deferred_cookies_file(_Assign(), _TARGETS, _Assign.url, None),
    )
    assert path is None
    assert note.startswith("none (")


def test_registry_failure_does_not_raise():
    class _Boom(_Agent):
        async def _fetch_cookies_txt_for(self, url, state, log=None):
            raise RuntimeError("hub down")

    path, note = asyncio.run(
        _Boom()._deferred_cookies_file(_Assign(), _TARGETS, _Assign.url, []),
    )
    assert path is None


def test_run_ytdlp_accepts_cookies_file_keyword():
    """The deferred path passes cookies_file=; the signature must keep it."""
    import inspect
    from core.fetcher import run_ytdlp

    assert "cookies_file" in inspect.signature(run_ytdlp).parameters
