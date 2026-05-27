"""One-shot helpers: ``requests.get(url)``-style sugar.

These open a fresh session, run one or more actions, and close the
session. Internally they use :class:`PaprikaClient` + :class:`Page`;
externally they're a single ``await snapshot(url)`` call.

The base hub URL is read in this order:
  1. explicit ``base_url=`` argument
  2. ``PAPRIKA_HUB`` environment variable
  3. ``http://localhost:8000``

For any non-trivial flow (multiple pages, retries, conditional logic)
prefer the session context manager directly.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Iterable, Optional

from ._client import PaprikaClient, async_paprika
from ._page import Page


def _default_base_url(base_url: Optional[str]) -> str:
    return base_url or os.environ.get("PAPRIKA_HUB") or "http://localhost:8000"


async def snapshot(
    url: str,
    *,
    base_url: Optional[str] = None,
    wait: float = 2.0,
    full_page: bool = False,
    path: Optional[str] = None,
) -> bytes:
    """Open ``url``, wait ``wait`` seconds for it to settle, capture a
    PNG and return the bytes. If ``path`` is given, also write to disk.

    The hub picks any free Lane; the session is closed before this
    function returns.
    """
    base = _default_base_url(base_url)
    async with async_paprika.connect(base) as cli:
        async with cli.session(initial_url=url) as page:
            if wait > 0:
                await asyncio.sleep(wait)
            return await page.screenshot(path=path)


async def outline(
    url: str,
    *,
    base_url: Optional[str] = None,
    wait: float = 2.0,
) -> str:
    """Open ``url``, return the page outline string. Same data the LLM
    agent sees, useful as a quick "what's clickable here?" probe."""
    base = _default_base_url(base_url)
    async with async_paprika.connect(base) as cli:
        async with cli.session(initial_url=url) as page:
            if wait > 0:
                await asyncio.sleep(wait)
            return await page.outline()


async def state(
    url: str,
    *,
    base_url: Optional[str] = None,
    wait: float = 2.0,
) -> dict:
    """Open ``url``, return the session state (``url``, ``title``, …)."""
    base = _default_base_url(base_url)
    async with async_paprika.connect(base) as cli:
        async with cli.session(initial_url=url) as page:
            if wait > 0:
                await asyncio.sleep(wait)
            return await page.state()


async def run(
    actions: Iterable[dict[str, Any]],
    *,
    initial_url: Optional[str] = None,
    base_url: Optional[str] = None,
    worker_id: Optional[str] = None,
    lane_hint: Optional[int] = None,
) -> dict:
    """Execute a sequence of actions in one session, then close.

    ``actions`` is a list of dicts as returned by the ``act.*`` helpers.
    Returns a dict with::

        {
          "session_id": "...",
          "worker_id":  "...",
          "lane_idx":   0,
          "results":    [ {kind, status, elapsed_ms, result}, ... ],
          "state":      {"url": ..., "title": ...},  # final state
          "visited":    [...],
        }

    Any ``act.goto / click / fill / press / scroll / back / wait /
    capture / outline / state / screenshot`` is supported. Unknown
    kinds raise ``ValueError``.

    If any action returns a non-OK status the run continues; check the
    individual ``results[i].status`` to detect step-level failures.
    Hard errors (HTTP / network) propagate as exceptions.
    """
    base = _default_base_url(base_url)
    async with async_paprika.connect(base) as cli:
        async with cli.session(
            initial_url=initial_url,
            worker_id=worker_id,
            lane_hint=lane_hint,
        ) as page:
            results: list[dict[str, Any]] = []
            for act in actions:
                results.append(await _dispatch(page, act))
            final_state = await page.state()
            visited = await page.visited_urls()
            return {
                "session_id": page.session_id,
                "worker_id":  page.worker_id,
                "lane_idx":   page.lane_idx,
                "results":    results,
                "state":      final_state,
                "visited":    visited,
            }


async def _dispatch(page: Page, act: dict) -> dict:
    """Map one action dict to the matching :class:`Page` method.

    Returns the action's HTTP reply (so callers can inspect status /
    elapsed_ms / result). Wrapper records the kind so reading a recipe
    log later is straightforward.
    """
    kind = (act or {}).get("kind") or ""
    info = {"kind": kind}
    try:
        if kind in ("navigate", "goto"):
            reply = await page.goto(act["url"])
        elif kind == "click":
            reply = await page.click(act["selector"])
        elif kind == "type":  # alias of fill
            reply = await page.fill(act["selector"], act.get("text", ""))
        elif kind == "fill":
            reply = await page.fill(act["selector"], act.get("value", ""))
        elif kind == "press_key" or kind == "press":
            reply = await page.press(act["key"])
        elif kind == "scroll":
            reply = await page.scroll(
                act.get("direction", "down"),
                int(act.get("amount", act.get("pixels", 800))),
            )
        elif kind == "back":
            reply = await page.back()
        elif kind == "wait":
            seconds = float(act.get("seconds", 2.0))
            await asyncio.sleep(seconds)
            reply = {"status": "OK", "elapsed_ms": int(seconds * 1000), "result": None}
        elif kind == "capture":
            reply = {
                "status": "OK",
                "result": await page.capture(act.get("label", "capture"),
                                             step=int(act.get("step", 0))),
            }
        elif kind == "outline":
            reply = {"status": "OK", "result": await page.outline()}
        elif kind == "state":
            reply = {"status": "OK", "result": await page.state()}
        elif kind == "screenshot":
            png = await page.screenshot()
            reply = {"status": "OK", "result_bytes": len(png)}
        else:
            raise ValueError(f"unknown action kind: {kind!r}")
        info.update(reply)
    except Exception as e:
        info["status"] = f"ERR: {type(e).__name__}: {e}"
    return info
