"""``/wall`` -- 取得結果の滝 (results waterfall).

A kiosk/visitor-facing page: every card is a real page this fleet just
fetched, sliding in as the fetches land, over a live readout of the true
fleet rate. Deliberately separate from ``/screenshots`` (the operator's lane
grid): that one answers "what is each lane doing", this one answers "how fast
is this thing going, and is it really getting whole pages".

Three routes, all pure Redis reads -- no job-table query, no worker RPC, and
no per-viewer capture, so a room full of people watching costs the same as
one:

  ``GET /wall``                 the page itself (static HTML)
  ``GET /wall/feed?after=<seq>`` new cards since the client's cursor + stats
  ``GET /wall/thumb/{seq}.jpg`` one card's JPEG (immutable, browser-cached)

The producing side lives in :mod:`server.hub.wallfeed`, fed from the
``final.jpg`` upload handler.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from server.hub import wallfeed

router = APIRouter(tags=["Wall"])

_WALL_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "wall.html"
_WALL_HTML_CACHE: tuple[float, str] | None = None


def _wall_html() -> str:
    """The page, re-read whenever the file's mtime moves.

    NOT read-once: ``hub_hash`` (what decides whether a deploy rolls the hubs)
    covers ``.py`` files only, so an HTML-only change reaches every hub's disk
    by rsync with no restart behind it. A process-lifetime cache would then
    keep serving the old markup until something unrelated restarted the hub --
    and, mid-rollout, different hubs would serve different versions of the
    page behind the same round-robin URL. One stat() per page load (not per
    poll) buys that away.
    """
    global _WALL_HTML_CACHE
    try:
        mtime = _WALL_HTML_PATH.stat().st_mtime
    except Exception:
        mtime = 0.0
    if _WALL_HTML_CACHE is not None and _WALL_HTML_CACHE[0] == mtime:
        return _WALL_HTML_CACHE[1]
    try:
        html = _WALL_HTML_PATH.read_text(encoding="utf-8")
    except Exception:
        html = ("<!doctype html><meta charset=utf-8>"
                "<p>wall.html is missing from this build.</p>")
    _WALL_HTML_CACHE = (mtime, html)
    return html


@router.get("/wall", response_class=HTMLResponse)
async def wall_page() -> HTMLResponse:
    return HTMLResponse(_wall_html())


@router.get("/wall/feed")
async def wall_feed(after: int = 0, limit: int = 40, series_s: int = 60) -> dict:
    """New cards + the fleet's real speed, in one Redis round-trip.

    ``after`` is the highest ``seq`` the client has already rendered; pass the
    response's ``seq`` back on the next poll. ``after=0`` (a fresh page load)
    returns the whole visible backlog so the wall is full immediately instead
    of filling in over the next ten seconds.
    """
    return await wallfeed.bundle(after=after, limit=limit, series_s=series_s)


@router.get("/wall/thumb/{seq}.jpg")
async def wall_thumb(seq: int) -> Response:
    """One card's JPEG. Content at a given seq never changes, so this is
    cached hard -- a card that scrolls off and back never re-fetches."""
    data = await wallfeed.thumb(seq)
    if not data:
        raise HTTPException(404, "thumbnail expired")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=900, immutable"},
    )
