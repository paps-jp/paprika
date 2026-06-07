r"""URL blacklist matcher — shared by worker session capture and fetch mode.

Supports four pattern syntaxes (chosen at compile time per line):

* **Plain substring** — no special chars. Case-insensitive ``in`` test.
  Example: ``media-hls.saawsedge.com``   → blocks any URL containing it.

* **Glob with ``*`` / ``?``** — any pattern containing ``*`` or ``?`` is
  compiled via :mod:`fnmatch` against the full URL. ``*`` matches any
  run of chars (including ``/``), ``?`` matches one char.
  Example: ``https://media-hls.saawsedge.com*`` → blocks any URL with
  that prefix.
  Example: ``*/ads/*.js`` → blocks ad scripts anywhere.

* **Regex (``/.../``)** — wrap with forward slashes. Compiled with
  ``re.IGNORECASE``. Use when glob isn't expressive enough.
  Example: ``/saawsedge\.com/(media|preview)/.*\.ts/``

* **Anchored prefix / suffix** — ``^`` (URL must start) / ``$`` (URL
  must end). May combine with glob. Just sugar over building the
  equivalent regex/glob.
  Example: ``^https://ads.``  → start-of-URL match.

Lines starting with ``#`` are comments and ignored. Blank lines are
ignored. Strip whitespace on every line.

The compiled matcher's ``match(url) -> str | None`` returns the first
pattern that matched, or None when the URL is allowed. Built so the
hot path inside the per-response handler does the minimum work:

* Pure-substring patterns are kept as a list of pre-lowered strings
  and tested with ``in`` (fast — no regex overhead).
* Glob / regex patterns are compiled to ``re.Pattern`` objects once.
* Iteration short-circuits on first hit.

Empty patterns lists are a degenerate fast-path: ``match()`` returns
None immediately without touching the input string.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Iterable


class _CompiledPattern:
    """One entry in the compiled blacklist. Knows how to test a URL
    against itself and returns the original operator-facing pattern
    string (so the log message can echo what they wrote)."""

    __slots__ = ("source", "_kind", "_lower_substring", "_regex")

    def __init__(self, source: str) -> None:
        self.source = source
        s = source.strip()
        # Regex form: /pattern/ (or /pattern/flags but we don't support flags)
        if len(s) >= 2 and s.startswith("/") and s.endswith("/"):
            self._kind = "regex"
            self._regex = re.compile(s[1:-1], re.IGNORECASE)
            self._lower_substring = ""
            return
        # Glob form: contains *, ?, or anchor (^ / $)
        if any(c in s for c in "*?^$"):
            # Convert anchors before handing off to fnmatch -- fnmatch
            # uses ^/$ as literals, so map them to regex anchors directly.
            # The simplest route: compile to regex via fnmatch.translate
            # then patch the anchors.
            anchored_start = s.startswith("^")
            anchored_end = s.endswith("$")
            body = s
            if anchored_start:
                body = body[1:]
            if anchored_end:
                body = body[:-1]
            pat = fnmatch.translate(body)
            # fnmatch.translate wraps with (?s:...)\Z — strip the \Z so
            # we can compose our own anchors. The (?s:) keeps . matching
            # newlines (irrelevant for URLs but harmless).
            if pat.endswith(r"\Z"):
                pat = pat[:-2]
            # Anchors: default = "substring" (no anchor on either side).
            # Do NOT wrap the unanchored body in ".*" -- re.search() already
            # matches anywhere, so a leading/trailing ".*" is REDUNDANT and,
            # placed adjacent to the ".*" that glob "*" expands to (under
            # re.DOTALL), produces CATASTROPHIC backtracking on long URLs.
            # This froze the worker's asyncio loop inside a single
            # re.search() on paps.jp's googleads/doubleclick URLs -> hub WS
            # keepalive timeout -> job failed "disconnected before finished".
            # (Diagnosed with py-spy: 6/6 samples stuck at
            # url_blacklist.py _regex.search.) search() makes the wrapping
            # unnecessary, so dropping it is behaviour-preserving.
            if anchored_start:
                pat = r"\A" + pat
            if anchored_end:
                pat = pat + r"\Z"
            self._kind = "regex"
            self._regex = re.compile(pat, re.IGNORECASE | re.DOTALL)
            self._lower_substring = ""
            return
        # Plain substring (default for ぱっぷす operator-friendly entries).
        self._kind = "substring"
        self._lower_substring = s.lower()
        self._regex = None  # type: ignore[assignment]

    def matches(self, url_lower: str, url_raw: str) -> bool:
        if self._kind == "substring":
            return self._lower_substring in url_lower
        # regex path — match against raw URL (regex is case-insensitive)
        return bool(self._regex.search(url_raw))


class BlacklistMatcher:
    """Compiled blacklist. ``match(url)`` returns the first source
    pattern that hit, or None. Empty / None patterns iterable produces
    a no-op matcher whose ``match()`` is always None."""

    __slots__ = ("_compiled",)

    def __init__(self, patterns: Iterable[str] | None) -> None:
        compiled: list[_CompiledPattern] = []
        if patterns:
            for raw in patterns:
                if raw is None:
                    continue
                s = str(raw).strip()
                if not s or s.startswith("#"):
                    continue
                try:
                    compiled.append(_CompiledPattern(s))
                except re.error:
                    # Operator typo in a regex — skip but keep the rest.
                    # Loud failure would block all blacklisting on one
                    # bad line; silent skip lets the other patterns work.
                    continue
        self._compiled = tuple(compiled)

    def __bool__(self) -> bool:
        return bool(self._compiled)

    def __len__(self) -> int:
        return len(self._compiled)

    def match(self, url: str) -> str | None:
        if not self._compiled:
            return None
        u = url or ""
        u_lower = u.lower()
        for cp in self._compiled:
            if cp.matches(u_lower, u):
                return cp.source
        return None


def compile_blacklist(patterns: Iterable[str] | None) -> BlacklistMatcher:
    """Build a :class:`BlacklistMatcher`. Wrapper exists so callers can
    type-hint against the function rather than the class."""
    return BlacklistMatcher(patterns)
