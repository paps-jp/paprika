"""noVNC HTTP + WebSocket proxy routes: /sessions/{id}/novnc/...

Why session-rooted (not job-rooted)
-----------------------------------
Worker advertises ``http://<worker-LAN-IP>:6080+lane/vnc_lite.html``
directly. Exposing 26 workers' IPs through a corporate proxy /
external viewer is operationally horrible: 26 ACL rules, 26 routing
entries, 26 cert pinnings. The hub is already the single entry point
everyone can reach (operator opens the admin UI through it). Route
noVNC through it too -- one URL, one cert, one ACL.

A noVNC view is fundamentally a window into ONE worker's ONE lane.
``SessionInfo`` carries the (worker_id, lane_idx) pair as a first-
class field; ``JobInfo`` only references the session indirectly via
``info.session_id``. Mapping the proxy onto the session means
1 URL = 1 lane = 1 noVNC view; session_id is ~128 bits of
``secrets.token_urlsafe(16)`` so URLs can't be guessed.

Three endpoints implement the proxy:

  GET  /sessions/{session_id}/novnc/                  -> vnc_lite.html
  GET  /sessions/{session_id}/novnc/{subpath:path}    -> any viewer asset
  WS   /sessions/{session_id}/novnc/websockify        -> RFB binary bridge

Plus the URL-builder helpers (``_hub_proxied_novnc_url`` and
``_proxy_info`` / ``_proxy_session_dict``) the rest of the hub uses to
rewrite ``novnc_url`` fields on JobInfo / SessionInfo response bodies.
app.py re-exports those for /jobs handlers that still live there.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse

from server.hub._state import state
from server.protocol import JobInfo

router = APIRouter(tags=["noVNC"])


# Inside-the-container default. Matches docker/worker/Dockerfile's
# NOVNC_BASE_PORT=6080. Lane port = NOVNC_BASE_PORT + lane_idx.
_NOVNC_BASE_PORT = 6080


# ----------------------------------------------------------------------------
# Session/JobInfo -> proxied URL builders (re-exported from app.py)
# ----------------------------------------------------------------------------


def _resolve_session_novnc_target(
    session_id: str,
) -> tuple[str, int] | None:
    """Map a ``session_id`` to ``(host, port)`` of its worker's noVNC.

    Single-step resolution: SessionInfo carries both ``worker_id`` and
    ``lane_idx`` as first-class fields, so we go straight from session
    -> worker connection -> client_address. No JobInfo round trip.

    Returns None when:
      * the session is unknown (already reaped, never existed, etc.)
      * the session has no lane bound
      * the worker is not currently connected to this hub
      * the worker's client_address is missing (impossible in practice
        but defended against to avoid building bogus URLs)
      * the worker advertises ``edition=portable`` / ``platform=windows``
        (= Windows portable build has no TightVNC + websockify; it uses
        the CDP-screencast based live viewer instead. The screencast
        URL is surfaced via ``_hub_proxied_novnc_url`` so admin UI's
        existing "live noVNC" link still works, just opens a different
        viewer page.)
    """
    if state.sessions is None or state.registry is None:
        return None
    sess = state.sessions.get(session_id)
    if sess is None or sess.lane_idx is None or not sess.worker_id:
        return None
    worker = state.registry.connections.get(sess.worker_id)
    if worker is None:
        return None
    # Skip workers that opt out of regular noVNC. Used by the Windows
    # portable edition; the URL builder below redirects those to the
    # CDP screencast viewer instead.
    labels = getattr(worker.capabilities, "labels", None) or {}
    if labels.get("edition") == "portable" or labels.get("platform") == "windows":
        return None
    host = (worker.client_address or "").strip()
    if not host:
        return None
    return (host, _NOVNC_BASE_PORT + int(sess.lane_idx))


def _has_screencast_target(session_id: str) -> bool:
    """True iff this session's worker advertises ``chrome_attach_port``
    (= it can serve the CDP-screencast live viewer at
    ``/sessions/{sid}/screencast/``)."""
    if state.sessions is None or state.registry is None:
        return False
    sess = state.sessions.get(session_id)
    if sess is None or not sess.worker_id:
        return False
    worker = state.registry.connections.get(sess.worker_id)
    if worker is None:
        return False
    return bool(getattr(worker.capabilities, "chrome_attach_port", None))


def _find_active_session_id(info: JobInfo) -> str | None:
    """Pick the session_id to expose as ``info.novnc_url`` for a job.

    Priority:
      1. ``info.session_id`` if the session still exists in the
         registry. Fetch-mode jobs set this explicitly at dispatch
         and it doesn't move during the job's lifetime, so this is
         the stable, "correct" answer for the common case.
      2. For codegen-loop / vision-agent jobs where ``info.session_id``
         was never wired up, fall back to scanning the SessionRegistry
         for entries tagged with this job_id and pick the most-recently
         active one. Each codegen-loop attempt spawns its own session,
         so the surfaced URL automatically tracks the current attempt.

    Returns None if nothing matches (queued job, between attempts,
    sessions all reaped, ...).
    """
    if state.sessions is None:
        return None
    if info.session_id:
        if state.sessions.get(info.session_id) is not None:
            return info.session_id
    matches = [
        s for s in state.sessions.all() if s.job_id == info.job_id and s.lane_idx is not None
    ]
    if not matches:
        return None
    matches.sort(key=lambda s: s.last_active_at, reverse=True)
    return matches[0].session_id


def _hub_proxied_novnc_url(
    info: JobInfo,
    request: Request | None = None,
) -> str | None:
    """Build the hub-relative, session-rooted live-viewer URL for a job.

    Three branches, in order:

      1. Worker has chrome_attach_port (= Windows portable build with
         bundled Chromium): use the CDP-screencast viewer at
         ``/sessions/{sid}/screencast/``. Works in headless too.

      2. Worker has a real noVNC bridge (= Linux fleet with Xvfb +
         lane VNC): the traditional ``vnc_lite.html`` proxy:
         ``/sessions/{sid}/novnc/?path=sessions/{sid}/novnc/websockify``

      3. Neither: return None so admin UI / SDK omits the live link.

    Output shape (case 2)::

        /sessions/{sid}/novnc/?path=sessions/{sid}/novnc/websockify
                              &autoconnect=1&resize=scale&reconnect=1
    """
    sid = _find_active_session_id(info)
    if sid is None:
        return None
    # Branch 1: Windows portable -> CDP screencast viewer.
    if _has_screencast_target(sid):
        return f"/sessions/{sid}/screencast/"
    # Branch 2: traditional noVNC. _resolve_session_novnc_target returns
    # None for opt-out workers (Windows portable already caught above).
    if _resolve_session_novnc_target(sid) is None:
        return None
    return (
        f"/sessions/{sid}/novnc/"
        f"?path=sessions/{sid}/novnc/websockify"
        f"&autoconnect=1&resize=scale&reconnect=1"
    )


def _proxy_info(info: JobInfo, request: Request | None = None) -> JobInfo:
    """Return a copy of ``info`` with ``novnc_url`` rewritten to the
    hub-proxied, session-rooted URL.

    Storage on disk / in Redis is left untouched: the worker-direct URL
    stays in the canonical record. Only API responses get rewritten so
    a future "expose direct LAN URL again" toggle is one line away.
    """
    proxied = _hub_proxied_novnc_url(info, request)
    if proxied is None:
        return info
    return info.model_copy(update={"novnc_url": proxied})


def _hub_proxied_novnc_url_for_session(session_id: str) -> str | None:
    """Same shape as :func:`_hub_proxied_novnc_url` but built straight
    from a ``session_id`` (no JobInfo round-trip).

    Used by /sessions and /sessions/{sid} response rewriting so admin
    UI session tiles + Live panel session iframes get the hub-proxy
    URL automatically. Same screencast / noVNC branching as the
    JobInfo-keyed version.
    """
    if _has_screencast_target(session_id):
        return f"/sessions/{session_id}/screencast/"
    if _resolve_session_novnc_target(session_id) is None:
        return None
    return (
        f"/sessions/{session_id}/novnc/"
        f"?path=sessions/{session_id}/novnc/websockify"
        f"&autoconnect=1&resize=scale&reconnect=1"
    )


def _proxy_session_dict(d: dict) -> dict:
    """Rewrite ``novnc_url`` on a session dict (from
    ``SessionInfo.to_json``) to point at the hub proxy. Operates on
    the plain dict because SessionInfo is a dataclass we don't want to
    copy-construct in the hot path.

    Returns the same dict (mutated) for chaining convenience.
    """
    sid = d.get("session_id")
    if not sid:
        return d
    proxied = _hub_proxied_novnc_url_for_session(sid)
    if proxied is not None:
        d["novnc_url"] = proxied
    return d


# ----------------------------------------------------------------------------
# HTTP forwarding plumbing
# ----------------------------------------------------------------------------

# Headers we DROP when proxying upstream HTTP responses back to the
# client. Hop-by-hop per RFC 7230 §6.1 + a few that httpx already
# manages itself (we re-encode the body, so transfer-encoding /
# content-length from upstream may not match).
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-encoding",
        "content-length",
    }
)


# Shared HTTP client for the asset proxy. Re-using a single AsyncClient
# (= keep-alive pool of TCP connections to workers) is much faster than
# building a fresh client per request: a noVNC viewer load pulls 30-50
# .js modules, and a 3-way TCP handshake each would add ~1-2 seconds of
# cold-start latency. Lazy-initialised on first use so import-time
# doesn't depend on the event loop being ready.
_novnc_http_client: httpx.AsyncClient | None = None


# Active noVNC WebSocket proxies, keyed by session_id. Used by
# close_session() to force-disconnect operator viewers the moment a
# session ends (reaper / explicit DELETE / cascade from job
# completion). Without this, the bridge between operator's browser
# and the worker's websockify would linger past session death --
# operator would see a frozen / stuck noVNC and not realise the
# browser is gone. Multi-tab viewer multiplexing (= 1 session viewed
# from N tabs) is supported via the set value.
_session_novnc_clients: dict[str, set[WebSocket]] = {}


async def _disconnect_session_novnc_clients(session_id: str) -> int:
    """Force-close every operator-facing noVNC WS bridge for
    ``session_id``. Returns the count of bridges closed.

    Called from close_session() (= reaper, DELETE /sessions, cascade
    from job complete) so the operator's noVNC tab gets an immediate
    "disconnected" close frame instead of staring at a frozen image.
    The bridge's own ``finally`` block normally handles cleanup, but
    only AFTER one of its two forwarder tasks notices the upstream
    has gone -- which can take 30s+ when the worker's websockify is
    still alive but the underlying VNC server is gone. Active close
    cuts that to ~0ms.

    Best-effort: a per-ws close failure must not block the rest.
    """
    bucket = _session_novnc_clients.pop(session_id, None)
    if not bucket:
        return 0
    closed = 0
    for ws in list(bucket):
        try:
            # 1000 = normal closure. Browsers / noVNC viewer treat
            # this as "the server politely closed" and surface a
            # "disconnected" state instead of a reconnect spinner.
            await ws.close(code=1000, reason="session ended")
            closed += 1
        except Exception:
            # Best-effort; the bridge's own teardown will catch the
            # broken socket on its next forwarder iteration.
            pass
    return closed


def _get_novnc_http_client() -> httpx.AsyncClient:
    """Return (and lazily build) the shared httpx client used by the
    noVNC asset proxy. Pool size is sized for ~25 workers x ~4 lanes
    each being viewed in parallel; well above realistic ceiling."""
    global _novnc_http_client
    if _novnc_http_client is None:
        _novnc_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, read=60.0),
            limits=httpx.Limits(
                max_connections=200,
                max_keepalive_connections=100,
            ),
        )
    return _novnc_http_client


# Asset MIME types that are safe to cache aggressively (= the noVNC
# viewer ships these inside the docker image; they only change when
# we rebuild the worker image). Keeping them out of the browser cache
# would make every page reload re-download ~50 .js modules through
# the hub, which is the biggest single source of hub asset proxy
# load. 1 day TTL: gives plenty of caching headroom without making
# bad-deploy recovery feel sluggish.
_NOVNC_CACHEABLE_TYPES = (
    "application/javascript",
    "text/javascript",
    "text/css",
    "image/png",
    "image/svg+xml",
    "image/x-icon",
    "image/vnd.microsoft.icon",
    "font/woff",
    "font/woff2",
    "application/font-woff",
)


def _is_cacheable_asset(content_type: str) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct in _NOVNC_CACHEABLE_TYPES


async def _proxy_novnc_get(
    host: str,
    port: int,
    subpath: str,
    request: Request,
) -> Response:
    """Forward a GET to ``http://{host}:{port}/{subpath}`` and stream
    the response back. Used by both the index (vnc_lite.html) and the
    arbitrary asset proxy.

    Body is streamed (no buffering) -- safe for large fonts / images
    that noVNC may pull. The response is closed in the body iterator's
    finally so a client disconnect mid-stream doesn't leak.

    Caching: viewer assets (.js / .css / fonts / icons) get a 1-day
    browser cache header so a page reload doesn't re-pull 50 files
    through the hub. The viewer HTML itself (vnc_lite.html) stays
    no-store so a hub redeploy is immediately reflected.
    """
    upstream = f"http://{host}:{port}/{subpath}"
    cli = _get_novnc_http_client()
    try:
        fwd_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower()
            in (
                "range",
                "if-modified-since",
                "if-none-match",
                "accept",
                "accept-encoding",
            )
        }
        req = cli.build_request("GET", upstream, headers=fwd_headers)
        r = await cli.send(req, stream=True)
    except Exception as e:
        raise HTTPException(
            502,
            f"upstream worker noVNC unreachable: {type(e).__name__}: {e}",
        )

    async def _iter_body():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            try:
                await r.aclose()
            except Exception:
                pass
            # NOTE: we do NOT close cli -- it's the shared pool.

    resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}
    # Caching policy: vnc_lite.html itself stays uncached (so a hub
    # redeploy lands instantly); everything else is fingerprintable
    # static content that browsers should be allowed to cache.
    ct = r.headers.get("content-type", "")
    is_html_entry = subpath in ("", "vnc_lite.html") or "html" in ct.lower()
    if is_html_entry:
        resp_headers["Cache-Control"] = "no-store"
    elif _is_cacheable_asset(ct):
        resp_headers["Cache-Control"] = "public, max-age=86400, immutable"
    else:
        resp_headers.setdefault("Cache-Control", "public, max-age=300")

    return StreamingResponse(
        _iter_body(),
        status_code=r.status_code,
        headers=resp_headers,
        media_type=ct or None,
    )


async def _proxy_novnc_websocket(
    ws: WebSocket,
    host: str,
    port: int,
    *,
    session_id: str | None = None,
) -> None:
    """Bidirectional WS bridge from the accepted client ``ws`` to the
    worker's websockify at ``ws://{host}:{port}/websockify``.

    Caller is responsible for ``ws.accept()`` (which has to happen
    before this is called so we can mirror the subprotocol). On any
    side closing, the partner task is cancelled and both ends are
    closed defensively.

    ``session_id``: when set, the proxy taps operator-driven RFB
    messages on the client->upstream stream (KeyEvent / PointerEvent
    / ClientCutText) and touches the session's ``last_active_at``
    so the idle reaper's 2-minute timer reflects real operator
    activity. Keepalive-ish messages (FramebufferUpdateRequest, msg
    type 3) are deliberately NOT counted -- they fire from the noVNC
    viewer regardless of whether the operator is at the keyboard.
    Throttled to once per 10 s to avoid a hot loop of Redis writes
    on rapid mouse movement.
    """
    import websockets

    upstream_url = f"ws://{host}:{port}/websockify"
    chosen = ws.scope.get("__chosen_subproto")  # set by the route handler
    try:
        upstream = await websockets.connect(
            upstream_url,
            subprotocols=[chosen] if chosen else None,
            # RFB messages are normally 1-100 KB. 1 MiB cap is a 10x
            # safety margin over real-world max while keeping the
            # per-connection memory ceiling low (cf. earlier 32 MiB
            # which was DoS amplification target + viewer-disconnect
            # buffer-release lag).
            max_size=2**20,
            ping_interval=20,
            ping_timeout=20,
        )
    except Exception as e:
        try:
            await ws.close(code=1011, reason=f"upstream unreachable: {type(e).__name__}")
        except Exception:
            pass
        return

    # Activity-touch state (per-bridge). RFB ClientToServer msg-type
    # bytes that count as real operator interaction:
    #   2  SetEncodings           (handshake / on-connect only)
    #   4  KeyEvent
    #   5  PointerEvent
    #   6  ClientCutText
    # Type 3 (FramebufferUpdateRequest) is the chatty viewer keepalive
    # and is deliberately NOT counted -- we want idle = "operator has
    # gone AFK", not "viewer is still attached but nobody's typing".
    _OPERATOR_RFB_TYPES = (4, 5, 6)
    _ACTIVITY_THROTTLE_S = 10.0
    _last_touch_t = 0.0

    async def _maybe_touch(frame: bytes) -> None:
        nonlocal _last_touch_t
        if session_id is None or state.sessions is None:
            return
        if not frame:
            return
        try:
            msg_type = frame[0]
        except Exception:
            return
        if msg_type not in _OPERATOR_RFB_TYPES:
            return
        # Throttle: at most one touch per _ACTIVITY_THROTTLE_S window
        # so a fast mouse drag (= hundreds of PointerEvents per sec)
        # doesn't slam the registry.
        import time as _time

        now = _time.monotonic()
        if now - _last_touch_t < _ACTIVITY_THROTTLE_S:
            return
        _last_touch_t = now
        # Refresh last_active_at so a watching operator's session isn't
        # idle-reaped.  state-model v1: we intentionally DO NOT flip the
        # job phase keepalive->running here anymore (the oscillation was
        # removed); keepalive is a stable phase.  "Is someone watching"
        # can be re-derived from live RFB connections separately without
        # mutating job state.
        try:
            state.sessions.touch(session_id)
        except Exception:
            pass

    async def _client_to_upstream() -> None:
        try:
            while True:
                msg = await ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    return
                if "bytes" in msg and msg["bytes"] is not None:
                    await _maybe_touch(msg["bytes"])
                    await upstream.send(msg["bytes"])
                elif "text" in msg and msg["text"] is not None:
                    await upstream.send(msg["text"])
        except (WebSocketDisconnect, Exception):
            return

    async def _upstream_to_client() -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, (bytes, bytearray)):
                    await ws.send_bytes(bytes(frame))
                else:
                    await ws.send_text(frame)
        except Exception:
            return

    # Register this bridge so close_session() can force-disconnect it
    # when the session goes away. Multi-viewer for the same session
    # is supported via the set.
    if session_id is not None:
        _session_novnc_clients.setdefault(session_id, set()).add(ws)
    try:
        c2s = asyncio.create_task(_client_to_upstream())
        s2c = asyncio.create_task(_upstream_to_client())
        done, pending = await asyncio.wait(
            {c2s, s2c},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        if session_id is not None:
            bucket = _session_novnc_clients.get(session_id)
            if bucket is not None:
                bucket.discard(ws)
                if not bucket:
                    _session_novnc_clients.pop(session_id, None)
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Public endpoints
# ----------------------------------------------------------------------------


@router.get("/sessions/{session_id}/novnc/", response_class=Response)
async def session_novnc_index(session_id: str, request: Request) -> Response:
    """Serve the worker's ``vnc_lite.html`` for the given session.

    The HTML is fetched verbatim from the worker. vnc_lite.html reads
    its own URL's query string for ``?host=&port=&path=`` so the
    operator's URL controls where the WebSocket lands -- no HTML
    rewriting needed.

    Returns 404 if the session is unknown (already reaped, etc.).
    Returns 502 if the session exists but the worker has lost its
    upstream HTTP socket somehow.
    """
    target = _resolve_session_novnc_target(session_id)
    if target is None:
        raise HTTPException(404, f"session '{session_id}' not found or not bound to a lane")
    host, port = target
    return await _proxy_novnc_get(host, port, "vnc_lite.html", request)


@router.get("/sessions/{session_id}/novnc/{subpath:path}")
async def session_novnc_asset(
    session_id: str,
    subpath: str,
    request: Request,
):
    """Proxy any viewer asset (JS modules, CSS, fonts, images) for the
    given session through the hub.

    ``subpath`` is everything after ``/novnc/``, e.g. ``core/rfb.js``
    or ``vendor/pako/lib/zlib/inflate.js``. The viewer's relative
    imports under base ``/sessions/{sid}/novnc/`` all flow through
    this route.

    ``websockify`` as the subpath is normally claimed by the explicit
    WebSocket route below (FastAPI matches WS routes before catch-all
    HTTP). A bare HTTP GET on that path lands here and forwards to
    upstream which replies with whatever websockify does on a
    non-upgrade request -- typically 400. That's a fine no-op.
    """
    target = _resolve_session_novnc_target(session_id)
    if target is None:
        raise HTTPException(404, f"session '{session_id}' not found or not bound to a lane")
    host, port = target
    return await _proxy_novnc_get(host, port, subpath, request)


@router.websocket("/sessions/{session_id}/novnc/websockify")
async def session_novnc_websockify(ws: WebSocket, session_id: str) -> None:
    """Bidirectional WS bridge between a client browser and the
    worker's websockify for ``session_id``.

    Lifecycle:
      * Resolve the session BEFORE ``accept()`` so a rejection comes
        back as a clean 1008/1011 close rather than a half-open
        handshake that browser DevTools can't diagnose.
      * Mirror the client's Sec-WebSocket-Protocol (``binary`` or
        ``base64``) when dialing upstream so framing stays consistent.
      * On either side closing, cancel the partner forwarder and
        defensively close both ends.
    """
    # Pick the subprotocol from the client's offer BEFORE accept().
    offered = [
        s.strip() for s in ws.headers.get("sec-websocket-protocol", "").split(",") if s.strip()
    ]
    chosen = next((p for p in offered if p in ("binary", "base64")), None)

    target = _resolve_session_novnc_target(session_id)
    if target is None:
        # No accept(): client gets the close frame on a brand-new
        # handshake which is what browsers actually display sensibly.
        await ws.close(code=1008, reason="session not found or not bound to a lane")
        return
    host, port = target

    await ws.accept(subprotocol=chosen)
    # Stash the chosen subprotocol where the helper can pick it up.
    ws.scope["__chosen_subproto"] = chosen
    await _proxy_novnc_websocket(ws, host, port, session_id=session_id)
