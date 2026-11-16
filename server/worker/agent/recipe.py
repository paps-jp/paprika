"""Per-host fetch recipe application + player-iframe discovery. (worker agent package; shared bits in _base.py)."""

from __future__ import annotations
import asyncio
import functools
import json
import os
import random
import shutil
import socket
import logging
import string
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
import httpx
from core.httpclient import make_async_client
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from core.fetcher import (
    FetchOptions,
    clone_chrome_profile,
    fetch,
)
from server.protocol import (
    AssetInfo,
    HubAssignJob,
    HubExpectedVersion,
    HubProfileDelete,
    HubProfileSync,
    HubRegistered,
    HubPreviewSubscribe,
    HubScreenshotRequest,
    HubSessionAction,
    HubSessionAgent,
    HubSessionEnd,
    HubSessionInteraction,
    HubSessionStart,
    HubUpdateGate,
    JobOptions,
    JobResult,
    JobStatus,
    ProfileCacheEntry,
    SessionStateSnapshot,
    WorkerCapabilities,
    WorkerDraining,
    WorkerHeartbeat,
    WorkerJobAccepted,
    WorkerJobComplete,
    WorkerJobFailed,
    ASSET_CAPTURE_MARKER,
    JOB_PROGRESS_MARKER,
    LINKS_CAPTURE_MARKER,
    NET_CAPTURE_MARKER,
    WorkerJobLog,
    WorkerJobProgress,
    WorkerRegister,
    WorkerPreviewFrame,
    WorkerScreenshotReply,
    WorkerSessionActionResult,
    WorkerSessionAgentResult,
    WorkerSessionAnnounce,
    WorkerSessionEndAck,
    WorkerSessionStartAck,
    YtdlpResult,
    decode_hub_msg,
    encode_msg,
)
from server.scheduler import HEARTBEAT_INTERVAL
from server.worker import browser_ops
from server.worker.sessions import SessionState
from server.worker._browser_helpers import (
    _LINKS_EXTRACT_JS,
    _VIDEO_DIRECT_RE,
    _VIDEO_STREAM_RE,
    _evaluate_in_frame,
    _looks_like_player_iframe,
)
from server.worker.session_actions import (
    _ActionCtx,
    _SESSION_ACTIONS,
)
import re as _re

from ._base import *  # noqa: F401,F403

async def _discover_player_iframes(tab) -> list[str]:
    """Return iframe[src] URLs that look like 3rd-party video players,
    ordered by likely promise (visible-and-large first). Vendor-neutral
    -- relies on :func:`_looks_like_player_iframe` heuristics."""
    try:
        raw = await tab.evaluate(
            "JSON.stringify("
            "[...document.querySelectorAll('iframe[src]')]"
            ".map(el => {"
            "  const r = el.getBoundingClientRect();"
            "  return {"
            "    src: el.src || el.getAttribute('src') || '',"
            "    w: Math.round(r.width),"
            "    h: Math.round(r.height),"
            "    vis: r.width > 0 && r.height > 0 "
            "         && el.offsetParent !== null,"
            "  };"
            "})"
            ")",
        )
    except Exception:
        return []
    if not raw:
        return []
    try:
        import json as _j
        rows = _j.loads(raw)
    except Exception:
        return []
    out: list[tuple[int, str]] = []
    for row in rows:
        src = (row.get("src") if isinstance(row, dict) else "") or ""
        if not _looks_like_player_iframe(src):
            continue
        score = 0
        if row.get("vis"):
            score += 10
        if (row.get("w") or 0) >= 200 and (row.get("h") or 0) >= 150:
            score += 5
        out.append((score, src))
    out.sort(key=lambda x: -x[0])
    return [s for _, s in out]


async def _trigger_playback_in_frame(tab, frame_id: str) -> None:
    """Per-frame .play() nudge -- the no-click equivalent of
    :func:`_trigger_video_playback`. Best-effort; failures swallowed."""
    await _evaluate_in_frame(
        tab,
        frame_id,
        "document.querySelectorAll('video,audio')"
        ".forEach(v => { try { v.play(); } catch(e){} });",
        user_gesture=True,
    )


async def _apply_fetch_recipe(tab, recipe: dict, log) -> dict:
    """Run a HostRecipe's ``actions`` list against ``tab``. Best-effort:
    each action's outcome is logged but a single failure doesn't abort
    the rest. Returns a diagnostic ``{"ran": N, "ok": N, "errors": [...]}``.

    Phase 1 scope: ``actions`` only. ``goal`` / ``code`` raise NotImpl
    so the operator knows those paths aren't live yet.

    Supported action kinds (each is a JSON dict):
      {"kind": "click",    "selector": "..."}              # CSS selector
      {"kind": "click",    "paprika_id": 5}                # outline @N
      {"kind": "click",    "selector": "...", "mouse": true, "dx": 0, "dy": 160}
                                                           # real mouse click inside the
                                                           # element's box: its middle
                                                           # (or fx, fy fractions) moved
                                                           # by dx, dy px. Reaches into
                                                           # an iframe.
      {"kind": "fill",     "selector": "...", "value": "..."}
      {"kind": "press",    "key": "Enter", "count": 1}
      {"kind": "type",     "text": "hello"}
      {"kind": "scroll",   "direction": "down", "amount": 800}
      {"kind": "wait",     "seconds": 1.5}
      {"kind": "navigate", "url": "..."}
      {"kind": "goto",     "url": "..."}                   # alias for navigate (recorded by SDK)
      {"kind": "evaluate", "expression": "JS"}             # read-only sanity
    """
    if not isinstance(recipe, dict):
        return {"ran": 0, "ok": 0, "errors": ["recipe is not a dict"]}
    actions = recipe.get("actions") or []
    goal = recipe.get("goal")
    code = recipe.get("code")
    if not actions and (goal or code):
        # Phase 1 does NOT execute goal / code. Surface the limit so
        # operators see why their non-actions recipe didn't run.
        log(
            f"  !! fetch_recipe: only 'actions' is supported in Phase 1; "
            f"goal={'set' if goal else 'unset'} / code="
            f"{'set' if code else 'unset'} are ignored."
        )
        return {
            "ran": 0,
            "ok": 0,
            "errors": ["goal/code execution requires Phase 2"],
        }
    if not actions:
        return {"ran": 0, "ok": 0, "errors": []}

    ran = 0
    ok = 0
    errors: list[str] = []
    log(
        f"  ... fetch_recipe: pattern={recipe.get('pattern')!r} "
        f"actions={len(actions)}"
    )
    for i, raw in enumerate(actions, 1):
        if not isinstance(raw, dict):
            errors.append(f"action[{i}]: not a dict ({type(raw).__name__})")
            continue
        ran += 1
        kind = (raw.get("kind") or "").strip()
        try:
            if kind == "wait":
                await asyncio.sleep(float(raw.get("seconds") or 0))
                status = "OK"
            elif kind in ("click", "fill", "type", "press", "scroll", "navigate", "goto"):
                # Translate to the browser_ops.execute() shape. The
                # action dict already mirrors that shape closely; just
                # remap a few fields and resolve paprika_id -> selector.
                # "goto" is an alias for "navigate" (the SDK records page.goto()
                # calls as kind="goto" with args=[url]; normalise here).
                effective_kind = "navigate" if kind == "goto" else kind
                op_action = {"kind": "type" if kind == "fill" else effective_kind}
                if "selector" in raw:
                    op_action["selector"] = raw["selector"]
                elif "paprika_id" in raw:
                    pid = raw["paprika_id"]
                    op_action["selector"] = f'[data-paprika-id="{int(pid)}"]'
                if kind == "click" and raw.get("mouse"):
                    op_action["mouse"] = True
                    for _k in ("fx", "fy", "dx", "dy"):
                        if raw.get(_k) is not None:
                            op_action[_k] = raw[_k]
                if kind == "fill":
                    op_action["text"] = raw.get("value") or ""
                elif kind == "type":
                    op_action["text"] = raw.get("text") or ""
                elif kind == "press":
                    op_action["kind"] = "press_key"
                    op_action["key"] = raw.get("key") or ""
                    if raw.get("count"):
                        op_action["count"] = int(raw["count"])
                elif kind == "scroll":
                    op_action["direction"] = raw.get("direction") or "down"
                    op_action["amount"] = int(raw.get("amount") or 800)
                elif kind in ("navigate", "goto"):
                    # goto stores the URL in args[0]; navigate uses "url" key.
                    op_action["url"] = (
                        raw.get("url")
                        or (raw.get("args") or [""])[0]
                        or ""
                    )
                status = await browser_ops.execute(tab, op_action, log)
            elif kind == "evaluate":
                # Read-only JS evaluate (best-effort; failures are tolerated).
                expr = raw.get("expression") or ""
                try:
                    await tab.evaluate(expr)
                    status = "OK"
                except Exception as e:
                    status = f"ERR: {type(e).__name__}: {e}"
            else:
                status = f"ERR: unknown action kind {kind!r}"
        except Exception as e:
            status = f"ERR: {type(e).__name__}: {e}"
        if status.startswith("OK"):
            ok += 1
        else:
            errors.append(f"action[{i}] {kind!r}: {status}")
        log(f"      [recipe {i}/{len(actions)}] {kind} -> {status}")
    return {"ran": ran, "ok": ok, "errors": errors}


def _looks_suspect(
    action: dict,
    *,
    viewport_w: int,
    viewport_h: int,
    last_box: dict | None = None,
) -> str | None:
    """Return a short reason string when CogAgent's action looks
    "confused", or None when the action looks healthy.

    Used in engine=auto mode: a suspect action triggers a fallback
    to the Qwen-VL agent for that step. Heuristics chosen from the
    failure modes we've actually observed:

      - box in the very top-left corner (CogAgent's "I don't know"
        pattern, ~50x50 px at (0,0))
      - same box as the previous step (loop, especially after a
        navigation that the model didn't notice)
      - box outside the viewport (math went sideways)
      - box too small (<8 px on a side, can't be a real target)
    """
    kind = action.get("kind") or "unknown"
    if kind in ("end", "done", "unknown", "wait"):
        # No box to evaluate; suspicion is a different concept here.
        return None
    box = action.get("box")
    if not box:
        return None  # opcodes like press_key have no box
    try:
        x1 = int(box["x1"])
        y1 = int(box["y1"])
        x2 = int(box["x2"])
        y2 = int(box["y2"])
    except Exception:
        return "malformed box"
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    w = x2 - x1
    h = y2 - y1
    if cx < 50 and cy < 50:
        return f"box in top-left corner ({cx},{cy})"
    if w < 8 or h < 8:
        return f"box too small ({w}x{h})"
    if cx >= viewport_w or cy >= viewport_h or cx < 0 or cy < 0:
        return f"box centre ({cx},{cy}) outside viewport {viewport_w}x{viewport_h}"
    if (
        last_box
        and last_box.get("x1") == x1
        and last_box.get("y1") == y1
        and last_box.get("x2") == x2
        and last_box.get("y2") == y2
    ):
        return "same box as previous step (loop)"
    return None

