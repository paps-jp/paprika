"""DOM summarizer — extract the *meaningful* parts of an HTML document.

Phase 1.5 of the v2 architecture. The PerceptionResult v0 sampling on 20
real hosts revealed a common failure mode: feeding the LLM the first 16 KB
of ``page.html`` wastes the entire budget on ``<head>`` + inline CSS +
preload scripts, leaving zero room for the body where actual content
lives. The eye correctly self-diagnosed this with ``empty_content`` /
``truncated_dom`` anomalies (11 of 20 hosts).

This module produces a compact text summary that focuses on the elements
the eye actually needs to perceive content:

  * Page title
  * Headings (h1-h6)
  * Visible text from <body> (script/style stripped)
  * Links (with both href and link text)
  * Images (src + alt text)
  * Interactive elements (button / input / select / form)
  * Video / audio elements + src

Implementation note: uses Python stdlib ``html.parser`` only -- no new
deps. The output is plain text, ~4-10x denser than raw HTML.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any


# Tags whose entire content should be skipped (treated as opaque blobs).
# Only tags with an *actual closing tag* belong here -- void elements
# like <meta>, <link>, <br>, <img>, <input> never close and would
# imbalance a counter-based skip-depth tracker.
_SKIP_CONTAINERS = frozenset({
    "script", "style", "noscript", "svg", "head",
    "iframe",  # iframes have their own document; we can't see inside
    "template",
})

# Void / self-closing HTML elements. Never tracked on the skip stack.
_VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

# Block-level tags that get a newline before/after their text.
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "main", "aside", "nav", "header", "footer",
    "ul", "ol", "li", "table", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "br", "hr",
    "blockquote", "pre",
})

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


class _Summarizer(HTMLParser):
    """Walks an HTML doc and emits a structured summary.

    Public state after ``feed()``:
      * ``title``      -- contents of <title>
      * ``headings``   -- list of (level, text) tuples
      * ``body_text``  -- visible text from <body>, normalised
      * ``links``      -- list of (href, link_text) tuples
      * ``images``     -- list of (src, alt) tuples
      * ``buttons``    -- list of button labels
      * ``inputs``     -- list of (type, name, placeholder) tuples
      * ``forms``      -- list of (action, method) tuples
      * ``videos``     -- list of (src, kind) tuples; kind ∈ {video, source}
      * ``audios``     -- list of (src,) tuples
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Public output
        self.title: str = ""
        self.headings: list[tuple[int, str]] = []
        self.links: list[tuple[str, str]] = []
        self.images: list[tuple[str, str]] = []
        self.buttons: list[str] = []
        self.inputs: list[tuple[str, str, str]] = []
        self.forms: list[tuple[str, str]] = []
        self.videos: list[tuple[str, str]] = []
        self.audios: list[tuple[str]] = []
        self._body_chunks: list[str] = []

        # Internal state.
        # Stack of currently-open skip-container tags (script/style/etc).
        # When non-empty, we're inside opaque content and ignore everything
        # except the matching end tag. Stack-based so unbalanced markup
        # (void elements without close tags) doesn't pollute it.
        self._skip_stack: list[str] = []
        self._in_title = False
        self._in_body = False
        self._heading_stack: list[tuple[int, list[str]]] = []  # (level, text-chunks)
        # Buffering link text and button text while inside <a> / <button>
        self._link_buffer: list[tuple[str, list[str]]] = []  # (href, text-chunks)
        self._button_buffer: list[list[str]] = []  # text-chunks

    # ------------------------------------------------------------------
    # Tag dispatch
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}

        if tag in _SKIP_CONTAINERS:
            # Push onto the skip stack. <head> is special-cased so we can
            # still capture <title> living inside it.
            self._skip_stack.append(tag)
            return

        if tag in _VOID_ELEMENTS:
            # Void elements never have a closing tag and contribute no body
            # children. Process them attributes-only for the data we want.
            if self._skip_stack:
                return  # void inside a skip container -- ignore
            if tag == "img":
                src = a.get("src", "").strip()
                alt = a.get("alt", "").strip()
                if src or alt:
                    self.images.append((src, alt))
            elif tag == "input":
                itype = a.get("type", "text").strip()
                name = a.get("name", "").strip()
                placeholder = a.get("placeholder", "").strip()
                self.inputs.append((itype, name, placeholder))
            elif tag == "source":
                src = a.get("src", "").strip()
                if src:
                    self.videos.append((src, "source"))
            elif tag == "br" and self._in_body:
                self._body_chunks.append("\n")
            return

        # Inside an opaque skip container? Title is the exception (we
        # capture <title> even though it lives inside <head>).
        if self._skip_stack and tag != "title":
            return

        if tag == "title":
            self._in_title = True
            return

        if tag == "body":
            self._in_body = True
            return

        # --- Interactive / structural extraction ------------------------
        if tag == "a":
            href = a.get("href", "").strip()
            self._link_buffer.append((href, []))
            return

        if tag == "button":
            self._button_buffer.append([])
            return

        if tag == "form":
            action = a.get("action", "").strip()
            method = a.get("method", "get").strip().lower()
            self.forms.append((action, method))
            return

        if tag == "video":
            src = a.get("src", "").strip()
            if src:
                self.videos.append((src, "video"))
            return

        if tag == "audio":
            src = a.get("src", "").strip()
            if src:
                self.audios.append((src,))
            return

        if tag in _HEADING_TAGS:
            level = int(tag[1])
            self._heading_stack.append((level, []))
            return

        if tag in _BLOCK_TAGS:
            # Just mark a block boundary in body_text for readability.
            if self._in_body:
                self._body_chunks.append("\n")
            return

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in _SKIP_CONTAINERS:
            # Pop only if the top of the skip stack actually matches --
            # tolerates unbalanced markup gracefully.
            if self._skip_stack and self._skip_stack[-1] == tag:
                self._skip_stack.pop()
            return

        if tag in _VOID_ELEMENTS:
            # End tag for a void element is malformed; ignore silently.
            return

        if self._skip_stack and tag != "title":
            return

        if tag == "title":
            self._in_title = False
            return

        if tag == "body":
            self._in_body = False
            return

        if tag == "a" and self._link_buffer:
            href, chunks = self._link_buffer.pop()
            text = _join_chunks(chunks)
            if href or text:
                self.links.append((href, text))
            return

        if tag == "button" and self._button_buffer:
            chunks = self._button_buffer.pop()
            text = _join_chunks(chunks)
            if text:
                self.buttons.append(text)
            return

        if tag in _HEADING_TAGS and self._heading_stack:
            level, chunks = self._heading_stack.pop()
            text = _join_chunks(chunks)
            if text:
                self.headings.append((level, text))
            return

        if tag in _BLOCK_TAGS:
            if self._in_body:
                self._body_chunks.append("\n")
            return

    # ------------------------------------------------------------------
    # Text data
    # ------------------------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._skip_stack and not self._in_title:
            return
        if self._in_title:
            self.title += data
            return

        # Buffer into open containers if any are active.
        if self._link_buffer:
            self._link_buffer[-1][1].append(data)
        if self._button_buffer:
            self._button_buffer[-1].append(data)
        if self._heading_stack:
            self._heading_stack[-1][1].append(data)

        # Always accumulate into body_text when in body (overlap with
        # link/heading buffers is intentional -- body_text shows
        # everything as it appears in flow).
        if self._in_body:
            self._body_chunks.append(data)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def body_text_compact(self) -> str:
        """Return body text with collapsed whitespace."""
        raw = "".join(self._body_chunks)
        return _normalize_whitespace(raw)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join_chunks(chunks: list[str]) -> str:
    return _normalize_whitespace("".join(chunks))


def _normalize_whitespace(s: str) -> str:
    """Collapse runs of whitespace to single spaces, keep paragraph breaks."""
    if not s:
        return ""
    # Normalise CR/LF combos first.
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of spaces / tabs within a line.
    out_lines: list[str] = []
    for line in s.split("\n"):
        stripped = " ".join(line.split())
        out_lines.append(stripped)
    # Collapse 3+ consecutive blank lines into one paragraph break.
    result_lines: list[str] = []
    blank_run = 0
    for ln in out_lines:
        if ln:
            result_lines.append(ln)
            blank_run = 0
        else:
            blank_run += 1
            if blank_run == 1:
                result_lines.append("")
    return "\n".join(result_lines).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def summarize(html: str, *, max_chars: int = 16000) -> str:
    """Produce a compact text summary of an HTML document.

    Output sections (in order, omitted when empty):
      TITLE      -- <title> contents
      HEADINGS   -- h1-h6 ordered
      TEXT       -- body visible text
      LINKS      -- href + text
      IMAGES     -- src + alt
      BUTTONS    -- visible button labels
      INPUTS     -- form input types/names
      FORMS      -- action / method
      VIDEOS     -- <video>/<source>/<audio> src

    The output is hard-capped at ``max_chars`` (default 16000) by
    progressively dropping the lowest-priority sections from the bottom
    (videos last, then inputs/forms, then images, then text body, ...).
    Title and headings are always kept.
    """
    if not html:
        return ""

    p = _Summarizer()
    try:
        p.feed(html)
    except Exception:
        # html.parser is forgiving but malformed input can still crash.
        # Best-effort: return whatever we got.
        pass

    sections: list[tuple[str, str, int]] = []  # (name, text, priority)

    if p.title.strip():
        sections.append(("TITLE", p.title.strip(), 100))

    if p.headings:
        headings_text = "\n".join(f"  h{lvl}: {txt}" for lvl, txt in p.headings)
        sections.append(("HEADINGS", headings_text, 90))

    body = p.body_text_compact()
    if body:
        sections.append(("TEXT", body, 50))

    if p.links:
        lines: list[str] = []
        for href, text in p.links[:200]:  # cap link explosion
            line = f"  [{text}] -> {href}" if text else f"  -> {href}"
            lines.append(line)
        sections.append(("LINKS", "\n".join(lines), 70))

    if p.images:
        lines = [f"  alt={alt!r} src={src}" for src, alt in p.images[:100]]
        sections.append(("IMAGES", "\n".join(lines), 60))

    if p.buttons:
        sections.append(("BUTTONS", "\n".join(f"  {b}" for b in p.buttons), 80))

    if p.inputs:
        lines = [
            f"  type={t} name={n!r} placeholder={ph!r}"
            for t, n, ph in p.inputs
        ]
        sections.append(("INPUTS", "\n".join(lines), 65))

    if p.forms:
        lines = [f"  action={a} method={m}" for a, m in p.forms]
        sections.append(("FORMS", "\n".join(lines), 65))

    if p.videos or p.audios:
        v_lines = [f"  video[{kind}] src={src}" for src, kind in p.videos]
        a_lines = [f"  audio src={src}" for (src,) in p.audios]
        sections.append(("MEDIA", "\n".join(v_lines + a_lines), 85))

    # Sort by priority desc, then keep adding sections until we hit max_chars.
    sections.sort(key=lambda x: -x[2])
    out_parts: list[str] = []
    remaining = max_chars
    for name, text, _prio in sections:
        block = f"## {name}\n{text}"
        if len(block) + 2 > remaining:
            # Truncate this section to fit; keep at least a stub if it's a
            # high-priority section (title/headings/buttons).
            if remaining > 100:
                truncated = block[: remaining - len("\n... [truncated]")]
                out_parts.append(truncated + "\n... [truncated]")
            break
        out_parts.append(block)
        remaining -= len(block) + 2  # 2 for "\n\n" separator

    return "\n\n".join(out_parts)
