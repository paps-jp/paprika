"""Navigation (navigate/back/forward/exists/history_first). (browser_ops package; see _base.py for shared helpers)."""

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
from ._base import LogFn, NAVIGATION_SETTLE_S, short_error

# Ceiling for how long a navigation waits for the document to finish
# parsing before proceeding anyway. Kept comfortably under the hub's 60s
# start_session timeout so a session open (which waits for load too) never
# trips it. Env-overridable for slow / ad-heavy fleets.
NAV_LOAD_TIMEOUT_S = float(os.environ.get("AGENT_NAV_LOAD_TIMEOUT_S", "20.0"))


async def wait_for_load(tab, log: Optional[LogFn] = None, *, timeout_s: Optional[float] = None) -> None:
    """Block until the tab's document has finished parsing
    (``document.readyState`` is ``interactive`` or ``complete``) or
    ``timeout_s`` elapses.

    This is what makes a navigation actually WAIT: ``page.goto`` / a
    session's ``initial_url`` used to fire ``cdp.page.navigate`` and return
    after a fixed sleep, so ``page.click(...)`` ran against a page that was
    still loading (selector ``NO_MATCH``). Polling readyState here means the
    nav returns only once the DOM is queryable.

    Best-effort: returns (never raises) on timeout so a slow / ad-heavy page
    still proceeds. Uses ``tab.evaluate`` (NOT ``tab.get()``) so the
    network.* listeners installed for session asset capture stay bound --
    the whole reason the low-level ``cdp.page.navigate`` is used. Mirrors the
    readyState poll on the fetch path (core/fetcher.py).
    """
    if timeout_s is None:
        timeout_s = NAV_LOAD_TIMEOUT_S
    # Small lead so we don't read the OUTGOING document's stale readyState
    # (about:blank / the previous page can still report 'complete' for a
    # tick after page.navigate() before the new load commits).
    try:
        await asyncio.sleep(0.15)
    except Exception:
        pass
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        try:
            ready = await tab.evaluate("document.readyState")
        except Exception as e:
            # -32000 "Inspected target navigated or closed" fires WHILE the
            # navigation is committing -- the page IS loading, keep polling.
            m = str(e).lower()
            if "-32000" in m or "navigat" in m or "closed" in m or "detached" in m:
                await asyncio.sleep(0.1)
                continue
            return  # unexpected eval error -> let the caller proceed
        if ready in ("interactive", "complete"):
            return
        await asyncio.sleep(0.1)
    if log:
        log(f"  [agent] wait_for_load: document not ready after {timeout_s:.0f}s; proceeding")


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
    # Phase 3 SSRF (authoritative worker-side check): in-session navigations
    # (page.goto / agent / macro) are NOT validated hub-side -- only the
    # initial job/session URL is -- so an in-session goto to a private /
    # loopback / cloud-metadata address (or file://) would otherwise reach
    # Chrome directly. Re-validate here, the point of actual connection, to
    # close the hub->worker DNS-rebind/TOCTOU gap. No-op for public URLs and
    # when PAPRIKA_ALLOW_PRIVATE_URLS=1 (LAN fleets).
    from core.ssrf_guard import navigation_block_reason
    _ssrf = navigation_block_reason(url)
    if _ssrf:
        log(f"  [agent] navigate {url!r}: BLOCKED by SSRF guard: {_ssrf}")
        return f"ERR: blocked by SSRF guard: {_ssrf}"
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

