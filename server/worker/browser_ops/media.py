"""Autoplay triggering, URL-capture hook, deep-iframe trace, single-tab. (browser_ops package; see _base.py for shared helpers)."""

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
from .mouse import click_at

_AUTOPLAY_ENABLED = (
    os.environ.get("PAPRIKA_AUTOPLAY", "1").lower()
    not in ("0", "false", "no", "")
)


_AUTOPLAY_ALL_FRAMES = (
    os.environ.get("PAPRIKA_AUTOPLAY_ALL_FRAMES", "0").lower()
    in ("1", "true", "yes", "on")
)


async def install_iframe_deep_trace(tab, log: LogFn | None = None) -> bool:
    """Hook CDP ``Target.setAutoAttach`` + ``AttachedToTarget`` so every
    cross-origin iframe / popup-page child target gets its own
    ``Network.enable``. The child sessions' Network events arrive on
    the parent socket (``flatten=True``) and are routed through
    whatever handlers are already registered on the parent tab. This
    lets HLS / DASH manifest URLs from cross-origin video players
    surface in the parent tab's network log without the caller
    having to manage child connections.

    Used by:
      * install_session_asset_capture (session-mode)
      * core.fetcher.fetch_url (plain Fetch mode, when
        ``opts.download_video`` is True)

    Idempotent on the same tab — stashes
    ``tab._paprika_iframe_deep_trace_on = True`` on first install
    so repeated calls (e.g. session start + later
    page.download_video()) are no-ops.

    Returns True when freshly installed, False when already on or
    install failed.
    """
    if getattr(tab, "_paprika_iframe_deep_trace_on", False):
        return False

    # Registry of attached cross-origin OOPIF sessions, keyed by
    # sessionId -> {target_id, type, url}.  trigger_autoplay() reads
    # this to fire a play-click into each OOPIF's OWN session (the top
    # session can't reach an out-of-process frame's JS world).  Stashed
    # on the tab so the asset-capture poller and download_video can both
    # reach it.
    oopif_sessions: dict = getattr(tab, "_paprika_oopif_sessions", None)
    if oopif_sessions is None:
        oopif_sessions = {}
        setattr(tab, "_paprika_oopif_sessions", oopif_sessions)

    _attached_target_ids: set = set()
    # Counter for raw CDP message IDs we send to sub-sessions. Start
    # above any reasonable nodriver counter; nodriver uses an
    # incrementing per-Connection itertools.count starting at 0, so
    # >1M is safely beyond collision.
    _subsession_msg_id = [10_000_000]

    # Auto-attach filter, defined ONCE here so both the main-tab
    # set_auto_attach (below) and the per-sub-session recursive
    # re-attach (inside _hook_subtarget) share the exact same shape.
    # nodriver's default filter MISSES OOPIF on Chrome 140+ (it doesn't
    # explicitly include "iframe" and Chrome treats absence as exclude).
    explicit_filter = cdp.target.TargetFilter([
        {"exclude": True, "type": "browser"},
        {"exclude": True, "type": "tab"},
        {"exclude": False, "type": "iframe"},
        {"exclude": False, "type": "page"},
        {"exclude": False, "type": "worker"},
        {"exclude": False, "type": "service_worker"},
        {"exclude": False, "type": "shared_worker"},
        {},  # catch-all for any unknown sub-target types
    ])

    async def _hook_subtarget(event):
        try:
            if not isinstance(event, cdp.target.AttachedToTarget):
                return
            ti = event.target_info
            session_id = event.session_id
            if ti.target_id in _attached_target_ids:
                return
            if ti.type_ not in ("iframe", "page"):
                # Skip browser / workers (no video) and tab targets.
                return
            if not session_id:
                if log:
                    log(
                        f"  [iframe-trace] sub-target {ti.type_} "
                        f"attached without session_id; skipping"
                    )
                return
            _attached_target_ids.add(ti.target_id)
            # Record this OOPIF session so trigger_autoplay() can fire a
            # play-click into its own JS world.  Keyed by sessionId.
            try:
                oopif_sessions[str(session_id)] = {
                    "target_id": ti.target_id,
                    "type": ti.type_,
                    "url": ti.url or "",
                }
            except Exception:
                pass
            # Enable Network on the sub-session by sending a raw CDP
            # message through the PARENT socket with explicit
            # sessionId routing. Routing through tab.socket means the
            # iframe's Network events arrive on the SAME websocket
            # where the parent tab's handlers are registered.
            # nodriver's process_event dispatches by event type only
            # -- the sessionId field is routing metadata, not a
            # filter -- so the parent's handlers fire for sub-session
            # events too.
            try:
                import json as _json
                gen = cdp.network.enable(
                    max_total_buffer_size=128 * 1024 * 1024,
                    max_resource_buffer_size=64 * 1024 * 1024,
                    max_post_data_size=4 * 1024 * 1024,
                )
                method, *raw_params = next(gen).values()
                params = raw_params.pop() if raw_params else {}
                _subsession_msg_id[0] += 1
                msg = {
                    "id": _subsession_msg_id[0],
                    "method": method,
                    "params": params,
                    "sessionId": session_id,
                }
                ws = getattr(tab, "socket", None)
                if ws is None:
                    if log:
                        log(
                            f"  [iframe-trace] tab has no socket; "
                            f"cannot enable Network on sub-target "
                            f"{ti.type_} {ti.target_id[:12]}"
                        )
                    return
                await ws.send(_json.dumps(msg))
                if log:
                    log(
                        f"  [iframe-trace] hooked sub-target "
                        f"{ti.type_}: {(ti.url or '')[:120]}"
                    )
                # RECURSE: tell THIS sub-session to auto-attach to its
                # OWN children too. Without this, only direct children
                # of the top tab are traced -- a nested player (e.g.
                # index page -> player iframe -> inner video
                # iframe whose HLS/MP4 stream is the real content) stays
                # invisible because its AttachedToTarget never fires.
                # flatten=True keeps the grandchildren's events on the
                # SAME parent socket, so _hook_subtarget fires again for
                # them and the trace recurses to arbitrary depth.
                # _attached_target_ids dedups, so re-attaches are cheap.
                try:
                    gen2 = cdp.target.set_auto_attach(
                        auto_attach=True,
                        wait_for_debugger_on_start=False,
                        flatten=True,
                        filter_=explicit_filter,
                    )
                    method2, *raw_params2 = next(gen2).values()
                    params2 = raw_params2.pop() if raw_params2 else {}
                    _subsession_msg_id[0] += 1
                    msg2 = {
                        "id": _subsession_msg_id[0],
                        "method": method2,
                        "params": params2,
                        "sessionId": session_id,
                    }
                    await ws.send(_json.dumps(msg2))
                    if log:
                        log(
                            f"  [iframe-trace] recursed setAutoAttach "
                            f"into {ti.type_} {ti.target_id[:12]}"
                        )
                except Exception as e:
                    if log:
                        log(
                            f"  [iframe-trace] recursive setAutoAttach "
                            f"on sub-target {ti.type_} failed "
                            f"(non-fatal): {type(e).__name__}: {e}"
                        )

                # Inject the url-capture hook into THIS (possibly
                # cross-origin) sub-frame.  Same-origin iframes already
                # get the hook via the top tab's
                # addScriptToEvaluateOnNewDocument, but a cross-origin
                # OOPIF runs in its own JS world that the top-frame
                # registration never reaches.  Inject here so the hook's
                # fetch/XHR monkey-patch runs inside the OOPIF too; its
                # _record() then postMessages captures up to the top
                # frame's bucket (the relay listener handles the
                # cross-origin boundary).  Two sends: register for the
                # next document load, AND evaluate now for the
                # already-loaded one.
                try:
                    _subsession_msg_id[0] += 1
                    await ws.send(_json.dumps({
                        "id": _subsession_msg_id[0],
                        "method": "Page.addScriptToEvaluateOnNewDocument",
                        "params": {"source": _URL_CAPTURE_HOOK_JS},
                        "sessionId": session_id,
                    }))
                    _subsession_msg_id[0] += 1
                    await ws.send(_json.dumps({
                        "id": _subsession_msg_id[0],
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": _URL_CAPTURE_HOOK_JS,
                            "returnByValue": True,
                        },
                        "sessionId": session_id,
                    }))
                    if log:
                        log(
                            f"  [iframe-trace] url-capture hook injected "
                            f"into {ti.type_} {ti.target_id[:12]}"
                        )
                except Exception as e:
                    if log:
                        log(
                            f"  [iframe-trace] url-capture inject on "
                            f"sub-target {ti.type_} failed (non-fatal): "
                            f"{type(e).__name__}: {e}"
                        )
            except Exception as e:
                if log:
                    log(
                        f"  [iframe-trace] Network.enable on sub-target "
                        f"{ti.type_} failed: {type(e).__name__}: {e}"
                    )
        except Exception as e:
            # Never let a handler exception kill the WS receive loop.
            if log:
                log(
                    f"  [iframe-trace] _hook_subtarget unexpected: "
                    f"{type(e).__name__}: {e}"
                )

    tab.handlers.setdefault(
        cdp.target.AttachedToTarget, []
    ).append(_hook_subtarget)

    # Re-call setAutoAttach with an EXPLICIT filter. nodriver's default
    # MISSES OOPIF (out-of-process iframes) on at least Chrome 140+
    # because its default filter doesn't explicitly include "iframe"
    # and Chrome treats absence as exclude. Verified on chrome 140 +
    # nodriver 0.50.3 with tikpornk: without the re-call,
    # AttachedToTarget never fires for cross-origin video player
    # iframes. flatten=True keeps all sub-session events on the
    # parent's websocket so _hook_subtarget can route per-iframe
    # Network.enable through the parent socket with explicit
    # sessionId rather than spinning up a new websocket per iframe.
    try:
        # explicit_filter defined above (shared with the recursive
        # per-sub-session re-attach in _hook_subtarget).
        await tab.send(
            cdp.target.set_auto_attach(
                auto_attach=True,
                wait_for_debugger_on_start=False,
                flatten=True,
                filter_=explicit_filter,
            )
        )
        setattr(tab, "_paprika_iframe_deep_trace_on", True)
        if log:
            log(
                "  [iframe-trace] ENABLED "
                "(set_auto_attach with iframe-inclusive filter)"
            )
        return True
    except Exception as e:
        if log:
            log(
                f"  [iframe-trace] install failed "
                f"(non-fatal): {type(e).__name__}: {e}"
            )
        return False


_URL_CAPTURE_HOOK_JS = r"""
(function() {
  if (window.__paprika_url_hook) return;
  window.__paprika_url_hook = true;
  try {
    var t = window.top;
    t.__paprika_hook_installs = (t.__paprika_hook_installs || 0) + 1;
  } catch (e) {
    window.__paprika_hook_installs = (window.__paprika_hook_installs || 0) + 1;
  }
  // Record one capture entry, bubbling toward the top frame.
  function _record(entry) {
    try {
      // same-origin chain: write straight to the top bucket.
      var t = window.top;
      if (!t.__paprika_url_capture) t.__paprika_url_capture = [];
      t.__paprika_url_capture.push(entry);
      return;
    } catch (e) {}
    // cross-origin boundary: hand the entry to our parent frame, whose
    // own hook relays it further up (see the message listener below).
    try { window.parent.postMessage({__paprika_cap: entry}, '*'); } catch (e) {}
  }
  // Relay: a child frame's capture arrives here via postMessage; push
  // it onward toward the top frame.  Cross-origin-safe (targetOrigin
  // '*').  Always travels UP, so no loops.
  try {
    window.addEventListener('message', function(ev) {
      var d = ev && ev.data;
      if (d && typeof d === 'object' && d.__paprika_cap
          && typeof d.__paprika_cap === 'object') {
        _record(d.__paprika_cap);
      }
    }, false);
  } catch (e) {}
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function(input) {
      try {
        var u = typeof input === 'string' ? input : (input && input.url) || '';
        if (u) _record({api: 'fetch', url: u, t: Date.now()});
      } catch (e) {}
      return origFetch.apply(this, arguments);
    };
  }
  var OrigXHR = window.XMLHttpRequest;
  if (OrigXHR && OrigXHR.prototype && OrigXHR.prototype.open) {
    var origOpen = OrigXHR.prototype.open;
    OrigXHR.prototype.open = function(method, url) {
      try {
        if (url) _record({api: 'xhr', method: method, url: String(url), t: Date.now()});
      } catch (e) {}
      return origOpen.apply(this, arguments);
    };
  }
})();
"""


_AUTOPLAY_CLICK_JS = r"""
(function(){
  try {
    // Only operate on REAL-PLAYER-SIZED videos.  A media detail page
    // (common on video index sites) is a grid of dozens of small related-work preview
    // <video> thumbnails; a blanket play() on every <video> starts them
    // all, which then each get auto-downloaded -- flooding the gallery
    // with the wrong videos and spawning many parallel yt-dlp procs.
    // The main player is large (or lives in its own OOPIF iframe, which
    // is driven via its own session), so gating on size keeps the main
    // video while ignoring the thumbnail grid.
    var MIN_W = 320, MIN_H = 180;
    var isBig = function(el){
      if (!el || !el.getBoundingClientRect) return false;
      var r = el.getBoundingClientRect();
      return r.width >= MIN_W && r.height >= MIN_H;
    };
    var allVids = Array.prototype.slice.call(
      document.querySelectorAll('video'));
    var bigVids = allVids.filter(isBig);
    var anyPlaying = bigVids.some(function(v){
      return !v.paused && !v.ended && v.currentTime > 0 && v.readyState > 2;
    });
    // Idempotent nudge: play() on a playing/buffering video is a no-op.
    bigVids.forEach(function(v){ try { v.play(); } catch(e){} });
    if (anyPlaying) { return {playing:true, clicked:false}; }

    var PLAY_TXT = /^(play|再生|スタート|start|▶|►|>)/i;
    var ARIA_RX  = /(play|再生|start|スタート)/i;
    var isVis = function(el){
      if (!el || !el.getBoundingClientRect) return false;
      var r = el.getBoundingClientRect();
      return r.width >= 20 && r.height >= 20 && el.offsetParent !== null;
    };
    var score = function(el){
      if (!isVis(el)) return -1;
      var s = 0;
      var aria = el.getAttribute('aria-label') || '';
      var title = el.getAttribute('title') || '';
      var txt = (el.textContent || '').trim();
      var cls = (typeof el.className === 'string'
        ? el.className : (el.className && el.className.baseVal) || '');
      if (ARIA_RX.test(aria)) s += 10;
      if (ARIA_RX.test(title)) s += 5;
      if (PLAY_TXT.test(txt)) s += 5;
      if (/play/i.test(cls)) s += 3;
      // Only reward a <video> as a click target if it's player-sized --
      // never let a small grid thumbnail become the click winner.
      if (el.tagName === 'VIDEO' && isBig(el)) s += 2;
      return s;
    };

    var didClick = false, clickScore = 0;
    // Click ONCE per document (see header comment).
    if (!window.__paprika_autoplay_clicked) {
      window.__paprika_autoplay_clicked = true;
      var els = document.querySelectorAll(
        'video, button, [role="button"], a, div, span');
      var best = null, bestScore = 0;
      for (var i = 0; i < els.length; i++) {
        var sc = score(els[i]);
        if (sc > bestScore) { best = els[i]; bestScore = sc; }
      }
      if (best && bestScore > 0) {
        try {
          var iv = best.querySelector ? best.querySelector('video') : null;
          if (iv && isBig(iv)) { try { iv.play(); } catch(e){} }
          best.click();
          didClick = true; clickScore = bestScore;
        } catch (e) {}
      }
    }

    // Largest video/iframe rect (this document's viewport coords) for
    // the top-frame trusted-Input fallback.
    var biggest = null, biggestArea = 0;
    var cands = document.querySelectorAll('video, iframe');
    for (var j = 0; j < cands.length; j++) {
      var rr = cands[j].getBoundingClientRect();
      if (rr.width >= 80 && rr.height >= 60) {
        var area = rr.width * rr.height;
        if (area > biggestArea) {
          biggestArea = area;
          biggest = {x: Math.round(rr.left + rr.width / 2),
                     y: Math.round(rr.top + rr.height / 2)};
        }
      }
    }
    return {playing:false, clicked:didClick, score:clickScore, biggest:biggest};
  } catch (e) { return {error: String(e)}; }
})()
"""


async def trigger_autoplay(tab, log: LogFn | None = None) -> dict:
    """Best-effort: start playback so click-gated players begin loading
    their real HLS/DASH manifest (which the url-capture hook then sees).

    Fires ``_AUTOPLAY_CLICK_JS`` via Runtime.evaluate(user_gesture=True)
    in the TOP frame, then (default) dispatches ONE trusted CDP Input
    click at the largest visible player rect so only the MAIN player
    starts -- the click hit-tests through to whichever frame (incl. a
    cross-origin OOPIF) sits at that pixel, leaving decoy / preview
    players untouched.  Set ``PAPRIKA_AUTOPLAY_ALL_FRAMES=1`` to instead
    blast the play-click into every attached OOPIF session
    (``tab._paprika_oopif_sessions``) -- captures more, over-captures more.

    Returns ``{"top": <result dict or None>, "trusted": bool,
    "oopif": <int count>}``.  The ``top`` dict carries ``biggest`` (a
    viewport-centre point).  Idempotent on the main-player click via
    ``tab._paprika_autoplay_trusted_done``.  Never raises.
    """
    result: dict = {"top": None, "oopif": 0}
    # --- top frame: real user gesture via the user_gesture flag ---
    try:
        remote, exc = await tab.send(cdp.runtime.evaluate(
            expression=_AUTOPLAY_CLICK_JS,
            user_gesture=True,
            return_by_value=True,
            await_promise=False,
        ))
        if exc is None and remote is not None:
            result["top"] = getattr(remote, "value", None)
    except Exception as e:
        if log:
            log(f"  [autoplay] top-frame click failed: "
                f"{type(e).__name__}: {e}")
    top = result["top"] or {}
    result["trusted"] = False

    if not _AUTOPLAY_ALL_FRAMES:
        # MAIN-PLAYER-ONLY (default).  Rather than blasting a play-click
        # into EVERY attached OOPIF -- which also starts decoy / mirror
        # players and the related-video preview grid -- dispatch ONE
        # trusted CDP Input click at the centre of the largest VISIBLE
        # player rect.  The click hit-tests through to whatever frame
        # occupies that pixel, so it reaches the main (possibly
        # cross-origin) player WITHOUT touching hidden / smaller decoy
        # iframes.  Fired once per tab; skipped when something is already
        # playing so we never toggle it back off.
        if (not getattr(tab, "_paprika_autoplay_trusted_done", False)
                and not top.get("playing")):
            biggest = top.get("biggest")
            if biggest:
                ok = await trigger_autoplay_trusted(tab, biggest, log=log)
                if ok:
                    setattr(tab, "_paprika_autoplay_trusted_done", True)
                    result["trusted"] = True
    else:
        # Opt-in legacy: fire the play-click into EVERY OOPIF session
        # (fire-and-forget raw CDP on each own session).  Captures more
        # cross-origin players but also starts decoys / previews.
        sessions = getattr(tab, "_paprika_oopif_sessions", None) or {}
        ws = getattr(tab, "socket", None)
        if ws is not None and sessions:
            import json as _json
            ctr = getattr(tab, "_paprika_autoplay_msg_id", None)
            if ctr is None:
                ctr = [20_000_000]
                setattr(tab, "_paprika_autoplay_msg_id", ctr)
            for sid in list(sessions.keys()):
                try:
                    ctr[0] += 1
                    await ws.send(_json.dumps({
                        "id": ctr[0],
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": _AUTOPLAY_CLICK_JS,
                            "userGesture": True,
                            "returnByValue": True,
                            "awaitPromise": False,
                        },
                        "sessionId": sid,
                    }))
                    result["oopif"] += 1
                except Exception as e:
                    if log:
                        log(f"  [autoplay] OOPIF click failed "
                            f"(sid {str(sid)[:8]}): {type(e).__name__}: {e}")
    if log:
        log(f"  [autoplay] top: clicked={top.get('clicked')} "
            f"score={top.get('score')} playing={top.get('playing')}; "
            f"main_trusted={result['trusted']} oopif_sessions={result['oopif']}")
    return result


async def trigger_autoplay_trusted(
    tab, rect: dict | None = None, log: LogFn | None = None,
) -> bool:
    """Trusted-Input fallback for the TOP frame: dispatch a real mouse
    click (via :func:`click_at`, which routes through CDP Input) at the
    centre of the largest player rect so canvas / transparent-overlay
    players that the scored eval-click can't target still receive a
    genuine user gesture.  ``rect`` = ``{"x":.., "y":..}`` (top-frame
    viewport coords) or None → viewport centre.  Fire once; never raises.
    """
    try:
        if not rect or "x" not in rect or "y" not in rect:
            try:
                remote, _exc = await tab.send(cdp.runtime.evaluate(
                    expression=("({x: Math.round((window.innerWidth||1280)/2),"
                                " y: Math.round((window.innerHeight||720)/2)})"),
                    return_by_value=True,
                ))
                rect = getattr(remote, "value", None) or {"x": 640, "y": 360}
            except Exception:
                rect = {"x": 640, "y": 360}
        x = int(rect.get("x", 640))
        y = int(rect.get("y", 360))
        _noop = (lambda *_a, **_k: None)
        await click_at(tab, x, y, log or _noop)
        if log:
            log(f"  [autoplay] trusted Input click at ({x},{y})")
        return True
    except Exception as e:
        if log:
            log(f"  [autoplay] trusted Input click failed: "
                f"{type(e).__name__}: {e}")
        return False


async def install_url_capture_hook(tab, log: LogFn | None = None) -> bool:
    """Inject ``_URL_CAPTURE_HOOK_JS`` into every new document via
    ``Page.addScriptToEvaluateOnNewDocument``.  Runs once per tab;
    idempotent on repeated calls.

    The injected script writes captured URLs to
    ``window.top.__paprika_url_capture`` so a single periodic poll of
    the top window covers ALL same-origin iframes (hls.js inside an
    embed iframe, ad widgets, lazy XHRs that don't surface in the CDP
    Network domain for whatever reason).

    Cross-origin iframes are already handled by
    ``install_iframe_deep_trace`` which gives each cross-origin
    target its own Network.enable.  This hook is the same-origin
    counterpart that the OOPIF-only deep-trace can't reach.

    Returns True on first install, False if already installed or the
    CDP call failed (non-fatal).
    """
    if getattr(tab, "_paprika_url_capture_hook_on", False):
        return False
    try:
        # 1. Register for FUTURE document loads (including iframes
        #    created after this call).  addScriptToEvaluateOnNewDocument
        #    by itself does NOT inject into the current document --
        #    only documents created AFTER registration get the script.
        _script_id = await tab.send(
            cdp.page.add_script_to_evaluate_on_new_document(
                source=_URL_CAPTURE_HOOK_JS,
            )
        )
        if log:
            log(f"  [url-capture] addScriptToEvaluateOnNewDocument id={_script_id}")
        # 2. Also inject into the CURRENT document NOW so we don't
        #    miss the first navigation's fetches.  The hook script's
        #    `if (window.__paprika_url_hook) return;` guard makes it
        #    safe to run twice.
        try:
            await tab.evaluate(_URL_CAPTURE_HOOK_JS)
        except Exception as _e:
            if log:
                log(f"  [url-capture] immediate inject failed: {_e}")
        # 3. Re-inject on EVERY main-frame navigation. Empirically
        #    addScriptToEvaluateOnNewDocument doesn't always apply to
        #    the next navigation when the registration happens while
        #    the tab is on about:blank (observed on this codebase).
        #    Hooking Page.frameNavigated and re-running the script
        #    via Runtime.evaluate guarantees the hook IS present in
        #    every document we end up on.
        async def _on_frame_navigated(event):
            try:
                # Only top frame: iframe sub-frame events are handled
                # by addScriptToEvaluateOnNewDocument's own iframe
                # support (cross-origin ones via install_iframe_deep_trace).
                frame = getattr(event, "frame", None)
                if frame is None:
                    return
                parent_id = getattr(frame, "parent_id", None) or getattr(frame, "parentId", None)
                if parent_id:
                    return  # iframe, skip (covered by addScript registration)
                try:
                    await tab.evaluate(_URL_CAPTURE_HOOK_JS)
                except Exception:
                    pass
            except Exception:
                pass

        try:
            tab.handlers.setdefault(
                cdp.page.FrameNavigated, []
            ).append(_on_frame_navigated)
            # Page domain must be enabled for FrameNavigated to fire.
            await tab.send(cdp.page.enable())
        except Exception as _e:
            if log:
                log(f"  [url-capture] frameNavigated hook failed: {_e}")
        setattr(tab, "_paprika_url_capture_hook_on", True)
        if log:
            log(
                "  [url-capture] fetch+XHR hook installed "
                "(addScript + immediate inject + frameNavigated reinject)"
            )
        return True
    except Exception as e:
        if log:
            log(
                f"  [url-capture] install failed "
                f"(non-fatal): {type(e).__name__}: {e}"
            )
        return False


async def read_url_capture(tab) -> list[dict]:
    """Read and clear ``window.top.__paprika_url_capture``.

    Returns the freshly-captured entries (each ``{api, url, t, ...}``)
    and resets the array so the next poll only sees new URLs.  Safe
    to call even when the hook isn't installed -- returns ``[]``.

    Called from the session-scope poller started by
    ``install_session_asset_capture``.
    """
    try:
        # Splice the array to empty and return what was there.  Done
        # in one expression so we don't race with the page hook
        # appending between read + reset.  Returns a JSON STRING so
        # tab.evaluate (which only returns Runtime.evaluate result.value
        # in nodriver, not return_by_value) gives us a parseable
        # string regardless of whether the bucket itself is JSON-safe.
        # Also includes __paprika_hook_installs so the caller can see
        # in worker logs whether the hook script actually ran in any
        # frame (helps distinguish "hook never executed" from "hook
        # executed but page makes no fetch/XHR").
        result = await tab.evaluate(
            "JSON.stringify({"
            "u: (window.__paprika_url_capture && "
            "window.__paprika_url_capture.splice(0)) || [], "
            "i: window.__paprika_hook_installs || 0"
            "})"
        )
        import json as _json
        parsed = None
        if isinstance(result, str):
            try:
                parsed = _json.loads(result)
            except Exception:
                parsed = None
        elif isinstance(result, dict):
            parsed = result
        elif isinstance(result, tuple) and result:
            inner = result[0]
            if isinstance(inner, str):
                try:
                    parsed = _json.loads(inner)
                except Exception:
                    parsed = None
            elif isinstance(inner, dict):
                parsed = inner
        if isinstance(parsed, dict):
            urls = parsed.get("u", [])
            installs = parsed.get("i", 0)
            if isinstance(urls, list):
                # Stash the install count on the function as a side
                # channel for the poller to surface in heartbeat logs.
                read_url_capture._last_installs = installs  # type: ignore[attr-defined]
                return urls
        # Old shape (list only): treat as URL list directly.
        if isinstance(parsed, list):
            return parsed
        return []
    except Exception:
        return []


async def force_single_tab(
    browser,
    *,
    keep_target_id: str | None = None,
    log: LogFn | None = None,
) -> int:
    """Reduce the browser to exactly ONE ``page`` target via CDP.

    Enumerates targets via ``Target.getTargets`` and closes everything
    of type ``"page"`` except the one identified by ``keep_target_id``
    (or the first one in the list when no id is supplied). Returns the
    number of targets actually closed.

    Use this at session_start to clean up tabs left over from a
    previous session, at session_end so the next user lands on a
    fresh single-tab browser, and around fetch jobs for the same
    reason. More reliable than reading ``browser.tabs``, which can
    lag CDP state on fast-changing pages (especially when popups
    or ad scripts open windows during navigation).

    Best-effort: per-target close errors are logged and swallowed
    so a runaway popup can't block the rest of the cleanup.
    """
    try:
        targets = await browser.send(cdp.target.get_targets()) or []
    except Exception as e:
        if log:
            log(f"  [tab-cleanup] get_targets failed: {e}")
        return 0
    pages = [t for t in targets if getattr(t, "type_", None) == "page"]
    if len(pages) <= 1:
        return 0
    if keep_target_id is None:
        # Prefer the FIRST page target -- usually the one that was
        # already open from the lane's Chrome startup.
        keep_target_id = getattr(pages[0], "target_id", None)
    closed = 0
    for t in pages:
        tid = getattr(t, "target_id", None)
        if not tid or tid == keep_target_id:
            continue
        try:
            await browser.send(cdp.target.close_target(target_id=tid))
            closed += 1
        except Exception as e:
            if log:
                log(f"  [tab-cleanup] close {tid[:8]}.. failed: {e}")
    if log and closed:
        log(
            f"  [tab-cleanup] closed {closed} extra tab(s) (kept {keep_target_id[:8] if keep_target_id else '?'}..)"
        )
    return closed

