"""Browser primitives shared by the agent loop and the Session HTTP API.

This module owns the actual "click / type / scroll / navigate / outline /
capture" implementations. Both pipelines call into
it so there is exactly one source of truth for what a browser action
means in paprika -- when click learns to retry on transient failures,
both the agent and the session API benefit.

Function shape conventions:

  * The first argument is always the nodriver ``tab`` (page-level CDP)
    or ``browser`` (browser-level CDP), the same object the caller
    already holds; this module does NOT manage the connection lifecycle.

  * Action functions (``click``, ``fill``, ``scroll`` …) return a short
    status string (``"OK"`` / ``"NO_MATCH"`` / ``"ERR: ..."``) so the
    caller can append it to history without parsing exceptions.

  * Naming follows Playwright where there is a Playwright equivalent
    (``fill``, ``press_key``, ``back``, ``navigate``), so LLM-generated
    code reads like idiomatic browser-automation code.

This is a pure refactor of code that previously lived in agent_runner.py
(see RFC-001 §8); behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from nodriver import cdp

# ---------------------------------------------------------------------------
# Type aliases / shared data structures
# ---------------------------------------------------------------------------

LogFn = Callable[[str], None]


@dataclass
class Snapshot:
    """One operator-visible capture saved during the agent loop."""

    label: str
    step: int
    url: str
    # Filenames under assets_dir/<label>/
    html_name: str
    png_name: str
    axtree_name: str


# ---------------------------------------------------------------------------
# Behavioural env knobs
# ---------------------------------------------------------------------------
#
# Default values match what agent_runner.py shipped before this refactor.
# They live here because every consumer of these constants is one of the
# functions defined in this module; lifting them into the higher-level
# orchestrator would only add an import detour.

# Sleep after each browser-state-changing action so the page has a moment
# to settle before the next observation.
ACTION_SETTLE_S = float(os.environ.get("AGENT_ACTION_SETTLE_S", "1.5"))
# Sleep after navigations (longer than a normal action).
NAVIGATION_SETTLE_S = float(os.environ.get("AGENT_NAVIGATION_SETTLE_S", "3.0"))

# Cap how big a chunk of AX-tree-equivalent text we ship per step. Models
# behave poorly past ~30K characters of context; 8K is a safe upper bound
# for smaller models on memory-constrained GPUs.
MAX_AX_TREE_CHARS = int(os.environ.get("AGENT_MAX_AX_TREE_CHARS", "8000"))
# Maximum number of interactive elements to surface in one page outline.
MAX_OUTLINE_ITEMS = int(os.environ.get("AGENT_MAX_OUTLINE_ITEMS", "60"))

# Opt-out switch for the in-page DOM hooks that try to keep navigation
# in the agent's tab (target="_blank" rewrite + window.open override).
# Kept for Phase 1 of the multi-tab refactor: most same-origin
# popups still collapse into the main tab, while cross-origin popups
# are now allowed to spawn real tabs (the CDP-level tab-killer was
# removed -- see ``install_tab_killer`` removal note below).
TAB_HOOKS_ENABLED = os.environ.get("AGENT_TAB_HOOKS", "1") not in ("0", "false", "no")

# Whether to include a PAGE TEXT section (body innerText excerpt) in the
# page outline. Disabled on sites whose prose triggers safety-trained
# models' silent refusal.
INCLUDE_BODY_TEXT = os.environ.get("AGENT_INCLUDE_BODY_TEXT", "1") not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# Page-state extraction (indexed interactive elements)
# ---------------------------------------------------------------------------
#
# The whole reason for this dance: instead of asking the LLM to invent
# robust CSS selectors (which is hard) we tag every visible interactive
# element with `data-paprika-id="N"` and let the model address it by N.
# IDs are regenerated each turn so numbering stays sequential and small.

_OUTLINE_JS = r"""
(() => {
  // -- Keep everything in this tab --------------------------------------
  // Sites that open links in new tabs (target="_blank" or window.open)
  // confuse the agent: our CDP attach watches one tab, so a click that
  // opens a new tab leaves the observed tab on the original page while
  // the actual content lives in a tab we never read. Disarm both
  // mechanisms on every observe so the next click navigates in-place.
  //
  // The whole block is gated on /*TAB_HOOKS*/ (substituted true/false
  // at runtime) so operators can disable it on sites where the
  // injection interferes with the page's own click handlers.
  if (/*TAB_HOOKS*/) try {
    // Only rewrite SAME-ORIGIN links. Cross-origin _blank links are
    // almost always ads or external destinations; rewriting them to
    // _self would either send the agent down an ad URL on click, or
    // (worse) let a click that was supposed to trigger an ad popup
    // navigate the agent's tab away from the content entirely.
    document.querySelectorAll('a[target="_blank"]').forEach(a => {
      try {
        const href = new URL(a.href, window.location.href);
        if (href.origin === window.location.origin) {
          a.setAttribute('target', '_self');
          a.removeAttribute('rel');  // strip "noopener" while we're here
        }
      } catch (_) { /* malformed href, skip */ }
    });
    document.querySelectorAll('form[target="_blank"]').forEach(f => {
      try {
        const act = new URL(f.action || '', window.location.href);
        if (act.origin === window.location.origin) {
          f.setAttribute('target', '_self');
        }
      } catch (_) { /* skip */ }
    });
    // Page scripts may also call window.open() directly. Two cases:
    //  a) Same-origin URL -> probably "open this content in a new tab".
    //     Redirect to same-window navigation so the agent stays with
    //     the content.
    //  b) Cross-origin URL -> almost always a popup ad (very common on
    //     adult / news sites where every click fires an ad popup).
    //     Silently swallow -- DON'T navigate the page, or every click
    //     would carry us off to an ad URL and the real click target
    //     would be ignored. Pages misbehave less when window.open
    //     pretends to succeed (returns a window object) than when it
    //     throws / returns null, so we still return `window`.
    if (!window.__paprika_open_patched) {
      window.open = function(url) {
        try {
          if (!url) return window;
          const target = new URL(url, window.location.href);
          if (target.origin === window.location.origin) {
            window.location.href = target.href;
          }
          // cross-origin: drop silently (ad popup blocked)
        } catch (_) { /* malformed URL etc. */ }
        return window;
      };
      window.__paprika_open_patched = true;
    }
  } catch (e) { /* keep observing even if the workaround failed */ }

  const SELECTOR = [
    'a[href]', 'button', 'input', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[role="tab"]',
    '[role="menuitem"]', '[role="checkbox"]', '[role="radio"]',
    '[onclick]', '[contenteditable=""]', '[contenteditable="true"]',
  ].join(',');

  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) return false;
    return true;
  };

  const trim = (s, n) => {
    s = (s || '').replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  };

  // Wipe any stale ids from a previous turn so numbering stays sequential.
  document.querySelectorAll('[data-paprika-id]').forEach(el => el.removeAttribute('data-paprika-id'));

  const items = [];
  let i = 0;
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (!isVisible(el)) continue;
    i += 1;
    el.setAttribute('data-paprika-id', String(i));
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || tag;
    const text = trim(
      el.getAttribute('aria-label') ||
      el.innerText ||
      el.value ||
      el.getAttribute('placeholder') ||
      el.getAttribute('title') ||
      '',
      120
    );
    const extra = {};
    if (tag === 'a') {
      // el.href is the browser-resolved absolute URL; el.getAttribute('href')
      // can be relative. We need absolute for matching against the
      // visited-URL set on the Python side, but we also keep the display
      // string trimmed so the outline stays readable.
      extra.href = trim(el.href || el.getAttribute('href') || '', 200);
    }
    if (tag === 'input' || tag === 'textarea') {
      extra.type = el.getAttribute('type') || tag;
      if (el.value) extra.value = trim(el.value, 80);
    }
    items.push({ id: i, tag, role, text, extra });
  }

  let title = '';
  try { title = document.title || ''; } catch (_) {}

  // A few bytes of page header so the LLM has scrolling/structure context
  // without having to read the AX tree.
  let bodyText = '';
  try { bodyText = trim(document.body && document.body.innerText, 1500); } catch (_) {}

  return JSON.stringify({ title, items, bodyText });
})()
"""


# ---------------------------------------------------------------------------
# URL canonicalisation (for the visited=true link marker)
# ---------------------------------------------------------------------------


def canon_url(url: str) -> str:
    """Normalise a URL for visited-set comparison.

    Strips the fragment (same page from the agent's point of view) and
    folds a missing trailing slash on bare hosts. Keeps query parameters
    since ``?id=1`` vs ``?id=2`` are different documents.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url.strip())
    except Exception:
        return url.strip()
    # `https://example.com` and `https://example.com/` are the same page.
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def href_in_visited(href: str, visited: set) -> bool:
    if not href or not visited:
        return False
    return canon_url(href) in visited


# ---------------------------------------------------------------------------
# Page outline
# ---------------------------------------------------------------------------


async def outline(tab, visited_urls: set | None = None) -> str:
    """Inject ids into interactive elements and return a text outline.

    Output looks like::

        TITLE: Example Domain

        [@1] a "More information…" href=https://www.iana.org/domains/example
        [@2] a "Top story" href=https://news.ycombinator.com/item?id=123 visited=true
        [@3] button "Submit"
        ...

        PAGE TEXT:
          (first ~1500 chars of body.innerText)

    The ``visited=true`` flag (just another key=value column) marks
    ``<a>`` whose ``href`` is in ``visited_urls`` -- equivalent to the
    browser's purple ``:visited`` colour for links, which JS can't
    read due to privacy restrictions, so paprika reconstructs the
    same hint server-side. Use ``"visited=true" in line`` for the
    natural Python check.

    The caller is expected to translate ``[@N]`` to
    ``[data-paprika-id="N"]`` when building action selectors; the JS
    above tags each element with the matching attribute.
    """
    js = _OUTLINE_JS.replace(
        "/*TAB_HOOKS*/",
        "true" if TAB_HOOKS_ENABLED else "false",
    )
    try:
        raw = await tab.evaluate(js)
    except Exception as e:
        return f"(could not extract outline: {e})"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return f"(outline parse error; raw: {str(raw)[:200]})"

    title = (data or {}).get("title") or ""
    items = (data or {}).get("items") or []
    body_text = (data or {}).get("bodyText") or ""
    total_items = len(items)

    # Cap items so the LLM never sees a 200-element wall of text. We keep
    # the first N in DOM order (~ visual top-to-bottom on screen); if the
    # caller needs to act on something further down it can scroll and
    # observe again.
    if total_items > MAX_OUTLINE_ITEMS:
        items = items[:MAX_OUTLINE_ITEMS]

    visited = visited_urls or set()

    lines: list[str] = []
    if title:
        lines.append(f"TITLE: {title}")
        lines.append("")
    if not items:
        lines.append("(no interactive elements found)")
    else:
        for it in items:
            # Shallow-copy so we can add the "visited" extra without
            # mutating the source dict; the JS-side outline doesn't
            # know about visited so we mix it in here.
            extra = dict(it.get("extra") or {})
            href = extra.get("href") or ""
            if href and href_in_visited(href, visited):
                extra["visited"] = "true"
            seg = [f"[@{it['id']}]", str(it.get("role") or it.get("tag") or "?")]
            text = it.get("text") or ""
            if text:
                seg.append(f'"{text}"')
            for k, v in extra.items():
                if v:
                    seg.append(f"{k}={v}")
            lines.append(" ".join(seg))
        if total_items > MAX_OUTLINE_ITEMS:
            lines.append(
                f"... ({total_items - MAX_OUTLINE_ITEMS} more elements "
                f"not shown; scroll to reveal more)"
            )
    if body_text and INCLUDE_BODY_TEXT:
        lines.append("")
        lines.append("PAGE TEXT:")
        lines.append(body_text)

    out = "\n".join(lines)
    if len(out) > MAX_AX_TREE_CHARS:
        out = out[:MAX_AX_TREE_CHARS] + "\n... (truncated)"
    return out


# ---------------------------------------------------------------------------
# Action primitives
# ---------------------------------------------------------------------------


_BRACKET_ID_RE = re.compile(r"^\s*\[@(\d+)\]\s*$")


def normalize_selector(selector: str) -> str:
    """Models routinely echo back the outline label ``[@N]`` as if it
    were a selector. Rewrite that to the actual
    ``[data-paprika-id="N"]`` form so a cosmetic mismatch doesn't cost
    us every click.
    """
    m = _BRACKET_ID_RE.match(selector or "")
    if m:
        return f'[data-paprika-id="{m.group(1)}"]'
    return selector


def short_error(e: BaseException) -> str:
    """CDP raises with a giant ExceptionDetails repr that's useless in
    history. Pull out the human-readable bit when we can.
    """
    s = str(e)
    msg_start = s.find("Failed to execute")
    if msg_start != -1:
        end = s.find("\\n", msg_start)
        return s[msg_start : end if end != -1 else msg_start + 200]
    msg_start = s.find("description=")
    if msg_start != -1:
        q1 = s.find('"', msg_start)
        q2 = s.find('"', q1 + 1)
        if q1 != -1 and q2 != -1:
            return s[q1 + 1 : q2]
    return s[:200]


async def click(tab, selector: str, log: LogFn) -> str:
    if not selector:
        return "ERR: empty selector"
    rewritten = normalize_selector(selector)
    if rewritten != selector:
        log(f"  [agent] click: rewrote {selector!r} -> {rewritten!r}")
        selector = rewritten
    # querySelector is the source of truth -- it handles the
    # [data-paprika-id="N"] form we lean on, and it's the same matcher
    # the LLM is asked to think in.
    js = (
        "(()=>{try{const el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return 'NO_MATCH'; el.click(); return 'OK';}"
        "catch(e){return 'ERR: '+e.message;}})()"
    )
    try:
        result = await tab.evaluate(js)
    except Exception as e:
        result = f"ERR: {short_error(e)}"
    if result != "OK":
        log(f"  [agent] click {selector!r}: {result}")
    await asyncio.sleep(ACTION_SETTLE_S)
    return result


async def fill(tab, selector: str, value: str, log: LogFn) -> str:
    """Set the value of an ``<input>``/``<textarea>``/contenteditable.

    Playwright's ``page.fill(selector, value)`` shape -- one call sets
    the value and fires ``input``/``change`` events. For per-character
    typing semantics use ``press_key`` in a loop.
    """
    if not selector:
        return "ERR: empty selector"
    rewritten = normalize_selector(selector)
    if rewritten != selector:
        log(f"  [agent] fill: rewrote {selector!r} -> {rewritten!r}")
        selector = rewritten
    js = (
        "(()=>{try{const el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return 'NO_MATCH'; el.focus();"
        "if('value' in el){el.value=" + json.dumps(value) + ";}"
        "else{el.innerText=" + json.dumps(value) + ";}"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "return 'OK';}catch(e){return 'ERR: '+e.message;}})()"
    )
    try:
        result = await tab.evaluate(js)
    except Exception as e:
        result = f"ERR: {short_error(e)}"
    if result != "OK":
        log(f"  [agent] fill {selector!r}: {result}")
    await asyncio.sleep(ACTION_SETTLE_S)
    return result


# W3C key name -> (CDP "code" string, Windows virtual key code).
#
# CDP Input.dispatchKeyEvent needs more than just ``key`` to reach the
# browser's text-editing handlers for special keys: ``code`` (the
# physical-position name) AND ``windows_virtual_key_code`` (the
# legacy keycode the editor watches) must both be set, otherwise
# Backspace / arrows / Ctrl+A and friends fire ``keydown`` events on
# the DOM but the browser's built-in text editor ignores them.
#
# Enter / Space happen to work without the keycode because Chrome's
# form-submit path uses the ``key`` string directly, which is why
# our earlier press_key seemed fine for those alone.
#
# Single printable letters (``"a"``, ``"A"``) are normalised at call
# time: code becomes ``KeyA``, keycode becomes 65, regardless of
# case. Modifier+letter combos (Ctrl+A) need this so the editor sees
# a real "Select All" gesture.
_SPECIAL_KEY_CODES: dict[str, tuple[str, int]] = {
    "Backspace": ("Backspace", 8),
    "Tab": ("Tab", 9),
    "Enter": ("Enter", 13),
    "Return": ("Enter", 13),
    "Shift": ("ShiftLeft", 16),
    "Control": ("ControlLeft", 17),
    "Alt": ("AltLeft", 18),
    "Pause": ("Pause", 19),
    "CapsLock": ("CapsLock", 20),
    "Escape": ("Escape", 27),
    " ": ("Space", 32),
    "Space": ("Space", 32),
    "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34),
    "End": ("End", 35),
    "Home": ("Home", 36),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39),
    "ArrowDown": ("ArrowDown", 40),
    "Insert": ("Insert", 45),
    "Delete": ("Delete", 46),
    "Meta": ("MetaLeft", 91),
    "F1": ("F1", 112),
    "F2": ("F2", 113),
    "F3": ("F3", 114),
    "F4": ("F4", 115),
    "F5": ("F5", 116),
    "F6": ("F6", 117),
    "F7": ("F7", 118),
    "F8": ("F8", 119),
    "F9": ("F9", 120),
    "F10": ("F10", 121),
    "F11": ("F11", 122),
    "F12": ("F12", 123),
}


def _resolve_key_payload(key: str) -> dict:
    """Build the CDP dispatch_key_event kwargs for ``key``.

    Returns a dict with ``key`` + (when applicable) ``code`` and
    ``windows_virtual_key_code``. The caller adds type_/modifiers.
    Unknown keys fall through with just ``key`` set; that matches
    the prior behaviour for arbitrary strings.
    """
    if not key:
        return {}
    # Single ASCII letter -> KeyA / KeyB / ... + keycode (65-90).
    if len(key) == 1 and key.isascii() and key.isalpha():
        upper = key.upper()
        return {
            "key": key,
            "code": f"Key{upper}",
            "windows_virtual_key_code": ord(upper),
        }
    # Single ASCII digit -> Digit0 / Digit1 / ... + keycode (48-57).
    if len(key) == 1 and key.isascii() and key.isdigit():
        return {
            "key": key,
            "code": f"Digit{key}",
            "windows_virtual_key_code": ord(key),
        }
    # Special key table.
    special = _SPECIAL_KEY_CODES.get(key)
    if special:
        code, kcode = special
        return {
            "key": key,
            "code": code,
            "windows_virtual_key_code": kcode,
        }
    # Anything else: just pass the raw key string. Chrome will do its
    # best; most one-shot symbol keys (``"+"``, ``"."``, etc.) work
    # via insertText / dispatch_key_event with key alone.
    return {"key": key}


# Modifier name -> CDP modifier bitfield.
# CDP Input.dispatchKeyEvent.modifiers is a bitmask:
#   Alt   = 1
#   Ctrl  = 2
#   Meta  = 4   (Command on macOS, Windows key on Win)
#   Shift = 8
# Accept the W3C names + common shorthands so LLM-generated code that
# writes "Cmd" / "Win" / "Control" / "Option" doesn't fall through.
_MODIFIER_BITS = {
    "alt": 1,
    "option": 1,
    "opt": 1,
    "ctrl": 2,
    "control": 2,
    "meta": 4,
    "cmd": 4,
    "command": 4,
    "win": 4,
    "super": 4,
    "shift": 8,
}


def _parse_key_combo(key: str) -> tuple[str, int]:
    """Split a combo string like ``"Ctrl+Shift+A"`` into ``("A", 2|8)``.

    Plain key strings (``"Enter"``, ``"a"``, ``"Backspace"``) come back
    unchanged with modifiers=0. Unknown modifier names are silently
    ignored (we'd rather press the bare key than refuse). Trailing /
    leading whitespace is tolerated.
    """
    if not key:
        return ("", 0)
    parts = [p.strip() for p in key.split("+") if p.strip()]
    if not parts:
        return ("", 0)
    *mod_parts, real_key = parts
    bits = 0
    for m in mod_parts:
        bits |= _MODIFIER_BITS.get(m.lower(), 0)
    return (real_key, bits)


async def press_key(
    tab,
    key: str,
    log: LogFn,
    *,
    count: int = 1,
    modifiers: int | None = None,
    inter_press_delay_s: float = 0.05,
) -> str:
    """Dispatch CDP keyDown+keyUp pairs.

    ``key`` accepts either a plain W3C key name (``"Enter"``, ``"Tab"``,
    ``"ArrowDown"``, ``"Backspace"``) or a combo string like
    ``"Ctrl+A"`` / ``"Ctrl+Shift+T"``. When ``modifiers`` is also
    provided explicitly it OR's with anything parsed from the combo
    string.

    ``count`` repeats the keyDown+keyUp pair N times with
    ``inter_press_delay_s`` between repeats (default 50ms -- short
    enough that the page feels a "rapid" sequence, long enough that
    auto-repeat-suppressing scripts still notice each press).
    A single ``ACTION_SETTLE_S`` wait runs once at the end so the
    overall settle behaviour matches the rest of browser_ops.
    """
    if not key:
        return "ERR: empty key"
    real_key, combo_bits = _parse_key_combo(key)
    if not real_key:
        return "ERR: empty key after combo parse"
    bits = (modifiers or 0) | combo_bits
    n = max(1, int(count))
    base_payload = _resolve_key_payload(real_key)
    try:
        for i in range(n):
            kwargs: dict = {"type_": "keyDown", **base_payload}
            if bits:
                kwargs["modifiers"] = bits
            await tab.send(cdp.input_.dispatch_key_event(**kwargs))
            kwargs["type_"] = "keyUp"
            await tab.send(cdp.input_.dispatch_key_event(**kwargs))
            if i + 1 < n and inter_press_delay_s > 0:
                await asyncio.sleep(inter_press_delay_s)
    except Exception as e:
        log(f"  [agent] press_key {key!r} (x{n}, modifiers={bits}): {e}")
        await asyncio.sleep(ACTION_SETTLE_S)
        return f"ERR: {e}"
    await asyncio.sleep(ACTION_SETTLE_S)
    return "OK"


async def type_text(tab, text: str, log: LogFn) -> str:
    """Insert ``text`` into whatever is currently focused.

    Uses ``Input.insertText`` (CDP) which is the "paste a string"
    primitive: it fires the same ``input`` / ``change`` events the page
    would see from real typing without simulating per-character
    keyDowns. Works for ``<input>``, ``<textarea>``, contenteditable,
    and even some canvas-based editors. Does NOT change focus -- click
    the target first if needed.

    Faster + safer than per-character ``press_key`` loops because it
    doesn't have to map every character to a virtual key code (which
    is unreliable for non-ASCII text, dead keys, IME composition, etc.).
    """
    if not text:
        return "ERR: empty text"
    try:
        await tab.send(cdp.input_.insert_text(text=text))
    except Exception as e:
        log(f"  [agent] type_text ({len(text)} chars): {e}")
        await asyncio.sleep(ACTION_SETTLE_S)
        return f"ERR: {e}"
    await asyncio.sleep(ACTION_SETTLE_S)
    return "OK"


async def scroll(tab, direction: str, pixels: int, log: LogFn) -> str:
    """Scroll the current viewport by ``pixels`` in ``direction``
    (``"up"``/``"down"``/``"left"``/``"right"``). Unknown directions
    are treated as ``"down"``.
    """
    dx, dy = 0, 0
    if direction == "down":
        dy = pixels
    elif direction == "up":
        dy = -pixels
    elif direction == "right":
        dx = pixels
    elif direction == "left":
        dx = -pixels
    js = f"window.scrollBy({dx}, {dy}); 'OK'"
    try:
        await tab.evaluate(js)
    except Exception as e:
        log(f"  [agent] scroll {direction} {pixels}: {e}")
        await asyncio.sleep(ACTION_SETTLE_S)
        return f"ERR: {e}"
    await asyncio.sleep(ACTION_SETTLE_S)
    return "OK"


# ---------------------------------------------------------------------------
# Pixel-coordinate primitives (vision agents)
#
# CogAgent and other GUI VLMs emit (x, y) instead of CSS selectors, so we
# need to drive the page via CDP Input.dispatchMouseEvent rather than
# document.querySelector(...).click(). These intentionally live next to
# their selector-based siblings so any agent loop can pick whichever
# action surface matches its model.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Human-like mouse movement
#
# Instead of teleporting the cursor to the target, we trace a Bézier curve
# with randomised control points, apply ease-in-out timing, and add ±1-2 px
# jitter along the path. The result is a trajectory that passes the mouse-
# movement heuristics in Cloudflare Turnstile, reCAPTCHA v3, and similar
# bot-detection systems.
#
# Env knobs:
#   PAPRIKA_HUMAN_MOUSE=0        → disable (teleport, legacy behaviour)
#   PAPRIKA_MOUSE_STEPS=30       → waypoints per move (more = smoother)
#   PAPRIKA_MOUSE_DURATION_MS=250 → total travel time in ms
# ---------------------------------------------------------------------------

_HUMAN_MOUSE_ENABLED = os.environ.get("PAPRIKA_HUMAN_MOUSE", "1") != "0"
_MOUSE_STEPS = int(os.environ.get("PAPRIKA_MOUSE_STEPS", "30"))
_MOUSE_DURATION_MS = int(os.environ.get("PAPRIKA_MOUSE_DURATION_MS", "250"))

# Module-level "last known" cursor position. Reset per tab is unnecessary
# because we only use this to calculate the starting point of the next
# move; the worst case is a single wrong starting point on the very first
# action of a fresh tab, which looks fine (the first move always starts
# from somewhere "off-screen" in practice).
_last_mouse: dict[str, tuple[int, int]] = {}  # tab_id -> (x, y)


def _bezier_curve(
    start: tuple[int, int],
    end: tuple[int, int],
    steps: int,
) -> list[tuple[int, int]]:
    """Generate waypoints along a cubic Bézier curve from *start* to *end*.

    Two randomised control points are placed at roughly 1/3 and 2/3 of the
    way between start and end, with lateral jitter proportional to the
    distance — this produces the slight S-curve that real hand movements
    exhibit.
    """
    sx, sy = start
    ex, ey = end
    dist = math.hypot(ex - sx, ey - sy)
    # Lateral jitter: bigger moves get bigger curves (capped at 120 px).
    spread = min(120.0, dist * 0.3)

    # Control points at ~1/3 and ~2/3 along the direct line, offset
    # perpendicularly by a random amount.
    dx, dy = ex - sx, ey - sy
    # Perpendicular unit vector (rotate 90°).
    if dist < 1:
        return [end]
    px, py = -dy / dist, dx / dist

    off1 = random.uniform(-spread, spread)
    off2 = random.uniform(-spread, spread)
    c1x = sx + dx * 0.33 + px * off1
    c1y = sy + dy * 0.33 + py * off1
    c2x = sx + dx * 0.67 + px * off2
    c2y = sy + dy * 0.67 + py * off2

    points: list[tuple[int, int]] = []
    for i in range(steps + 1):
        t = i / steps
        # ease-in-out: slow at start/end, fast in the middle.
        t = _ease_in_out(t)
        # De Casteljau (cubic Bézier).
        u = 1 - t
        bx = u**3 * sx + 3 * u**2 * t * c1x + 3 * u * t**2 * c2x + t**3 * ex
        by = u**3 * sy + 3 * u**2 * t * c1y + 3 * u * t**2 * c2y + t**3 * ey
        # Micro-jitter: ±1-2 px random noise (skip the endpoints).
        if 0 < i < steps:
            bx += random.uniform(-1.5, 1.5)
            by += random.uniform(-1.5, 1.5)
        points.append((int(round(bx)), int(round(by))))
    return points


def _ease_in_out(t: float) -> float:
    """Sinusoidal ease-in-out: ``0→0``, ``0.5→0.5``, ``1→1``."""
    return 0.5 * (1 - math.cos(math.pi * t))


async def _human_move_to(tab, x: int, y: int) -> None:
    """Trace a human-like Bézier path from the last known position to (x, y).

    Each waypoint fires a CDP ``Input.dispatchMouseEvent("mouseMoved")``.
    The total travel time is ``_MOUSE_DURATION_MS`` with the inter-step
    delay spread across the waypoints (typically 6-10 ms each for 30 steps).
    """
    tab_id = str(id(tab))
    start = _last_mouse.get(tab_id, (x // 2, y + 80))  # default: below-centre
    _last_mouse[tab_id] = (x, y)

    none_enum = _mouse_button("none")
    points = _bezier_curve(start, (x, y), _MOUSE_STEPS)
    delay = _MOUSE_DURATION_MS / 1000.0 / max(len(points), 1)

    for px, py in points:
        await tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mouseMoved",
                x=px,
                y=py,
                button=none_enum,
            )
        )
        if delay > 0:
            await asyncio.sleep(delay)


def _mouse_button(name: str):
    """Resolve a ``"left"`` / ``"right"`` / ``"middle"`` string to the
    matching ``cdp.input_.MouseButton`` enum member.

    nodriver's CDP wrapper types ``button`` as ``Optional[MouseButton]``;
    passing a raw string blows up later in the JSON serializer with
    ``'str' object has no attribute 'to_json'`` because the encoder
    assumes a typed enum. Same bridging dance we do for CookieParam.
    """
    mb = cdp.input_.MouseButton
    return {
        "left": mb.LEFT,
        "right": mb.RIGHT,
        "middle": mb.MIDDLE,
        "none": mb.NONE,
    }.get((name or "left").lower(), mb.LEFT)


async def click_at(
    tab,
    x: int,
    y: int,
    log: LogFn,
    *,
    button: str = "left",
    click_count: int = 1,
) -> str:
    """Issue a mouse press + release at ``(x, y)``.

    Goes through CDP so anything an actual user click would trigger
    (event listeners, focus changes, navigation) also fires. ``button``
    accepts ``left`` / ``middle`` / ``right`` strings (we map them to
    nodriver's MouseButton enum internally); pass ``click_count=2``
    for a double-click.
    """
    btn_enum = _mouse_button(button)
    none_enum = _mouse_button("none")
    try:
        # Trace a human-like Bézier path to the target so bot-detection
        # systems (Turnstile, reCAPTCHA v3) see a realistic mousemove
        # trajectory. Falls back to a single teleport-style mouseMoved
        # when PAPRIKA_HUMAN_MOUSE=0 or on very short distances.
        if _HUMAN_MOUSE_ENABLED:
            await _human_move_to(tab, x, y)
        else:
            await tab.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mouseMoved",
                    x=x,
                    y=y,
                    button=none_enum,
                )
            )
        await tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mousePressed",
                x=x,
                y=y,
                button=btn_enum,
                click_count=click_count,
            )
        )
        await tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mouseReleased",
                x=x,
                y=y,
                button=btn_enum,
                click_count=click_count,
            )
        )
    except Exception as e:
        log(f"  [vagent] click_at ({x},{y}): {short_error(e)}")
        return f"ERR: {short_error(e)}"
    await asyncio.sleep(ACTION_SETTLE_S)
    return "OK"


async def hover_at(tab, x: int, y: int, log: LogFn) -> str:
    """Move the cursor to ``(x, y)`` without pressing.

    Useful for menus that expand on hover before they can be clicked.
    """
    try:
        if _HUMAN_MOUSE_ENABLED:
            await _human_move_to(tab, x, y)
        else:
            none_enum = _mouse_button("none")
            await tab.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mouseMoved",
                    x=x,
                    y=y,
                    button=none_enum,
                )
            )
    except Exception as e:
        log(f"  [vagent] hover_at ({x},{y}): {short_error(e)}")
        return f"ERR: {short_error(e)}"
    await asyncio.sleep(ACTION_SETTLE_S)
    return "OK"


async def type_at(
    tab,
    x: int,
    y: int,
    text: str,
    log: LogFn,
) -> str:
    """Click at ``(x, y)`` to focus a field, then type ``text``.

    Uses ``Input.insertText`` rather than per-character keyDown/Up so
    IME composition + autocomplete UIs see the text appear in one shot
    (matches paste behaviour). For per-character typing semantics, call
    ``click_at`` then ``press_key`` in a loop.
    """
    if not text:
        return "ERR: empty text"
    # 1) Click to focus.
    click_status = await click_at(tab, x, y, log)
    if click_status != "OK":
        return click_status
    # 2) Insert text.
    try:
        await tab.send(cdp.input_.insert_text(text=text))
    except Exception as e:
        log(f"  [vagent] type_at ({x},{y}) insert: {short_error(e)}")
        return f"ERR: {short_error(e)}"
    await asyncio.sleep(ACTION_SETTLE_S)
    return "OK"


async def wheel_at(
    tab,
    x: int,
    y: int,
    delta_x: int,
    delta_y: int,
    log: LogFn,
) -> str:
    """Dispatch a mouse-wheel event at ``(x, y)``.

    Positive ``delta_y`` scrolls down (matches browser convention).
    CogAgent emits SCROLL_DOWN with a ``step_count``; the caller
    converts that to pixels (one "step" = roughly one notch, ~100px
    on Chrome).
    """
    none_enum = _mouse_button("none")
    try:
        await tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mouseWheel",
                x=x,
                y=y,
                button=none_enum,
                delta_x=delta_x,
                delta_y=delta_y,
            )
        )
    except Exception as e:
        log(f"  [vagent] wheel_at ({x},{y}) d=({delta_x},{delta_y}): {short_error(e)}")
        return f"ERR: {short_error(e)}"
    await asyncio.sleep(ACTION_SETTLE_S)
    return "OK"


# ---------------------------------------------------------------------------
# Vision-agent action executor
# ---------------------------------------------------------------------------


# How many viewport pixels per CogAgent "step_count" notch. The model
# was trained on desktop browsers where one notch ~= 100px; we keep that
# default but expose it via env so an operator can dial it for touchpads
# that send finer wheel events.
VISION_WHEEL_STEP_PX = int(os.environ.get("VISION_WHEEL_STEP_PX", "100"))


async def execute_vision_action(
    tab,
    action: dict,
    log: LogFn,
    *,
    viewport_width: int,
    viewport_height: int,
) -> str:
    """Translate one CogAgent ParsedAction dict into a CDP call.

    ``action`` must follow the shape emitted by ``cogagent_service``:
    ``kind`` + optional ``box`` (with x1/y1/x2/y2 keys, pixel space) +
    ``text`` / ``key`` / ``step_count`` / ``name``. ``end`` and
    ``unknown`` are NOT executed here -- the caller handles them
    (loop break, retry, etc.).

    Returns the same short status strings as ``execute()``.
    """
    kind = action.get("kind") or "unknown"

    # Resolve target point from box (clamped to viewport so a stray
    # off-screen coord doesn't pass through to CDP).
    def _xy() -> tuple[int, int] | None:
        box = action.get("box")
        if not box:
            return None
        cx = (int(box["x1"]) + int(box["x2"])) // 2
        cy = (int(box["y1"]) + int(box["y2"])) // 2
        cx = max(0, min(viewport_width - 1, cx))
        cy = max(0, min(viewport_height - 1, cy))
        return cx, cy

    if kind in ("click", "double_click", "right_click"):
        pt = _xy()
        if pt is None:
            return "ERR: missing box for click"
        button = "right" if kind == "right_click" else "left"
        click_count = 2 if kind == "double_click" else 1
        return await click_at(tab, pt[0], pt[1], log, button=button, click_count=click_count)

    if kind in ("hover", "long_press"):
        # CogAgent emits LONG_PRESS for touch-style sustained taps;
        # on a desktop browser the closest equivalent is a hover (mouse
        # over for context menus), so we treat them the same. Real
        # touch emulation would need Input.dispatchTouchEvent; not
        # worth the complexity for desktop crawls.
        pt = _xy()
        if pt is None:
            return "ERR: missing box for hover"
        return await hover_at(tab, pt[0], pt[1], log)

    if kind == "type":
        pt = _xy()
        if pt is None:
            return "ERR: missing box for type"
        text = action.get("text") or ""
        return await type_at(tab, pt[0], pt[1], text, log)

    if kind == "press_key":
        # CogAgent emits PRESS_KEY(key='Enter') / sometimes the model
        # writes combos like "Ctrl+L" into the key string. press_key
        # parses both. count / modifiers may also be present when this
        # call originates from a non-CogAgent caller.
        return await press_key(
            tab,
            action.get("key") or "",
            log,
            count=int(action.get("count") or 1),
            modifiers=action.get("modifiers"),
        )

    if kind in ("scroll_up", "scroll_down", "scroll_left", "scroll_right"):
        # Scroll origin defaults to the viewport centre when the model
        # didn't ground the action to a specific box (some pages
        # scroll the body, not a sub-region; CogAgent then emits
        # box=[[0,0,1000,1000]] which after pixel mapping = the full
        # viewport).
        pt = _xy() or (viewport_width // 2, viewport_height // 2)
        steps = int(action.get("step_count") or 1)
        px = max(1, steps) * VISION_WHEEL_STEP_PX
        if kind == "scroll_up":
            dx, dy = 0, -px
        elif kind == "scroll_down":
            dx, dy = 0, px
        elif kind == "scroll_left":
            dx, dy = -px, 0
        else:  # scroll_right
            dx, dy = px, 0
        return await wheel_at(tab, pt[0], pt[1], dx, dy, log)

    if kind == "wait":
        # CogAgent's WAIT() takes no argument; one settle period is
        # plenty (the loop will re-observe right after).
        await asyncio.sleep(ACTION_SETTLE_S)
        return "OK"

    if kind == "launch":
        # Mobile-only opcode; we get it occasionally when CogAgent
        # mis-identifies a desktop browser as mobile. No-op + log.
        log(f"  [vagent] LAUNCH({action.get('name')!r}) ignored (desktop)")
        return "OK"

    # end / unknown / anything else: caller handles.
    return "OK"


async def navigate(tab, url: str, log: LogFn) -> str:
    """Load ``url`` in the current tab. Returns ``"OK"`` once the
    initial response arrives and ``NAVIGATION_SETTLE_S`` has elapsed.

    Uses the low-level CDP page.navigate command rather than nodriver's
    ``tab.get()`` so the tab's CDP session id stays stable and any
    network.* listeners attached for session-mode asset capture remain
    bound. ``tab.get()`` can re-attach to a fresh target which silently
    detaches the listeners.
    """
    if not url:
        return "ERR: empty url"
    try:
        await tab.send(cdp.page.navigate(url))
    except Exception as e:
        log(f"  [agent] navigate {url!r}: {e}")
        return f"ERR: navigate failed: {e}"
    await asyncio.sleep(NAVIGATION_SETTLE_S)
    return "OK"


async def back(tab, log: LogFn) -> str:
    """Equivalent to pressing the browser's Back button
    (``window.history.back()``). Always returns ``"OK"`` -- the
    caller's loop doesn't bail in any boundary case.

    Defensive against two distinct CDP-level edges:

      * **Already at the start of history.** ``window.history.length``
        doesn't help here (it counts total entries, not "remaining
        back hops"), so we query CDP's ``Page.getNavigationHistory``
        and skip the back call when ``currentIndex == 0``. Returns
        OK without waiting -- no navigation to settle for.

      * **CDP eval cancelled mid-navigation** (``-32000 Inspected
        target navigated or closed``). Raised when
        ``window.history.back()`` triggers a real navigation whose
        Page.frameStartedLoading event races against the JS eval.
        The navigation succeeded; the eval just got cut short.
        Logged for visibility and treated as OK so the macro / agent
        loop continues.
    """
    # Pre-check via CDP whether there's any prior entry.
    # ``Page.getNavigationHistory`` in nodriver returns a
    # ``(current_index, entries)`` tuple, NOT an object with
    # attributes -- accessing .current_index returns nothing.
    try:
        hist = await tab.send(cdp.page.get_navigation_history())
        cur_idx: int | None = None
        if isinstance(hist, tuple) and len(hist) >= 1:
            cur_idx = int(hist[0])
        elif isinstance(hist, dict):
            cur_idx = int(hist.get("currentIndex", -1))
        elif hasattr(hist, "current_index"):
            cur_idx = int(hist.current_index)
        if isinstance(cur_idx, int) and cur_idx <= 0:
            log(f"  [agent] back: at history start (currentIndex={cur_idx}), no-op")
            return "OK"
    except Exception as e:
        # If we can't query history, fall through and just try the
        # back -- the JS-eval catch below covers the rest.
        log(
            f"  [agent] back: getNavigationHistory probe failed "
            f"({type(e).__name__}); attempting back anyway"
        )

    try:
        await tab.evaluate("window.history.back()")
    except Exception as e:
        msg = str(e)
        if "-32000" in msg or "navigated or closed" in msg.lower():
            log("  [agent] back: navigation in progress (eval cancelled, treating as OK)")
            await asyncio.sleep(NAVIGATION_SETTLE_S)
            return "OK"
        log(f"  [agent] back: {e}")
        return f"ERR: back failed: {short_error(e)}"
    await asyncio.sleep(NAVIGATION_SETTLE_S)
    return "OK"


async def forward(tab, log: LogFn) -> str:
    """Equivalent to pressing the browser's Forward button
    (``window.history.forward()``). Symmetric counterpart to
    :func:`back` -- same defensive handling of the two CDP edges
    (already at the end of history, eval cancelled mid-navigation).
    """
    # Pre-check: skip if there's no forward entry to navigate to.
    try:
        hist = await tab.send(cdp.page.get_navigation_history())
        cur_idx: int | None = None
        n_entries: int | None = None
        if isinstance(hist, tuple) and len(hist) >= 2:
            cur_idx = int(hist[0])
            try:
                n_entries = len(hist[1])
            except Exception:
                n_entries = None
        elif isinstance(hist, dict):
            cur_idx = int(hist.get("currentIndex", -1))
            try:
                n_entries = len(hist.get("entries") or [])
            except Exception:
                n_entries = None
        elif hasattr(hist, "current_index"):
            cur_idx = int(hist.current_index)
            try:
                n_entries = len(getattr(hist, "entries", []) or [])
            except Exception:
                n_entries = None
        if isinstance(cur_idx, int) and isinstance(n_entries, int) and cur_idx >= n_entries - 1:
            log(
                f"  [agent] forward: at history end "
                f"(currentIndex={cur_idx}, entries={n_entries}), no-op"
            )
            return "OK"
    except Exception as e:
        log(
            f"  [agent] forward: getNavigationHistory probe failed "
            f"({type(e).__name__}); attempting forward anyway"
        )

    try:
        await tab.evaluate("window.history.forward()")
    except Exception as e:
        msg = str(e)
        if "-32000" in msg or "navigated or closed" in msg.lower():
            log("  [agent] forward: navigation in progress (eval cancelled, treating as OK)")
            await asyncio.sleep(NAVIGATION_SETTLE_S)
            return "OK"
        log(f"  [agent] forward: {e}")
        return f"ERR: forward failed: {short_error(e)}"
    await asyncio.sleep(NAVIGATION_SETTLE_S)
    return "OK"


def _entry_url(e) -> str:
    """Best-effort url extraction from a nodriver NavigationEntry.
    The class shape varies (dataclass / dict / tuple / object), so
    we probe multiple access patterns."""
    if e is None:
        return ""
    if hasattr(e, "url"):
        v = getattr(e, "url")
        if v:
            return str(v)
    if isinstance(e, dict):
        return str(e.get("url") or "")
    if isinstance(e, (tuple, list)) and len(e) >= 2:
        # Common ordering: (id, url, ...) -- best-effort.
        try:
            return str(e[1])
        except Exception:
            return ""
    return ""


async def exists(tab, selector: str, log: LogFn) -> tuple[str, bool]:
    """Check whether a CSS selector currently matches at least one
    element on the page. Cheap, deterministic, doesn't touch any LLM.

    Returns ``("OK", True/False)`` on success, ``("ERR: ...", False)``
    on CDP eval failure. The boolean travels back to the client as the
    ``result`` field of the session-action reply.
    """
    if not selector:
        return ("ERR: exists failed: empty selector", False)
    import json as _json

    js = f"!!document.querySelector({_json.dumps(selector)})"
    try:
        raw = await tab.evaluate(js)
    except Exception as e:
        log(f"  [agent] exists: eval failed: {e}")
        return (f"ERR: exists failed: {short_error(e)}", False)
    return ("OK", bool(raw))


async def history_first(tab, log: LogFn) -> str:
    """Jump to the first **user-visible** page in this session.

    Chrome's actual history entry 0 is usually ``about:blank`` (the
    tab's start page before ``initial_url`` was applied). What the
    operator means by "the first page" is the first real navigation,
    which lives at index 1 in that case.

    Algorithm:
      1. Read ``Page.getNavigationHistory`` for current_index + entries
      2. Pick target_index = first entries[i] whose url is not
         ``about:*`` (about:blank / about:newtab / ...).
         Falls back to 0 if every entry is about:* somehow.
      3. ``window.history.go(-(current_index - target_index))``.
         No-op if we're already at target.

    Uses ``history.go(-N)`` rather than CDP
    ``Page.navigateToHistoryEntry`` because the NavigationEntry
    .id field name / type varies between nodriver versions.
    history.go() is universally supported and preserves the
    forward-history (page.forward() still works after).
    """
    cur_idx: int | None = None
    entries: list = []
    try:
        hist = await tab.send(cdp.page.get_navigation_history())
        if isinstance(hist, tuple) and len(hist) >= 2:
            cur_idx = int(hist[0])
            entries = list(hist[1])
        elif isinstance(hist, dict):
            cur_idx = int(hist.get("currentIndex", -1))
            entries = list(hist.get("entries") or [])
        elif hasattr(hist, "current_index"):
            cur_idx = int(hist.current_index)
            entries = list(getattr(hist, "entries", []) or [])
    except Exception as e:
        log(
            f"  [agent] history_first: getNavigationHistory probe failed "
            f"({type(e).__name__}); will fall back to history.go(-99)"
        )

    # Pick the first entry whose URL is a real page, not an internal
    # placeholder like about:blank.
    target_idx = 0
    if entries:
        for i, e in enumerate(entries):
            u = _entry_url(e)
            if u and not u.startswith("about:") and not u.startswith("chrome:"):
                target_idx = i
                break

    if isinstance(cur_idx, int) and cur_idx <= target_idx:
        log(
            f"  [agent] history_first: already at target "
            f"(currentIndex={cur_idx}, target={target_idx}), no-op"
        )
        return "OK"

    # cur_idx may be None if the probe failed -- fall back to going
    # back a lot; history.go silently clamps at the earliest entry.
    if isinstance(cur_idx, int):
        n_back = cur_idx - target_idx
    else:
        n_back = 99
    try:
        await tab.evaluate(f"window.history.go(-{n_back})")
    except Exception as e:
        msg = str(e)
        if "-32000" in msg or "navigated or closed" in msg.lower():
            log("  [agent] history_first: navigation in progress (eval cancelled, treating as OK)")
            await asyncio.sleep(NAVIGATION_SETTLE_S)
            return "OK"
        log(f"  [agent] history_first: {e}")
        return f"ERR: history_first failed: {short_error(e)}"
    await asyncio.sleep(NAVIGATION_SETTLE_S)
    return "OK"


# ``${name}`` placeholder substitution. Variables travel from the SDK
# (page.fill / page.type / page.agent with variables={...}) through the
# hub to the worker untouched, and ONLY this function (called at the
# CDP edge inside execute()) ever sees the real values. The worker
# never echoes them back to the hub log or to any LLM prompt -- the
# placeholder name stays visible everywhere except CDP.
_VAR_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _apply_variables(text: str, vars_map: Optional[dict]) -> str:
    if not text or not vars_map or "${" not in text:
        return text
    def _repl(m: "re.Match[str]") -> str:
        return str(vars_map.get(m.group(1), m.group(0)))
    return _VAR_PLACEHOLDER_RE.sub(_repl, text)


# ---------------------------------------------------------------------------
# Playwright-compatible HTTP Response capture for navigation actions.
#
# Plain CDP page.navigate(...) doesn't return the document's HTTP status,
# so page.goto("https://example.com/missing") would happily report ``OK``
# even when the server replied 404. We bridge the gap by hooking
# Network.requestWillBeSent + Network.responseReceived around the
# navigation, picking out the first main-document request, and reading
# its response.status / response.headers off the CDP event.
#
# Best-effort: if no main-frame Document response arrives within
# ``timeout_s`` (cached navigation, immediate aborts, ...), we return
# ``response=None`` and the caller treats it as "status unknown" rather
# than failing the action.
# ---------------------------------------------------------------------------


def _resource_type_is_document(event) -> bool:
    """Whether a Network.requestWillBeSent event is for a Document.

    Tries multiple attribute spellings because nodriver's CDP wrapper
    has varied over releases (`type`, `type_`, plain dict). Returns
    False conservatively when nothing matches -- callers fall back to
    URL-match heuristics.
    """
    for attr in ("type", "type_", "resource_type"):
        rt = getattr(event, attr, None)
        if rt is None:
            continue
        s = str(rt).rsplit(".", 1)[-1].lower()
        if s == "document":
            return True
    return False


def _request_event_url(event) -> str:
    """Best-effort extraction of the URL from a Network.requestWillBeSent."""
    req = getattr(event, "request", None)
    if req is not None:
        url = getattr(req, "url", None)
        if url:
            return str(url)
    # Some CDP wrappers expose .request_url at the top level too.
    return str(getattr(event, "request_url", "") or "")


async def _capture_nav_response(
    tab,
    nav_coro,
    *,
    timeout_s: float = 5.0,
    expected_url: str = "",
) -> tuple[str, Optional[dict]]:
    """Execute ``nav_coro`` (an awaitable returning ``"OK"`` / ``"ERR: …"``)
    and concurrently capture the main document's HTTP response.

    Returns ``(status_str, response_dict)`` where ``response_dict`` is::

        {
          "url":         "https://example.com/path",   # final URL after redirects
          "status":      404,                          # HTTP status code
          "status_text": "Not Found",
          "ok":          False,                        # 200 <= status < 300
          "headers":     {"content-type": "text/html; charset=UTF-8", ...},
          "mime":        "text/html",
        }

    ``response_dict`` is ``None`` when the navigation produced no
    Document-type response we could correlate (cached pages, naked
    media URLs that bypass the navigation pipeline, race conditions,
    timeouts).
    """
    import sys as _sys
    captured: dict[str, Any] = {}
    doc_request_ids: set[str] = set()
    armed = {"value": False}  # toggled True right before nav_coro starts
    response_event = asyncio.Event()
    _dbg = os.environ.get("PAPRIKA_NAV_RESPONSE_DEBUG", "") in ("1", "true", "yes")
    _expected_norm = (expected_url or "").rstrip("/").lower()

    def _d(msg: str) -> None:
        if _dbg:
            print(f"[nav-resp DEBUG] {msg}", file=_sys.stderr, flush=True)

    def _url_matches_expected(url: str) -> bool:
        if not _expected_norm or not url:
            return False
        return (url or "").rstrip("/").lower() == _expected_norm

    async def _on_request(event):
        try:
            if not armed["value"]:
                # Pre-nav noise (lingering loads from before goto). Ignore.
                return
            url = _request_event_url(event)
            is_doc = _resource_type_is_document(event)
            # Three signals that this is the document we care about:
            #   1) CDP reported ResourceType=Document (preferred, when available)
            #   2) Request URL equals the URL we are navigating to
            #   3) We haven't picked a candidate yet AND this is the first
            #      request after we armed -- fallback for cached navs that
            #      skip resource-type metadata
            first_unknown = not doc_request_ids and not is_doc
            url_match = _url_matches_expected(url)
            if is_doc or url_match or first_unknown:
                doc_request_ids.add(str(event.request_id))
                _d(
                    f"REQ-MATCH is_doc={is_doc} url_match={url_match} "
                    f"first={first_unknown} url={url[:120]}"
                )
        except Exception as e:
            _d(f"_on_request raised: {e!r}")

    async def _on_response(event):
        try:
            if str(event.request_id) not in doc_request_ids:
                return
            resp = event.response
            try:
                status_code = int(getattr(resp, "status", 0) or 0)
            except Exception:
                status_code = 0
            # Headers come back as a CDP "Headers" object that behaves
            # like a dict. Normalise keys to lowercase so callers can
            # do response["headers"]["content-type"] portably.
            hdrs_raw = getattr(resp, "headers", None) or {}
            try:
                hdrs = {str(k).lower(): str(v) for k, v in hdrs_raw.items()}
            except Exception:
                hdrs = {}
            captured.update({
                "url":         getattr(resp, "url", "") or "",
                "status":      status_code,
                "status_text": getattr(resp, "status_text", "") or "",
                "ok":          200 <= status_code < 300,
                "headers":     hdrs,
                "mime":        getattr(resp, "mime_type", "") or "",
            })
            response_event.set()
        except Exception:
            pass

    handlers = getattr(tab, "handlers", None)
    if handlers is None:
        # No CDP listener surface -- run the nav anyway, just no
        # response info.
        _d("tab has no .handlers attr; falling back to no-capture")
        return await nav_coro, None
    handlers.setdefault(cdp.network.RequestWillBeSent, []).append(_on_request)
    handlers.setdefault(cdp.network.ResponseReceived, []).append(_on_response)
    _d(
        f"installed handlers; req-listeners="
        f"{len(handlers.get(cdp.network.RequestWillBeSent, []))} "
        f"resp-listeners={len(handlers.get(cdp.network.ResponseReceived, []))} "
        f"expected_url={_expected_norm!r}"
    )
    armed["value"] = True
    try:
        status_str = await nav_coro
        _d(f"nav_coro returned {status_str!r}; waiting for response event")
        try:
            await asyncio.wait_for(response_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            _d(f"timeout after {timeout_s}s; captured={captured}")
    finally:
        try:
            handlers.get(cdp.network.RequestWillBeSent, []).remove(_on_request)
        except (ValueError, AttributeError):
            pass
        try:
            handlers.get(cdp.network.ResponseReceived, []).remove(_on_response)
        except (ValueError, AttributeError):
            pass
    return status_str, (captured if captured else None)


async def install_last_response_tracker(
    tab,
    on_response_captured,
    log: LogFn | None = None,
) -> None:
    """Passive tracker that keeps ``state.last_response`` current.

    Hooks Network.requestWillBeSent + Network.responseReceived for the
    LIFETIME of the session (vs the per-call _capture_nav_response
    which runs only across a single goto/back/forward). Updates the
    session's ``last_response`` field whenever a main-document response
    arrives, no matter what triggered the navigation: page.goto, a
    click on a link, a form submit, window.location.href = ..., or a
    JS redirect.

    The session action ``kind: "last_response"`` reads back whatever
    this tracker stored most recently. SDK exposes it as
    ``page.last_response()``.

    Args:
      tab: nodriver Tab handle for the session's main page.
      on_response_captured: sync callable invoked with the response
        dict each time a new top-level document response is observed.
        The agent wires this to ``state.last_response = info``.
      log: optional logger for diagnostics.
    """
    # Mirrors _capture_nav_response's logic but runs unconditionally
    # for the session's lifetime. The first request after install
    # might not be a navigation -- a page already has resources in
    # flight at install time -- so we don't seed an initial value.
    pending_docs: dict[str, str] = {}   # request_id -> request url

    def _request_url(event) -> str:
        req = getattr(event, "request", None)
        if req is not None:
            return str(getattr(req, "url", "") or "")
        return str(getattr(event, "request_url", "") or "")

    def _is_doc(event) -> bool:
        for attr in ("type", "type_", "resource_type"):
            rt = getattr(event, attr, None)
            if rt is None:
                continue
            if str(rt).rsplit(".", 1)[-1].lower() == "document":
                return True
        return False

    async def _on_request(event):
        try:
            if not _is_doc(event):
                return
            pending_docs[str(event.request_id)] = _request_url(event)
        except Exception:
            pass

    async def _on_response(event):
        try:
            rid = str(event.request_id)
            if rid not in pending_docs:
                return
            pending_docs.pop(rid, None)
            resp = event.response
            try:
                status_code = int(getattr(resp, "status", 0) or 0)
            except Exception:
                status_code = 0
            hdrs_raw = getattr(resp, "headers", None) or {}
            try:
                hdrs = {str(k).lower(): str(v) for k, v in hdrs_raw.items()}
            except Exception:
                hdrs = {}
            info = {
                "url":         getattr(resp, "url", "") or "",
                "status":      status_code,
                "status_text": getattr(resp, "status_text", "") or "",
                "ok":          200 <= status_code < 300,
                "headers":     hdrs,
                "mime":        getattr(resp, "mime_type", "") or "",
            }
            try:
                on_response_captured(info)
            except Exception:
                if log:
                    log(
                        f"  [last-response] on_captured raised "
                        f"(ignored): {info.get('url','')[:120]}"
                    )
        except Exception:
            pass

    handlers = getattr(tab, "handlers", None)
    if handlers is None:
        if log:
            log("  [last-response] tab has no .handlers; tracker disabled")
        return
    handlers.setdefault(cdp.network.RequestWillBeSent, []).append(_on_request)
    handlers.setdefault(cdp.network.ResponseReceived, []).append(_on_response)
    if log:
        log("  [last-response] tracker installed")


async def execute_nav_with_response(
    tab, action: dict, log: LogFn,
) -> tuple[str, Optional[dict]]:
    """Variant of :func:`execute` that returns the captured HTTP response
    info for nav-kind actions (navigate / back / forward / history_first).

    For non-nav kinds the second element is always None -- callers can
    use this uniformly without branching on kind.
    """
    kind = action.get("kind")
    vars_map = action.get("variables") or None
    if kind == "navigate":
        url = _apply_variables(action.get("url") or "", vars_map)
        return await _capture_nav_response(
            tab, navigate(tab, url, log), expected_url=url,
        )
    if kind == "back":
        return await _capture_nav_response(tab, back(tab, log))
    if kind == "forward":
        return await _capture_nav_response(tab, forward(tab, log))
    if kind == "history_first":
        return await _capture_nav_response(tab, history_first(tab, log))
    # Non-nav: delegate to the existing dispatcher; no response info.
    return await execute(tab, action, log), None


async def execute(tab, action: dict, log: LogFn) -> str:
    """Translate one ParsedAction dict into a single primitive call.

    Returns a short status string (e.g. ``"OK"``, ``"NO_MATCH"``,
    ``"ERR: ..."``) so the caller can append it to its action history.
    ``capture`` and ``done`` are NOT handled here -- they require
    additional state (assets_dir, summary string) that only the agent
    loop / session API knows about.

    ``action["variables"]`` (optional): ``{name: value}`` map used to
    substitute ``${name}`` placeholders in text/selector fields right
    before the CDP call. The placeholder values are never logged.
    """
    kind = action.get("kind")
    # Pull variables once. _apply_variables is a no-op when the map is
    # absent or the string has no ${} sequences, so calling it
    # unconditionally below stays cheap.
    vars_map = action.get("variables") or None
    if kind == "click":
        sel = _apply_variables(action.get("selector") or "", vars_map)
        return await click(tab, sel, log)
    if kind == "type":
        # Two flavours under the same action kind:
        #   selector + text  -> fill the input (existing behaviour)
        #   text only        -> type_text into whatever is focused
        sel = _apply_variables(action.get("selector") or "", vars_map)
        txt = _apply_variables(action.get("text") or "", vars_map)
        if sel:
            return await fill(tab, sel, txt, log)
        return await type_text(tab, txt, log)
    if kind == "type_text":
        return await type_text(
            tab, _apply_variables(action.get("text") or "", vars_map), log,
        )
    if kind == "press_key":
        return await press_key(
            tab,
            action.get("key") or "",
            log,
            count=int(action.get("count") or 1),
            modifiers=action.get("modifiers"),
        )
    if kind == "scroll":
        amount = int(action.get("amount") or 0) or 800
        direction = action.get("direction") or "down"
        return await scroll(tab, direction, amount, log)
    if kind == "navigate":
        return await navigate(tab, action.get("url") or "", log)
    if kind == "back":
        return await back(tab, log)
    if kind == "forward":
        return await forward(tab, log)
    if kind == "history_first":
        return await history_first(tab, log)
    if kind == "wait":
        seconds = float(action.get("seconds") or 2.0)
        await asyncio.sleep(seconds)
        return "OK"
    # capture / done / unknown are handled by the caller.
    return "OK"


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def safe_label(label: str) -> str:
    label = (label or "").strip().lower()
    return re.sub(r"[^a-z0-9._-]+", "-", label).strip("-")[:60]


async def capture(
    tab,
    label: str,
    step: int,
    assets_dir: Path,
    log: LogFn,
) -> Snapshot:
    """Persist the current page state (HTML + PNG + AX tree) under
    ``assets_dir/<label>/``. Returns the :class:`Snapshot` record.
    """
    label_safe = safe_label(label) or f"capture-{step}"
    dir_path = assets_dir / label_safe
    dir_path.mkdir(parents=True, exist_ok=True)

    # 1) HTML
    try:
        html = await tab.evaluate("document.documentElement && document.documentElement.outerHTML")
    except Exception as e:
        log(f"  [agent] capture {label_safe}: failed to read HTML ({e})")
        html = ""
    html_name = f"{label_safe}.html"
    (dir_path / html_name).write_text(html or "", encoding="utf-8")

    # 2) Screenshot
    png_name = f"{label_safe}.png"
    png_path = dir_path / png_name
    try:
        result = await tab.send(cdp.page.capture_screenshot(format_="png"))
        png_bytes = base64.b64decode(result)
        png_path.write_bytes(png_bytes)
    except Exception as e:
        log(f"  [agent] capture {label_safe}: screenshot failed ({e})")
        png_path.write_bytes(b"")

    # 3) Page outline (the same indexed text we'd ship to the LLM --
    #    useful for replay/debugging "what did the model see here?").
    ax_name = f"{label_safe}.axtree.txt"
    try:
        ax = await outline(tab)
    except Exception as e:
        ax = f"(error: {e})"
    (dir_path / ax_name).write_text(ax, encoding="utf-8")

    try:
        current_url = await tab.evaluate("document.location.href")
    except Exception:
        current_url = ""

    return Snapshot(
        label=label_safe,
        step=step,
        url=current_url or "",
        html_name=html_name,
        png_name=png_name,
        axtree_name=ax_name,
    )


# ---------------------------------------------------------------------------
# CDP-level tab containment
# ---------------------------------------------------------------------------


#: MIME prefixes worth saving from a session's network traffic. Mirrors
#: core.fetcher.SAVE_MIME_PREFIXES -- we keep the two values in sync but
#: avoid the import to keep worker-side modules independent.
#:
#: video/* is intentionally EXCLUDED. Modern streaming sites emit MSE
#: / DASH fragmented MP4 (each chunk ~100-200KB, names like
#: "<video-id>_480p_h264_NNNN_<hash>_<ts>.mp4"). The passive listener
#: would save hundreds of these per video per page, flooding the
#: gallery with un-playable fragments. Real video capture flows
#: through ``page.download_video()`` (yt-dlp), which produces a
#: single playable .mp4.
SESSION_SAVE_MIME_PREFIXES = ("image/", "audio/")


# URL-extension → MIME fallback for responses with no / generic
# Content-Type header. Mirrors core.fetcher._EXT_TO_MIME -- kept inline
# to avoid pulling the whole fetcher stack into worker session paths.
# Triggered case: Cloudflare-fronted WordPress sites that serve AVIF
# without setting Content-Type, see job 4b9aff01bc6f.
_SESSION_EXT_TO_MIME = {
    "avif": "image/avif",
    "webp": "image/webp",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "bmp": "image/bmp",
    "ico": "image/x-icon",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "jxl": "image/jxl",
    "heic": "image/heic",
    "heif": "image/heif",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "opus": "audio/opus",
}


def _session_effective_mime(server_mime: str, url: str) -> str:
    """Effective MIME for the save filter. Falls back to URL extension
    when the server returned empty / generic Content-Type."""
    m = (server_mime or "").strip().lower()
    if m and m not in ("application/octet-stream", "binary/octet-stream"):
        return m
    try:
        from urllib.parse import urlparse

        path = urlparse(url).path
    except Exception:
        return ""
    if "." not in path:
        return ""
    ext = path.rsplit(".", 1)[-1].lower()
    return _SESSION_EXT_TO_MIME.get(ext, "")


def _session_filename(url: str, mime: str, fallback: str) -> str:
    """Mirror of core.fetcher._filename_from -- mint a usable filename
    from the response URL + mime. Kept inline to avoid pulling fetch's
    whole stack into the worker session path."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    name = Path(parsed.path).name or fallback
    if "." not in name:
        ext = (mime or "").split(";")[0].split("/")[-1] or "bin"
        name = f"{name}.{ext}"
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name[:180]


def _session_unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


async def install_iframe_deep_trace(tab, log: LogFn | None = None) -> bool:
    """Hook CDP ``Target.setAutoAttach`` + ``AttachedToTarget`` so every
    cross-origin iframe / popup-page child target gets its own
    ``Network.enable``. The child sessions' Network events arrive on
    the parent socket (``flatten=True``) and are routed through
    whatever handlers are already registered on the parent tab. This
    lets HLS / DASH manifest URLs from cross-origin video players
    surface in the parent tab's network log without the caller
    having to manage child connections.

    Used by:
      * install_session_asset_capture (session-mode)
      * core.fetcher.fetch_url (plain Fetch mode, when
        ``opts.download_video`` is True)

    Idempotent on the same tab — stashes
    ``tab._paprika_iframe_deep_trace_on = True`` on first install
    so repeated calls (e.g. session start + later
    page.download_video()) are no-ops.

    Returns True when freshly installed, False when already on or
    install failed.
    """
    if getattr(tab, "_paprika_iframe_deep_trace_on", False):
        return False

    _attached_target_ids: set = set()
    # Counter for raw CDP message IDs we send to sub-sessions. Start
    # above any reasonable nodriver counter; nodriver uses an
    # incrementing per-Connection itertools.count starting at 0, so
    # >1M is safely beyond collision.
    _subsession_msg_id = [10_000_000]

    # Auto-attach filter, defined ONCE here so both the main-tab
    # set_auto_attach (below) and the per-sub-session recursive
    # re-attach (inside _hook_subtarget) share the exact same shape.
    # nodriver's default filter MISSES OOPIF on Chrome 140+ (it doesn't
    # explicitly include "iframe" and Chrome treats absence as exclude).
    explicit_filter = cdp.target.TargetFilter([
        {"exclude": True, "type": "browser"},
        {"exclude": True, "type": "tab"},
        {"exclude": False, "type": "iframe"},
        {"exclude": False, "type": "page"},
        {"exclude": False, "type": "worker"},
        {"exclude": False, "type": "service_worker"},
        {"exclude": False, "type": "shared_worker"},
        {},  # catch-all for any unknown sub-target types
    ])

    async def _hook_subtarget(event):
        try:
            if not isinstance(event, cdp.target.AttachedToTarget):
                return
            ti = event.target_info
            session_id = event.session_id
            if ti.target_id in _attached_target_ids:
                return
            if ti.type_ not in ("iframe", "page"):
                # Skip browser / workers (no video) and tab targets.
                return
            if not session_id:
                if log:
                    log(
                        f"  [iframe-trace] sub-target {ti.type_} "
                        f"attached without session_id; skipping"
                    )
                return
            _attached_target_ids.add(ti.target_id)
            # Enable Network on the sub-session by sending a raw CDP
            # message through the PARENT socket with explicit
            # sessionId routing. Routing through tab.socket means the
            # iframe's Network events arrive on the SAME websocket
            # where the parent tab's handlers are registered.
            # nodriver's process_event dispatches by event type only
            # -- the sessionId field is routing metadata, not a
            # filter -- so the parent's handlers fire for sub-session
            # events too.
            try:
                import json as _json
                gen = cdp.network.enable(
                    max_total_buffer_size=128 * 1024 * 1024,
                    max_resource_buffer_size=64 * 1024 * 1024,
                    max_post_data_size=4 * 1024 * 1024,
                )
                method, *raw_params = next(gen).values()
                params = raw_params.pop() if raw_params else {}
                _subsession_msg_id[0] += 1
                msg = {
                    "id": _subsession_msg_id[0],
                    "method": method,
                    "params": params,
                    "sessionId": session_id,
                }
                ws = getattr(tab, "socket", None)
                if ws is None:
                    if log:
                        log(
                            f"  [iframe-trace] tab has no socket; "
                            f"cannot enable Network on sub-target "
                            f"{ti.type_} {ti.target_id[:12]}"
                        )
                    return
                await ws.send(_json.dumps(msg))
                if log:
                    log(
                        f"  [iframe-trace] hooked sub-target "
                        f"{ti.type_}: {(ti.url or '')[:120]}"
                    )
                # RECURSE: tell THIS sub-session to auto-attach to its
                # OWN children too. Without this, only direct children
                # of the top tab are traced -- a nested player (e.g.
                # supjav -> sptvp/supremejav iframe -> inner video
                # iframe whose HLS/MP4 stream is the real content) stays
                # invisible because its AttachedToTarget never fires.
                # flatten=True keeps the grandchildren's events on the
                # SAME parent socket, so _hook_subtarget fires again for
                # them and the trace recurses to arbitrary depth.
                # _attached_target_ids dedups, so re-attaches are cheap.
                try:
                    gen2 = cdp.target.set_auto_attach(
                        auto_attach=True,
                        wait_for_debugger_on_start=False,
                        flatten=True,
                        filter_=explicit_filter,
                    )
                    method2, *raw_params2 = next(gen2).values()
                    params2 = raw_params2.pop() if raw_params2 else {}
                    _subsession_msg_id[0] += 1
                    msg2 = {
                        "id": _subsession_msg_id[0],
                        "method": method2,
                        "params": params2,
                        "sessionId": session_id,
                    }
                    await ws.send(_json.dumps(msg2))
                    if log:
                        log(
                            f"  [iframe-trace] recursed setAutoAttach "
                            f"into {ti.type_} {ti.target_id[:12]}"
                        )
                except Exception as e:
                    if log:
                        log(
                            f"  [iframe-trace] recursive setAutoAttach "
                            f"on sub-target {ti.type_} failed "
                            f"(non-fatal): {type(e).__name__}: {e}"
                        )

                # Inject the url-capture hook into THIS (possibly
                # cross-origin) sub-frame.  Same-origin iframes already
                # get the hook via the top tab's
                # addScriptToEvaluateOnNewDocument, but a cross-origin
                # OOPIF runs in its own JS world that the top-frame
                # registration never reaches.  Inject here so the hook's
                # fetch/XHR monkey-patch runs inside the OOPIF too; its
                # _record() then postMessages captures up to the top
                # frame's bucket (the relay listener handles the
                # cross-origin boundary).  Two sends: register for the
                # next document load, AND evaluate now for the
                # already-loaded one.
                try:
                    _subsession_msg_id[0] += 1
                    await ws.send(_json.dumps({
                        "id": _subsession_msg_id[0],
                        "method": "Page.addScriptToEvaluateOnNewDocument",
                        "params": {"source": _URL_CAPTURE_HOOK_JS},
                        "sessionId": session_id,
                    }))
                    _subsession_msg_id[0] += 1
                    await ws.send(_json.dumps({
                        "id": _subsession_msg_id[0],
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": _URL_CAPTURE_HOOK_JS,
                            "returnByValue": True,
                        },
                        "sessionId": session_id,
                    }))
                    if log:
                        log(
                            f"  [iframe-trace] url-capture hook injected "
                            f"into {ti.type_} {ti.target_id[:12]}"
                        )
                except Exception as e:
                    if log:
                        log(
                            f"  [iframe-trace] url-capture inject on "
                            f"sub-target {ti.type_} failed (non-fatal): "
                            f"{type(e).__name__}: {e}"
                        )
            except Exception as e:
                if log:
                    log(
                        f"  [iframe-trace] Network.enable on sub-target "
                        f"{ti.type_} failed: {type(e).__name__}: {e}"
                    )
        except Exception as e:
            # Never let a handler exception kill the WS receive loop.
            if log:
                log(
                    f"  [iframe-trace] _hook_subtarget unexpected: "
                    f"{type(e).__name__}: {e}"
                )

    tab.handlers.setdefault(
        cdp.target.AttachedToTarget, []
    ).append(_hook_subtarget)

    # Re-call setAutoAttach with an EXPLICIT filter. nodriver's default
    # MISSES OOPIF (out-of-process iframes) on at least Chrome 140+
    # because its default filter doesn't explicitly include "iframe"
    # and Chrome treats absence as exclude. Verified on chrome 140 +
    # nodriver 0.50.3 with tikpornk: without the re-call,
    # AttachedToTarget never fires for cross-origin video player
    # iframes. flatten=True keeps all sub-session events on the
    # parent's websocket so _hook_subtarget can route per-iframe
    # Network.enable through the parent socket with explicit
    # sessionId rather than spinning up a new websocket per iframe.
    try:
        # explicit_filter defined above (shared with the recursive
        # per-sub-session re-attach in _hook_subtarget).
        await tab.send(
            cdp.target.set_auto_attach(
                auto_attach=True,
                wait_for_debugger_on_start=False,
                flatten=True,
                filter_=explicit_filter,
            )
        )
        setattr(tab, "_paprika_iframe_deep_trace_on", True)
        if log:
            log(
                "  [iframe-trace] ENABLED "
                "(set_auto_attach with iframe-inclusive filter)"
            )
        return True
    except Exception as e:
        if log:
            log(
                f"  [iframe-trace] install failed "
                f"(non-fatal): {type(e).__name__}: {e}"
            )
        return False


# JS injected into every document on every navigation.  Monkey-patches
# ``fetch`` and ``XMLHttpRequest.prototype.open`` so every request URL
# is captured and bubbled up to ``window.top.__paprika_url_capture``.
# A worker-side poller periodically reads that array and feeds the URLs
# into the same maybe_download_video pipeline as the CDP Network
# listener — so HLS manifests hidden inside player iframes (e.g.
# 7mmtv.sx's play.php → streamsuperpro.com m3u8) show up in
# page.network() AND trigger automatic yt-dlp downloads.
#
# Cross-origin reach:
#   * same-origin iframes: _record() writes straight to window.top.
#   * cross-origin iframes (OOPIF): window.top is unreachable by JS, so
#     _record() posts the entry to window.parent via postMessage; each
#     ancestor frame relays it further up until it lands in the top
#     frame's bucket.  This requires the hook to be present in EVERY
#     frame -- install_iframe_deep_trace injects it into each attached
#     OOPIF session via CDP (Page.addScriptToEvaluateOnNewDocument +
#     an immediate Runtime.evaluate).  The relay listener below makes
#     the postMessage chain work.
#
# Notes:
#   * Idempotency guard (__paprika_url_hook) avoids double-wrapping.
#   * Errors are swallowed so a broken hook never crashes the page.
_URL_CAPTURE_HOOK_JS = r"""
(function() {
  if (window.__paprika_url_hook) return;
  window.__paprika_url_hook = true;
  try {
    var t = window.top;
    t.__paprika_hook_installs = (t.__paprika_hook_installs || 0) + 1;
  } catch (e) {
    window.__paprika_hook_installs = (window.__paprika_hook_installs || 0) + 1;
  }
  // Record one capture entry, bubbling toward the top frame.
  function _record(entry) {
    try {
      // same-origin chain: write straight to the top bucket.
      var t = window.top;
      if (!t.__paprika_url_capture) t.__paprika_url_capture = [];
      t.__paprika_url_capture.push(entry);
      return;
    } catch (e) {}
    // cross-origin boundary: hand the entry to our parent frame, whose
    // own hook relays it further up (see the message listener below).
    try { window.parent.postMessage({__paprika_cap: entry}, '*'); } catch (e) {}
  }
  // Relay: a child frame's capture arrives here via postMessage; push
  // it onward toward the top frame.  Cross-origin-safe (targetOrigin
  // '*').  Always travels UP, so no loops.
  try {
    window.addEventListener('message', function(ev) {
      var d = ev && ev.data;
      if (d && typeof d === 'object' && d.__paprika_cap
          && typeof d.__paprika_cap === 'object') {
        _record(d.__paprika_cap);
      }
    }, false);
  } catch (e) {}
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function(input) {
      try {
        var u = typeof input === 'string' ? input : (input && input.url) || '';
        if (u) _record({api: 'fetch', url: u, t: Date.now()});
      } catch (e) {}
      return origFetch.apply(this, arguments);
    };
  }
  var OrigXHR = window.XMLHttpRequest;
  if (OrigXHR && OrigXHR.prototype && OrigXHR.prototype.open) {
    var origOpen = OrigXHR.prototype.open;
    OrigXHR.prototype.open = function(method, url) {
      try {
        if (url) _record({api: 'xhr', method: method, url: String(url), t: Date.now()});
      } catch (e) {}
      return origOpen.apply(this, arguments);
    };
  }
})();
"""


async def install_url_capture_hook(tab, log: LogFn | None = None) -> bool:
    """Inject ``_URL_CAPTURE_HOOK_JS`` into every new document via
    ``Page.addScriptToEvaluateOnNewDocument``.  Runs once per tab;
    idempotent on repeated calls.

    The injected script writes captured URLs to
    ``window.top.__paprika_url_capture`` so a single periodic poll of
    the top window covers ALL same-origin iframes (hls.js inside an
    embed iframe, ad widgets, lazy XHRs that don't surface in the CDP
    Network domain for whatever reason).

    Cross-origin iframes are already handled by
    ``install_iframe_deep_trace`` which gives each cross-origin
    target its own Network.enable.  This hook is the same-origin
    counterpart that the OOPIF-only deep-trace can't reach.

    Returns True on first install, False if already installed or the
    CDP call failed (non-fatal).
    """
    if getattr(tab, "_paprika_url_capture_hook_on", False):
        return False
    try:
        # 1. Register for FUTURE document loads (including iframes
        #    created after this call).  addScriptToEvaluateOnNewDocument
        #    by itself does NOT inject into the current document --
        #    only documents created AFTER registration get the script.
        _script_id = await tab.send(
            cdp.page.add_script_to_evaluate_on_new_document(
                source=_URL_CAPTURE_HOOK_JS,
            )
        )
        if log:
            log(f"  [url-capture] addScriptToEvaluateOnNewDocument id={_script_id}")
        # 2. Also inject into the CURRENT document NOW so we don't
        #    miss the first navigation's fetches.  The hook script's
        #    `if (window.__paprika_url_hook) return;` guard makes it
        #    safe to run twice.
        try:
            await tab.evaluate(_URL_CAPTURE_HOOK_JS)
        except Exception as _e:
            if log:
                log(f"  [url-capture] immediate inject failed: {_e}")
        # 3. Re-inject on EVERY main-frame navigation. Empirically
        #    addScriptToEvaluateOnNewDocument doesn't always apply to
        #    the next navigation when the registration happens while
        #    the tab is on about:blank (observed on this codebase).
        #    Hooking Page.frameNavigated and re-running the script
        #    via Runtime.evaluate guarantees the hook IS present in
        #    every document we end up on.
        async def _on_frame_navigated(event):
            try:
                # Only top frame: iframe sub-frame events are handled
                # by addScriptToEvaluateOnNewDocument's own iframe
                # support (cross-origin ones via install_iframe_deep_trace).
                frame = getattr(event, "frame", None)
                if frame is None:
                    return
                parent_id = getattr(frame, "parent_id", None) or getattr(frame, "parentId", None)
                if parent_id:
                    return  # iframe, skip (covered by addScript registration)
                try:
                    await tab.evaluate(_URL_CAPTURE_HOOK_JS)
                except Exception:
                    pass
            except Exception:
                pass

        try:
            tab.handlers.setdefault(
                cdp.page.FrameNavigated, []
            ).append(_on_frame_navigated)
            # Page domain must be enabled for FrameNavigated to fire.
            await tab.send(cdp.page.enable())
        except Exception as _e:
            if log:
                log(f"  [url-capture] frameNavigated hook failed: {_e}")
        setattr(tab, "_paprika_url_capture_hook_on", True)
        if log:
            log(
                "  [url-capture] fetch+XHR hook installed "
                "(addScript + immediate inject + frameNavigated reinject)"
            )
        return True
    except Exception as e:
        if log:
            log(
                f"  [url-capture] install failed "
                f"(non-fatal): {type(e).__name__}: {e}"
            )
        return False


async def read_url_capture(tab) -> list[dict]:
    """Read and clear ``window.top.__paprika_url_capture``.

    Returns the freshly-captured entries (each ``{api, url, t, ...}``)
    and resets the array so the next poll only sees new URLs.  Safe
    to call even when the hook isn't installed -- returns ``[]``.

    Called from the session-scope poller started by
    ``install_session_asset_capture``.
    """
    try:
        # Splice the array to empty and return what was there.  Done
        # in one expression so we don't race with the page hook
        # appending between read + reset.  Returns a JSON STRING so
        # tab.evaluate (which only returns Runtime.evaluate result.value
        # in nodriver, not return_by_value) gives us a parseable
        # string regardless of whether the bucket itself is JSON-safe.
        # Also includes __paprika_hook_installs so the caller can see
        # in worker logs whether the hook script actually ran in any
        # frame (helps distinguish "hook never executed" from "hook
        # executed but page makes no fetch/XHR").
        result = await tab.evaluate(
            "JSON.stringify({"
            "u: (window.__paprika_url_capture && "
            "window.__paprika_url_capture.splice(0)) || [], "
            "i: window.__paprika_hook_installs || 0"
            "})"
        )
        import json as _json
        parsed = None
        if isinstance(result, str):
            try:
                parsed = _json.loads(result)
            except Exception:
                parsed = None
        elif isinstance(result, dict):
            parsed = result
        elif isinstance(result, tuple) and result:
            inner = result[0]
            if isinstance(inner, str):
                try:
                    parsed = _json.loads(inner)
                except Exception:
                    parsed = None
            elif isinstance(inner, dict):
                parsed = inner
        if isinstance(parsed, dict):
            urls = parsed.get("u", [])
            installs = parsed.get("i", 0)
            if isinstance(urls, list):
                # Stash the install count on the function as a side
                # channel for the poller to surface in heartbeat logs.
                read_url_capture._last_installs = installs  # type: ignore[attr-defined]
                return urls
        # Old shape (list only): treat as URL list directly.
        if isinstance(parsed, list):
            return parsed
        return []
    except Exception:
        return []


async def install_session_asset_capture(
    tab,
    assets_dir: Path,
    on_saved=None,
    log: LogFn | None = None,
    seen_urls: set | None = None,
    min_asset_size_bytes: int = 0,
    extra_mime_prefixes: tuple = (),
    network_log: list | None = None,
    on_stream_detected=None,
    enable_iframe_deep_trace: bool = True,
) -> None:
    """Hook CDP network listeners so every image/video/audio response
    the browser loads while this tab is alive lands in ``assets_dir``.

    This is the session-mode counterpart to what core.fetcher does in
    fetch mode: instead of trying to scrape ``<img src>``/``<video src>``
    after the fact (which misses lazy-loaded / scripted assets), we
    passively persist anything the browser already downloaded for us.

    ``on_saved(path, info)`` is called once per persisted file -- the
    worker uses this to immediately upload the file to the parent job's
    /assets endpoint.

    ``seen_urls`` is an external set (owned by the caller) used to
    dedup across page navigations -- a long-running session that
    revisits the same image URL across multiple pages won't end up
    with foo.png + foo_1.png + foo_2.png in the gallery. Caller is
    free to share this set with other components (e.g. SessionState
    can keep one set per session and pass it in here).

    ``extra_mime_prefixes`` widens the default filter
    (``image/`` + ``audio/``) with additional prefixes the caller
    wants to capture. vision-agent jobs pass ``("video/",)`` because
    their popup-follow flow lands on naked MP4 URLs; the
    ``min_asset_size_bytes`` filter is what keeps MSE/DASH fragment
    floods (~100-200KB per chunk) out of the gallery when video
    capture is enabled.

    ``on_stream_detected(url, referer)`` is an optional sync callable
    invoked from on_response whenever the response URL looks like an
    HLS / DASH playlist (``.m3u8`` / ``.mpd``). The session-wide
    caller wires this to the agent's ``maybe_download_video`` closure
    so yt-dlp fires the moment a playlist is observed -- WITHOUT
    waiting for the SDK / LLM to call ``page.download_video()``
    explicitly. The CDP listener captures the playlist URL into the
    network log regardless; the callback is the auto-download trigger.
    Idempotent on the same URL (the downloader's internal set
    short-circuits repeats).

    Idempotent on the same tab: hooking twice would duplicate every
    save, so callers should only invoke once at session_start.
    """
    # URL-based detector for video resources we want the
    # ``on_stream_detected`` callback to fire on:
    #
    #   * HLS / DASH playlists (.m3u8 / .mpd): yt-dlp merges
    #     segments into a single mp4 in the downloader.
    #   * Direct video files (.mp4 / .webm / .mov / .m4v / .mkv):
    #     the downloader fetches via httpx with proper referer
    #     handling.
    #
    # We match on URL shape (not MIME) because some CDNs serve these
    # with generic ``application/octet-stream`` -- relying on
    # Content-Type would miss the trigger on exactly the sites where
    # we need it most. The downstream maybe_download closure is
    # idempotent on the same URL, so repeat firings (e.g. an mp4
    # that's also matched by URL shape) are harmless.
    _STREAM_URL_RE = re.compile(
        r"\.(m3u8|mpd|mp4|webm|mov|m4v|mkv)($|\?)", re.I,
    )
    save_prefixes = tuple(SESSION_SAVE_MIME_PREFIXES) + tuple(extra_mime_prefixes)
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    metadata: dict = {}
    # request_id -> document_url snapshot at the time the request was
    # issued. Populated by on_request (RequestWillBeSent) and consumed
    # by on_response so we know which page initiated each asset request.
    request_documents: dict = {}
    if seen_urls is None:
        seen_urls = set()
    # Per-(host, basename) collisions across different URLs are rare but
    # possible (think /a/img.png and /b/img.png). When the second URL
    # arrives, _session_unique_path mints foo_1.png. We keep that
    # fallback so we don't drop legitimately-different assets, but
    # same-URL repeats are short-circuited up front via seen_urls.

    async def on_request(event):
        # ``document_url`` is the URL of the document that initiated the
        # request, captured at request-issue time -- BEFORE any
        # subsequent navigation can clobber tab.url. That gives us the
        # "which page did this image come from" answer the gallery
        # popup wants to show.
        try:
            doc = getattr(event, "document_url", None) or ""
            if doc:
                request_documents[event.request_id] = doc
        except Exception:
            pass

    # Network log: every media response the browser loads. Each entry
    # is a dict with url/mime/size/saved/document_url/timestamp. The
    # Live panel "Network" tab reads this via session action so the
    # operator can inspect traffic and cherry-pick assets.
    _net_log = network_log if network_log is not None else []
    # Track which URLs we already appended to _net_log to avoid
    # duplicate rows when the same URL is re-encountered.
    _net_logged_urls: set = set()

    async def on_response(event):
        try:
            url = event.response.url or ""
            if url in seen_urls:
                return
            server_mime = (event.response.mime_type or "").lower()
            # _session_effective_mime falls back to URL extension when
            # the response has no / generic Content-Type. Mainly for
            # Cloudflare-fronted WordPress AVIF (server returns no
            # Content-Type) -- see job 4b9aff01bc6f post-mortem.
            mime = _session_effective_mime(server_mime, url)
            # NB: compose passes SESSION_ASSETS_DEBUG=0 by default, and
            # the string "0" is truthy in Python -- so a bare
            # os.environ.get() check fired the debug log on every
            # response even when "disabled".  Treat 0/false/no/"" as off.
            if log and os.environ.get("SESSION_ASSETS_DEBUG", "").lower() not in ("", "0", "false", "no"):
                log(
                    f"  [session-assets DEBUG] resp server_mime="
                    f"{server_mime!r} effective={mime!r} url={url[:120]}"
                )
            # Record ALL media responses in the network log (before
            # the save-prefix filter) so the operator sees everything.
            is_media = any(mime.startswith(p) for p in save_prefixes)
            # Also log common media MIME types that the save filter
            # might not cover (e.g. video/* when extra_mime_prefixes
            # doesn't include it, or application/octet-stream for
            # binary downloads).
            is_interesting = is_media or any(
                mime.startswith(p) for p in ("image/", "audio/", "video/", "font/")
            )
            # HLS/DASH playlists have MIME application/vnd.apple.mpegurl
            # or application/dash+xml -- not image/audio/video, so the
            # filter above misses them. Force-log any URL that matches
            # the stream pattern so page.network() exposes .m3u8/.mpd
            # entries and codegen scripts can sniff + pass them to
            # page.download_video(url=...).
            if not is_interesting and _STREAM_URL_RE.search(url):
                is_interesting = True
            if is_interesting and url not in _net_logged_urls:
                _net_logged_urls.add(url)
                # Content-Length from response headers (if available).
                content_length = None
                try:
                    for h in event.response.headers or {}:
                        if h.lower() == "content-length":
                            content_length = int(event.response.headers[h])
                            break
                except Exception:
                    pass
                _net_log.append(
                    {
                        "url": url,
                        "mime": mime,
                        "size": content_length,
                        "saved": False,
                        "document_url": request_documents.get(event.request_id) or "",
                        "timestamp": time.time(),
                    }
                )

            # Auto-trigger yt-dlp the moment an HLS/DASH playlist is
            # seen -- don't wait for an explicit page.download_video()
            # call. The downloader closure is idempotent on the same
            # URL, so re-firing is harmless. Match on URL shape so
            # CDNs that serve playlists as application/octet-stream
            # still trip the hook.
            if on_stream_detected and _STREAM_URL_RE.search(url):
                try:
                    on_stream_detected(
                        url,
                        request_documents.get(event.request_id) or "",
                    )
                except Exception as e:
                    if log:
                        log(
                            f"  [session-assets] on_stream_detected "
                            f"failed: {type(e).__name__}: {e}"
                        )
            if not is_media:
                # Not a saveable media response -- drop any preliminary
                # doc URL we stashed so we don't accumulate garbage.
                request_documents.pop(event.request_id, None)
                return
            seen_urls.add(url)
            metadata[event.request_id] = {
                "url": url,
                "mime": mime,
                "document_url": request_documents.pop(event.request_id, None),
            }
        except Exception:
            pass

    async def on_finished(event):
        info = metadata.pop(event.request_id, None)
        if info is None:
            return
        try:
            body, is_b64 = await tab.send(cdp.network.get_response_body(event.request_id))
        except Exception as e:
            if log:
                log(f"  [session-assets] SKIP {info['url']}: {e}")
            return
        try:
            data = base64.b64decode(body) if is_b64 else body.encode("utf-8")
        except Exception:
            return
        actual_size = len(data)
        # Update network_log entry with actual body size.
        for entry in reversed(_net_log):
            if entry["url"] == info["url"]:
                entry["size"] = actual_size
                break
        # Min-size filter: drop assets smaller than the configured
        # threshold (default 0 = no filter). Matches the fetch-mode
        # behaviour in core.fetcher so the same Settings knob takes
        # effect across all capture modes.
        if min_asset_size_bytes and actual_size < min_asset_size_bytes:
            if log:
                log(
                    f"  [session-assets] SKIP {info['url']}: "
                    f"{actual_size / 1024:.1f}KB < min "
                    f"{min_asset_size_bytes / 1024:.1f}KB"
                )
            return
        name = _session_filename(
            info["url"],
            info["mime"],
            f"resource_{len(list(assets_dir.iterdir()))}",
        )
        path = _session_unique_path(assets_dir, name)
        try:
            path.write_bytes(data)
        except Exception as e:
            if log:
                log(f"  [session-assets] write failed: {e}")
            return
        # Mark as saved in network log.
        for entry in reversed(_net_log):
            if entry["url"] == info["url"]:
                entry["saved"] = True
                break
        if log:
            log(f"  [session-assets] SAVED [{actual_size / 1024:>8.1f} KB] {path.name}")
        if on_saved is not None:
            try:
                # ``on_saved`` may be sync or async; await if it's a coroutine.
                res = on_saved(path, info)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                if log:
                    log(f"  [session-assets] on_saved failed: {e}")

    async def on_failed(event):
        # Don't release the URL from seen_urls -- if a load failed once,
        # a future success is rare enough not to merit a re-try slot.
        metadata.pop(event.request_id, None)
        request_documents.pop(event.request_id, None)

    tab.handlers[cdp.network.RequestWillBeSent].append(on_request)
    tab.handlers[cdp.network.ResponseReceived].append(on_response)
    tab.handlers[cdp.network.LoadingFinished].append(on_finished)
    tab.handlers[cdp.network.LoadingFailed].append(on_failed)

    # iframe deep-trace is gated on ``enable_iframe_deep_trace``. The
    # actual setup lives in the module-level ``install_iframe_deep_trace``
    # helper so the plain Fetch path (core/fetcher) can reuse it.
    # Idempotency is keyed on a flag stashed on the tab, so an early
    # session-start install + late page.download_video() trigger
    # collapse into one effective install.
    if enable_iframe_deep_trace:
        await install_iframe_deep_trace(tab, log=log)
    elif log:
        log(
            "  [session-assets] iframe deep-trace DEFERRED "
            "(download_video=False; will install on first "
            "page.download_video() call)"
        )

    # Generous buffers -- one session may run for minutes / many pages.
    # Main-session Network.enable is unconditional: the regular asset
    # capture (images / fonts / mp4 from the top frame) needs this
    # regardless of whether iframe deep-trace is on.
    await tab.send(
        cdp.network.enable(
            max_total_buffer_size=1536 * 1024 * 1024,
            max_resource_buffer_size=512 * 1024 * 1024,
        )
    )

    # Same-origin iframe XHR / fetch hook + poller. CDP's Network
    # domain on the parent target SHOULD surface same-origin iframe
    # requests, but in practice (observed on 7mmtv.sx → play.php iframe
    # hosting hls.js → streamsuperpro.com m3u8) the iframe's hls.js
    # XHRs don't appear in Network.responseReceived events for reasons
    # that look like a Chromium quirk. Inject a fetch/XHR monkey-patch
    # via Page.addScriptToEvaluateOnNewDocument and poll the result
    # bucket so the hidden URLs land in network_log + trigger
    # maybe_download_video the same way Network.responseReceived would.
    await install_url_capture_hook(tab, log=log)

    # Background poller. Reads window.top.__paprika_url_capture every
    # 1.5 s and feeds new URLs through the same code path as on_response.
    # Stops itself when the tab closes (evaluate throws); the outer
    # session teardown also cancels the asyncio task.
    _hook_seen: set = set()
    _hook_poll_n = [0]
    _hook_total_captured = [0]

    async def _url_capture_poller():
        # Stagger the first poll so the page has time to load + the
        # hook to be applied to its initial document.
        await asyncio.sleep(2.0)
        while True:
            try:
                captured = await read_url_capture(tab)
            except Exception as _e:
                if log:
                    log(f"  [url-capture] poller exiting (eval error): {_e}")
                return
            _hook_poll_n[0] += 1
            if captured:
                _hook_total_captured[0] += len(captured)
                if log:
                    log(
                        f"  [url-capture] poll #{_hook_poll_n[0]}: "
                        f"{len(captured)} new URL(s) "
                        f"(total captured so far: {_hook_total_captured[0]})"
                    )
            elif _hook_poll_n[0] in (5, 20, 60):
                # Heartbeat at ~7.5 s / 30 s / 90 s so the operator
                # sees that the poller is alive even when no XHRs
                # have been observed yet.
                if log:
                    log(
                        f"  [url-capture] poll #{_hook_poll_n[0]}: "
                        f"alive, bucket empty"
                    )
            for entry in captured:
                url = entry.get("url") or ""
                if not url or url in _hook_seen:
                    continue
                _hook_seen.add(url)
                # Mirror the on_response logic for stream URLs: log to
                # network_log + fire maybe_download_video. We skip the
                # image/audio mime save path -- that path needs the
                # response body, which we don't have here.
                if _STREAM_URL_RE.search(url):
                    if url not in _net_logged_urls:
                        _net_logged_urls.add(url)
                        _net_log.append({
                            "url": url,
                            "mime": "",
                            "size": None,
                            "saved": False,
                            "document_url": "",
                            "source": "iframe_xhr_hook",
                            "timestamp": time.time(),
                        })
                    if on_stream_detected:
                        try:
                            on_stream_detected(url, "")
                        except Exception as e:
                            if log:
                                log(
                                    f"  [url-capture] on_stream_detected "
                                    f"failed: {e}"
                                )
            try:
                await asyncio.sleep(1.5)
            except asyncio.CancelledError:
                return

    try:
        _poller_task = asyncio.create_task(_url_capture_poller())
        # Stash on tab so session teardown can cancel it cleanly. The
        # task is otherwise fire-and-forget; an unhandled exception is
        # absorbed by the bare try/except inside the poller body.
        existing = getattr(tab, "_paprika_url_capture_tasks", None)
        if existing is None:
            existing = []
            setattr(tab, "_paprika_url_capture_tasks", existing)
        existing.append(_poller_task)
    except Exception as e:
        if log:
            log(f"  [url-capture] poller spawn failed: {e}")


async def force_single_tab(
    browser,
    *,
    keep_target_id: str | None = None,
    log: LogFn | None = None,
) -> int:
    """Reduce the browser to exactly ONE ``page`` target via CDP.

    Enumerates targets via ``Target.getTargets`` and closes everything
    of type ``"page"`` except the one identified by ``keep_target_id``
    (or the first one in the list when no id is supplied). Returns the
    number of targets actually closed.

    Use this at session_start to clean up tabs left over from a
    previous session, at session_end so the next user lands on a
    fresh single-tab browser, and around fetch jobs for the same
    reason. More reliable than reading ``browser.tabs``, which can
    lag CDP state on fast-changing pages (especially when popups
    or ad scripts open windows during navigation).

    Best-effort: per-target close errors are logged and swallowed
    so a runaway popup can't block the rest of the cleanup.
    """
    try:
        targets = await browser.send(cdp.target.get_targets()) or []
    except Exception as e:
        if log:
            log(f"  [tab-cleanup] get_targets failed: {e}")
        return 0
    pages = [t for t in targets if getattr(t, "type_", None) == "page"]
    if len(pages) <= 1:
        return 0
    if keep_target_id is None:
        # Prefer the FIRST page target -- usually the one that was
        # already open from the lane's Chrome startup.
        keep_target_id = getattr(pages[0], "target_id", None)
    closed = 0
    for t in pages:
        tid = getattr(t, "target_id", None)
        if not tid or tid == keep_target_id:
            continue
        try:
            await browser.send(cdp.target.close_target(target_id=tid))
            closed += 1
        except Exception as e:
            if log:
                log(f"  [tab-cleanup] close {tid[:8]}.. failed: {e}")
    if log and closed:
        log(
            f"  [tab-cleanup] closed {closed} extra tab(s) (kept {keep_target_id[:8] if keep_target_id else '?'}..)"
        )
    return closed
