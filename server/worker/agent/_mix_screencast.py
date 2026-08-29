"""WorkerAgent mixin: CDP-screencast relay for the Linux fleet's Live
viewer -- an env-gated alternative to the VNC/x11vnc bridge (see
``WorkerCapabilities.screencast_relay`` / ``PAPRIKA_LIVE_VIEW_SCREENCAST``).

Windows portable already skips VNC by having the HUB dial the worker's
Chrome CDP port directly (routes/screencast.py). That only works because
paprika.exe's CDP port is reachable from the hub; the Linux fleet's
per-lane CDP ports live inside each worker's container network the same
way the VNC port does, so the hub can't dial them either.

Instead the WORKER opens its own loopback CDP connection to the lane's
Chrome (CDP accepts multiple concurrent sessions per target, so this
doesn't disturb whatever job/session already has the lane attached) and
relays ``Page.screencastFrame`` events + CDP command replies over the
ALREADY-CONNECTED worker<->hub control WebSocket, tagged by session_id.
That's the whole point: a VNC viewer costs the hub a fresh TCP dial +
RFB handshake to the worker for every open; this rides a connection
that's already up, so opening a Live view is just one WS message each
way instead of a new network round trip.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import websockets

from server.protocol import (
    HubScreencastClose,
    HubScreencastCmd,
    HubScreencastOpen,
    WorkerScreencastClosed,
    WorkerScreencastEvent,
)


def screencast_relay_enabled() -> bool:
    """``PAPRIKA_LIVE_VIEW_SCREENCAST=1`` switches this worker's Live
    viewer from the default VNC/x11vnc bridge to the CDP-screencast
    relay. x11vnc/websockify keep running on every lane regardless --
    this only changes which viewer URL the hub hands to clients, so
    flipping it back is a plain restart with no other side effects."""
    return os.environ.get("PAPRIKA_LIVE_VIEW_SCREENCAST", "").strip().lower() in (
        "1", "true", "yes",
    )


def _id_seq():
    n = 0
    while True:
        n += 1
        yield n


async def _open_local_cdp_target(chrome_port: int) -> str:
    """Pick the first ``type=page`` CDP target on localhost and return its
    websocket debugger URL. Mirrors the hub's Windows-portable equivalent
    (``routes/screencast.py:_open_cdp_target``) but always targets
    127.0.0.1 -- this runs INSIDE the worker, next to its own lane's
    Chrome, never across the LAN."""
    url = f"http://127.0.0.1:{chrome_port}/json"
    async with httpx.AsyncClient(timeout=3.0) as http:
        r = await http.get(url)
        r.raise_for_status()
        targets = r.json()
    pages = [
        t for t in targets
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]
    if not pages:
        raise RuntimeError("no Chrome page target available")
    pages.sort(key=lambda t: (not bool(t.get("url", "")), t.get("id", "")))
    return pages[0]["webSocketDebuggerUrl"]


async def _close_ws_quietly(ws) -> None:
    try:
        await ws.close()
    except Exception:
        pass


class _ScreencastRelayMixin:
    async def _handle_screencast_open(self, msg: HubScreencastOpen) -> None:
        if msg.session_id in self._screencast_upstreams:
            return  # duplicate open (hub retry) -- already running
        try:
            if self.lane_pool is None:
                raise RuntimeError("worker has no lane pool")
            if not (0 <= msg.lane_idx < len(self.lane_pool.lanes)):
                raise RuntimeError(
                    f"lane_idx {msg.lane_idx} out of range "
                    f"(have {len(self.lane_pool.lanes)} lanes)"
                )
            lane = self.lane_pool.lanes[msg.lane_idx]
            cdp_url = await _open_local_cdp_target(lane.chrome_port)
            upstream = await websockets.connect(
                cdp_url,
                max_size=32 * 1024 * 1024,  # JPEG frames can be ~MB on big viewports
                ping_interval=20,
            )
        except Exception as e:
            try:
                await self._send(WorkerScreencastClosed(
                    session_id=msg.session_id,
                    error=f"{type(e).__name__}: {e}",
                ))
            except Exception:
                pass
            return

        self._screencast_upstreams[msg.session_id] = upstream
        self._screencast_lanes[msg.session_id] = msg.lane_idx
        next_id = _id_seq()

        async def _pump() -> None:
            notify_closed = True
            try:
                await upstream.send(json.dumps({
                    "id": next(next_id), "method": "Page.enable",
                }))
                await upstream.send(json.dumps({
                    "id": next(next_id),
                    "method": "Page.startScreencast",
                    "params": {
                        "format": "jpeg",
                        "quality": msg.quality,
                        "maxWidth": msg.max_width,
                        "maxHeight": msg.max_height,
                        "everyNthFrame": 1,
                    },
                }))
                async for raw in upstream:
                    try:
                        payload = json.loads(raw)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    # Auto-ack screencastFrame so Chrome keeps streaming.
                    # Done here (not by the hub/viewer) so a relay-mode
                    # session needs no special client-side logic beyond
                    # what the direct-CDP (Windows) viewer already does.
                    if payload.get("method") == "Page.screencastFrame":
                        frame_sid = (payload.get("params") or {}).get("sessionId")
                        if frame_sid is not None:
                            try:
                                await upstream.send(json.dumps({
                                    "id": next(next_id),
                                    "method": "Page.screencastFrameAck",
                                    "params": {"sessionId": frame_sid},
                                }))
                            except Exception:
                                break
                    try:
                        await self._send(WorkerScreencastEvent(
                            session_id=msg.session_id, payload=payload,
                        ))
                    except Exception:
                        # Control WS is gone -- nothing left to notify.
                        notify_closed = False
                        break
            except asyncio.CancelledError:
                # External close (HubScreencastClose / lane release): the
                # caller already knows and is tearing down state itself --
                # _close_screencast's own finally-driven call would
                # otherwise re-send a redundant WorkerScreencastClosed.
                notify_closed = False
                raise
            except Exception as e:
                try:
                    await self._send(WorkerScreencastClosed(
                        session_id=msg.session_id,
                        error=f"{type(e).__name__}: {e}",
                    ))
                    notify_closed = False
                except Exception:
                    pass
            finally:
                self._close_screencast(msg.session_id, _notify=notify_closed)

        self._screencast_tasks[msg.session_id] = asyncio.create_task(
            _pump(), name=f"screencast-relay-{msg.session_id[:8]}",
        )

    async def _handle_screencast_cmd(self, msg: HubScreencastCmd) -> None:
        upstream = self._screencast_upstreams.get(msg.session_id)
        if upstream is None:
            return
        try:
            await upstream.send(json.dumps(msg.payload))
        except Exception:
            pass

    async def _handle_screencast_close(self, msg: HubScreencastClose) -> None:
        self._close_screencast(msg.session_id, _notify=False)

    def _close_screencast(self, session_id: str, *, _notify: bool = True) -> None:
        """Tear down one session's screencast relay: cancel the pump task
        and close the local CDP connection. Safe no-op if nothing is
        open. Called explicitly (HubScreencastClose, lane release) and
        from the pump's own finally block (``_notify=False`` there --
        it already sent its own WorkerScreencastClosed on failure, or
        the hub already knows because IT asked for the close)."""
        self._screencast_lanes.pop(session_id, None)
        task = self._screencast_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        upstream = self._screencast_upstreams.pop(session_id, None)
        if upstream is not None:
            asyncio.create_task(_close_ws_quietly(upstream))
        if _notify:
            asyncio.create_task(self._send_screencast_closed_quietly(session_id))

    async def _send_screencast_closed_quietly(self, session_id: str) -> None:
        try:
            await self._send(WorkerScreencastClosed(session_id=session_id))
        except Exception:
            pass

    def _close_screencasts_for_lane(self, lane_idx: int) -> None:
        """Call right before a lane is released back to the pool (job end
        / session teardown) so a leftover screencast relay never keeps
        streaming frames of a lane about to run someone else's job.
        Cheap no-op when nothing was open on that lane."""
        for sid, li in list(self._screencast_lanes.items()):
            if li == lane_idx:
                self._close_screencast(sid)
