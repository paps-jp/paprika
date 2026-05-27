"""Page metadata extractor.

Pulls ``title`` / ``description`` / ``thumbnail_url`` out of a saved
``page.html`` so ``GET /jobs/{id}/meta`` can return them as a small
JSON dict the operator (or downstream UI) can consume without
re-parsing 5 MB of HTML themselves.

Extraction order (first non-empty wins):

  title:
    1. ``<title>...</title>``
    2. ``<meta property="og:title">``
    3. ``<meta name="twitter:title">``

  description:
    1. ``<meta name="description">``
    2. ``<meta property="og:description">``
    3. ``<meta name="twitter:description">``

  thumbnail_url:
    1. ``<meta property="og:image:secure_url">``
    2. ``<meta property="og:image:url">``
    3. ``<meta property="og:image">``
    4. ``<meta name="twitter:image:src">``
    5. ``<meta name="twitter:image">``
    6. ``<link rel="apple-touch-icon">``
    7. ``<link rel="icon">``  /  ``<link rel="shortcut icon">``

The thumbnail URL is absolutised against ``base_url`` via urljoin so
the caller doesn't have to worry about Chrome's relative-path
quirks.

Standalone (no BeautifulSoup4 / lxml dependency). Uses the stdlib
``html.parser.HTMLParser`` -- enough for our needs since we only
care about ``<title>``, ``<meta>``, ``<link>``, and we stop scanning
at the end of ``<head>`` to keep big-page parses snappy.
"""

from __future__ import annotations

import html as _htmllib
from html.parser import HTMLParser
from urllib.parse import urljoin


class _StopParsing(Exception):
    """Internal sentinel raised when we hit </head> so we can short-
    circuit out of HTMLParser.feed() without scanning the body."""


class _HeadParser(HTMLParser):
    """Pulls every interesting tag out of <head>. The actual
    "pick the first non-empty value" cascade runs in the caller --
    we just record everything we see, in order.
    """

    def __init__(self) -> None:
        # convert_charrefs=True lets &amp; / &#x27; / etc. land as
        # decoded characters in handle_data, so we don't have to
        # decode them ourselves.
        super().__init__(convert_charrefs=True)
        # Capture buffers. Each list preserves source order so the
        # cascade picks "first wins" naturally.
        self.title_texts: list[str] = []
        self.metas: list[dict[str, str]] = []  # attrs of every <meta>
        self.links: list[dict[str, str]] = []  # attrs of every <link>
        # State for accumulating <title> text content.
        self._in_title = False
        self._title_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "title":
            self._in_title = True
            self._title_buf = []
        elif t == "meta":
            self.metas.append({k.lower(): (v or "") for k, v in attrs})
        elif t == "link":
            self.links.append({k.lower(): (v or "") for k, v in attrs})

    def handle_startendtag(self, tag, attrs):
        # Self-closing forms (<meta .../>) -- treat same as starttag.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "title" and self._in_title:
            self.title_texts.append("".join(self._title_buf).strip())
            self._in_title = False
            self._title_buf = []
        elif t == "head":
            # Stop here -- meta / link / title only live in <head>,
            # and body parsing on a multi-MB page is wasted work.
            raise _StopParsing

    def handle_data(self, data):
        if self._in_title:
            self._title_buf.append(data)


def _first_nonempty(values: list[str | None]) -> str | None:
    """Return the first ``v.strip()`` that's truthy, else None."""
    for v in values:
        if v is None:
            continue
        s = v.strip() if isinstance(v, str) else ""
        if s:
            return s
    return None


def _meta_lookup(metas: list[dict], key_attr: str, key_value: str) -> str | None:
    """``<meta {key_attr}={key_value} content="...">`` -> content,
    case-insensitive on the key value. Returns the first match in
    source order so og: takes precedence over later twitter: when
    a page has both."""
    kv_lower = key_value.lower()
    for m in metas:
        if m.get(key_attr, "").lower() == kv_lower:
            content = m.get("content")
            if content is not None:
                return content
    return None


def _link_lookup(links: list[dict], rel_value: str) -> str | None:
    """``<link rel="rel_value" href="...">`` -> href. ``rel`` may
    be a space-separated list (e.g. ``rel="shortcut icon"``) so we
    membership-test rather than equality-match."""
    rv = rel_value.lower()
    for ln in links:
        rels = (ln.get("rel") or "").lower().split()
        if rv in rels:
            href = ln.get("href")
            if href is not None:
                return href
    return None


def extract_meta(html: str, base_url: str = "") -> dict:
    """Parse ``html`` and return a meta dict::

        {
            "title":          str | None,
            "description":    str | None,
            "thumbnail_url":  str | None,   # absolutised against base_url
        }

    Relative URLs in thumbnail_url are resolved via ``urljoin``. If
    ``base_url`` is empty, relative thumbnails are left as-is.
    Caller can stack the ``url`` it knows about on top of this
    return value to round out the response payload.
    """
    p = _HeadParser()
    try:
        p.feed(html)
    except _StopParsing:
        pass
    except Exception:
        # Malformed HTML shouldn't take the whole request down. We
        # return whatever we managed to gather before the parser
        # choked.
        pass
    # Title cascade
    title = _first_nonempty(
        [
            *p.title_texts,
            _meta_lookup(p.metas, "property", "og:title"),
            _meta_lookup(p.metas, "name", "og:title"),  # tolerated variant
            _meta_lookup(p.metas, "name", "twitter:title"),
        ]
    )
    # Description cascade
    description = _first_nonempty(
        [
            _meta_lookup(p.metas, "name", "description"),
            _meta_lookup(p.metas, "property", "og:description"),
            _meta_lookup(p.metas, "name", "twitter:description"),
        ]
    )
    # Thumbnail cascade. og:image first (most standard), twitter:
    # next, then app-icon-style links as fallback.
    thumbnail = _first_nonempty(
        [
            _meta_lookup(p.metas, "property", "og:image:secure_url"),
            _meta_lookup(p.metas, "property", "og:image:url"),
            _meta_lookup(p.metas, "property", "og:image"),
            _meta_lookup(p.metas, "name", "og:image"),  # tolerated variant
            _meta_lookup(p.metas, "name", "twitter:image:src"),
            _meta_lookup(p.metas, "name", "twitter:image"),
            _meta_lookup(p.metas, "property", "twitter:image"),  # tolerated variant
            _link_lookup(p.links, "apple-touch-icon"),
            _link_lookup(p.links, "icon"),
            _link_lookup(p.links, "shortcut icon"),
        ]
    )
    if thumbnail and base_url:
        try:
            thumbnail = urljoin(base_url, thumbnail)
        except Exception:
            # urljoin shouldn't fail on real-world inputs but if
            # the base_url is weird (e.g. a non-URL string from a
            # malformed JobInfo), keep the raw value rather than
            # raising into the FastAPI handler.
            pass

    # Decode any HTML entities the parser missed (convert_charrefs
    # handles most but defensive coding here is cheap).
    def _decode(v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return _htmllib.unescape(v).strip() or None
        except Exception:
            return v

    return {
        "title": _decode(title),
        "description": _decode(description),
        "thumbnail_url": _decode(thumbnail),
    }
