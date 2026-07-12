"""Interaction primitives: navigate/click/fill/press/type/scroll/back/forward/history_first/zoom/evaluate/set_input_files/ext.

Part of the sessions/ package; shared bits in _base.py."""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re as _re
from datetime import datetime
from pathlib import Path
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from server.hub._state import config, get_storage_dir, state
from server.hub._helpers import _asset_upload_url
from server.hub.codegen import (
    CODEGEN_LLM_URL,
    CODEGEN_MODEL_NAME,
    generate_script,
)
from server.hub.hosts import _normalise_host, cookies_for_cdp
from server.hub.routes.hosts import _require_hosts
from server.hub.sessions import SessionInfo, new_session_id
from server.protocol import JobStatus
from server.runner import DONE_SENTINEL

log = logging.getLogger(__name__)

from server.hub.routes.sessions._base import *  # noqa: F401,F403

@router.post("/sessions/{session_id}/evaluate")
async def session_evaluate(session_id: str, body: dict) -> dict:
    """Evaluate a JS expression in the session tab's page context and
    return ``{status, result, elapsed_ms}`` where ``result`` is the
    expression's value (must be JSON-serialisable).

    Body: ``{"expression": "...", "await_promise": false, "page_id": "..."}``

    This is the low-level primitive the SDK builds Locator getters
    (``text_content`` / ``get_attribute`` / …), ``wait_for_selector``,
    and the JS-dispatched input helpers (``hover`` / ``select_option`` /
    …) on top of. LAN-trusted: arbitrary JS runs in the browser, same
    trust model as cookie injection / profile upload.
    """
    body = body or {}
    expr = (body.get("expression") or "").strip()
    if not expr:
        raise HTTPException(400, "missing 'expression'")
    action = _route_to_page(
        {
            "kind": "evaluate",
            "expression": expr,
            "await_promise": bool(body.get("await_promise")),
        },
        body,
    )
    return await _send_session_action(session_id, action, timeout=30.0)


@router.post("/sessions/{session_id}/set_input_files")
async def session_set_input_files(session_id: str, body: dict) -> dict:
    """Set the files on an ``<input type=file>`` matched by ``selector``.

    Body::

        {
          "selector": "input[type=file]",
          "files": [{"name": "photo.jpg", "content_b64": "..."}],
          "page_id": "..."        // optional, multi-tab
        }

    The worker materialises the base64 payloads in a tempdir and points
    the input at them via CDP ``DOM.setFileInputFiles`` (JS can't set a
    file input). Returns ``{status, result:{files, count}, elapsed_ms}``.
    """
    body = body or {}
    selector = (body.get("selector") or "").strip()
    if not selector:
        raise HTTPException(400, "missing 'selector'")
    files = body.get("files")
    if not isinstance(files, list) or not files:
        raise HTTPException(400, "missing 'files' (non-empty list)")
    action = _route_to_page(
        {"kind": "set_input_files", "selector": selector, "files": files},
        body,
    )
    # File payloads can be sizeable; give the worker a generous window.
    return await _send_session_action(session_id, action, timeout=60.0)


@router.post("/sessions/{session_id}/navigate")
async def session_navigate(session_id: str, body: dict) -> dict:
    url = (body or {}).get("url")
    if not url:
        raise HTTPException(400, "missing url")
    # SSRF guard: each navigation is its own chance to dial a private
    # IP, so we re-validate on every page.goto() call. The script
    # could still trigger in-browser navigations (window.location =
    # ..., JS redirects, fetch('http://10.0.0.5/')) which don't go
    # through this endpoint -- the worker iptables egress firewall is
    # the defense for those.
    from server.hub.url_safety import assert_public_url_async
    await assert_public_url_async(url)  # off-loop: getaddrinfo mustn't stall the loop
    action = _route_to_page({"kind": "navigate", "url": url}, body)
    return await _send_session_action(session_id, action, timeout=60.0)


@router.post("/sessions/{session_id}/click")
async def session_click(session_id: str, body: dict) -> dict:
    sel = (body or {}).get("selector")
    if not sel:
        raise HTTPException(400, "missing selector")
    action = _route_to_page({"kind": "click", "selector": sel}, body)
    return await _send_session_action(session_id, action)


@router.post("/sessions/{session_id}/fill")
async def session_fill(session_id: str, body: dict) -> dict:
    body = body or {}
    sel = body.get("selector")
    val = body.get("value")
    if not sel:
        raise HTTPException(400, "missing selector")
    if val is None:
        raise HTTPException(400, "missing value")
    # Wire name is still `text` on the worker side -- browser_ops.execute
    # reads action.text and maps to fill(value=).
    payload = {"kind": "type", "selector": sel, "text": val}
    # ${name} placeholder substitution: when the SDK passes variables=
    # (page.fill(sel, "${pw}", variables={"pw": SECRET})) the dict travels
    # untouched to the worker, which substitutes at the CDP edge so the
    # real value never appears in hub logs or any LLM prompt.
    if body.get("variables"):
        payload["variables"] = dict(body["variables"])
    action = _route_to_page(payload, body)
    return await _send_session_action(session_id, action)


@router.post("/sessions/{session_id}/press")
async def session_press(session_id: str, body: dict) -> dict:
    """Press a key (or key combo) on the bound tab.

    Body::

        {
          "key": "Backspace",        # or "Ctrl+A", "Enter", "ArrowDown"...
          "count": 3,                # optional, default 1
          "modifiers": ["Ctrl"]      # optional; OR'd with anything
                                     # parsed from the combo string.
                                     # Accepts Ctrl/Shift/Alt/Meta and
                                     # common synonyms (Cmd, Option,
                                     # Control, Win, Super).
        }
    """
    body = body or {}
    key = body.get("key")
    if not key:
        raise HTTPException(400, "missing key")
    count = int(body.get("count") or 1)
    if count < 1 or count > 100:
        raise HTTPException(400, "count must be in [1, 100]")
    # Normalise modifiers list -> CDP bitfield. The worker also
    # supports raw int but we keep the wire format human-readable so
    # operators inspecting captured traffic can read it.
    _MOD_BITS = {
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
    mods = body.get("modifiers")
    bits: int | None = None
    if isinstance(mods, list):
        bits = 0
        for m in mods:
            if isinstance(m, str):
                bits |= _MOD_BITS.get(m.lower(), 0)
    elif isinstance(mods, int):
        bits = mods
    payload: dict = {"kind": "press_key", "key": key, "count": count}
    if bits:
        payload["modifiers"] = bits
    payload = _route_to_page(payload, body)
    return await _send_session_action(session_id, payload)


@router.post("/sessions/{session_id}/type")
async def session_type(session_id: str, body: dict) -> dict:
    """Insert text into the currently-focused element.

    Body::

        {"text": "hello world"}

    Uses CDP Input.insertText -- one shot, no per-character round
    trip, works for <input>/<textarea>/contenteditable. Caller must
    have already clicked / focused the target; this endpoint does
    NOT move focus.
    """
    body = body or {}
    text = body.get("text")
    if text is None or text == "":
        raise HTTPException(400, "missing 'text'")
    if not isinstance(text, str):
        raise HTTPException(400, "'text' must be a string")
    payload = {"kind": "type_text", "text": text}
    if body.get("variables"):
        # Same ${name} substitution semantics as /fill -- worker swaps
        # placeholders for real values at the CDP edge so secrets never
        # surface in hub logs / LLM prompts.
        payload["variables"] = dict(body["variables"])
    action = _route_to_page(payload, body)
    return await _send_session_action(session_id, action)


@router.post("/sessions/{session_id}/scroll")
async def session_scroll(session_id: str, body: dict) -> dict:
    body = body or {}
    direction = body.get("direction") or "down"
    pixels = int(body.get("pixels") or body.get("amount") or 800)
    action = _route_to_page(
        {"kind": "scroll", "direction": direction, "amount": pixels},
        body,
    )
    return await _send_session_action(session_id, action)


@router.post("/sessions/{session_id}/back")
async def session_back(session_id: str, body: dict | None = None) -> dict:
    action = _route_to_page({"kind": "back"}, body)
    return await _send_session_action(session_id, action, timeout=60.0)


@router.post("/sessions/{session_id}/forward")
async def session_forward(session_id: str, body: dict | None = None) -> dict:
    """Browser の Forward ボタン相当。history の 1 つ先の entry に進む。
    既に末尾なら no-op で OK を返す。"""
    action = _route_to_page({"kind": "forward"}, body)
    return await _send_session_action(session_id, action, timeout=60.0)


@router.post("/sessions/{session_id}/history_first")
async def session_history_first(session_id: str, body: dict | None = None) -> dict:
    """履歴の 0 番目 (このセッションで最初に開いたページ) に戻る。
    既に 0 番目なら no-op。"""
    action = _route_to_page({"kind": "history_first"}, body)
    return await _send_session_action(session_id, action, timeout=60.0)


@router.post("/sessions/{session_id}/zoom")
async def session_zoom(session_id: str, body: dict) -> dict:
    """Set the in-browser PAGE zoom (visual magnification of the rendered
    page, including full-viewport cross-origin iframe players). 1.0 =
    100%. Implemented via CDP ``Emulation.setPageScaleFactor`` on the
    worker -- NOT CSS ``zoom``, which can't scale a 100vw/100vh iframe.

    Body: ``{"factor": 1.25}`` (or ``{"percent": 125}``). Allowed even
    on a fetch-owned session (it's a viewing aid, not a write action).
    """
    body = body or {}
    factor = body.get("factor")
    if factor is None and body.get("percent") is not None:
        try:
            factor = float(body["percent"]) / 100.0
        except Exception:
            factor = None
    try:
        factor = float(factor)
    except Exception:
        raise HTTPException(400, "missing/invalid 'factor' (e.g. 1.25)")
    action = _route_to_page({"kind": "zoom", "factor": factor}, body)
    return await _send_session_action(session_id, action, timeout=20.0)


@router.post("/sessions/{session_id}/ext")
async def session_ext(session_id: str, body: dict) -> dict:
    """Run a generic Paprika Agent extension command on the session.

    The Paprika Agent extension exposes a command bus for Chrome
    capabilities CDP / nodriver can't reach (request header/block rules,
    content settings, privacy, downloads, proxy, tab capture, genuine
    zoom, ...). This single endpoint relays any command; thin typed
    wrappers (``page.set_referer``, ``page.allow_popups``, ...) live in
    the Python client.

    Body::

        {"cmd": "netSetHeader", "args": {...}, "timeout"?: 20}

    Returns ``{status, result, elapsed_ms}`` where ``result`` is the
    extension handler's return value. Allowed on a fetch-owned session
    (browser config, not a navigation/DOM write).
    """
    body = body or {}
    cmd = (body.get("cmd") or "").strip()
    if not cmd:
        raise HTTPException(400, "missing 'cmd'")
    try:
        timeout = float(body.get("timeout") or 20.0)
    except Exception:
        timeout = 20.0
    action = _route_to_page(
        {
            "kind": "ext",
            "cmd": cmd,
            "args": body.get("args") or {},
            "timeout": timeout,
        },
        body,
    )
    return await _send_session_action(
        session_id, action, timeout=max(timeout + 5.0, 20.0),
    )

