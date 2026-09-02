r"""URL blacklist matcher — shared by worker session capture and fetch mode.

Supports four pattern syntaxes (chosen at compile time per line):

* **Plain substring** — no special chars. Case-insensitive ``in`` test.
  Example: ``media-hls.example-cdn.com``   → blocks any URL containing it.

* **Glob with ``*`` / ``?``** — any pattern containing ``*`` or ``?`` is
  compiled via :mod:`fnmatch` against the full URL. ``*`` matches any
  run of chars (including ``/``), ``?`` matches one char.
  Example: ``https://media-hls.example-cdn.com*`` → blocks any URL with
  that prefix.
  Example: ``*/ads/*.js`` → blocks ad scripts anywhere.

* **Regex (``/.../``)** — wrap with forward slashes. Compiled with
  ``re.IGNORECASE``. Use when glob isn't expressive enough.
  Example: ``/example-cdn\.com/(media|preview)/.*\.ts/``

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

Two input guards protect the caller's event loop (see ``match()``):
inline-payload URLs (``data:`` / ``blob:``) skip matching entirely, and
the regex path only ever sees the first ``_REGEX_MAX_URL_CHARS`` chars.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Iterable


#: How much of a URL the regex path is allowed to see.
#:
#: Glob patterns starting with ``*`` translate to a lazy-any prefix, so a
#: FAILED ``re.search()`` retries from every offset -- O(n^2) in URL length.
#: Measured with the production blacklist (9 patterns, 5 of them ``*``-led):
#: 2 KB URL = 49 ms, 20 KB = 4.6 s, 40 KB = 18.5 s. A ~400 KB inline URI
#: therefore pins the worker's asyncio loop for ~35 minutes inside ONE
#: ``on_response`` callback: no heartbeat, no logs, hub marks the worker
#: offline and its job dies with it. Twelve of 272 workers were sitting in
#: exactly that state when this was diagnosed (2026-08-30, py-spy: 6/6
#: wedged workers stuck at ``_regex.search``).
#:
#: Blacklist patterns address a host + path, which live in the first few
#: hundred chars; truncating past 2 KB costs no realistic match and caps the
#: worst case at ~50 ms. Truncate rather than skip so a long-but-ordinary URL
#: (tracking query strings) still matches on its host/path prefix.
_REGEX_MAX_URL_CHARS = 2048

#: URL schemes whose "URL" is an inline payload, not a network location --
#: a base64 image can be megabytes long and can never meaningfully match a
#: host/path pattern, so it is not worth a single regex step.
_INLINE_SCHEMES = ("data:", "blob:")


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
        # regex path — match against raw URL (regex is case-insensitive).
        # ``url_raw`` arrives already capped to _REGEX_MAX_URL_CHARS by
        # BlacklistMatcher.match(); do not re-widen it here.
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
        # Guard 1: inline payloads (data: / blob:) are never a host+path,
        # so no operator pattern can meaningfully describe them -- and they
        # are the URLs long enough to make the regex path pathological.
        if u[:5].lower() in _INLINE_SCHEMES:
            return None
        u_lower = u.lower()
        # Guard 2: bound what the regex path sees. The substring path stays
        # on the full URL -- ``in`` is a linear C scan, not a backtracker.
        u_regex = u if len(u) <= _REGEX_MAX_URL_CHARS else u[:_REGEX_MAX_URL_CHARS]
        for cp in self._compiled:
            if cp.matches(u_lower, u_regex):
                return cp.source
        return None


def compile_blacklist(patterns: Iterable[str] | None) -> BlacklistMatcher:
    """Build a :class:`BlacklistMatcher`. Wrapper exists so callers can
    type-hint against the function rather than the class."""
    return BlacklistMatcher(patterns)


# Manifest-URL passthrough — used by the asset-capture gates in
# core/fetcher.py + server/worker/browser_ops/capture.py to let an HLS/DASH
# manifest URL flow through to yt-dlp candidate collection EVEN WHEN the
# operator's blacklist matched it via a general host/path pattern.
#
# Why: a rule like ``https://*.example-cdn.com*`` (added to suppress noisy
# .ts / .mp4 segments + preview thumbs from polluting the gallery) also
# matched the main video's ``.m3u8`` manifest -- which silently dropped it
# from ``video_urls_seen`` / ``on_stream_detected``, so yt-dlp never saw the
# real video and fell back to iframe-generic ad URLs (job 63f9bf436c2f
# post-mortem 2026-06-14). Solution: a 2-tier semantics --
#
#   * MANIFEST-SPECIFIC pattern (literal ``.m3u8`` / ``.mpd`` in the rule,
#     e.g. ``*/trailer*.m3u8``) -> still blocks manifests. Operator was
#     explicitly asking to skip a manifest, honour it.
#   * GENERAL pattern (host / path glob without ``.m3u8`` / ``.mpd``) ->
#     manifests bypass the block. The save/log suppression still applies
#     (silent passthrough); only the yt-dlp candidate tracking is allowed.
#
# Direct video URLs (.mp4 / .webm / .ts segments) are NOT in the bypass --
# they're the noise the operator is trying to suppress.
_MANIFEST_URL_RE = re.compile(r"\.(m3u8|mpd)($|\?)", re.I)
_MANIFEST_PATTERN_RE = re.compile(r"\.(m3u8|mpd)", re.I)


def is_manifest_url(url: str) -> bool:
    """True if ``url`` looks like an HLS (.m3u8) or DASH (.mpd) manifest."""
    return bool(_MANIFEST_URL_RE.search(url or ""))


def pattern_targets_manifests(pattern: str) -> bool:
    """True if a blacklist source pattern literally mentions ``.m3u8`` /
    ``.mpd`` -- i.e. the operator was explicitly blocking a manifest, not
    just incidentally matching one with a host/path rule.

    The check is intentionally textual on the pattern source (NOT a match
    against compiled regex) so a glob like ``*/trailer*.m3u8`` is detected
    independently of how the matcher decomposes it. A host-only pattern
    like ``*.example-cdn.com*`` returns False even though it would match
    manifest URLs at runtime."""
    return bool(_MANIFEST_PATTERN_RE.search(pattern or ""))
