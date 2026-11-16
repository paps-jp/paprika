"""Trusted/human mouse + cursor overlay (click_at/hover_at/wheel_at). (browser_ops package; see _base.py for shared helpers)."""

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

from ._base import *  # noqa: F401,F403
from ._base import ACTION_SETTLE_S, LogFn, normalize_selector, short_error

_HUMAN_MOUSE_ENABLED = os.environ.get("PAPRIKA_HUMAN_MOUSE", "1") != "0"


_SHOW_CURSOR = os.environ.get("PAPRIKA_SHOW_CURSOR", "1") != "0"


_MOUSE_STEPS = int(os.environ.get("PAPRIKA_MOUSE_STEPS", "30"))


_MOUSE_DURATION_MS = int(os.environ.get("PAPRIKA_MOUSE_DURATION_MS", "250"))


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


_CURSOR_INJECT_JS = r"""
(function() {
  if (document.getElementById('__paprika_cursor')) return;

  var css = document.createElement('style');
  css.textContent = `
    #__paprika_cursor {
      position: fixed; z-index: 2147483647; pointer-events: none;
      width: 20px; height: 20px; margin-left: -10px; margin-top: -10px;
      border-radius: 50%;
      background: rgba(220, 50, 50, 0.7);
      box-shadow: 0 0 6px 2px rgba(220, 50, 50, 0.4);
      transition: left 0.01s linear, top 0.01s linear;
      display: none;
    }
    #__paprika_cursor.visible { display: block; }
    @keyframes __paprika_ripple {
      0%   { transform: scale(1);   opacity: 0.7; }
      100% { transform: scale(3.5); opacity: 0; }
    }
    .__paprika_click_ring {
      position: fixed; z-index: 2147483646; pointer-events: none;
      width: 20px; height: 20px; margin-left: -10px; margin-top: -10px;
      border-radius: 50%;
      border: 2px solid rgba(220, 50, 50, 0.8);
      animation: __paprika_ripple 0.5s ease-out forwards;
    }
  `;
  document.head.appendChild(css);

  var dot = document.createElement('div');
  dot.id = '__paprika_cursor';
  document.body.appendChild(dot);
})();
"""


_CURSOR_MOVE_JS = r"""
(function(x, y) {
  var c = document.getElementById('__paprika_cursor');
  if (!c) return;
  c.classList.add('visible');
  c.style.left = x + 'px';
  c.style.top  = y + 'px';
})(%d, %d);
"""


_CURSOR_CLICK_JS = r"""
(function(x, y) {
  var ring = document.createElement('div');
  ring.className = '__paprika_click_ring';
  ring.style.left = x + 'px';
  ring.style.top  = y + 'px';
  document.body.appendChild(ring);
  setTimeout(function() { ring.remove(); }, 600);
})(%d, %d);
"""


_CURSOR_HIDE_JS = r"""
(function() {
  var c = document.getElementById('__paprika_cursor');
  if (c) c.classList.remove('visible');
})();
"""


_cursor_injected: set[str] = set()


async def _ensure_cursor(tab) -> None:
    """Inject the virtual cursor overlay if not already present."""
    if not _SHOW_CURSOR:
        return
    tab_id = str(id(tab))
    if tab_id in _cursor_injected:
        return
    try:
        await tab.send(cdp.runtime.evaluate(expression=_CURSOR_INJECT_JS))
        _cursor_injected.add(tab_id)
    except Exception:
        pass  # page not ready / navigating — skip silently


async def _move_cursor(tab, x: int, y: int) -> None:
    """Move the virtual cursor dot to (x, y)."""
    if not _SHOW_CURSOR:
        return
    try:
        await tab.send(cdp.runtime.evaluate(
            expression=_CURSOR_MOVE_JS % (x, y),
        ))
    except Exception:
        pass


async def _flash_click(tab, x: int, y: int) -> None:
    """Show a ripple animation at (x, y) on click."""
    if not _SHOW_CURSOR:
        return
    try:
        await tab.send(cdp.runtime.evaluate(
            expression=_CURSOR_CLICK_JS % (x, y),
        ))
    except Exception:
        pass


async def _human_move_to(tab, x: int, y: int) -> None:
    """Trace a human-like Bézier path from the last known position to (x, y).

    Each waypoint fires a CDP ``Input.dispatchMouseEvent("mouseMoved")``
    and updates the virtual cursor overlay (visible on noVNC).
    The total travel time is ``_MOUSE_DURATION_MS`` with the inter-step
    delay spread across the waypoints (typically 6-10 ms each for 30 steps).
    """
    tab_id = str(id(tab))
    start = _last_mouse.get(tab_id, (x // 2, y + 80))  # default: below-centre
    _last_mouse[tab_id] = (x, y)

    # Ensure the virtual cursor element exists in the DOM.
    await _ensure_cursor(tab)

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
        # Update the visual cursor position (non-blocking best-effort).
        await _move_cursor(tab, px, py)
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
        # Show click ripple on the virtual cursor overlay.
        await _flash_click(tab, x, y)
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


#: A mouse click that has not come back after this long never will: Chrome
#: does not answer for a page it is not showing.
_CLICK_TIMEOUT_S = 20.0


async def bring_to_front(tab, log: LogFn | None = None) -> bool:
    """Make ``tab`` the page Chrome is showing. Never raises.

    A click on a video player very often opens an advertisement in a new
    tab or window, in front of ours. Chrome delivers no input to a page
    that is not visible and does not paint it either: the next mouse click
    or screenshot on our tab then waits for an answer that never comes
    (seen live 2026-11-14 -- ``document.visibilityState`` was "hidden" 13 s
    after loading such a page, and every action on it timed out).
    """
    try:
        await asyncio.wait_for(tab.send(cdp.page.bring_to_front()), timeout=5.0)
        return True
    except Exception as e:
        if log:
            log(f"  [agent] bring_to_front: {short_error(e)}")
        return False


def _fraction(value, default: float = 0.5) -> float:
    """``value`` as a position inside a box, 0..1 (anything else: the middle)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return min(1.0, max(0.0, f))


def _offset(value) -> int:
    """``value`` as a pixel offset (anything unusable: none)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    if f != f:  # NaN
        return 0
    return int(round(min(5000.0, max(-5000.0, f))))


async def click_in(
    tab,
    selector: str,
    log: LogFn,
    *,
    fx: float | None = 0.5,
    fy: float | None = 0.5,
    dx: float | None = 0,
    dy: float | None = 0,
) -> str:
    """A real mouse click at a point inside the element ``selector`` matches.

    The plain ``click`` primitive calls ``el.click()`` in the page: no
    pointer events, not a user gesture, and nothing at all when the element
    is an ``<iframe>`` -- the click never reaches the document inside it.
    This one goes through CDP Input like a person's click (:func:`click_at`),
    so it is hit-tested at the pixel and reaches whatever is drawn there,
    including the content of a cross-origin frame (an embedded video player).

    The point is ``(fx, fy)`` as a fraction of the element's box -- (0.5,
    0.5) is the middle, (0, 0) the top-left corner -- moved by ``(dx, dy)``
    pixels, and kept inside the box. The offset exists because controls keep
    their size when the box does not: a play button 160 px below the middle
    of a player stays 160 px below the middle when the player is larger on
    the next page load, which no fraction describes. A point outside the
    viewport is scrolled into view first. Returns ``"OK"`` / ``"NO_MATCH"``
    / ``"NO_BOX"`` (matched, but nothing is drawn) / ``"ERR: ..."``.
    """
    if not selector:
        return "ERR: empty selector"
    selector = normalize_selector(selector)
    await bring_to_front(tab, log)
    js = (
        "(()=>{try{const el=document.querySelector(" + json.dumps(selector) + ");"
        "if(!el)return 'NO_MATCH';"
        "const fx=" + repr(_fraction(fx)) + ",fy=" + repr(_fraction(fy)) + ","
        "dx=" + repr(_offset(dx)) + ",dy=" + repr(_offset(dy)) + ";"
        "let r=el.getBoundingClientRect();"
        "if(r.width<1||r.height<1)return 'NO_BOX';"
        "const at=()=>[Math.min(Math.max(r.left+r.width*fx+dx,r.left+1),r.right-1),"
        "Math.min(Math.max(r.top+r.height*fy+dy,r.top+1),r.bottom-1)];"
        "let [x,y]=at();"
        "if(x<0||y<0||x>innerWidth-1||y>innerHeight-1){"
        "el.scrollIntoView({block:'center',inline:'center',behavior:'instant'});"
        "r=el.getBoundingClientRect();[x,y]=at();}"
        "x=Math.min(Math.max(Math.round(x),1),Math.max(1,innerWidth-2));"
        "y=Math.min(Math.max(Math.round(y),1),Math.max(1,innerHeight-2));"
        "return 'XY:'+x+','+y;}"
        "catch(e){return 'ERR: '+e.message;}})()"
    )
    try:
        result = await tab.evaluate(js)
    except Exception as e:
        result = f"ERR: {short_error(e)}"
    if not isinstance(result, str) or not result.startswith("XY:"):
        result = result if isinstance(result, str) and result else "ERR: no position"
        log(f"  [agent] mouse click {selector!r}: {result}")
        return result
    try:
        xs, ys = result[3:].split(",", 1)
        x, y = int(xs), int(ys)
    except ValueError:
        return f"ERR: bad position {result!r}"
    try:
        return await asyncio.wait_for(click_at(tab, x, y, log), timeout=_CLICK_TIMEOUT_S)
    except asyncio.TimeoutError:
        log(f"  [agent] mouse click {selector!r} at ({x},{y}): no answer in {_CLICK_TIMEOUT_S:.0f}s")
        return "ERR: mouse click timed out (is the page visible?)"


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

