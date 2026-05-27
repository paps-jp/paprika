"""Per-host visited-URL registry.

Stores the set of URLs each host has already had successfully crawled,
*separately* from the HostRecord cookie store so the visited file can
grow large (10k-100k URLs for a long-running crawl) without bloating
every cookie-related read.

Storage layout::

    {data_dir}/hosts/visited/<safe-host>.json

File content::

    {"host": "example.com", "count": 12345,
     "urls": ["https://...", ...]}

URLs are deduped at add time. The file is rewritten in full on every
mutation, so callers should batch when possible. For paprika's scale
(a few tens of thousands of URLs per host max), this is simpler than
sqlite or append-only logs.

Walker integration: ``pap.walk(host_dedup=True)`` (the default) calls
``GET /hosts/{host}/visited`` at start to seed its ``attempted``
set, and ``POST /hosts/{host}/visited`` to record each new URL.
The host's ``recrawl_patterns`` (stored on HostRecord) override the
dedup check so frontier pages can be revisited each run.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

from server.hub.hosts import _normalise_host, _safe_filename


def url_hash(url: str) -> str:
    """Stable id for a URL. Used as the URL-path component when
    deleting an individual entry (URLs themselves contain chars that
    don't go cleanly into a REST path)."""
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


def _matches_any_pattern(url: str, patterns: Iterable[str]) -> bool:
    """Glob-style match: ``*`` matches any run of chars, ``?`` matches
    one. Used for ``recrawl_patterns`` so frontier pages can be
    declared as "always re-crawl me" while individual content pages
    still get dedup'd."""
    if not url or not patterns:
        return False
    for p in patterns:
        if not p:
            continue
        # Build regex: escape, then unescape * and ?
        rx = re.escape(p).replace(r"\*", ".*").replace(r"\?", ".")
        try:
            if re.fullmatch(rx, url):
                return True
        except re.error:
            continue
    return False


def _pattern_to_regex(p: str):
    """Compile one glob pattern to a regex. Returns None on bad input
    so callers can fall back to "no match" rather than crash."""
    if not p:
        return None
    rx = re.escape(p).replace(r"\*", ".*").replace(r"\?", ".")
    try:
        return re.compile(rx)
    except re.error:
        return None


class HostVisitedRegistry:
    """File-backed per-host visited URL set."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "hosts" / "visited"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, host: str) -> Path:
        h = _normalise_host(host)
        return self.dir / f"{_safe_filename(h)}.json"

    def _load(self, host: str) -> tuple[str, list[str]]:
        """Read the on-disk URL list. Returns (normalised_host, urls)."""
        h = _normalise_host(host)
        p = self._path(h)
        if not p.exists():
            return h, []
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            urls = list(d.get("urls") or [])
            return h, urls
        except Exception:
            return h, []

    def _write(self, host: str, urls: list[str]) -> None:
        h = _normalise_host(host)
        p = self._path(h)
        p.parent.mkdir(parents=True, exist_ok=True)
        # De-dup while preserving order. Convert to a set for the
        # final write to ensure correctness; insertion order is
        # preserved by Python 3.7+.
        seen: set[str] = set()
        deduped: list[str] = []
        for u in urls:
            if u and u not in seen:
                seen.add(u)
                deduped.append(u)
        p.write_text(
            json.dumps(
                {"host": h, "count": len(deduped), "urls": deduped},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    # ----- read --------------------------------------------------------

    def all_urls(self, host: str) -> list[str]:
        """Return the full visited URL list (in insertion order).
        Use ``page()`` for large hosts (Admin UI / API)."""
        _, urls = self._load(host)
        return urls

    def count(self, host: str) -> int:
        _, urls = self._load(host)
        return len(urls)

    def page(
        self,
        host: str,
        *,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        """Return ``{total, count, offset, limit, urls}`` for one
        page of the visited set. ``q`` is a case-insensitive substring
        match against the URL text."""
        h, urls = self._load(host)
        if q:
            ql = q.lower()
            filtered = [u for u in urls if ql in u.lower()]
        else:
            filtered = urls
        total = len(filtered)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 1000))
        page_urls = filtered[offset : offset + limit]
        return {
            "host": h,
            "total": total,
            "count": len(page_urls),
            "offset": offset,
            "limit": limit,
            "q": q or "",
            "urls": [{"url": u, "hash": url_hash(u)} for u in page_urls],
        }

    def contains(self, host: str, url: str) -> bool:
        _, urls = self._load(host)
        return url in set(urls)

    def match_counts(
        self,
        host: str,
        patterns: list[str],
    ) -> list[int]:
        """For each pattern, count how many of this host's visited
        URLs would match. Used by the admin UI to flag patterns
        that don't actually hit anything (likely typos).

        Returns a list of ints aligned with the input ``patterns``.
        Empty patterns or unparseable ones map to 0."""
        _, urls = self._load(host)
        if not urls:
            return [0] * len(patterns)
        compiled = [_pattern_to_regex(p) for p in patterns]
        out = []
        for rx in compiled:
            if rx is None:
                out.append(0)
                continue
            n = 0
            for u in urls:
                if rx.fullmatch(u):
                    n += 1
            out.append(n)
        return out

    # ----- write -------------------------------------------------------

    def add(self, host: str, url: str) -> bool:
        """Add one URL. Returns True if newly inserted, False if it
        was already present."""
        h, urls = self._load(host)
        if url in set(urls):
            return False
        urls.append(url)
        self._write(h, urls)
        return True

    def add_many(self, host: str, urls_in: Iterable[str]) -> int:
        """Add many URLs in one rewrite. Returns the count of newly-
        inserted URLs (i.e. excludes duplicates)."""
        h, existing = self._load(host)
        seen = set(existing)
        added = 0
        for u in urls_in:
            if u and u not in seen:
                seen.add(u)
                existing.append(u)
                added += 1
        if added:
            self._write(h, existing)
        return added

    def remove(self, host: str, url: str) -> bool:
        h, urls = self._load(host)
        if url not in set(urls):
            return False
        urls = [u for u in urls if u != url]
        self._write(h, urls)
        return True

    def remove_by_hash(self, host: str, sha: str) -> str | None:
        """Remove the URL whose ``url_hash()`` equals ``sha``. Returns
        the deleted URL, or None if no match."""
        h, urls = self._load(host)
        target = None
        for u in urls:
            if url_hash(u) == sha:
                target = u
                break
        if target is None:
            return None
        urls = [u for u in urls if u != target]
        self._write(h, urls)
        return target

    def clear(self, host: str) -> int:
        h, urls = self._load(host)
        n = len(urls)
        if n == 0:
            # Still remove file if it exists, for hygiene
            p = self._path(h)
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
            return 0
        self._write(h, [])
        return n

    def delete_host(self, host: str) -> bool:
        """Drop the entire file (called when the parent host record
        is deleted)."""
        p = self._path(host)
        if not p.exists():
            return False
        try:
            p.unlink()
            return True
        except Exception:
            return False
