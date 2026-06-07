"""Nav-response tracking + the action `execute` dispatcher. (browser_ops package; see _base.py for shared helpers)."""

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
from ._base import LogFn
from .input import click, fill, press_key, scroll, type_text
from .nav import back, forward, history_first, navigate, wait_for_load

_VAR_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _apply_variables(text: str, vars_map: Optional[dict]) -> str:
    if not text or not vars_map or "${" not in text:
        return text
    def _repl(m: "re.Match[str]") -> str:
        return str(vars_map.get(m.group(1), m.group(0)))
    return _VAR_PLACEHOLDER_RE.sub(_repl, text)


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
            # Always overwrite captured -- a redirect chain fires multiple
            # responseReceived events on the same requestId (300, then the
            # 200 at the final hop). We want the LAST one (= the user's
            # actual destination), matching Playwright semantics:
            #   await page.goto("http://x") -> Response{url: "https://x", status: 200}
            # NOT 301-from-the-first-hop. So overwrite is correct; the
            # signaller below ensures we only "complete" on a non-3xx.
            captured.update({
                "url":         getattr(resp, "url", "") or "",
                "status":      status_code,
                "status_text": getattr(resp, "status_text", "") or "",
                "ok":          200 <= status_code < 300,
                "headers":     hdrs,
                "mime":        getattr(resp, "mime_type", "") or "",
            })
            # Only treat this as "navigation completed" when the status
            # is NOT a redirect. A 301/302/303/307/308 means another
            # responseReceived is coming for the same requestId; waiting
            # for it gives us the final response Playwright would surface.
            # The wait_for in the caller has a 5s ceiling so even a
            # broken chain (server returns 302 -> 302 -> ... forever)
            # can't hang the SDK.
            if not (300 <= status_code < 400):
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
        result = await _capture_nav_response(
            tab, navigate(tab, url, log), expected_url=url,
        )
    elif kind == "back":
        result = await _capture_nav_response(tab, back(tab, log))
    elif kind == "forward":
        result = await _capture_nav_response(tab, forward(tab, log))
    elif kind == "history_first":
        result = await _capture_nav_response(tab, history_first(tab, log))
    else:
        # Non-nav: delegate to the existing dispatcher; no response info,
        # no navigation to wait on.
        return await execute(tab, action, log), None
    # Wait for the navigated document to reach DOM-ready BEFORE returning, so
    # the SDK's next page.click()/fill()/etc. runs against a parsed page
    # rather than a still-loading one (the response capture above only waits
    # for the document's HTTP headers, not the DOM). The HTTP response was
    # already captured with listeners armed; this is a pure readyState poll.
    await wait_for_load(tab, log)
    return result


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

