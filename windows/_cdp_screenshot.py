"""Tiny raw-CDP screenshot helper.

Used by ``windows.lane_pool_stub._WinLane.screenshot()`` to capture the
current page of the bundled Chromium without going through nodriver
(which may already be holding the CDP session for an in-flight job).

The CDP protocol over WebSocket is simple enough that we hand-roll
the 2 messages we need (Target.getTargets + Page.captureScreenshot)
to avoid pulling chrome_devtools_protocol as a runtime dep.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import websockets


async def grab_screenshot(
    *,
    chrome_port: int,
    format: str = "jpeg",  # noqa: A002
    quality: int = 60,
    max_width: int | None = None,
) -> bytes:
    """Capture the active tab. Returns raw image bytes.

    Strategy:
      1. GET http://127.0.0.1:{port}/json -- list of all targets
      2. Pick the first ``type=page`` target (excluding extensions /
         service workers) and read its ``webSocketDebuggerUrl``
      3. Open WS, send Page.captureScreenshot, decode the base64
         result.
    """
    targets_url = f"http://127.0.0.1:{chrome_port}/json"
    async with httpx.AsyncClient(timeout=3.0) as http:
        r = await http.get(targets_url)
        r.raise_for_status()
        targets = r.json()
    page_targets = [
        t for t in targets
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]
    if not page_targets:
        raise RuntimeError("no Chrome page target available")
    ws_url = page_targets[0]["webSocketDebuggerUrl"]

    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        msg = {
            "id": 1,
            "method": "Page.captureScreenshot",
            "params": {
                "format": format,
                "quality": quality,
                # Note: maxWidth is not a CDP param; just clip
                # client-side later if needed. The bundled Chromium
                # window size is whatever the OS gave it.
            },
        }
        await ws.send(json.dumps(msg))
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            raw = await ws.recv()
            payload = json.loads(raw)
            if payload.get("id") == 1:
                if "error" in payload:
                    raise RuntimeError(
                        f"Page.captureScreenshot failed: {payload['error']}"
                    )
                return base64.b64decode(payload["result"]["data"])
        raise asyncio.TimeoutError("Page.captureScreenshot reply not seen")
