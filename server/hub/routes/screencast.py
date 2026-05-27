"""CDP-Screencast based live viewer (Windows portable noVNC replacement).

Linux fleet 版は Xvfb + x11vnc + websockify + noVNC で物理画面を
viewer に配信する。Windows portable はそのスタックを同梱しない
(TightVNC ライセンス + サイズ + 安定性のトレードオフで v1.1 送り
にしてた) ので、代わりに Chrome DevTools Protocol の
``Page.startScreencast`` を使う。

利点:
  * **headless Chrome でも動く** -- 物理画面を必要としない。
    Chrome の内部 render buffer から直接 JPEG を取り出す
  * 追加バイナリ同梱なし (Chrome 自身が screencast を吐く)
  * 双方向: マウス / キーボードイベントを ``Input.dispatchMouseEvent``
    / ``Input.dispatchKeyEvent`` で worker Chrome に転送できる
  * noVNC 規格ではないが UI 体験は同等

エンドポイント:
  GET /sessions/{sid}/screencast/        -- HTML viewer page
  WS  /sessions/{sid}/screencast/ws      -- bi-di frame + input bridge

WS の wire format:
  Server -> Client (frame):
    {"type": "frame", "data": "<base64 jpeg>", "w": 1280, "h": 720,
     "metadata": {...}}
  Server -> Client (cursor reply):
    {"type": "cursor", "value": "pointer"|"text"|"default"|...}
  Client -> Server (input):
    {"type": "mouse", "event": "mousePressed"|"mouseReleased"|"mouseMoved",
     "x": int, "y": int, "button": "left"|"right"|"middle"|"none",
     "clickCount": int, "modifiers": int}
    {"type": "key", "event": "keyDown"|"keyUp"|"char",
     "key": str, "code": str, "text": str, "modifiers": int}
    {"type": "resize", "w": int, "h": int}
    {"type": "cursor_query", "x": int, "y": int}
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from server.hub._state import state

log = logging.getLogger(__name__)
router = APIRouter(tags=["Screencast"])


def _resolve_session_chrome_target(session_id: str) -> tuple[str, int] | None:
    """Find the CDP HTTP endpoint for a session's chrome.

    Mirrors ``routes/novnc.py:_resolve_session_novnc_target`` but returns
    ``(host, chrome_port)`` instead of ``(host, novnc_port)``. Pulled
    out as its own helper because Windows portable workers register
    their chrome_port via the WindowsWorkerSupervisor + capabilities
    pipeline, and the CDP port is the same one the worker's nodriver
    session attaches to.

    Returns None when:
      * session not found / not bound to a lane
      * worker not connected
      * worker didn't advertise a chrome_port (Linux fleet workers
        don't, by design -- they use Xvfb + per-lane VNC instead)
    """
    if state.sessions is None or state.registry is None:
        return None
    sess = state.sessions.get(session_id)
    if sess is None or not sess.worker_id:
        return None
    worker = state.registry.connections.get(sess.worker_id)
    if worker is None:
        return None
    # ``chrome_attach_port`` is what WorkerAgent advertises in
    # WorkerCapabilities when it was started with chrome_host/port (=
    # "I'm attached to an external Chrome at this port"). Linux fleet
    # workers use their own per-lane Xvfb + chrome and don't fill this
    # in -- they get the regular noVNC bridge instead.
    chrome_port = getattr(worker.capabilities, "chrome_attach_port", None)
    if chrome_port is None:
        return None
    # Prefer the worker's attach_host (the actual host the worker
    # connected to chrome at; usually 127.0.0.1 for windows portable),
    # then fall back to the worker's client_address. For a same-PC
    # paprika.exe these resolve to the same thing.
    host = (
        getattr(worker.capabilities, "chrome_attach_host", None)
        or worker.client_address
        or "127.0.0.1"
    ).strip()
    return (host, int(chrome_port))


def _resolve_worker_chrome_target(
    worker_id: str,
    lane_idx: int,
) -> tuple[str, int] | None:
    """Lane-keyed variant of :func:`_resolve_session_chrome_target`.

    Lets the admin UI's Live Preview tile open the screencast viewer
    even when no session is currently bound to the lane (= operator
    wants to peek at an idle Chromium). Single-Chromium workers
    (Windows portable) ignore the lane_idx because there's only one
    lane; lane-pool workers would honor it once they're ported to the
    screencast pipeline.

    Returns ``None`` when the worker isn't connected or hasn't
    advertised a chrome_attach_port."""
    if state.registry is None:
        return None
    worker = state.registry.connections.get(worker_id)
    if worker is None:
        return None
    chrome_port = getattr(worker.capabilities, "chrome_attach_port", None)
    if chrome_port is None:
        return None
    host = (
        getattr(worker.capabilities, "chrome_attach_host", None)
        or worker.client_address
        or "127.0.0.1"
    ).strip()
    # lane_idx is unused for single-Chromium portable workers; future
    # multi-lane portable support would map lane_idx -> per-lane chrome
    # port here. We accept (and ignore) the argument so the URL shape
    # is forward-compatible.
    _ = lane_idx
    return (host, int(chrome_port))


async def _open_cdp_target(host: str, chrome_port: int) -> str:
    """Pick the first ``type=page`` target from Chrome's REST API and
    return its WebSocket debugger URL.

    nodriver may already be attached to one of the targets via its own
    session. CDP targets accept multiple concurrent sessions, so our
    screencast WS does not have to wait for nodriver to detach."""
    url = f"http://{host}:{chrome_port}/json"
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
    # Prefer a target whose URL isn't blank (= nodriver hasn't taken
    # over the about:blank lane-init tab yet). Falls back to first.
    pages.sort(key=lambda t: (not bool(t.get("url", "")), t.get("id", "")))
    return pages[0]["webSocketDebuggerUrl"]


# ---------------------------------------------------------------------------
# HTML viewer
# ---------------------------------------------------------------------------


_VIEWER_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>paprika screencast</title>
<style>
  html,body { margin:0; padding:0; background:#222; color:#ddd;
              font-family: ui-monospace, monospace; overflow: hidden; }
  #status { position: fixed; top: 4px; right: 8px; padding: 2px 8px;
            background: rgba(0,0,0,.6); border-radius: 3px; font-size: 11px;
            z-index: 10; }
  #status.ok  { color:#9f9; }
  #status.err { color:#f99; }
  #wrap { display: flex; align-items: center; justify-content: center;
          width: 100vw; height: 100vh; }
  /* Stack canvas + invisible IME catcher in the same box so the
     textarea sits exactly on top of the canvas. Required for the
     OS IME (Japanese / Chinese) candidate popup to anchor near where
     the user is actually typing. */
  #vp { position: relative; max-width: 100%; max-height: 100%; }
  canvas { background:#000; cursor: default; max-width:100%; max-height:100%;
           outline: none; display: block; }
  /* IME catcher. Sits over the canvas, takes keyboard focus, is
     completely invisible (transparent bg/text/caret). pointer-events:
     none lets mouse clicks fall through to the canvas underneath.
     The OS-level IME candidate popup anchors to this textarea, so
     the operator sees the composition candidates near the canvas. */
  #ime {
    position: absolute; top: 0; left: 0;
    width: 100%; height: 100%;
    border: 0; outline: 0; padding: 0; margin: 0; resize: none;
    background: transparent; color: transparent; caret-color: transparent;
    pointer-events: none;
    font-size: 16px;  /* big enough that mobile/desktop IMEs anchor sensibly */
  }
  /* Chrome-ish context menu. The look mimics Chrome's actual menu:
     white background, faint border, 6px radius shadow, 13px Roboto-y
     font, light gray hover. */
  #ctxmenu {
    position: fixed; z-index: 100;
    background: #ffffff; color: #202124;
    border: 1px solid #dadce0;
    border-radius: 8px;
    box-shadow: 0 4px 12px rgba(0,0,0,.18);
    padding: 6px 0;
    min-width: 220px; max-width: 360px;
    font-family: "Segoe UI", system-ui, -apple-system, "Helvetica Neue", sans-serif;
    font-size: 13px; line-height: 1;
    user-select: none;
    display: none;
  }
  #ctxmenu .item {
    position: relative;
    padding: 8px 16px 8px 36px;
    cursor: default; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  #ctxmenu .item:hover { background: #f1f3f4; }
  #ctxmenu .item.disabled { color: #80868b; }
  #ctxmenu .item.disabled:hover { background: transparent; }
  #ctxmenu .item .ico { position: absolute; left: 10px; top: 50%;
                        transform: translateY(-50%);
                        width: 16px; height: 16px;
                        display: inline-flex;
                        align-items: center; justify-content: center;
                        color: #5f6368; }
  #ctxmenu .item .ico svg { display: block; width: 16px; height: 16px; }
  #ctxmenu .item.disabled .ico { color: #bdc1c6; }
  #ctxmenu .sep { height: 1px; background: #e8eaed; margin: 6px 0; }
  #toast { position: fixed; bottom: 16px; left: 50%;
           transform: translateX(-50%);
           background: rgba(32,33,36,.92); color: #fff;
           padding: 8px 16px; border-radius: 4px;
           font: 13px system-ui; z-index: 200;
           opacity: 0; transition: opacity .3s; pointer-events: none; }
  #toast.show { opacity: 1; }
</style>
</head><body>
<div id="status">connecting...</div>
<div id="toast"></div>
<div id="ctxmenu"></div>
<!-- tabindex=0 makes the canvas keyboard-focusable. Without this the
     keydown / keyup listeners below never fire because canvas can't
     be ``document.activeElement``. autofocus pulls focus to canvas on
     page load so the operator can just start typing. -->
<div id="wrap">
  <div id="vp">
    <canvas id="c" width="1280" height="720" tabindex="0" autofocus></canvas>
    <!-- IME composition catcher. autocomplete/correct off so IMEs
         don't try to suggest based on previous values. spellcheck
         off so red squiggles don't leak through opacity. -->
    <textarea id="ime" autocomplete="off" autocorrect="off"
              autocapitalize="off" spellcheck="false"></textarea>
  </div>
</div>
<script>
(function(){
  // === Icons: same feather/lucide style as paprika admin.js =========
  // Defined once at top so buildMenuItems() can drop them in.
  const _SVG = 'xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
             + 'fill="none" stroke="currentColor" stroke-width="2" '
             + 'stroke-linecap="round" stroke-linejoin="round" '
             + 'width="16" height="16"';
  const ICONS = {
    copy:     '<svg ' + _SVG + '><rect x="9" y="9" width="13" height="13" rx="2"/>'
            + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    link:     '<svg ' + _SVG + '><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>'
            + '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
    externalLink: '<svg ' + _SVG + '><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>'
            + '<polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>',
    clipboard: '<svg ' + _SVG + '><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>'
            + '<rect x="8" y="2" width="8" height="4" rx="1"/></svg>',
    camera:   '<svg ' + _SVG + '><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>'
            + '<circle cx="12" cy="13" r="4"/></svg>',
    scissors: '<svg ' + _SVG + '><circle cx="6" cy="6" r="3"/>'
            + '<circle cx="6" cy="18" r="3"/>'
            + '<line x1="20" y1="4" x2="8.12" y2="15.88"/>'
            + '<line x1="14.47" y1="14.48" x2="20" y2="20"/>'
            + '<line x1="8.12" y1="8.12" x2="12" y2="12"/></svg>',
  };

  // Suppress the *outer* browser's native context menu everywhere on
  // this page. Without this, right-clicking the screencast canvas
  // pops up our viewer's host-browser context menu (Chrome's own
  // page menu), which is confusing -- the operator wants to interact
  // with the REMOTE Chrome, not the host. We re-add a per-canvas
  // contextmenu handler below that DOES build the proper context
  // menu for the remote Chrome.
  document.addEventListener('contextmenu', e => e.preventDefault());

  // The WS endpoint is always at ``ws`` under the current viewer URL,
  // regardless of whether we landed on the session-keyed path
  // (/sessions/{sid}/screencast/) or the lane-keyed path
  // (/workers/{wid}/lanes/{idx}/screencast/). Same viewer, two
  // entry points, one client.
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const basePath = location.pathname.endsWith('/')
    ? location.pathname : location.pathname + '/';
  const wsUrl = `${proto}//${location.host}${basePath}ws`;
  const canvas = document.getElementById('c');
  const ctx = canvas.getContext('2d');
  const status = document.getElementById('status');
  let ws = null;
  let imgCount = 0;

  function setStatus(text, cls) {
    status.textContent = text;
    status.className = cls || '';
  }

  // Latest frame metadata from CDP. Used to translate viewer-canvas
  // coordinates into Chrome viewport coordinates for input events.
  //   offsetTop    = top of the captured frame, in CSS pixels, from
  //                  viewport top (= mouse y must be offset by this)
  //   deviceWidth  = viewport width in CSS pixels (NOT the image's px
  //                  width -- Chrome scales the screencast image to fit
  //                  within maxWidth/maxHeight while keeping aspect)
  //   deviceHeight = same for height
  let lastMeta = { offsetTop: 0, deviceWidth: 0, deviceHeight: 0 };

  function connect() {
    ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => setStatus('connected', 'ok');
    ws.onclose = (e) => {
      setStatus(`closed (${e.code}) -- reconnecting in 2s`, 'err');
      setTimeout(connect, 2000);
    };
    ws.onerror = () => setStatus('error', 'err');
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.type === 'frame') {
        // Cache CDP metadata for input-coord translation.
        if (msg.metadata) {
          lastMeta = {
            offsetTop:    msg.metadata.offsetTop    || 0,
            deviceWidth:  msg.metadata.deviceWidth  || 0,
            deviceHeight: msg.metadata.deviceHeight || 0,
          };
        }
        const img = new Image();
        img.onload = () => {
          if (canvas.width !== img.width || canvas.height !== img.height) {
            canvas.width = img.width;
            canvas.height = img.height;
          }
          ctx.drawImage(img, 0, 0);
          imgCount++;
          if (imgCount % 10 === 0) setStatus(`frame #${imgCount}`, 'ok');
        };
        img.src = 'data:image/jpeg;base64,' + msg.data;
      } else if (msg.type === 'cursor') {
        // Mirror the cursor Chrome would show at the operator's
        // hover point. "pointer" / "text" / "wait" / "grab" etc.
        // ``ew-resize`` / ``move`` / etc. all valid CSS cursor names.
        canvas.style.cursor = msg.value || 'default';
      } else if (msg.type === 'context_probe_result') {
        // Reply to a contextmenu probe -- build the menu now.
        if (pendingMenuAt) {
          showMenuFor(msg, pendingMenuAt.vx, pendingMenuAt.vy);
        }
      } else if (msg.type === 'screenshot_data') {
        // Trigger a browser download for the PNG bytes.
        const bin = atob(msg.data || '');
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const blob = new Blob([bytes], {type: 'image/png'});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `paprika-${Date.now()}.png`;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
        showToast('スクリーンショットを保存しました');
      } else if (msg.type === 'error') {
        setStatus('upstream error: ' + msg.message, 'err');
      }
    };
  }

  // ---- Input → server -------------------------------------------------
  // Translate viewer-canvas coordinates into Chrome's *viewport*
  // CSS-pixel coordinate space, which is what Input.dispatchMouseEvent
  // expects.
  //
  // Pipeline (3 different coordinate systems):
  //   (clientX, clientY)       -- viewer browser CSS pixels
  //   minus rect.left/top      -- canvas-local CSS pixels (0..rect.w/h)
  //   * deviceW / rect.w       -- Chrome viewport CSS pixels
  //   + offsetTop on y         -- account for captured-region offset
  //
  // ``canvas.width`` (= image pixel width) is intentionally NOT used as
  // an intermediate. The JPEG image is scaled by Chrome to fit within
  // maxWidth/maxHeight while preserving aspect, so image_px !=
  // viewport_px. We go directly from rect (viewer DOM size) to viewport
  // (Chrome DOM size) using ``deviceWidth/Height`` from CDP metadata.
  //
  // metadata.offsetTop fixes the symptom "mouse pointer is ~200px off
  // vertically" that happens when Chrome's screencast captures a region
  // that doesn't start at viewport y=0 (e.g. when an overlay or extra
  // UI eats top space).
  function toChromeXY(e) {
    const rect = canvas.getBoundingClientRect();
    // Fall back to canvas buffer dims when no frame has arrived yet
    // (= initial mousedown before the first frame).
    const dw = lastMeta.deviceWidth  || canvas.width;
    const dh = lastMeta.deviceHeight || canvas.height;
    const sx = dw / rect.width;
    const sy = dh / rect.height;
    return {
      x: Math.round((e.clientX - rect.left) * sx),
      y: Math.round((e.clientY - rect.top) * sy) + (lastMeta.offsetTop || 0),
    };
  }
  function modifiers(e) {
    // CDP modifier bitmask: 1=Alt 2=Ctrl 4=Meta 8=Shift
    return (e.shiftKey ? 8 : 0) | (e.ctrlKey ? 2 : 0)
         | (e.altKey ? 1 : 0) | (e.metaKey ? 4 : 0);
  }
  function buttonName(e) {
    if (e.button === 0) return 'left';
    if (e.button === 1) return 'middle';
    if (e.button === 2) return 'right';
    return 'none';
  }
  function sendMouse(event, e) {
    if (!ws || ws.readyState !== 1) return;
    const xy = toChromeXY(e);
    // CRITICAL: clickCount must be >=1 on BOTH mousePressed AND
    // mouseReleased for Chrome to treat the pair as a click. We
    // previously sent 0 on Released which is why links / buttons
    // didn't fire even though the mouse moved correctly.
    const isPressOrRelease = event === 'mousePressed' || event === 'mouseReleased';
    ws.send(JSON.stringify({
      type: 'mouse', event,
      x: xy.x, y: xy.y,
      button: buttonName(e),
      clickCount: isPressOrRelease ? (e.detail || 1) : 0,
      modifiers: modifiers(e),
    }));
  }
  function sendWheel(e) {
    if (!ws || ws.readyState !== 1) return;
    const xy = toChromeXY(e);
    // CDP mouseWheel uses deltaX/deltaY in CSS pixels. Browser wheel
    // events report deltaMode: 0=pixel 1=line 2=page. Multiply lines
    // by a typical line-height; pages by viewport height. (Chrome
    // itself does ~40px/line for line mode.)
    let dx = e.deltaX, dy = e.deltaY;
    if (e.deltaMode === 1) { dx *= 40; dy *= 40; }
    else if (e.deltaMode === 2) { dx *= canvas.height; dy *= canvas.height; }
    ws.send(JSON.stringify({
      type: 'mouse', event: 'mouseWheel',
      x: xy.x, y: xy.y,
      button: 'none',
      deltaX: dx, deltaY: dy,
      modifiers: modifiers(e),
    }));
  }
  canvas.addEventListener('mousedown', e => { e.preventDefault(); sendMouse('mousePressed', e); });
  canvas.addEventListener('mouseup',   e => { e.preventDefault(); sendMouse('mouseReleased', e); });
  canvas.addEventListener('mousemove', e => { sendMouse('mouseMoved', e); });

  // Cursor mirror: ask Chrome what cursor it would show under the
  // pointer (= ``getComputedStyle(elementFromPoint).cursor``). Throttle
  // to ~7 Hz so the CDP Runtime.evaluate round-trip doesn't drown
  // the WebSocket. The reply updates ``canvas.style.cursor`` so the
  // operator sees pointer / text / grab / wait etc. just like in a
  // normal browser.
  let cursorLastSent = 0;
  canvas.addEventListener('mousemove', e => {
    const now = performance.now();
    if (now - cursorLastSent < 150) return;
    cursorLastSent = now;
    if (!ws || ws.readyState !== 1) return;
    const xy = toChromeXY(e);
    ws.send(JSON.stringify({ type: 'cursor_query', x: xy.x, y: xy.y }));
  });
  // When the pointer leaves the canvas, snap back to default so the
  // viewer page chrome (status badge, scrollbar) shows its own cursor.
  canvas.addEventListener('mouseleave', () => { canvas.style.cursor = 'default'; });
  // passive=false so preventDefault() actually stops the page scrolling
  // beneath the canvas (otherwise the viewer page scrolls instead of
  // the remote Chrome).
  canvas.addEventListener('wheel', e => { e.preventDefault(); sendWheel(e); }, { passive: false });
  // Right-click: query Chrome for context (selection text, link href)
  // BEFORE showing the menu so the items can be context-aware.
  canvas.addEventListener('contextmenu', e => {
    e.preventDefault();
    hideMenu();
    pendingMenuAt = { vx: e.clientX, vy: e.clientY };
    if (!ws || ws.readyState !== 1) {
      showMenuFor({selection: '', href: null}, e.clientX, e.clientY);
      return;
    }
    const xy = toChromeXY(e);
    ws.send(JSON.stringify({type: 'context_probe', x: xy.x, y: xy.y}));
  });

  // -- Chrome-ish context menu --------------------------------------
  const menu = document.getElementById('ctxmenu');
  const toast = document.getElementById('toast');
  let pendingMenuAt = null;
  function showToast(text) {
    toast.textContent = text;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 1800);
  }
  function hideMenu() { menu.style.display = 'none'; }
  function showMenuFor(info, vx, vy) {
    pendingMenuAt = null;
    menu.innerHTML = '';
    const items = buildMenuItems(info);
    items.forEach(it => {
      if (it === 'sep') {
        const s = document.createElement('div'); s.className = 'sep';
        menu.appendChild(s); return;
      }
      const d = document.createElement('div');
      d.className = 'item' + (it.disabled ? ' disabled' : '');
      // ico is trusted SVG markup from ICONS table; label is escaped.
      d.innerHTML = '<span class="ico">' + (it.icon || '') + '</span>'
                  + escHtml(it.label);
      if (!it.disabled) d.onclick = () => { hideMenu(); it.action(); };
      menu.appendChild(d);
    });
    menu.style.display = 'block';
    const r = menu.getBoundingClientRect();
    const px = Math.min(vx, window.innerWidth  - r.width  - 4);
    const py = Math.min(vy, window.innerHeight - r.height - 4);
    menu.style.left = px + 'px';
    menu.style.top  = py + 'px';
  }
  function escHtml(s) {
    return String(s).replace(/[<>&"]/g, c =>
      ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'})[c]);
  }
  function ellipsize(s, n) {
    s = String(s || ''); return s.length > n ? s.slice(0, n-1) + '…' : s;
  }
  function buildMenuItems(info) {
    const items = [];
    const sel = (info.selection || '').trim();
    const href = info.href || null;
    // 1) Text selection → 切り取り + コピー
    if (sel) {
      items.push({
        icon: ICONS.copy,
        label: 'コピー  「' + ellipsize(sel, 24) + '」',
        action: () => copyToWindows(sel),
      });
      items.push({
        icon: ICONS.scissors,
        label: '切り取り  「' + ellipsize(sel, 24) + '」',
        action: () => cutToWindows(sel),
      });
    }
    // 2) Link → 新しいタブで開く
    if (href) {
      items.push({
        icon: ICONS.externalLink,
        label: '新しいタブでリンクを開く',
        action: () => sendNewTab(href),
      });
      items.push({
        icon: ICONS.link,
        label: 'リンクの URL をコピー',
        action: () => copyToWindows(href),
      });
    }
    // 3) Always: paste (from Windows clipboard into Chrome)
    items.push({
      icon: ICONS.clipboard,
      label: '貼り付け  (Windows のクリップボードから)',
      action: () => pasteFromWindows(),
    });
    items.push('sep');
    // 4) Always: screenshot
    items.push({
      icon: ICONS.camera,
      label: 'スクリーンショットを保存',
      action: () => requestScreenshot(),
    });
    return items;
  }

  // ---- Clipboard helpers (browser ↔ Chromium 越し) ----
  function copyToWindows(text) {
    // navigator.clipboard.writeText works on localhost (treated as
    // secure context). Falls back to a hidden textarea + execCommand
    // for the rare case it doesn't.
    const ok = (msg) => showToast(msg);
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text)
        .then(() => ok('コピーしました'))
        .catch(() => fallbackCopy(text, ok));
    } else {
      fallbackCopy(text, ok);
    }
  }
  function fallbackCopy(text, ok) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); ok('コピーしました'); }
    catch (_) { showToast('コピー失敗'); }
    document.body.removeChild(ta);
  }
  function cutToWindows(text) {
    // "Cut" = copy + delete-selection. CDP doesn't have a single
    // ``Input.cut`` command, so we send a Backspace key event after
    // copying: Chrome treats Backspace with a non-empty selection as
    // "delete the selection". Works for <input> / <textarea> /
    // contentEditable alike (= the same path the OS uses).
    copyToWindows(text);
    if (ws && ws.readyState === 1) {
      // CDP key event sequence: rawKeyDown + keyUp. Backspace =
      // virtual key code 8 (= same constant on every platform).
      const downMsg = {
        type: 'key', event: 'keyDown',
        key: 'Backspace', code: 'Backspace',
        keyCode: 8, text: '', modifiers: 0,
      };
      const upMsg = Object.assign({}, downMsg, {event: 'keyUp'});
      ws.send(JSON.stringify(downMsg));
      ws.send(JSON.stringify(upMsg));
    }
  }
  function pasteFromWindows() {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      showToast('このブラウザはクリップボード読み取りに非対応');
      return;
    }
    navigator.clipboard.readText().then(text => {
      if (!text) { showToast('クリップボードは空'); return; }
      if (ws && ws.readyState === 1) {
        ws.send(JSON.stringify({type: 'paste', text: text}));
        showToast('貼り付けました (' + ellipsize(text, 20) + ')');
      }
    }).catch(() => showToast('クリップボード読み取り拒否'));
  }
  function sendNewTab(url) {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({type: 'new_tab', url: url}));
      showToast('新しいタブで開きました');
    }
  }
  function requestScreenshot() {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({type: 'screenshot'}));
    }
  }

  // Click anywhere else closes the menu.
  document.addEventListener('mousedown', e => {
    if (e.target.closest('#ctxmenu')) return;
    hideMenu();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') hideMenu();
  });

  function sendKey(event, e) {
    if (!ws || ws.readyState !== 1) return;
    // ``text`` is set only on the ``char`` event (= input-producing
    // keystroke). keyDown/keyUp carry just key/code/keyCode so CDP
    // routes through dispatchKeyEvent's "rawKeyDown" path, which is
    // what makes Backspace/Delete/Enter/Tab/Arrow actually fire.
    //
    // ``e.keyCode`` is what CDP wants as windowsVirtualKeyCode
    // (server side converts). It's deprecated DOM API but every
    // browser still fills it in; modern e.code/key are extra hints
    // we pass alongside.
    ws.send(JSON.stringify({
      type: 'key', event,
      key: e.key, code: e.code,
      keyCode: e.keyCode || e.which || 0,
      text: event === 'char' ? (e.key.length === 1 ? e.key : '') : '',
      modifiers: modifiers(e),
    }));
  }
  // ---- Keyboard + IME via the hidden textarea ---------------------
  // All keyboard input goes through the IME catcher (#ime), not the
  // canvas. Why: canvas can't receive ``compositionstart`` /
  // ``compositionend`` events, so Japanese / Chinese IME would silently
  // drop the converted text. textarea CAN receive those events.
  //
  // While not composing (= raw ASCII typing), keydown/keyup are
  // forwarded 1:1 to Chrome (with windowsVirtualKeyCode on the
  // server side, so Backspace/Delete/Enter/Tab/Arrow keys work).
  //
  // While composing (= IME candidate window is open), we DON'T send
  // intermediate keystrokes to Chrome -- only the final committed
  // text via ``compositionend.data`` → Input.insertText. This avoids
  // sending half-baked ローマ字 (nihongo) to Chrome before the user
  // confirms 日本語.
  const ime = document.getElementById('ime');

  ime.addEventListener('keydown', e => {
    if (e.isComposing || e.keyCode === 229) return;  // IME in progress
    e.preventDefault();
    sendKey('keyDown', e);
    if (e.key.length === 1) sendKey('char', e);
  });
  ime.addEventListener('keyup', e => {
    if (e.isComposing) return;
    e.preventDefault();
    sendKey('keyUp', e);
  });
  // IME composition: ignore start/update (= candidates still open in
  // the OS popup), commit on compositionend.
  ime.addEventListener('compositionend', e => {
    if (e.data && ws && ws.readyState === 1) {
      // CDP Input.insertText takes the composed string as-is.
      // Empty string (= user cancelled the composition) is skipped.
      ws.send(JSON.stringify({type: 'paste', text: e.data}));
    }
    // Always clear so the next composition starts from empty.
    ime.value = '';
  });
  ime.addEventListener('input', e => {
    // Non-composition input events: keydown→char already covered the
    // common case. We only handle the IME edge case here (= textarea
    // got committed text via paste / autocorrect, not keystrokes).
    // Drop the value to prevent accumulation.
    if (!e.isComposing) ime.value = '';
  });

  // Click on canvas → re-focus the IME catcher so typing works after
  // tab-switching back to the viewer.
  canvas.addEventListener('mousedown', () => ime.focus());
  setTimeout(() => ime.focus(), 0);

  connect();
})();
</script>
</body></html>
"""


@router.get("/sessions/{session_id}/screencast/", response_class=HTMLResponse)
async def screencast_viewer(session_id: str, request: Request) -> HTMLResponse:
    """Static viewer page. Embeds a ``<canvas>`` that opens a WS to
    ``/sessions/{sid}/screencast/ws`` and renders incoming JPEG frames.
    Forwards mouse / keyboard events back over the same WS."""
    # Don't gate on session existence here -- the viewer's WS reconnect
    # loop survives a brief "session not ready" window during job spin-up.
    return HTMLResponse(content=_VIEWER_HTML)


# ---------------------------------------------------------------------------
# Bi-directional WS proxy
# ---------------------------------------------------------------------------


# CDP message id allocator. Each browser-bound message needs a unique
# integer id so we can match replies. Per-connection counter is enough;
# we never share an upstream ws across HTTP connections.
def _id_seq():
    n = 0
    while True:
        n += 1
        yield n


async def _pump_cdp_to_client(
    upstream: websockets.WebSocketClientProtocol,
    client: WebSocket,
    next_id,
    *,
    session_id: str | None,
    cursor_query_ids: set[int],
    probe_ids: set[int],
    screenshot_ids: set[int],
) -> None:
    """Drain CDP events. ``Page.screencastFrame`` -> client.send_text.
    Cursor-query replies (Runtime.evaluate id in ``cursor_query_ids``)
    -> client.send_text. Everything else is dropped.

    When ``session_id`` is set, each frame delivery touches the
    session so a passively-watching operator (= just looking, not
    clicking) also keeps the session alive. Lane-keyed viewers (=
    no session bound) skip the touch."""
    frame_count = 0
    async for raw in upstream:
        try:
            payload = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue
        method = payload.get("method")
        pid = payload.get("id")
        # Cursor-query result: id matches one we issued from a client
        # cursor_query. Forward the resolved cursor string back.
        if pid is not None and pid in cursor_query_ids:
            cursor_query_ids.discard(pid)
            result = (payload.get("result") or {}).get("result") or {}
            cursor = (result.get("value") or "default").strip() or "default"
            try:
                await client.send_text(json.dumps({
                    "type": "cursor", "value": cursor,
                }))
            except Exception:
                pass
            continue
        # Context probe result (right-click): selection text + link href
        # discovered via Runtime.evaluate.
        if pid is not None and pid in probe_ids:
            probe_ids.discard(pid)
            result = (payload.get("result") or {}).get("result") or {}
            info = result.get("value") or {}
            try:
                await client.send_text(json.dumps({
                    "type": "context_probe_result",
                    "selection": info.get("selection") or "",
                    "href": info.get("href"),
                    "linkText": info.get("linkText"),
                }))
            except Exception:
                pass
            continue
        # Screenshot reply: Page.captureScreenshot -> base64 PNG.
        if pid is not None and pid in screenshot_ids:
            screenshot_ids.discard(pid)
            data = (payload.get("result") or {}).get("data") or ""
            try:
                await client.send_text(json.dumps({
                    "type": "screenshot_data", "data": data,
                }))
            except Exception:
                pass
            continue
        if method != "Page.screencastFrame":
            # CDP replies (id=N) for our own commands -- ignore.
            continue
        params = payload.get("params") or {}
        await client.send_text(json.dumps({
            "type": "frame",
            "data": params.get("data", ""),
            "metadata": params.get("metadata", {}),
        }))
        # Touch every ~30 frames (~1s at default 30fps) so passive
        # viewers also keep the session alive. Doing it per frame is
        # safe but wastes a dict lookup per frame; throttling is enough.
        frame_count += 1
        if (
            frame_count % 30 == 0
            and session_id is not None
            and state.sessions is not None
        ):
            try:
                state.sessions.touch(session_id)
            except Exception:
                pass
        # ACK: Chrome won't send the next frame until we acknowledge.
        sid = params.get("sessionId")
        if sid is not None:
            try:
                await upstream.send(json.dumps({
                    "id": next(next_id),
                    "method": "Page.screencastFrameAck",
                    "params": {"sessionId": sid},
                }))
            except Exception:
                return


async def _pump_client_to_cdp(
    client: WebSocket,
    upstream: websockets.WebSocketClientProtocol,
    next_id,
    *,
    session_id: str | None,
    cursor_query_ids: set[int],
    probe_ids: set[int],
    screenshot_ids: set[int],
) -> None:
    """Drain client input events. Translate to Input.dispatchMouseEvent
    / Input.dispatchKeyEvent and send upstream.

    When ``session_id`` is set, each input event also bumps
    ``session.last_active_at`` so the reaper doesn't kill an
    actively-viewed session. Lane-keyed viewers skip the touch."""
    while True:
        text = await client.receive_text()
        try:
            msg = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            continue
        t = msg.get("type")
        # Touch first -- even malformed events count as "the operator is
        # alive and using this session". Cheap operation on a dict.
        # Skipped for lane-keyed viewers (no session bound).
        if session_id is not None and state.sessions is not None:
            try:
                state.sessions.touch(session_id)
            except Exception:
                pass
        try:
            if t == "mouse":
                # Build CDP params. mouseWheel events ALSO use
                # Input.dispatchMouseEvent (CDP overloads it) but with
                # type="mouseWheel" + deltaX/deltaY. We pass the deltas
                # through unconditionally; CDP ignores them on non-wheel
                # event types so this stays compatible with click/move.
                params = {
                    "type": msg.get("event", "mouseMoved"),
                    "x": int(msg.get("x", 0)),
                    "y": int(msg.get("y", 0)),
                    "button": msg.get("button", "none"),
                    "clickCount": int(msg.get("clickCount", 0)),
                    "modifiers": int(msg.get("modifiers", 0)),
                }
                if "deltaX" in msg or "deltaY" in msg:
                    params["deltaX"] = float(msg.get("deltaX", 0))
                    params["deltaY"] = float(msg.get("deltaY", 0))
                await upstream.send(json.dumps({
                    "id": next(next_id),
                    "method": "Input.dispatchMouseEvent",
                    "params": params,
                }))
            elif t == "key":
                # Build CDP params. windowsVirtualKeyCode is what
                # actually makes non-character keys (Backspace, Delete,
                # Enter, Tab, ArrowLeft/Right/Up/Down, F1..F12, Esc,
                # Home, End, PageUp/Down) work -- without it Chrome
                # treats the event as "raw text" and ignores it for
                # input fields' navigation/edit behavior.
                #
                # Also map ``keyDown`` -> ``rawKeyDown`` when there is
                # no text, which is CDP's recommended type for
                # navigation keys (= triggers default action like
                # backspace deletion). With text, keep ``keyDown`` so
                # the field receives the character.
                ev = msg.get("event", "keyDown")
                text = msg.get("text", "")
                if ev == "keyDown" and not text:
                    ev = "rawKeyDown"
                params = {
                    "type": ev,
                    "key": msg.get("key", ""),
                    "code": msg.get("code", ""),
                    "text": text,
                    "modifiers": int(msg.get("modifiers", 0)),
                }
                kc = int(msg.get("keyCode") or 0)
                if kc:
                    params["windowsVirtualKeyCode"] = kc
                    params["nativeVirtualKeyCode"] = kc
                await upstream.send(json.dumps({
                    "id": next(next_id),
                    "method": "Input.dispatchKeyEvent",
                    "params": params,
                }))
            elif t == "resize":
                # Re-request screencast with new viewport size so the
                # frame stream matches the operator's window.
                await upstream.send(json.dumps({
                    "id": next(next_id),
                    "method": "Page.startScreencast",
                    "params": {
                        "format": "jpeg",
                        "quality": 60,
                        "maxWidth": int(msg.get("w", 1280)),
                        "maxHeight": int(msg.get("h", 720)),
                        "everyNthFrame": 1,
                    },
                }))
            elif t == "context_probe":
                # Probe Chrome at (x, y) for: current selection text,
                # and the nearest enclosing <a>'s href + visible text.
                # Result drives the context menu items on the viewer.
                qid = next(next_id)
                probe_ids.add(qid)
                x = int(msg.get("x", 0))
                y = int(msg.get("y", 0))
                expr = (
                    "(function(){"
                    "var el=document.elementFromPoint(" + str(x) + "," + str(y) + ")"
                    "||document.body;"
                    "var a=(el&&el.closest)?el.closest('a'):null;"
                    "return {"
                    "selection:(window.getSelection?window.getSelection().toString():''),"
                    "href:(a&&a.href)?a.href:null,"
                    "linkText:a?(a.innerText||a.href):null"
                    "};"
                    "})()"
                )
                await upstream.send(json.dumps({
                    "id": qid,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expr, "returnByValue": True},
                }))
            elif t == "paste":
                # Insert text into whatever element has focus in Chrome.
                # CDP Input.insertText is the canonical "paste-like" op
                # -- it goes through the same path as IME / keyboard
                # input so it triggers ``input`` events and survives
                # paste-blocking event handlers that intercept Ctrl+V.
                text = str(msg.get("text", ""))
                await upstream.send(json.dumps({
                    "id": next(next_id),
                    "method": "Input.insertText",
                    "params": {"text": text},
                }))
            elif t == "new_tab":
                # Open the URL in a new Chrome tab (same browser
                # context, so cookies and storage carry over).
                url = str(msg.get("url", "about:blank"))
                await upstream.send(json.dumps({
                    "id": next(next_id),
                    "method": "Target.createTarget",
                    "params": {"url": url, "newWindow": False},
                }))
            elif t == "screenshot":
                # Full viewport PNG. Reply carries the base64 data
                # back to the viewer which triggers a download.
                qid = next(next_id)
                screenshot_ids.add(qid)
                await upstream.send(json.dumps({
                    "id": qid,
                    "method": "Page.captureScreenshot",
                    "params": {"format": "png"},
                }))
            elif t == "cursor_query":
                # Ask Chrome what cursor it WOULD show under the
                # operator's mouse pointer (if Chrome were rendering
                # to a visible window). Returns the CSS ``cursor``
                # computed style of the element at (x, y). We
                # ``elementFromPoint || document.body`` so the bottom
                # of the page (= white space) still resolves to
                # "default" instead of an unhandled ``null.cursor``.
                # The expression is single-line + JSON-quoted so the
                # CDP wire stays compact.
                qid = next(next_id)
                cursor_query_ids.add(qid)
                x = int(msg.get("x", 0))
                y = int(msg.get("y", 0))
                expr = (
                    "(function(){"
                    "var e=document.elementFromPoint(" + str(x) + "," + str(y) + ")"
                    "||document.body;"
                    "return e?window.getComputedStyle(e).cursor:'default';"
                    "})()"
                )
                await upstream.send(json.dumps({
                    "id": qid,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expr,
                        "returnByValue": True,
                    },
                }))
        except Exception:
            return


async def _run_screencast_bridge(
    ws: WebSocket,
    *,
    target: tuple[str, int] | None,
    session_id: str | None,
    label: str,
) -> None:
    """Shared lifecycle: open CDP, start screencast, run both pumps,
    teardown. Both ``/sessions/{sid}/screencast/ws`` and
    ``/workers/{wid}/lanes/{idx}/screencast/ws`` go through here --
    the only difference is how ``target`` was resolved.

    ``session_id`` is forwarded to the pumps for last_active_at
    touching (None = lane-keyed viewer, no touch)."""
    if target is None:
        await ws.close(code=1008, reason="no chrome_attach_port for " + label)
        return
    host, chrome_port = target

    try:
        cdp_url = await _open_cdp_target(host, chrome_port)
    except Exception as e:
        await ws.close(code=1011, reason=f"could not resolve CDP target: {e}")
        return

    await ws.accept()
    next_id = _id_seq()

    try:
        async with websockets.connect(
            cdp_url,
            max_size=32 * 1024 * 1024,  # JPEG frames can be ~MB on big viewports
            ping_interval=20,
        ) as upstream:
            # Initial protocol setup -- Page domain + start streaming.
            await upstream.send(json.dumps({
                "id": next(next_id), "method": "Page.enable",
            }))
            await upstream.send(json.dumps({
                "id": next(next_id),
                "method": "Page.startScreencast",
                "params": {
                    "format": "jpeg",
                    "quality": 60,
                    "maxWidth": 1280,
                    "maxHeight": 720,
                    "everyNthFrame": 1,
                },
            }))

            # Shared sets so client_to_cdp can register an id it
            # issued, and cdp_to_client can recognise the matching
            # reply. Each set is for one CDP method we care about
            # routing back to the viewer (Runtime.evaluate for
            # cursor + context, Page.captureScreenshot for ad-hoc
            # PNG download).
            cursor_query_ids: set[int] = set()
            probe_ids: set[int] = set()
            screenshot_ids: set[int] = set()
            up_task = asyncio.create_task(
                _pump_cdp_to_client(
                    upstream, ws, next_id,
                    session_id=session_id,
                    cursor_query_ids=cursor_query_ids,
                    probe_ids=probe_ids,
                    screenshot_ids=screenshot_ids,
                ),
                name=f"screencast-up-{label}",
            )
            dn_task = asyncio.create_task(
                _pump_client_to_cdp(
                    ws, upstream, next_id,
                    session_id=session_id,
                    cursor_query_ids=cursor_query_ids,
                    probe_ids=probe_ids,
                    screenshot_ids=screenshot_ids,
                ),
                name=f"screencast-down-{label}",
            )

            # Wait for either side to close.
            done, pending = await asyncio.wait(
                [up_task, dn_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            # Best-effort stop so we don't leave the streaming target
            # spewing frames into a dead WS.
            try:
                await upstream.send(json.dumps({
                    "id": next(next_id),
                    "method": "Page.stopScreencast",
                }))
            except Exception:
                pass

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("screencast bridge failed for %s", label)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public WS endpoints (session + worker/lane variants)
# ---------------------------------------------------------------------------


@router.websocket("/sessions/{session_id}/screencast/ws")
async def screencast_ws_session(ws: WebSocket, session_id: str) -> None:
    """Session-keyed variant. Resolves chrome_port from the session
    registry; touches session.last_active_at on each frame/input so
    the reaper doesn't kill an actively-watched session."""
    target = _resolve_session_chrome_target(session_id)
    await _run_screencast_bridge(
        ws,
        target=target,
        session_id=session_id,
        label=f"sess-{session_id[:8]}",
    )


@router.websocket("/workers/{worker_id}/lanes/{lane_idx}/screencast/ws")
async def screencast_ws_worker(
    ws: WebSocket, worker_id: str, lane_idx: int,
) -> None:
    """Worker/lane-keyed variant. Lets the Live Preview tile open
    the viewer even on an idle lane (no session bound yet). No
    session touching since there's no session to touch."""
    target = _resolve_worker_chrome_target(worker_id, lane_idx)
    await _run_screencast_bridge(
        ws,
        target=target,
        session_id=None,
        label=f"worker-{worker_id[:12]}-{lane_idx}",
    )


@router.get(
    "/workers/{worker_id}/lanes/{lane_idx}/screencast/",
    response_class=HTMLResponse,
)
async def screencast_viewer_worker(
    worker_id: str, lane_idx: int, request: Request,
) -> HTMLResponse:
    """Same viewer HTML as the session-keyed endpoint. The viewer JS
    derives its WS URL from ``location.pathname`` so this serves the
    same page; only the WS attachment target differs upstream."""
    return HTMLResponse(content=_VIEWER_HTML)
