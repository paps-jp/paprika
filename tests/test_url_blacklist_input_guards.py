"""Regression: the blacklist must not pin the worker's event loop.

2026-08-30. Twelve of 272 fleet workers were sitting silent (up to 27 min,
no heartbeat -> hub shows them offline, their jobs die with them). py-spy
found 6/6 of the wedged ones stuck in the SAME frame::

    matches         (core/url_blacklist.py)   <- self._regex.search(url_raw)
    match           (core/url_blacklist.py)
    _is_blacklisted (server/worker/browser_ops/capture.py)
    on_response     (server/worker/browser_ops/capture.py)

A glob starting with ``*`` translates to a lazy-any prefix, so a FAILED
``re.search()`` retries from every offset -- quadratic in URL length. With
the production blacklist (9 patterns, 5 of them ``*``-led) a 20 KB URL cost
4.6 s and a ~400 KB inline ``data:`` URI cost ~35 minutes, all inside one
``on_response`` callback. ``capture.py`` hands over whatever Chrome reports,
with no length or scheme guard.

Pinned here:
  * data: / blob: URLs skip matching entirely
  * the regex path never sees more than _REGEX_MAX_URL_CHARS
  * a huge URL stays cheap (the actual bug)
  * ordinary URLs still match exactly as before
"""

import time

import pytest

from core.url_blacklist import _REGEX_MAX_URL_CHARS, compile_blacklist

# The live prod blacklist at the time of the incident.
PROD_PATTERNS = [
    "https://*.saawsedge.com*",
    "*/ads/*.js",
    "^https://*.doubleclick.net",
    "*/googleadservices.com/*",
    "*/preview/*.mp4",
    "*/trailer*.m3u8",
    "*/tracker.gif",
    "*/beacon/*",
    "^https://stripchat.com/api/front/v2/",
]


@pytest.mark.parametrize("scheme", ["data:", "blob:", "DATA:", "Blob:"])
def test_inline_payload_urls_skip_matching(scheme):
    """A base64 image is not a host+path -- no pattern may claim it, and it
    must not cost a regex step (this is the URL shape that froze workers)."""
    m = compile_blacklist(PROD_PATTERNS + ["*"])  # '*' matches literally anything
    assert m.match(scheme + "image/png;base64," + "A" * 100_000) is None


def test_regex_path_is_capped():
    """The pattern only matches past the cap, so a hit proves the regex saw
    more than it should."""
    tail = "/ads/x.js"
    url = "https://cdn.example.com/" + "a" * (_REGEX_MAX_URL_CHARS * 2) + tail
    assert compile_blacklist(["*/ads/*.js"]).match(url) is None
    # ... while the same pattern still hits when it lands inside the window.
    assert compile_blacklist(["*/ads/*.js"]).match(
        "https://cdn.example.com" + tail,
    ) == "*/ads/*.js"


def test_substring_path_is_not_capped():
    """Substring matching is a linear C scan, so it keeps the whole URL."""
    url = "https://cdn.example.com/" + "a" * (_REGEX_MAX_URL_CHARS * 2) + "/tracker"
    assert compile_blacklist(["/tracker"]).match(url) == "/tracker"


def test_huge_url_stays_cheap():
    """The bug itself: 1 MB of URL used to take ~30 minutes."""
    m = compile_blacklist(PROD_PATTERNS)
    url = "https://cdn.example.com/" + "a" * 1_000_000
    t0 = time.perf_counter()
    assert m.match(url) is None
    assert time.perf_counter() - t0 < 0.5


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://a.saawsedge.com/x.jpg", "https://*.saawsedge.com*"),
        ("https://cdn.x.com/ads/track.js", "*/ads/*.js"),
        ("https://x.doubleclick.net/y", "^https://*.doubleclick.net"),
        ("https://s/googleadservices.com/z", "*/googleadservices.com/*"),
        ("https://s/preview/clip.mp4", "*/preview/*.mp4"),
        ("https://s/trailer_hd.m3u8", "*/trailer*.m3u8"),
        ("https://s/img/tracker.gif", "*/tracker.gif"),
        ("https://s/beacon/1", "*/beacon/*"),
        ("https://stripchat.com/api/front/v2/x", "^https://stripchat.com/api/front/v2/"),
        ("https://example.com/photo.jpg", None),
        ("https://example.com/video/main.m3u8", None),
    ],
)
def test_ordinary_urls_unchanged(url, expected):
    assert compile_blacklist(PROD_PATTERNS).match(url) == expected
