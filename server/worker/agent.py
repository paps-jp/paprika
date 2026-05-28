"""Worker agent: connects to a hub via WebSocket, accepts jobs, runs them
locally with nodriver, streams logs back, uploads assets via HTTP.

The agent is fully autonomous — given a `--hub-url`, it registers itself,
sends heartbeats, picks up assigned jobs, and reports completion.
"""

from __future__ import annotations

import asyncio
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
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from core.fetcher import (
    FetchOptions,
    clone_chrome_profile,
    fetch,
)
from server.protocol import (
    AssetInfo,
    HubAssignJob,
    HubProfileDelete,
    HubProfileSync,
    HubRegistered,
    HubScreenshotRequest,
    HubSessionAction,
    HubSessionAgent,
    HubSessionEnd,
    HubSessionStart,
    JobOptions,
    JobResult,
    JobStatus,
    ProfileCacheEntry,
    SessionStateSnapshot,
    WorkerCapabilities,
    WorkerHeartbeat,
    WorkerJobAccepted,
    WorkerJobComplete,
    WorkerJobFailed,
    WorkerJobLog,
    WorkerRegister,
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


_logger = logging.getLogger(__name__)

# JS snippet that extracts every navigatable <a href> from the current
# document. Returns a JSON-stringified array of {href,text,target,rel}.
# Shared by the live ``kind=links`` action handler AND the session-end
# dump path (see ``_dump_session_to_parent_job``). Kept at module scope
# so the two callers can't drift -- a fix to the extraction logic (e.g.
# a new skipProto entry) lands in both places automatically.
_LINKS_EXTRACT_JS = r"""
(() => {
  const seen = new Set();
  const out = [];
  const skipProto = (u) => {
    const lc = u.toLowerCase();
    return lc.startsWith('javascript:')
        || lc.startsWith('mailto:')
        || lc.startsWith('tel:')
        || lc.startsWith('blob:')
        || lc.startsWith('data:')
        || lc.startsWith('about:');
  };
  for (const a of document.links) {
    const u = a.href || '';
    if (!u || skipProto(u) || seen.has(u)) continue;
    seen.add(u);
    let t = (a.textContent || '').replace(/\s+/g, ' ').trim();
    if (t.length > 120) t = t.slice(0, 119) + '…';
    out.push({
      href: u,
      text: t,
      target: a.target || '',
      rel: a.rel || '',
    });
  }
  return JSON.stringify(out);
})()
"""


def _resolve_worker_id_file() -> Path:
    """Cross-platform default for the worker_id persistence file.

    Resolution order:

      1. ``PAPRIKA_WORKER_ID_FILE`` env var — explicit override. Use this
         when you want the worker_id to land in an unusual place (e.g.
         a host-mounted Windows path under Docker Desktop, or a shared
         network filesystem). The directory is created on demand.

      2. ``~/.paprika/worker_id`` — the historical default. Resolves to::

           Linux container:   /root/.paprika/worker_id   (default $HOME)
           Linux native:      /home/<user>/.paprika/worker_id
           macOS:             /Users/<user>/.paprika/worker_id
           Windows native:    C:\\Users\\<user>\\.paprika\\worker_id

         The docker-compose worker service mounts ``paprika-worker-state``
         at ``/root/.paprika`` so this path survives container restarts.

      3. ``<tempdir>/paprika/worker_id`` — fallback when ``Path.home()``
         is unusable (rare Windows service contexts, restricted Docker
         runtimes). Survives the process but not a host reboot.

      4. ``./.paprika/worker_id`` — last resort, relative to CWD.
    """
    env = os.environ.get("PAPRIKA_WORKER_ID_FILE", "").strip()
    if env:
        return Path(env)
    try:
        home = Path.home()
        # Path.home() can return Path("/") or similar nonsense under
        # some service-account / minimal-env Docker configurations; only
        # honor it if it points somewhere with depth.
        if str(home) not in ("", "/", "\\", ".") and home.parent != home:
            return home / ".paprika" / "worker_id"
    except Exception:
        pass
    try:
        import tempfile as _tempfile

        return Path(_tempfile.gettempdir()) / "paprika" / "worker_id"
    except Exception:
        pass
    return Path(".paprika") / "worker_id"


WORKER_ID_FILE = _resolve_worker_id_file()
VERSION_FILE = Path("/app/VERSION")


# ---------------------------------------------------------------------------
# page.agent() helpers (engine dispatch + JP->EN translation)
# ---------------------------------------------------------------------------

# Hiragana / katakana / CJK ideographs. We only detect "looks non-ASCII /
# probably needs translation" -- the actual translation step decides if
# it's already English-ish and short-circuits.
import re as _re

_JP_CHAR_RE = _re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")


def _looks_non_english(text: str) -> bool:
    """Cheap heuristic: is this string worth running through a
    translator? True when any Japanese/Chinese ideograph/kana is
    present. The downstream translator is a no-op for already-English
    text, but we skip the round-trip in the common case.
    """
    if not text:
        return False
    return bool(_JP_CHAR_RE.search(text))


async def _translate_to_english(
    text: str,
    *,
    agent_llm_url: str,
    model_name: str,
    timeout_s: float = 30.0,
    log=None,
) -> str:
    """Ask the configured chat-completions LLM (Qwen2.5-VL by default)
    to render ``text`` as a one-line English imperative.

    Used as a pre-step before CogAgent / page.agent() so Japanese or
    Chinese goals (which CogAgent mis-parses) become English ones the
    GUI models actually understand. Falls back to the original ``text``
    on any error -- we'd rather lose translation than block the agent.
    """
    import httpx as _httpx

    if not text:
        return text
    prompt = (
        "Rewrite the following GUI task as a single short English "
        "imperative sentence. Keep it specific and actionable, like the "
        "examples below. Reply with ONLY the rewritten sentence -- no "
        "quotes, no preamble, no explanation.\n\n"
        "Examples:\n"
        "  Input:  ログインボタンをクリック\n"
        "  Output: Click the login button.\n"
        "  Input:  サイト上の画像イメージを５秒ごとにクリックして\n"
        "  Output: Click each image thumbnail on the page in turn.\n"
        "  Input:  この動画を再生\n"
        "  Output: Click the play button on the video.\n\n"
        f"Input:  {text}\n"
        f"Output:"
    )
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 256,
    }
    try:
        async with _httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(
                f"{agent_llm_url.rstrip('/')}/v1/chat/completions",
                json=body,
            )
            r.raise_for_status()
            data = r.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        # Some models echo back "Output:" prefix; strip it.
        if content.lower().startswith("output:"):
            content = content[7:].strip()
        # Strip surrounding quotes the model sometimes adds.
        if len(content) >= 2 and content[0] in ('"', "'", "「", "『"):
            content = content.strip("\"'「『」』 ")
        if not content:
            if log:
                log("  [translate] empty output, keeping original")
            return text
        return content
    except Exception as e:
        if log:
            log(f"  [translate] failed ({type(e).__name__}: {e}); keeping original")
        return text


# ---------------------------------------------------------------------------
# Video URL downloader (shared between mode=vision-agent and page.agent)
# ---------------------------------------------------------------------------
#
# Chrome doesn't fire Network.responseReceived for top-level navigations
# that land on a media MIME (it hands the response to the built-in
# video player), so the passive asset capture misses cases like:
#
#   click thumbnail -> popup_policy=follow redirects main tab to
#                      video.twimg.com/.../*.mp4 (twitter)
#                   -> popup_policy=follow redirects to /watch?v=...
#                      that *is* an m3u8 playlist (streaming sites)
#
# Both job mode (_run_vision_agent_job) and session mode
# (_handle_session_agent's auto/cogagent engine) need to detect the
# URL change and pull the file themselves. This factory builds the
# tracker + downloader closures + a drain() to await pending tasks
# before the browser tears down.

_VIDEO_DIRECT_RE = _re.compile(r"\.(mp4|webm|mov|m4v|mkv)($|\?)", _re.I)
_VIDEO_STREAM_RE = _re.compile(r"\.(m3u8|mpd)($|\?)", _re.I)

# ---------------------------------------------------------------------------
# Vendor-neutral video-discovery heuristics used by page.download_video().
# Deliberately reference NO specific hostnames -- the patterns target URL
# shape (path keywords + opaque-token query params) and DOM structure so
# they generalise across video-host sites paprika has crawled.
# ---------------------------------------------------------------------------

# URL path components common to player / embed endpoints (case-insensitive
# substring match). NOT a regex of host names. Tuned from the curated
# video-download Skills + past codegen-loop runs.
_PLAYER_PATH_KEYWORDS = (
    "/embed",
    "/player",
    "/iframe",
    "/frame",
    "/play",
    "/watch",
    "/v/",
    "/vid/",
    "/stream",
    "/video",
)

# A query-string value of this length composed only of alnum + the
# base64 / urlsafe-base64 / hex padding chars is a strong signal of an
# opaque player token (per-session encrypted id). Many video hosts route
# their iframe through a URL like ``/frame?pi=<big-token>``; detecting
# the token shape avoids needing to recognise the host itself.
_OPAQUE_TOKEN_MIN_LEN = 32
_OPAQUE_TOKEN_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "+/=_-."
)


def _looks_like_player_iframe(src: str) -> bool:
    """Vendor-neutral: does this iframe src look like an embedded video
    player? Returns True when either:

    * the URL path contains a player-like keyword
      (/embed, /player, /iframe, /frame, /play, /watch, /v/, /vid/,
      /stream, /video), OR
    * a query-string value is a long opaque token (>= 32 chars of
      base64-ish characters) -- characteristic of per-session player
      keys handed off by the outer page.

    Both heuristics are URL-shape based -- no hostnames involved.
    """
    if not src or not (
        src.startswith("http://") or src.startswith("https://")
    ):
        return False
    from urllib.parse import urlparse, parse_qs

    try:
        p = urlparse(src)
    except Exception:
        return False
    path = (p.path or "/").lower()
    for kw in _PLAYER_PATH_KEYWORDS:
        if kw in path:
            return True
    try:
        q = parse_qs(p.query)
    except Exception:
        q = {}
    for values in q.values():
        for v in values:
            if (
                len(v) >= _OPAQUE_TOKEN_MIN_LEN
                and all(c in _OPAQUE_TOKEN_CHARS for c in v)
            ):
                return True
    return False


async def _extract_dom_video_urls(tab) -> list[str]:
    """Return absolute URLs from <video src=""> and <source src="">
    elements in the main document. Best-effort -- returns [] on
    evaluate failures."""
    try:
        urls = await tab.evaluate(
            "JSON.stringify("
            "[...document.querySelectorAll('video[src], source[src]')]"
            ".map(el => el.src || el.getAttribute('src') || '')"
            ".filter(u => u && u.startsWith('http'))"
            ")",
        )
    except Exception:
        return []
    if not urls:
        return []
    try:
        import json as _j
        return [u for u in _j.loads(urls) if isinstance(u, str)]
    except Exception:
        return []


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


async def _trigger_video_playback(tab) -> None:
    """Best-effort: nudge any visible <video>/<audio> to start so HLS
    init segments fire into network_log. Cross-origin iframes can't be
    touched from the outer document -- that's handled by the
    iframe-walk tier of download_video."""
    try:
        await tab.evaluate(
            "document.querySelectorAll('video,audio')"
            ".forEach(v => { try { v.play(); } catch(e){} });"
        )
    except Exception:
        pass


async def _try_click_play_button(tab) -> bool:
    """Best-effort: click the most likely play-button on the current
    page so a player that blocks programmatic autoplay still starts
    loading its HLS / DASH manifest. Returns True when something was
    clicked.

    Vendor-neutral heuristic -- looks at:
      * aria-label / title containing play / 再生 / start
      * visible textContent starting with play / 再生 / ▶ / ► / start
      * class name containing "play"
      * the <video> element itself (many players overlay a transparent
        click target on top to convert the play click into a user
        gesture)
    Visible elements only (rect >= 20x20 and offsetParent set), and the
    first match (by ranked confidence) is clicked exactly once."""
    try:
        clicked = await tab.evaluate(
            "(() => {"
            "  const PLAY_TXT = /^(play|再生|スタート|start|▶|►|>)/i;"
            "  const ARIA_RX  = /(play|再生|start|スタート)/i;"
            "  const isVis = el => {"
            "    if (!el || !el.getBoundingClientRect) return false;"
            "    const r = el.getBoundingClientRect();"
            "    return r.width >= 20 && r.height >= 20"
            "      && el.offsetParent !== null;"
            "  };"
            "  const score = el => {"
            "    if (!isVis(el)) return -1;"
            "    let s = 0;"
            "    const aria = el.getAttribute('aria-label') || '';"
            "    const title = el.getAttribute('title') || '';"
            "    const txt = (el.textContent || '').trim();"
            "    const cls = (typeof el.className === 'string'"
            "      ? el.className"
            "      : (el.className && el.className.baseVal) || '');"
            "    if (ARIA_RX.test(aria)) s += 10;"
            "    if (ARIA_RX.test(title)) s += 5;"
            "    if (PLAY_TXT.test(txt)) s += 5;"
            "    if (/play/i.test(cls)) s += 3;"
            "    if (el.tagName === 'VIDEO') s += 2;"
            "    return s;"
            "  };"
            "  const sel = "
            "    'video, button, [role=\"button\"], a, div, span';"
            "  let best = null; let bestScore = 0;"
            "  for (const el of document.querySelectorAll(sel)) {"
            "    const sc = score(el);"
            "    if (sc > bestScore) { best = el; bestScore = sc; }"
            "  }"
            "  if (best) { try { best.click(); return true; } "
            "             catch (e) { return false; } }"
            "  return false;"
            "})()"
        )
    except Exception:
        return False
    return bool(clicked)


# ============================================================
# Phase 3a: per-frame CDP helpers for iframe-aware Tier 4
# ============================================================
#
# Why per-frame matters: many embedded video players check
# ``window.top === window.self`` and show a refusal page when loaded
# at top level. The legacy Tier 4 path that navigates the top frame
# to the iframe's URL trips that check. The CDP isolated-world path
# below keeps the iframe inside its parent context -- the player
# loads normally and we can poke it via Runtime.evaluate(contextId).
#
# All helpers are vendor-neutral (no hostname checks anywhere).


async def _enumerate_all_frames(tab, *, max_depth: int = 3) -> list[dict]:
    """Walk CDP Page.getFrameTree and return a flat list of
    ``{frame_id, url, depth}`` for every NON-top frame, depth-limited.

    Sees frames that ``document.querySelectorAll('iframe[src]')`` can't:
      * JS-injected iframes added after page-load (DOM query would
        need a wait + re-query; CDP sees them via the live frame tree)
      * Nested iframes (iframe inside iframe), recursive
      * Cross-origin frames (DOM query gives src but not the actual
        loaded URL after redirects; frame tree carries the post-
        redirect URL)

    Returns shallow-first; caller decides priority.
    """
    from nodriver import cdp as _cdp
    try:
        tree = await tab.send(_cdp.page.get_frame_tree())
    except Exception:
        return []

    out: list[dict] = []

    def _walk(node, depth: int) -> None:
        if depth > max_depth:
            return
        if depth > 0:  # skip top frame -- already covered by other tiers
            frame = getattr(node, "frame", None)
            if frame is not None:
                fid = getattr(frame, "id_", None) or getattr(frame, "id", None)
                furl = getattr(frame, "url", "") or ""
                if fid:
                    out.append({
                        "frame_id": str(fid),
                        "url": furl,
                        "depth": depth,
                    })
        for child in (getattr(node, "child_frames", None) or []):
            _walk(child, depth + 1)

    _walk(tree, 0)
    return out


async def _evaluate_in_frame(
    tab,
    frame_id: str,
    expression: str,
    *,
    user_gesture: bool = False,
    log=None,
):
    """Run ``expression`` inside ``frame_id``'s isolated world. Returns
    the JS return value (JSON-coerced via ``return_by_value=True``) or
    None on any error. Best-effort.

    Isolated world prevents collisions with the frame's own globals
    and (more importantly) gives us a stable execution context id even
    when the frame's main world reloads underneath us.

    ``user_gesture=True`` is the magic that lets a click() call here
    count as a real user gesture for autoplay-blocked players.
    """
    from nodriver import cdp as _cdp
    try:
        ctx_id = await tab.send(
            _cdp.page.create_isolated_world(
                _cdp.page.FrameId(frame_id),
                world_name="paprika_iframe_probe",
            )
        )
    except Exception as e:
        if log:
            log(
                f"  ... isolated world create for frame "
                f"{frame_id[:8]} failed: {type(e).__name__}: {e}"
            )
        return None
    try:
        remote, exc = await tab.send(
            _cdp.runtime.evaluate(
                expression=expression,
                context_id=ctx_id,
                return_by_value=True,
                await_promise=True,
                user_gesture=user_gesture,
            )
        )
        if exc is not None:
            if log:
                log(
                    f"  ... evaluate in frame {frame_id[:8]} threw: "
                    f"{getattr(exc, 'text', None) or exc}"
                )
            return None
        return getattr(remote, "value", None) if remote else None
    except Exception as e:
        if log:
            log(
                f"  ... evaluate in frame {frame_id[:8]} failed: "
                f"{type(e).__name__}: {e}"
            )
        return None


async def _extract_dom_video_urls_in_frame(tab, frame_id: str) -> list[str]:
    """Per-frame version of :func:`_extract_dom_video_urls`. Returns
    URLs from <video src="..."> / <source src="..."> elements INSIDE
    the named frame (not its parents)."""
    raw = await _evaluate_in_frame(
        tab,
        frame_id,
        "JSON.stringify("
        "[...document.querySelectorAll('video[src], source[src]')]"
        ".map(el => el.src || el.getAttribute('src') || '')"
        ".filter(u => u && u.startsWith('http'))"
        ")",
    )
    if not raw or not isinstance(raw, str):
        return []
    try:
        import json as _j
        return [u for u in _j.loads(raw) if isinstance(u, str)]
    except Exception:
        return []


async def _try_click_play_button_in_frame(tab, frame_id: str) -> bool:
    """Per-frame version of :func:`_try_click_play_button`. Synthesises
    a user-gesture click on the most play-like visible element inside
    ``frame_id``, which is the step that unlocks autoplay-blocked
    HLS manifest requests without touching the top frame."""
    js = (
        "(() => {"
        "  const PLAY_TXT = /^(play|再生|スタート|start|▶|►|>)/i;"
        "  const ARIA_RX  = /(play|再生|start|スタート)/i;"
        "  const isVis = el => {"
        "    if (!el || !el.getBoundingClientRect) return false;"
        "    const r = el.getBoundingClientRect();"
        "    return r.width >= 20 && r.height >= 20"
        "      && el.offsetParent !== null;"
        "  };"
        "  const score = el => {"
        "    if (!isVis(el)) return -1;"
        "    let s = 0;"
        "    const aria = el.getAttribute('aria-label') || '';"
        "    const title = el.getAttribute('title') || '';"
        "    const txt = (el.textContent || '').trim();"
        "    const cls = (typeof el.className === 'string'"
        "      ? el.className"
        "      : (el.className && el.className.baseVal) || '');"
        "    if (ARIA_RX.test(aria)) s += 10;"
        "    if (ARIA_RX.test(title)) s += 5;"
        "    if (PLAY_TXT.test(txt)) s += 5;"
        "    if (/play/i.test(cls)) s += 3;"
        "    if (el.tagName === 'VIDEO') s += 2;"
        "    return s;"
        "  };"
        "  const sel = "
        "    'video, button, [role=\"button\"], a, div, span';"
        "  let best = null; let bestScore = 0;"
        "  for (const el of document.querySelectorAll(sel)) {"
        "    const sc = score(el);"
        "    if (sc > bestScore) { best = el; bestScore = sc; }"
        "  }"
        "  if (best) {"
        "    try {"
        "      const v = best.querySelector ? best.querySelector('video') : null;"
        "      if (v) { try { v.play(); } catch(e){} }"
        "      best.click();"
        "      return true;"
        "    } catch (e) { return false; }"
        "  }"
        "  return false;"
        "})()"
    )
    result = await _evaluate_in_frame(
        tab, frame_id, js, user_gesture=True,
    )
    return bool(result)


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


def _sniff_stream_urls_from_log(network_log) -> list[str]:
    """Return ``.m3u8`` / ``.mpd`` URLs from a session network_log,
    newest-first, de-duplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for e in reversed(network_log or []):
        u = e.get("url") or ""
        if not u or u in seen:
            continue
        if _VIDEO_STREAM_RE.search(u):
            out.append(u)
            seen.add(u)
    return out


async def _paprika_agent_run(
    chrome_port: int,
    cmd: str,
    args: dict | None = None,
    *,
    timeout: float = 10.0,
    log=None,
) -> dict | None:
    """Run a Paprika Agent extension command on its service-worker
    target via raw CDP, and return the parsed ``{ok, result|error}``
    dict -- or ``None`` if the agent SW couldn't be found/reached
    (caller should then fall back).

    The Paprika Agent extension (server/web/extensions/paprika-agent,
    loaded into every lane's Chrome) exposes one command bus,
    ``globalThis.__paprikaAgent.run(cmd, args)``, for capabilities CDP
    can't drive directly (genuine chrome.tabs page zoom, ...). We reach
    it by listing the lane's debug targets, finding the extension's
    service-worker target, opening a raw CDP websocket to it, and
    Runtime.evaluate-ing the command. Self-contained (doesn't touch the
    nodriver connection) so it can't perturb the main tab.
    """
    import json as _json

    args = args or {}
    try:
        import websockets as _ws  # nodriver dependency; always present
    except Exception:
        return None
    base = f"http://127.0.0.1:{int(chrome_port)}"
    # Enumerate targets; extension service workers show up as type
    # "service_worker" with a chrome-extension://<id>/background.js url.
    try:
        async with httpx.AsyncClient(timeout=5.0) as _c:
            resp = await _c.get(base + "/json/list")
            targets = resp.json()
    except Exception as e:
        if log:
            log(f"[agent] target list failed: {type(e).__name__}: {e}")
        return None
    sw_targets = [
        t for t in (targets or [])
        if t.get("type") == "service_worker"
        and t.get("webSocketDebuggerUrl")
        and "background.js" in (t.get("url") or "")
    ]
    if not sw_targets:
        return None
    expr = (
        "(globalThis.__paprikaAgent ? __paprikaAgent.run("
        + _json.dumps(cmd) + "," + _json.dumps(args)
        + ") : ({ok:false,error:'no-agent'}))"
    )
    loop = asyncio.get_event_loop()
    for t in sw_targets:
        try:
            async with _ws.connect(t["webSocketDebuggerUrl"], max_size=None) as ws:
                await ws.send(_json.dumps({
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expr,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }))
                deadline = loop.time() + timeout
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    msg = _json.loads(raw)
                    if msg.get("id") != 1:
                        continue
                    res = (msg.get("result") or {}).get("result") or {}
                    val = res.get("value")
                    if isinstance(val, dict):
                        if val.get("error") == "no-agent":
                            break  # this SW isn't ours; try the next
                        return val
                    break
        except Exception as e:
            if log:
                log(f"[agent] SW call failed: {type(e).__name__}: {e}")
            continue
    return None


def _make_video_downloader(
    *,
    assets_dir: Path,
    min_asset_size: int,
    on_saved,  # async callable (path: Path, info: dict) -> None
    log,  # sync logger callable (line: str) -> None -- worker stderr
    job_id_for_logs: str,
    job_log=None,  # optional callable (line: str) -> None -- streams to
    #              Live panel via WorkerJobLog if the session is tied
    #              to a parent job. None when the downloader runs in
    #              a context without a job (rare; mostly tests).
    page_url_provider=None,  # optional sync callable () -> str|None
    #              returning the session's current TOP-LEVEL page URL.
    #              Used as a referer fallback for HLS/DASH streams that
    #              were loaded inside a cross-origin iframe player: the
    #              CDP-observed document URL (passed as ``referer``) is
    #              then the IFRAME url, which the CDN often rejects --
    #              the top page url is the referer it actually expects.
):
    """Return ``(maybe_download, drain)`` closures.

    ``maybe_download(url, referer)`` is sync: if ``url`` looks like a
    video (regex match on .mp4/.webm/... or .m3u8/.mpd), it spawns a
    background asyncio.Task that fetches + uploads the file.
    Idempotent on the same URL (a tracked set short-circuits repeats).

    ``drain()`` is async: awaits all pending download tasks. Call it
    before tearing down the browser (otherwise yt-dlp on an HLS
    stream gets killed mid-merge). Uses an idle-window policy
    (default 45s of zero progress before abandoning) rather than a
    fixed wall-clock timeout, so a 1+ GB stream that's still
    actively downloading keeps the lane open until it finishes -- a
    fixed cap would cancel a 90%-complete download just because the
    file was large.

    ``on_saved`` is the parent's upload-to-hub callback; for jobs
    that's the assign-level uploader, for sessions it's the
    session-asset uploader.
    """
    downloaded_urls: set = set()
    pending: list = []
    # url -> last_activity_unixtime. Updated by _download_direct on
    # each chunk it reads and by _download_stream on each yt-dlp log
    # line. Read by drain() to decide whether to keep waiting (active
    # progress -> wait) or give up (no progress for idle_window -> cancel).
    last_progress: dict[str, float] = {}
    import httpx as _httpx

    def _both(line: str) -> None:
        """Log a line to BOTH the worker stderr and (when bound) the
        parent job's Live panel via WorkerJobLog. Use this for
        operator-visible status: detection, progress, save/fail.
        Reserve plain ``log`` for chatty internals nobody outside the
        worker container needs to see."""
        try:
            log(line)
        except Exception:
            pass
        if job_log is not None:
            try:
                job_log(line)
            except Exception:
                pass

    async def _download_direct(target_url: str, referer: str) -> None:
        base_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        }
        # Attempt ordering: prefer WITH referer when we have one,
        # then fall back to none. Reversed from the original
        # ("no-referer first") order after observing that
        # hotlink-protected video CDNs (e.g. 238.2babes.com behind
        # bird.openhub.tv) reject the bare request with 400, then
        # the same /<asset>?md5=... URL is rate-limited / 503'd on
        # the immediate retry -- the second referer-bearing request
        # arrives within the CDN's anti-abuse cooldown window and
        # fails too. By leading with the correct referer we win on
        # the first hit; the no-referer fallback only matters for
        # the older twitter/twitch case where the CDN rejects third-
        # party referers and accepts none.
        attempts: list[dict] = []
        if referer:
            attempts.append({"Referer": referer})
        attempts.append({})

        # Pre-compute the target path so we can stream directly to
        # disk without buffering the whole body in RAM -- a 1+ GB
        # mp4 would otherwise need 2.5+ GB of memory (bytearray +
        # bytes copy) and trip the container's OOM killer on
        # workers sized for typical image scraping. Writing through
        # a .part suffix lets us atomically rename on completion so
        # readers never see a truncated file.
        from urllib.parse import urlparse as _up
        name = Path(_up(target_url).path).name or "video.mp4"
        name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:180]
        target_path = assets_dir / name
        if target_path.exists():
            stem, suffix = target_path.stem, target_path.suffix
            n = 1
            while (assets_dir / f"{stem}_{n}{suffix}").exists():
                n += 1
            target_path = assets_dir / f"{stem}_{n}{suffix}"
        part_path = target_path.with_suffix(target_path.suffix + ".part")

        # No read timeout at the httpx level -- a 1 GB+ video over a
        # slow link can easily exceed any fixed read window, and we
        # rely on drain()'s idle detection (which sees per-chunk
        # progress via last_progress) for stuck-download cleanup. The
        # connect timeout still guards against unreachable hosts.
        last_progress[target_url] = time.time()
        ok_attempt: dict | None = None
        mime: str | None = None
        total_written: int = 0
        last_err: str | None = None
        async with _httpx.AsyncClient(
            timeout=_httpx.Timeout(connect=15, read=None, write=30, pool=10),
            follow_redirects=True,
        ) as dl_client:
            for i, extra in enumerate(attempts):
                # Reset partial output on each retry.
                if part_path.exists():
                    try:
                        part_path.unlink()
                    except OSError:
                        pass
                total_written = 0
                try:
                    async with dl_client.stream(
                        "GET",
                        target_url,
                        headers={**base_headers, **extra},
                    ) as resp:
                        resp.raise_for_status()
                        mime = (
                            resp.headers.get("content-type") or ""
                        ).split(";")[0].strip()
                        # Surface the total expected bytes up front
                        # so the Live panel shows file size before
                        # the download starts (operator sees "yep,
                        # the 1.2 GB video is on the way, not stuck").
                        cl_header = resp.headers.get("content-length")
                        try:
                            total_expected = int(cl_header) if cl_header else 0
                        except ValueError:
                            total_expected = 0
                        used = "Referer" if extra else "no Referer"
                        if total_expected:
                            _both(
                                f"  ⬇ downloading "
                                f"{total_expected / 1024 / 1024:.1f} MB "
                                f"({mime or '?'}, "
                                f"{used}): {target_url[:100]}"
                            )
                        else:
                            _both(
                                f"  ⬇ downloading (unknown size, "
                                f"{mime or '?'}, "
                                f"{used}): {target_url[:100]}"
                            )
                        # Stream chunks directly to disk, bumping the
                        # last_progress timestamp on every chunk so
                        # drain() can tell the download is still
                        # actively flowing. 64 KB chunks amortise
                        # per-chunk overhead while still updating
                        # progress often enough that the idle window
                        # detects a stall within seconds. Buffered
                        # writes via Python's default file buffering
                        # are fine here -- the OS flushes on close.
                        #
                        # Emit a Live-panel progress line every ~10s
                        # of wall time so the operator can watch a
                        # 20-minute download tick instead of staring
                        # at a frozen screen.
                        progress_interval = 10.0
                        next_progress_at = time.time() + progress_interval
                        dl_started_at = time.time()
                        with open(part_path, "wb") as fout:
                            async for chunk in resp.aiter_bytes(
                                chunk_size=64 * 1024,
                            ):
                                fout.write(chunk)
                                total_written += len(chunk)
                                now = time.time()
                                last_progress[target_url] = now
                                if now >= next_progress_at:
                                    next_progress_at = now + progress_interval
                                    mb = total_written / 1024 / 1024
                                    elapsed = now - dl_started_at
                                    rate_mbps = (mb / elapsed) if elapsed > 0 else 0
                                    if total_expected:
                                        pct = (
                                            total_written * 100.0 / total_expected
                                        )
                                        remaining_mb = (
                                            (total_expected - total_written)
                                            / 1024 / 1024
                                        )
                                        eta_s = (
                                            remaining_mb / rate_mbps
                                            if rate_mbps > 0 else 0
                                        )
                                        _both(
                                            f"  ⬇ {mb:6.1f} / "
                                            f"{total_expected/1024/1024:.1f} MB "
                                            f"({pct:5.1f}%) at "
                                            f"{rate_mbps:.2f} MB/s "
                                            f"-- ETA {eta_s:.0f}s "
                                            f"({Path(target_path).name})"
                                        )
                                    else:
                                        _both(
                                            f"  ⬇ {mb:6.1f} MB at "
                                            f"{rate_mbps:.2f} MB/s "
                                            f"({Path(target_path).name})"
                                        )
                    _both(
                        f"  ✔ download body complete "
                        f"({total_written / 1024 / 1024:.1f} MB written "
                        f"with {used})"
                    )
                    ok_attempt = extra
                    break
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    if i + 1 < len(attempts):
                        # Brief backoff before the fallback attempt so
                        # the CDN's anti-abuse rate-limiter window
                        # (typically a few seconds for 4xx/5xx-tripped
                        # blacklist) has a chance to expire. Without
                        # this, an immediate retry usually hits 503.
                        log(
                            f"    attempt {i + 1} failed ({last_err}); "
                            f"sleeping 3s then trying alt referer mode"
                        )
                        await asyncio.sleep(3.0)
        # Either succeeded or exhausted attempts -- in both cases
        # we're no longer making progress on this URL.
        last_progress.pop(target_url, None)
        if ok_attempt is None:
            # Clean up the partial file from the last failed attempt.
            if part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass
            raise RuntimeError(last_err or "no attempts succeeded")
        if total_written == 0:
            log(f"  !! video download empty: {target_url[:80]}")
            try:
                part_path.unlink()
            except OSError:
                pass
            return
        if min_asset_size and total_written < min_asset_size:
            log(
                f"  !! video below min_asset_size "
                f"({total_written} < {min_asset_size}); skipping"
            )
            try:
                part_path.unlink()
            except OSError:
                pass
            return
        # Atomic rename so consumers / on_saved never see a partial
        # file with the final name.
        part_path.replace(target_path)
        _both(
            f"  ✅ video saved: {target_path.name} "
            f"({total_written / 1024 / 1024:.1f} MB, {mime or '?'}) -- "
            f"uploading to job assets..."
        )
        await on_saved(
            target_path,
            {
                "url": target_url,
                "mime": mime or "video/mp4",
                "document_url": referer,
            },
        )
        _both(f"  📤 video uploaded to gallery: {target_path.name}")

    async def _download_stream(target_url: str, referer: str) -> None:
        from core.fetcher import run_ytdlp

        ytdlp_timeout = int(
            os.environ.get(
                "VISION_YTDLP_TIMEOUT_S",
                "1800",
            )
        )
        log(f"  🎬 detected HLS/DASH URL, running yt-dlp (timeout={ytdlp_timeout}s)")
        before = {p.name for p in assets_dir.iterdir() if p.is_file()}
        # Mark the URL as actively progressing before yt-dlp starts
        # so drain() doesn't immediately abandon it during the
        # subprocess spawn / first-byte window.
        last_progress[target_url] = time.time()

        _loop = asyncio.get_running_loop()

        def _ytdlp_log(line: str) -> None:
            # Runs in asyncio.to_thread (a plain OS thread), so we
            # cannot call log() directly -- it uses ensure_future
            # which requires the calling thread to own the event loop.
            # call_soon_threadsafe is the correct cross-thread bridge.
            last_progress[target_url] = time.time()
            _logger.info(f"[{job_id_for_logs} yt-dlp] {line}")
            try:
                _loop.call_soon_threadsafe(log, f"  [yt-dlp] {line}")
            except RuntimeError:
                pass  # loop already stopped (job was cancelled)

        # Referer fallback chain. A stream loaded inside a cross-origin
        # iframe player (supjav -> lk1.supremejav.com -> saawsedge HLS)
        # carries the IFRAME's document URL as ``referer``; the CDN
        # 403s it and yt-dlp writes no file. The top-level page URL is
        # the referer the CDN actually expects -- proven on supjav: a
        # saawsedge main-content m3u8 that yields 0 bytes with the
        # iframe referer downloads in full with the supjav.com page
        # referer. So: try the frame referer first (correct for normal
        # / same-origin players, and the sidebar-preview streams that
        # already work), then fall back to the top page URL, then to no
        # referer. Stop the instant yt-dlp writes a file.
        page_url = None
        try:
            if page_url_provider is not None:
                page_url = page_url_provider()
        except Exception:
            page_url = None
        referer_candidates: list = []
        for _r in (referer, page_url, ""):
            if _r is None:
                continue
            if _r not in referer_candidates:
                referer_candidates.append(_r)

        ok = False
        msg = ""
        new_files = []
        try:
            for _idx, _cand_ref in enumerate(referer_candidates):
                if _idx > 0:
                    _kind = (
                        "page-url" if _cand_ref == page_url
                        else "no-referer" if not _cand_ref
                        else "frame"
                    )
                    log(
                        f"  🔁 yt-dlp retry with {_kind} referer: "
                        f"{(_cand_ref or '(none)')[:80]}"
                    )
                last_progress[target_url] = time.time()
                ok, msg = await asyncio.to_thread(
                    run_ytdlp,
                    target_url,
                    assets_dir,
                    _cand_ref or None,
                    None,
                    ytdlp_timeout,
                    _ytdlp_log,
                )
                after = {p.name for p in assets_dir.iterdir() if p.is_file()}
                new_files = sorted(after - before)
                if ok and new_files:
                    break  # got a file -- don't burn the other referers
        finally:
            last_progress.pop(target_url, None)
        after = {p.name for p in assets_dir.iterdir() if p.is_file()}
        new_files = sorted(after - before)
        if not ok and not new_files:
            log(
                f"  !! yt-dlp failed (tried {len(referer_candidates)} "
                f"referer(s)): {msg}"
            )
        else:
            log(f"  ✅ yt-dlp completed: {msg} ({len(new_files)} new file(s))")
        for name in new_files:
            path = assets_dir / name
            try:
                if min_asset_size and path.stat().st_size < min_asset_size:
                    log(f"  !! yt-dlp output {name} below min_asset_size; skipping")
                    continue
            except Exception:
                pass
            mime_guess = (
                "video/mp4"
                if path.suffix.lower() == ".mp4"
                else "video/webm"
                if path.suffix.lower() == ".webm"
                else None
            )
            await on_saved(
                path,
                {
                    "url": target_url,
                    "mime": mime_guess,
                    "document_url": referer,
                },
            )

    async def _run(target_url: str, referer: str, is_stream: bool) -> None:
        try:
            if is_stream:
                await _download_stream(target_url, referer)
            else:
                await _download_direct(target_url, referer)
        except asyncio.CancelledError:
            # Drain hit a timeout / hard cap. Worth surfacing to the
            # Live panel so the operator knows WHY no video appeared.
            _both(f"  !! video download cancelled (drain timeout): {target_url[:100]}")
            raise
        except Exception as e:
            _both(f"  !! video download failed: {type(e).__name__}: {e}")

    def maybe_download(target_url: str, referer: str) -> None:
        if not target_url or target_url in downloaded_urls:
            return
        is_stream = bool(_VIDEO_STREAM_RE.search(target_url))
        is_direct = bool(_VIDEO_DIRECT_RE.search(target_url))
        if not (is_stream or is_direct):
            return
        downloaded_urls.add(target_url)
        _both(
            f"  🎬 detected video URL ({'HLS/DASH' if is_stream else 'direct'}): {target_url[:100]}"
        )
        pending.append(asyncio.create_task(_run(target_url, referer, is_stream)))

    async def drain() -> None:
        """Block until every pending download completes OR has been
        idle (no chunks / no yt-dlp output) for ``idle_window``
        seconds. Lets a 1 GB+ video finish even if it takes 5
        minutes, while still abandoning a download that hangs.

        Tunables read from env:
          PAPRIKA_VIDEO_DRAIN_IDLE_S (default 45) -- give up after
            this many seconds of zero progress.
          PAPRIKA_VIDEO_DRAIN_HARD_S (default 1800 = 30 min) -- hard
            wall-clock cap regardless of progress. Safety net only;
            the outer _teardown_session_state wraps drain() in
            another asyncio.wait_for at the same value.
        """
        if not pending:
            return
        idle_window = float(
            os.environ.get("PAPRIKA_VIDEO_DRAIN_IDLE_S", "45.0")
        )
        hard_cap = float(
            os.environ.get("PAPRIKA_VIDEO_DRAIN_HARD_S", "1800.0")
        )
        started = time.time()
        # Tracks the moment last_progress first became empty while
        # tasks were still pending. Reset whenever a fresh chunk /
        # yt-dlp line repopulates last_progress. Used by the
        # "post-download cleanup" branch below to allow on_saved
        # (e.g. the multipart upload of a 1+ GB mp4 to the parent
        # job's /assets) to finish without drain prematurely
        # cancelling it: when _download_direct hits its final
        # ``last_progress.pop(url)`` the task is still alive doing
        # rename + upload work, so we need to give it a grace
        # window before assuming it's hung.
        empty_since: Optional[float] = None
        # Grace period (seconds) between last_progress going empty
        # and drain giving up. Short because the post-download
        # cleanup (rename + on_saved) should complete in a few
        # seconds; if it takes longer something's wrong.
        post_dl_grace = 30.0
        log(
            f"  ... waiting for {len(pending)} video download(s) "
            f"(idle window {idle_window:.0f}s, hard cap {hard_cap:.0f}s)"
        )
        # Poll loop: 2s tick is fine -- per-chunk progress updates
        # last_progress at ~tens of KB/s minimum, so the freshness
        # check is meaningful even on slow links.
        while True:
            still = [t for t in pending if not t.done()]
            if not still:
                log("  ... all video downloads settled")
                break
            now = time.time()
            elapsed = now - started
            if elapsed > hard_cap:
                log(
                    f"  ... drain hard cap reached ({hard_cap:.0f}s); "
                    f"cancelling {len(still)} in-flight download(s)"
                )
                for t in still:
                    t.cancel()
                break
            # Activity check: pick the most-recent last_progress
            # stamp across all tracked URLs. If it's older than
            # idle_window, no chunks have been observed recently --
            # safe to abandon.
            if last_progress:
                empty_since = None  # progress resumed; reset grace
                most_recent = max(last_progress.values())
                idle_for = now - most_recent
                if idle_for > idle_window:
                    log(
                        f"  ... no video download progress for "
                        f"{idle_for:.0f}s (> {idle_window:.0f}s "
                        f"idle window); cancelling {len(still)} "
                        f"in-flight download(s)"
                    )
                    for t in still:
                        t.cancel()
                    break
            else:
                # last_progress empty but tasks still pending.
                # Two scenarios:
                #   * Startup: between maybe_download() spawning
                #     the task and the task's first chunk/log
                #     line setting last_progress. Usually <100ms.
                #   * Post-download cleanup: _download_direct
                #     pops last_progress[url] BEFORE doing
                #     rename + on_saved (= the upload to parent
                #     job's /assets). For a 1+ GB mp4 upload
                #     that's seconds to tens-of-seconds of post-
                #     progress work the task is still doing.
                # Both deserve a short grace window measured from
                # WHEN last_progress went empty -- NOT total drain
                # elapsed (which would cancel cleanly-finishing
                # tasks after a long active download).
                if empty_since is None:
                    empty_since = now
                empty_for = now - empty_since
                if empty_for > post_dl_grace:
                    log(
                        f"  ... {len(still)} task(s) pending without "
                        f"progress markers for {empty_for:.0f}s "
                        f"(> {post_dl_grace:.0f}s post-DL grace); "
                        f"cancelling"
                    )
                    for t in still:
                        t.cancel()
                    break
            await asyncio.sleep(2.0)
        # Surface exceptions / clean up cancelled tasks.
        await asyncio.gather(*pending, return_exceptions=True)

    return maybe_download, drain


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


# ---------------------------------------------------------------------------
# Version identity
#
# Primary source of truth is now a deterministic content hash of the
# bind-mounted source tree (``/app/server`` + ``/app/core``). This was
# previously the VERSION file -- which is checked into the repo as the
# literal string ``"dev"``, so unless the operator manually bumped it
# the hub and every worker reported the same ``"dev"`` and the
# self-update flow could never fire.
#
# Hashing the actual code on disk sidesteps that: edits to any .py file
# change the hash deterministically, and after a worker pulls the hub's
# tarball its source matches the hub's, so the hash naturally matches
# too (= no infinite update loop). Hub and worker compute the hash the
# same way so the two ends compare apples to apples.
# ---------------------------------------------------------------------------

_SOURCE_HASH_ROOTS: tuple[Path, ...] = (Path("/app/server"), Path("/app/core"))


def _compute_source_version() -> str:
    """SHA-256 of every ``.py`` under /app/server and /app/core,
    truncated to 12 hex chars. Empty string if neither dir is present
    or the walk fails entirely (caller falls back to VERSION / env).

    Files are hashed in sorted, path-prefixed order so the result is
    stable across hosts as long as the tree contents match. Symlinks
    and non-files are silently skipped.
    """
    try:
        import hashlib

        h = hashlib.sha256()
        any_file = False
        for root in _SOURCE_HASH_ROOTS:
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*.py")):
                if not p.is_file():
                    continue
                try:
                    rel = p.relative_to("/app").as_posix().encode("utf-8")
                    h.update(rel)
                    h.update(b"\0")
                    h.update(p.read_bytes())
                    any_file = True
                except Exception:
                    continue
        if not any_file:
            return ""
        return h.hexdigest()[:12]
    except Exception:
        return ""


_CACHED_WORKER_VERSION: str | None = None
_CACHED_AT: float = 0.0
# How long to trust the cached source-hash before re-walking the
# tree. Picked at ~10s so the 2026-05-25-incident asymmetry resolves
# inside one heartbeat round: an operator scp + worker-restart will
# have the hub catch up to the new hash before the next handshake.
# Walking ~few-hundred .py files takes ~200ms so we can afford this.
_VERSION_CACHE_TTL_S: float = 10.0


def default_worker_version() -> str:
    """Identify which build this worker is running.

    Resolution order:
      1. SHA-256 hash of the source tree (``server/`` + ``core/``).
         Deterministic across hosts so the handshake comparison
         actually works. Re-walked every ``_VERSION_CACHE_TTL_S`` so a
         live bind-mount edit on the hub host is visible to the still-
         running hub process within seconds (without this, the hub
         keeps reporting its boot-time hash and any worker that picks
         up the new source via rsync+restart looks "newer" than hub,
         triggering the self-update loop -- see the 2026-05-25 post-
         mortem in CHANGELOG).
      2. ``/app/VERSION`` file (legacy; ``scripts/sync-workers.sh``
         used to write a ``${SHA} ${TS}`` stamp here -- still honored).
      3. ``WORKER_VERSION`` env var override.
      4. ``"dev"`` sentinel (kept for the case where none of the
         above can produce a string; treated as "I don't know my
         version" by ``_versions_meaningfully_differ``).

    Cached with a TTL so a bind-mount source change shows up at the
    next handshake instead of requiring a process restart. Fallback
    paths (2-4) only matter when source-hash returns empty, and they
    don't change at runtime, so they keep the historical permanent-
    cache behaviour.
    """
    global _CACHED_WORKER_VERSION, _CACHED_AT
    now = time.monotonic()
    if (
        _CACHED_WORKER_VERSION is not None
        and (now - _CACHED_AT) < _VERSION_CACHE_TTL_S
    ):
        return _CACHED_WORKER_VERSION

    # Try source-hash first. This is the only path whose result can
    # change while the process is alive; refreshing it is the whole
    # point of having a TTL here.
    v = _compute_source_version()
    if v:
        _CACHED_WORKER_VERSION = v
        _CACHED_AT = now
        return v

    # Fallbacks: source tree unavailable (test contexts, missing
    # bind-mount, etc). Once these resolve we keep the answer forever
    # -- they don't change at runtime.
    if _CACHED_WORKER_VERSION is not None:
        # Already resolved via a fallback on a prior call; just refresh
        # the timestamp so we don't thrash the work in case the fall-
        # back paths are themselves I/O-heavy.
        _CACHED_AT = now
        return _CACHED_WORKER_VERSION
    try:
        if VERSION_FILE.exists():
            disk = VERSION_FILE.read_text().strip()
            if disk:
                _CACHED_WORKER_VERSION = disk
                _CACHED_AT = now
                return disk
    except Exception:
        pass
    env = os.environ.get("WORKER_VERSION", "").strip()
    _CACHED_WORKER_VERSION = env or "dev"
    _CACHED_AT = now
    return _CACHED_WORKER_VERSION


# Exit code emitted when the worker self-terminates due to a version
# mismatch (hub or GitHub says we're outdated). Picked to be distinct
# from the conventional 0/1/130/137 so an external supervisor can
# special-case it (e.g. trigger a `docker pull` before restart).
WORKER_EXIT_CODE_VERSION_MISMATCH = 42


def _auto_exit_on_version_mismatch() -> bool:
    """Whether to ``sys.exit(42)`` on a detected version mismatch.

    On by default -- the user explicitly opted into the "warn + auto-exit"
    behavior so the docker restart policy can pull the new image. Set
    ``PAPRIKA_WORKER_AUTO_EXIT_ON_VERSION_MISMATCH=0`` to downgrade to
    warning-only (the worker keeps running but logs a banner on every
    successful registration).
    """
    val = (
        os.environ.get(
            "PAPRIKA_WORKER_AUTO_EXIT_ON_VERSION_MISMATCH",
            "1",
        )
        .strip()
        .lower()
    )
    return val in ("1", "true", "yes", "on")


def _versions_meaningfully_differ(local: str, expected: str) -> bool:
    """Decide whether ``local`` vs ``expected`` should trigger a warning.

    Both must be non-empty. The ``"dev"`` sentinel means "this side
    can't compute a real version" (e.g. source tree absent, hash
    walk failed) -- if BOTH sides report dev, neither knows anything
    actionable; otherwise the side that DOES know wins and a mismatch
    fires normally. Previously *either* dev was a blanket no-op, which
    silently disabled auto-update whenever the (literal ``"dev"``)
    VERSION file was left untouched.
    """
    if not local or not expected:
        return False
    if local == "dev" and expected == "dev":
        return False
    return local != expected


def _print_version_mismatch_banner(
    *,
    local: str,
    expected: str,
    source: str,
) -> None:
    """Emit a hard-to-miss banner so operators notice in busy log output."""
    bar = "!" * 60
    lines = [
        "",
        bar,
        "!! PAPRIKA WORKER VERSION MISMATCH",
        f"!!   reported by:  {source}",
        f"!!   expected:     {expected}",
        f"!!   this worker:  {local}",
        "!!",
        "!! To upgrade:",
        "!!   docker compose pull worker && docker compose up -d worker",
        "!!",
        "!! Disable auto-exit with:",
        "!!   PAPRIKA_WORKER_AUTO_EXIT_ON_VERSION_MISMATCH=0",
        bar,
        "",
    ]
    _logger.info("\n".join(lines))


async def _check_github_release_once(*, log_prefix: str = "[worker]") -> None:
    """Optional GitHub-releases version check, run once at worker startup.

    Disabled unless ``PAPRIKA_GITHUB_REPO=owner/repo`` is set. Useful as
    a fallback when the worker can reach the public internet but cannot
    reach the hub (e.g. a hub-misconfiguration window where you'd rather
    have outdated workers self-restart than silently keep running).

    Best-effort: network failures, rate limits, malformed responses and
    private-repo 404s are all logged at info level and swallowed -- a
    flaky GitHub should not gate the worker from starting up.
    """
    repo = os.environ.get("PAPRIKA_GITHUB_REPO", "").strip()
    if not repo:
        return
    local = default_worker_version()
    if local == "dev":
        # Dev builds (bind-mounted source) intentionally have no
        # comparable version, so skip the noise.
        return

    headers = {
        "User-Agent": "paprika-worker",
        "Accept": "application/vnd.github+json",
    }
    tok = os.environ.get("PAPRIKA_GITHUB_TOKEN", "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        _logger.info(
            f"{log_prefix} GitHub version check skipped ({type(e).__name__}: {e})",
        )
        return

    tag = (data.get("tag_name") or "").strip()
    # Allow either "v1.2.3" or "1.2.3" in the release tag; the VERSION
    # file usually omits the leading 'v'.
    expected = tag[1:] if tag.startswith(("v", "V")) else tag

    if _versions_meaningfully_differ(local=local, expected=expected):
        _print_version_mismatch_banner(
            local=local,
            expected=expected,
            source=f"GitHub releases ({repo})",
        )
        if _auto_exit_on_version_mismatch():
            _logger.info(
                f"{log_prefix} exiting with code "
                f"{WORKER_EXIT_CODE_VERSION_MISMATCH} so the supervisor "
                f"can pull the new image",
            )
            sys.exit(WORKER_EXIT_CODE_VERSION_MISMATCH)


# Paths under /app that get overwritten by a hub-pushed source update.
# Mirror the hub's _WORKER_SOURCE_TREE_PATHS list. Anything outside this
# whitelist in the downloaded tarball is rejected (defence in depth
# against a compromised / misconfigured hub trying to drop files into
# arbitrary places like /etc or /root).
#
# Source tarball is intentionally narrow: server / core / VERSION only.
# Plugins (data/tools/...) ride their own endpoint -- see
# _fetch_worker_plugins_from_hub() below. Keeping the source whitelist
# narrow means a newly-bundled plugin can never break the worker fleet's
# self-update path again. We DO still accept "data" here for backwards
# compat: if an old hub serves the bundled tarball with data/, we strip
# it gracefully rather than refuse.
_WORKER_SOURCE_TARGETS = ("server", "core", "VERSION", "data")
_WORKER_SOURCE_ROOT = Path("/app")
_WORKER_SOURCE_MAX_BYTES = 50 * 1024 * 1024  # 50 MB; current tree is ~few MB.

# Plugin tarball: separate channel, separate validation, separate cadence.
_WORKER_PLUGIN_PATH_PREFIX = "data/tools/"
_WORKER_PLUGIN_ROOT = Path("/app")
_WORKER_PLUGIN_MAX_BYTES = 100 * 1024 * 1024  # 100 MB headroom for future plugins.


def _auto_fetch_source() -> bool:
    """Whether to download + apply the hub's source tarball on version
    mismatch. On by default; set
    ``PAPRIKA_WORKER_AUTO_FETCH_SOURCE=0`` to fall back to the previous
    "log banner + exit(42)" behaviour (useful when the worker's source
    is git-tracked and you don't want auto-overwrites)."""
    val = (
        os.environ.get(
            "PAPRIKA_WORKER_AUTO_FETCH_SOURCE",
            "1",
        )
        .strip()
        .lower()
    )
    return val in ("1", "true", "yes", "on")


def _validate_tar_member(name: str) -> str:
    """Reject obviously hostile tar entry paths.

    Returns the cleaned name (forward-slash, no leading slash) on
    success, raises ValueError otherwise. The accepted shape::

        server/...   |   core/...   |   VERSION
    """
    if not name:
        raise ValueError("empty member name")
    if name.startswith("/"):
        raise ValueError(f"absolute path in tarball: {name!r}")
    parts = name.replace("\\", "/").split("/")
    if any(p in ("", "..") for p in parts):
        raise ValueError(f"path traversal in tarball: {name!r}")
    top = parts[0]
    if top not in _WORKER_SOURCE_TARGETS:
        raise ValueError(f"unexpected top-level in tarball: {top!r}")
    return "/".join(parts)


async def _fetch_and_apply_source_from_hub(
    *,
    hub_http_url: str,
    log_prefix: str = "[worker]",
) -> bool:
    """Download the hub's source tarball and apply it over /app/ in-place.

    Designed to be called immediately before ``sys.exit(42)`` when the
    handshake reports a version mismatch -- the process is about to die
    anyway, so we can overwrite the bind-mounted paths without
    worrying about file-in-use semantics. The docker restart policy
    then boots a fresh process which loads the new code from those
    bind mounts.

    **In-place update, not directory swap.** ``/app/server`` etc. are
    typically docker bind mounts -- you can't ``rename()`` a mountpoint
    (EBUSY / "device or resource busy"). So we walk each tarball entry
    and write it directly to its target path; directories are created
    on demand. After extraction we walk the live tree and delete files
    that weren't in the tarball, so a file removed upstream actually
    disappears locally (avoids stale-module imports after upgrades
    that rename or delete things).

    Safety / validation:
      * Cap tarball size at 50 MB to avoid memory blow-up.
      * Reject absolute paths and ``..`` segments (path traversal).
      * Reject top-level entries outside the agreed whitelist
        (server / core / VERSION).
      * Per-file atomic write: temp file in the same dir + rename.
      * Failure is non-fatal: caller is expected to ``sys.exit(42)``
        regardless, so a botched download just means the next boot
        runs on the same stale code (and the version-mismatch banner
        keeps firing until someone investigates).

    Returns True on a clean, successfully-applied update; False on any
    failure.
    """
    import io
    import os as _os
    import shutil
    import tarfile

    url = f"{hub_http_url.rstrip('/')}/worker-source.tar.gz"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            body = r.content
    except Exception as e:
        _logger.info(
            f"{log_prefix} self-update: tarball fetch failed "
            f"({type(e).__name__}: {e}); will exit anyway",
        )
        return False

    if len(body) > _WORKER_SOURCE_MAX_BYTES:
        _logger.info(
            f"{log_prefix} self-update: tarball too large "
            f"({len(body)} bytes > {_WORKER_SOURCE_MAX_BYTES}); aborting",
        )
        return False

    # Validate + collect entries. We DON'T extract to a staging dir
    # because that would require renaming directories into place at the
    # end, and our targets are bind mounts (un-renameable).
    paths_seen: set[Path] = set()  # absolute paths that we wrote to
    dirs_touched: set[str] = set()  # top-level prefixes we touched
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            members = tar.getmembers()
            for m in members:
                _validate_tar_member(m.name)
            for m in members:
                target = _WORKER_SOURCE_ROOT / m.name
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not (m.isfile() or m.isreg()):
                    # Skip symlinks / devices / hardlinks; we never
                    # publish those in the hub tarball.
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                # Atomic per-file write: extract into a sibling temp
                # file, then rename. fsync optional; bind-mount targets
                # are non-critical durability-wise.
                fobj = tar.extractfile(m)
                if fobj is None:
                    continue
                tmp = target.with_name(target.name + ".paprika-tmp")
                try:
                    with open(tmp, "wb") as out:
                        shutil.copyfileobj(fobj, out)
                    try:
                        _os.replace(tmp, target)
                    except OSError as rename_err:
                        # EBUSY (16) on Linux when the target is itself
                        # a docker bind mount -- typically a single-file
                        # mount like /app/VERSION. The kernel refuses
                        # renames onto a mountpoint, so fall back to
                        # writing the bytes directly into the live file.
                        # Loses per-file atomicity, which is fine here
                        # because the process is about to exit(42) anyway.
                        if getattr(rename_err, "errno", None) == 16:
                            with open(tmp, "rb") as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                        else:
                            raise
                finally:
                    if tmp.exists():
                        try:
                            tmp.unlink()
                        except Exception:
                            pass
                paths_seen.add(target.resolve())
                dirs_touched.add(m.name.split("/", 1)[0])
    except Exception as e:
        _logger.info(
            f"{log_prefix} self-update: tarball extract failed ({type(e).__name__}: {e}); aborting",
        )
        return False

    # Prune files that exist locally under the updated trees but were
    # NOT in the tarball -- those were renamed or deleted upstream.
    # Restrict the walk to the directories we actually touched so a
    # botched / partial tarball can't wipe arbitrary places. Skip
    # VERSION since it's a single file (already overwritten above).
    pruned = 0
    for top in dirs_touched:
        if top == "VERSION":
            continue
        root = _WORKER_SOURCE_ROOT / top
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file() and path.resolve() not in paths_seen:
                    # Don't delete our own temp-file leftovers either
                    # (extension belt-and-suspenders).
                    if path.name.endswith(".paprika-tmp"):
                        continue
                    path.unlink()
                    pruned += 1
            except Exception:
                pass

    _logger.info(
        f"{log_prefix} self-update: applied "
        f"{len(paths_seen)} file(s) across {sorted(dirs_touched)}; "
        f"pruned {pruned} stale file(s)",
    )
    return True


async def _fetch_worker_plugins_from_hub(
    *,
    hub_http_url: str,
    log_prefix: str = "[worker]",
) -> bool:
    """Best-effort sync of the hub's plugin tree into /app/data/tools/.

    Called right after a successful registration handshake so the worker
    has a current copy of every installed plugin before the main job
    loop starts. Plugins live OUTSIDE the source tarball path on
    purpose (see ``_WORKER_SOURCE_TARGETS`` comment) so a newly-bundled
    plugin can never trigger the exit-42 / refuse loop that hit the
    fleet on 2026-05-27.

    Failure is non-fatal: a worker without the latest plugins just falls
    back to whatever it has on disk (or fails the next plugin-using job
    cleanly with PluginNotAvailable). The main code path keeps running.

    Returns True on a successful extract, False otherwise.
    """
    import io
    import os as _os
    import shutil
    import tarfile

    url = f"{hub_http_url.rstrip('/')}/worker-plugins.tar.gz"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.get(url)
            if r.status_code == 404:
                # Hub doesn't advertise plugins yet (older hub) -- silently OK.
                return False
            r.raise_for_status()
            body = r.content
    except Exception as e:
        _logger.info(
            f"{log_prefix} plugin sync: skipped ({type(e).__name__}: {e})",
        )
        return False

    if len(body) > _WORKER_PLUGIN_MAX_BYTES:
        _logger.info(
            f"{log_prefix} plugin sync: tarball too large "
            f"({len(body)} > {_WORKER_PLUGIN_MAX_BYTES}); skipping",
        )
        return False

    target_root = _WORKER_PLUGIN_ROOT
    paths_seen: set[Path] = set()
    extracted = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                name = (member.name or "").replace("\\", "/")
                # Reject absolute paths / traversal / wrong prefix.
                if name.startswith("/") or ".." in name.split("/"):
                    _logger.info(
                        f"{log_prefix} plugin sync: rejecting unsafe path {name!r}",
                    )
                    return False
                if not name.startswith(_WORKER_PLUGIN_PATH_PREFIX):
                    # Only the data/tools/ subtree is accepted here.
                    _logger.info(
                        f"{log_prefix} plugin sync: rejecting out-of-tree path {name!r}",
                    )
                    return False
                target_path = (target_root / name).resolve()
                root_resolved = target_root.resolve()
                if root_resolved not in target_path.parents and target_path != root_resolved:
                    _logger.info(
                        f"{log_prefix} plugin sync: refusing escape via symlink {name!r}",
                    )
                    return False
                target_path.parent.mkdir(parents=True, exist_ok=True)
                fobj = tf.extractfile(member)
                if fobj is None:
                    continue
                content = fobj.read()
                tmp = target_path.with_suffix(target_path.suffix + ".tmp")
                tmp.write_bytes(content)
                _os.replace(tmp, target_path)
                # Preserve executable bit (adapters may shell out via subprocess).
                if member.mode & 0o111:
                    try:
                        _os.chmod(target_path, 0o755)
                    except Exception:
                        pass
                paths_seen.add(target_path)
                extracted += 1
    except Exception as e:
        _logger.info(
            f"{log_prefix} plugin sync: tarball extract failed "
            f"({type(e).__name__}: {e}); aborting",
        )
        return False

    # Prune local plugin files that the hub no longer ships. Limits the
    # walk to data/tools/installed/ so a stray data/tools/catalog.json
    # disappearing doesn't wipe an unrelated subtree.
    pruned = 0
    installed_root = target_root / "data" / "tools" / "installed"
    if installed_root.is_dir():
        try:
            for p in installed_root.rglob("*"):
                if p.is_file() and p not in paths_seen:
                    try:
                        p.unlink()
                        pruned += 1
                    except Exception:
                        pass
            # Sweep up empty plugin dirs left behind.
            for d in sorted(
                (p for p in installed_root.rglob("*") if p.is_dir()),
                key=lambda x: len(x.parts),
                reverse=True,
            ):
                try:
                    d.rmdir()
                except OSError:
                    pass
        except Exception:
            pass

    _logger.info(
        f"{log_prefix} plugin sync: applied {extracted} file(s); "
        f"pruned {pruned} stale file(s)",
    )
    return extracted > 0


class _WorkerIdReassigned(Exception):
    """Raised when the hub instructs this worker to adopt a fresh ID.

    The hub detects clone collisions (same persisted ``worker_id`` arriving
    from a different client IP than the still-alive original) and replies
    via ``HubRegistered.assigned_worker_id``. We catch this in the outer
    reconnect loop in :meth:`WorkerAgent.run` so the next attempt dials
    the link URL with the freshly-persisted ID.
    """


def default_worker_id() -> str:
    """Auto-generate (or recall) a worker ID.

    First checks `~/.paprika/worker_id`. If present, returns its content (so
    the same machine/container always gets the same ID across restarts —
    mount this dir as a Docker volume to persist).

    Otherwise generates `<hostname>-<rand4>` and writes it to the file
    for next time.
    """
    try:
        if WORKER_ID_FILE.exists():
            persisted = WORKER_ID_FILE.read_text().strip()
            if persisted:
                return persisted
    except Exception:
        pass

    host = socket.gethostname()
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    nid = f"{host}-{suffix}"

    try:
        WORKER_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        WORKER_ID_FILE.write_text(nid)
    except Exception:
        pass
    return nid


def hub_http_base(ws_url: str) -> str:
    """Convert ws:// -> http://, wss:// -> https://."""
    parts = urlsplit(ws_url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    new = parts._replace(scheme=scheme)
    return urlunsplit(new).rstrip("/")


def detect_yt_dlp() -> bool:
    return shutil.which("yt-dlp") is not None


def parse_attach(spec: str | None) -> tuple[str | None, int | None]:
    if not spec:
        return None, None
    spec = spec.strip()
    if ":" in spec:
        h, p = spec.rsplit(":", 1)
        return (h or "127.0.0.1"), int(p)
    return "127.0.0.1", int(spec)


def _normalise_extracted_profile(root: Path, *, log=None) -> None:
    """Reshape ``root`` so it matches Chrome's expected "User Data"
    layout (``Default/`` for the profile, ``Local State`` at top).

    Mirrors the hub-side _detect_profile_remap rules; used as a
    defensive second pass in the worker so a cached tarball that
    was uploaded BEFORE the hub gained upload-time normalisation
    still extracts into something Chrome can use.

    Cases handled:

      * ``root/Default/`` exists -> already correct, no-op.
      * ``root/<single_dir>/`` (single top-level dir, no
        Local-State-shaped files at root) -> rename to
        ``root/Default/``.
      * Chrome profile markers (Preferences / Cookies / etc.)
        directly under ``root`` -> wrap them in ``root/Default/``.
      * Anything else -> leave alone (no safe guess).
    """
    PROFILE_MARKERS = {
        "Preferences",
        "Cookies",
        "History",
        "Bookmarks",
    }
    USER_DATA_FILES = {"Local State", "First Run"}
    entries = list(root.iterdir()) if root.exists() else []
    dir_names = {e.name for e in entries if e.is_dir()}
    file_names = {e.name for e in entries if e.is_file()}
    msg_log = (lambda m: log(m)) if log else (lambda m: _logger.info(m))

    # Already correct.
    if "Default" in dir_names and (file_names & USER_DATA_FILES):
        return
    # Single non-Default directory -> rename it to "Default".
    if len(dir_names) == 1 and not file_names and "Default" not in dir_names:
        only = next(iter(dir_names))
        src = root / only
        dst = root / "Default"
        try:
            src.rename(dst)
            msg_log(f"  ... normalised extracted profile: '{only}' -> 'Default'")
        except OSError:
            # Cross-fs or rename failure -- fall back to copy.
            shutil.copytree(src, dst, dirs_exist_ok=True)
            shutil.rmtree(src, ignore_errors=True)
            msg_log(f"  ... normalised extracted profile (copy): '{only}' -> 'Default'")
        return
    # Flat layout: Preferences directly at root -> wrap in Default/.
    if file_names & PROFILE_MARKERS:
        dst = root / "Default"
        dst.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            if entry.name in USER_DATA_FILES:
                continue  # Local State stays at root
            try:
                entry.rename(dst / entry.name)
            except OSError:
                if entry.is_dir():
                    shutil.copytree(entry, dst / entry.name, dirs_exist_ok=True)
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    shutil.copy2(entry, dst / entry.name)
                    entry.unlink(missing_ok=True)
        msg_log("  ... normalised extracted profile: wrapped flat layout in 'Default/'")


class WorkerAgent:
    def __init__(
        self,
        hub_ws_url: str,
        worker_id: str,
        max_concurrent: int = 1,
        labels: dict[str, str] | None = None,
        chrome_host: str | None = None,
        chrome_port: int | None = None,
        worker_secret: str | None = None,
        novnc_url: str | None = None,
        lane_pool=None,  # Optional[LanePool]
    ) -> None:
        # hub_ws_url like ws://hub:8000  (no /...)
        self.hub_ws_url = hub_ws_url.rstrip("/")
        self.hub_http_url = hub_http_base(self.hub_ws_url)
        self.worker_id = worker_id
        self.max_concurrent = max_concurrent
        self.labels = labels or {}
        self.chrome_host = chrome_host
        self.chrome_port = chrome_port
        self.worker_secret = worker_secret
        self.novnc_url = novnc_url
        self.lane_pool = lane_pool

        self._send_lock = asyncio.Lock()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._sem = asyncio.Semaphore(max_concurrent)
        self._in_flight = 0
        # Active session_id -> SessionState. Sessions hold the
        # nodriver browser/tab attached to a Lane between actions.
        self._sessions: dict[str, SessionState] = {}
        # session_ids that the hub has asked us to abort but which
        # weren't yet in ``self._sessions`` when the end-message
        # arrived (e.g. the hub's session_start ``wait_for(future,
        # timeout=60)`` fired before the worker finished setting up).
        # ``_handle_session_start`` checks this set right before
        # sending its ack and self-aborts if the sid is present, so a
        # slow initial navigation can't leak a lane forever.
        self._aborted_sessions: set[str] = set()
        self._http: httpx.AsyncClient | None = None
        # Local cache for operator-uploaded Chrome profiles. Filled by
        # HubProfileSync messages (broadcast on POST /profiles/{name})
        # and on every WS handshake (hub re-syncs its full state).
        # Map: profile_name -> {etag, dir, size_bytes}. The directory
        # holds an extracted "User Data"-shaped tree ready to copy
        # into a lane's user-data-dir slot. Lives under /tmp so it's
        # discarded on container restart (the hub re-sync at the next
        # WS handshake rebuilds it).
        self._profile_cache: dict[str, dict] = {}
        self._profile_cache_lock = asyncio.Lock()
        self._profile_cache_root = Path("/tmp/paprika-profile-cache")
        # Hub-managed extension cache: every enabled extension on the
        # hub gets fetched + extracted here so every lane can pass
        # them via --load-extension on Chrome startup. Mirrors the
        # profile-cache shape: one dir per extension slug, etag for
        # cache-busting on re-upload. The lane code reads this on
        # each Chrome launch (no per-lane state needed).
        self._extension_cache: dict[str, dict] = {}
        self._extension_cache_lock = asyncio.Lock()
        self._extension_cache_root = Path("/tmp/paprika-extensions")
        # Name of the operator-set "default" profile, mirrored from
        # HubProfileSync.is_default. When set, the worker proactively
        # installs this profile into every idle lane so noVNC viewers
        # see the operator's logged-in Chrome on lanes that haven't
        # run a job yet. None = no default set, lanes stay on the
        # baked-in chrome-lane-N user-data-dir.
        self._ambient_default_name: str | None = None

    @property
    def capabilities(self) -> WorkerCapabilities:
        # If this worker runs a lane pool, expose each lane's noVNC URL so
        # the hub's admin UI can link from a live-screenshot tile straight
        # to the matching VNC viewer.
        lane_urls: list[str] = []
        if self.lane_pool is not None:
            lane_urls = [s.novnc_url for s in self.lane_pool.lanes]
        return WorkerCapabilities(
            max_concurrent=self.max_concurrent,
            labels=self.labels,
            chrome_attach_host=self.chrome_host,
            chrome_attach_port=self.chrome_port,
            chrome_version=None,
            has_yt_dlp=detect_yt_dlp(),
            version=default_worker_version(),
            novnc_url=self.novnc_url,
            lane_novnc_urls=lane_urls,
        )

    # ------------------------------------------------------------------ send

    async def _send(self, msg) -> None:
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            await ws.send(encode_msg(msg))

    # --------------------------------------------------------- engine resolver

    async def resolve_engine(
        self,
        slug: str,
        fallback_kind: str = "chat",
    ) -> dict | None:
        """Ask the hub for the full config of an engine.

        Used by ``page.ask`` (chat) and the in-coming ``page.agent``
        engine-registry path so worker code doesn't need to know
        endpoints, models, or API keys directly -- the operator owns
        all of that in the admin UI.

        ``slug`` may be ``"auto"`` to mean "pick the promoted engine of
        ``fallback_kind``"; otherwise it's the literal engine slug.
        Returns the dict the hub's ``/engines/.../resolve`` endpoint
        produced, or None if the resolve failed (caller falls back to
        the legacy AGENT_LLM_URL env path).

        Best-effort: any HTTP error is swallowed + logged so a
        misconfigured engine record can't kill the action.
        """
        slug = (slug or "").strip().lower() or "auto"
        if slug == "auto":
            url = f"{self.hub_http_url.rstrip('/')}/engines/auto/{fallback_kind}/resolve"
        else:
            url = f"{self.hub_http_url.rstrip('/')}/engines/{slug}/resolve"
        body: dict = {}
        if self.worker_secret:
            body["secret"] = self.worker_secret
        try:
            r = await self._http.post(url, json=body, timeout=10.0)
            if r.status_code == 404:
                # No registry entry -- caller falls back to legacy env
                # path. Don't log noisily, this is the common "engine
                # registry not seeded yet" case on fresh deploys.
                return None
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                return None
            return data
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] resolve_engine({slug}) "
                f"failed: {type(e).__name__}: {e}; falling back to "
                f"AGENT_LLM_URL",
            )
            return None

    # ----------------------------------------------------------------- run

    async def run(self) -> None:
        """Reconnect loop. Reconnects with backoff on disconnect."""
        # Optional GitHub-releases version check. Fires before any heavy
        # setup so a stale worker can exit fast and let its supervisor
        # pull a fresh image. Disabled unless PAPRIKA_GITHUB_REPO is set;
        # network failures are swallowed so an offline worker still
        # boots. Behaves identically to the hub-driven check on
        # mismatch (banner + sys.exit(42) when auto-exit is enabled).
        await _check_github_release_once(
            log_prefix=f"[worker {self.worker_id}]",
        )

        # Pre-spawn pool if configured
        if self.lane_pool is not None:
            _logger.info(
                f"[worker {self.worker_id}] starting lane pool "
                f"({len(self.lane_pool.lanes)} lanes)...",
            )
            await self.lane_pool.start_all()

        backoff = 1.0
        async with httpx.AsyncClient(timeout=60.0) as http:
            self._http = http
            while True:
                # Recomputed each iteration: a clone-collision reassignment
                # mutates self.worker_id mid-loop so the next dial uses
                # the freshly-minted id.
                url = f"{self.hub_ws_url}/workers/{self.worker_id}/link"
                try:
                    _logger.info(f"[worker {self.worker_id}] connecting to {url}")
                    async with websockets.connect(url, max_size=2**24) as ws:
                        self._ws = ws
                        await self._handshake_and_loop()
                        backoff = 1.0
                except _WorkerIdReassigned as e:
                    # Fast-path reconnect with the new id; no penalty
                    # backoff since this isn't an error condition.
                    _logger.info(
                        f"[worker] reconnecting immediately with new id={e}",
                    )
                    backoff = 0.5
                except (ConnectionClosed, OSError) as e:
                    _logger.info(
                        f"[worker {self.worker_id}] disconnected ({e}); "
                        f"reconnecting in {backoff:.1f}s",
                    )
                except KeyboardInterrupt:
                    return
                finally:
                    self._ws = None
                    # The hub is the source of truth for session
                    # lifecycle; once our WS to it drops we can no
                    # longer drive any session we currently hold (the
                    # corresponding paprika-runner process has lost
                    # its /sessions/{id} reachability too, since the
                    # runner talks to the hub, not directly to us).
                    # Force-close everything so when we reconnect the
                    # worker is in a clean state -- matches the new
                    # hub's empty session registry.
                    try:
                        n = await self._force_end_all_sessions(
                            "hub WS disconnected",
                        )
                        if n:
                            _logger.info(
                                f"[worker {self.worker_id}] reset lane pool "
                                f"after WS drop: {n} session(s) cleared",
                            )
                    except Exception as e:
                        _logger.info(
                            f"[worker {self.worker_id}] WS-drop cleanup failed: "
                            f"{type(e).__name__}: {e}",
                        )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handshake_and_loop(self) -> None:
        # Send register
        await self._send(
            WorkerRegister(
                worker_id=self.worker_id,
                capabilities=self.capabilities,
                secret=self.worker_secret,
            )
        )
        # Wait for hub's HubRegistered ack
        raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        ack = decode_hub_msg(raw)
        if not isinstance(ack, HubRegistered):
            raise RuntimeError(f"unexpected ack: {ack}")

        # Clone-collision: the hub detected our worker_id is already
        # held by a different host (different client IP, original still
        # alive). It minted us a new ID; persist it, update our state,
        # and bail out of this connection so the outer loop reconnects
        # with the new URL.
        new_id = ack.assigned_worker_id
        if new_id and new_id != self.worker_id:
            _logger.info(
                f"[worker {self.worker_id}] hub reassigned id -> {new_id} "
                f"(clone collision detected); persisting and reconnecting",
            )
            try:
                WORKER_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
                WORKER_ID_FILE.write_text(new_id)
            except Exception as e:
                _logger.info(
                    f"[worker {self.worker_id}] WARNING: could not persist "
                    f"reassigned id to {WORKER_ID_FILE}: {e}. Will still use "
                    f"new id this session, but a restart may collide again.",
                )
            self.worker_id = new_id
            raise _WorkerIdReassigned(new_id)

        # Hub-driven version check. The hub's expected_worker_version is
        # whatever its bind-mounted /app/VERSION reports; if our local
        # build is older, we either log a banner (warn-only mode) or
        # exit with WORKER_EXIT_CODE_VERSION_MISMATCH so the docker
        # restart policy can pick up a freshly-pulled image. Dev builds
        # on either side disable the check (see
        # _versions_meaningfully_differ).
        expected = ack.expected_worker_version
        local_version = default_worker_version()
        if _versions_meaningfully_differ(local=local_version, expected=expected or ""):
            _print_version_mismatch_banner(
                local=local_version,
                expected=expected or "",
                source=f"Hub ({self.hub_http_url})",
            )
            # When auto-fetch is enabled (default), pull the hub's
            # source tarball and apply it before we exit. The docker
            # restart policy then boots the fresh code right away;
            # no registry / docker push / Watchtower needed. When
            # disabled the worker just exits and the operator is
            # expected to update the bind-mounted source themselves
            # (git pull / rsync / whatever).
            if _auto_fetch_source():
                _logger.info(
                    f"[worker {self.worker_id}] self-update: fetching source from hub...",
                )
                applied = await _fetch_and_apply_source_from_hub(
                    hub_http_url=self.hub_http_url,
                    log_prefix=f"[worker {self.worker_id}]",
                )
                if applied:
                    _logger.info(
                        f"[worker {self.worker_id}] self-update: success; "
                        f"restarting to load new code",
                    )
            if _auto_exit_on_version_mismatch():
                _logger.info(
                    f"[worker {self.worker_id}] exiting with code "
                    f"{WORKER_EXIT_CODE_VERSION_MISMATCH} so the supervisor "
                    f"can pick up the new code",
                )
                sys.exit(WORKER_EXIT_CODE_VERSION_MISMATCH)

        _logger.info(
            f"[worker {self.worker_id}] registered. server_time={ack.server_time}"
        )

        # Sync the plugin tree from the hub on every successful register.
        # Best-effort -- failures are logged but never block the worker.
        # See _fetch_worker_plugins_from_hub for the design rationale
        # (the 2026-05-27 fleet outage that prompted splitting source
        # and plugin tarballs into separate endpoints).
        try:
            await _fetch_worker_plugins_from_hub(
                hub_http_url=self.hub_http_url,
                log_prefix=f"[worker {self.worker_id}]",
            )
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] plugin sync crashed unexpectedly "
                f"({type(e).__name__}: {e}); continuing",
            )

        # Defensive lane cleanup: if we have NO sessions registered AND
        # no jobs currently in flight on this worker, lanes marked busy
        # are a stuck reservation from some past failure path (release()
        # missed in a finally, worker code crashed mid-job, etc.). The
        # ``not self._sessions`` check alone wasn't enough because
        # there's a window between lane.acquire() at the top of
        # _run_assigned_job and the session registration that happens
        # later inside fetch()'s on_browser_ready callback -- during
        # that window, freeing the lane caused a future job to acquire
        # the same lane and confuse nodriver into the no-attach path
        # (jobs 6fde9a29166a / others: "could not find a valid chrome
        # browser binary"). ``self._in_flight == 0`` covers that
        # window cleanly because the in_flight counter is incremented
        # at the very top of _run_assigned_job, before lane acquire.
        if self.lane_pool is not None and not self._sessions and self._in_flight == 0:
            stuck = [lane for lane in self.lane_pool.lanes if lane.busy]
            if stuck:
                _logger.info(
                    f"[worker {self.worker_id}] freeing "
                    f"{len(stuck)} stuck busy lane(s) on connect "
                    f"(no sessions registered, in_flight=0): "
                    f"{[lane.lane_idx for lane in stuck]}",
                )
                for lane in stuck:
                    lane.busy = False

        # Announce every session we currently hold so the hub can
        # reconcile its SessionRegistry against worker reality. Covers
        # hub restart (= hub forgot us; we tell it what we have so
        # detached keepalive sessions get rebuilt) AND worker restart
        # (= we have nothing; hub drops stale entries for us). Each
        # session contributes one SessionStateSnapshot with enough
        # fields for the hub to rebuild SessionInfo or 404 it as
        # an orphan.
        try:
            snapshots: list[SessionStateSnapshot] = []
            for sid, sess in list(self._sessions.items()):
                try:
                    lane = sess.lane
                    lane_idx = getattr(lane, "lane_idx", None)
                    if lane_idx is None:
                        continue
                    snapshots.append(
                        SessionStateSnapshot(
                            session_id=sid,
                            lane_idx=int(lane_idx),
                            novnc_url=getattr(lane, "novnc_url", None),
                            job_id=sess.job_id,
                            detached=(not bool(sess.is_fetch_owned)) and bool(sess.job_id),
                            is_fetch_owned=bool(sess.is_fetch_owned),
                        )
                    )
                except Exception as e:
                    _logger.info(
                        f"[worker {self.worker_id}] announce: skipping "
                        f"session {sid} ({type(e).__name__}: {e})",
                    )
            await self._send(WorkerSessionAnnounce(sessions=snapshots))
            _logger.info(
                f"[worker {self.worker_id}] announced {len(snapshots)} session(s) to hub",
            )
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] session announce failed "
                f"({type(e).__name__}: {e}); hub will still see this "
                f"worker but won't know about pre-existing sessions",
            )

        # Pull the hub's current extension set into our local cache.
        # Lanes pass each cached extension dir to Chrome via
        # --load-extension on every restart, so any new extensions
        # uploaded since this worker last started become active on
        # the next lane bounce. Errors are best-effort: a missing
        # extension shouldn't prevent the worker from accepting
        # jobs.
        try:
            await self._sync_extensions_from_hub()
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] extension sync failed "
                f"({type(e).__name__}: {e}); lanes will boot without "
                f"hub-managed extensions until the next reconnect",
            )
        # Push the cache snapshot to every lane so the NEXT Chrome
        # (re)start picks them up via --load-extension. Lanes that
        # are already running with old / no extensions will refresh
        # on their next bounce (watchdog respawn, profile swap, ...).
        try:
            paths = self.loaded_extension_paths()
            if self.lane_pool is not None:
                for lane in self.lane_pool.lanes:
                    try:
                        lane.set_extra_extension_paths(paths)
                    except Exception:
                        pass
                if paths:
                    _logger.info(
                        f"[worker {self.worker_id}] extension cache: "
                        f"pushed {len(paths)} path(s) to "
                        f"{len(self.lane_pool.lanes)} lane(s)",
                    )
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] extension push to lanes "
                f"failed ({type(e).__name__}: {e})",
            )

        # Run heartbeat + idle-tab reaper + message loop concurrently
        hb_task = asyncio.create_task(self._heartbeat_loop())
        reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
        try:
            async for raw in self._ws:
                try:
                    msg = decode_hub_msg(raw)
                except Exception as e:
                    _logger.info(f"[worker {self.worker_id}] decode error: {e}")
                    continue
                await self._handle_hub_message(msg)
        finally:
            hb_task.cancel()
            reaper_task.cancel()

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                try:
                    # Snapshot the profile cache so the hub can show
                    # "ready on X/N workers" in the Profiles tab and
                    # "has profiles [...]" in the Workers tab. Copy
                    # under the lock so a concurrent sync / delete
                    # can't mutate the list mid-snapshot.
                    async with self._profile_cache_lock:
                        cached = [
                            ProfileCacheEntry(
                                name=n,
                                etag=str(e.get("etag") or ""),
                                size_bytes=int(e.get("size_bytes") or 0),
                            )
                            for n, e in self._profile_cache.items()
                        ]
                    await self._send(
                        WorkerHeartbeat(
                            in_flight=self._in_flight,
                            capacity=self.max_concurrent,
                            profiles_cached=cached,
                        )
                    )
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    # ---- idle-lane tab reaper -----------------------------------------
    # Lanes are long-lived: a crawler / codegen-loop job that opened
    # many tabs (gallery-style popup chains, ad windows, agent
    # mis-clicks) leaves them open when it finishes. force_single_tab
    # only runs at the NEXT job's START, so an idle lane sits there
    # with a tab-bar full of leftovers until something new is
    # assigned -- visible + ugly in noVNC, and a slow memory leak.
    #
    # This loop sweeps idle lanes periodically and closes everything
    # back down to one tab. Skips busy lanes entirely, so a running
    # job / live keep_session / operator-driven session is never
    # touched. Uses Chrome's HTTP DevTools endpoints (/json/list +
    # /json/close/{id}) so there's no nodriver attach overhead -- a
    # lane with a single tab costs one cheap HTTP GET per sweep.

    async def _idle_tab_reaper_loop(self) -> None:
        # Configurable cadence; default 60 s is frequent enough that
        # an idle lane looks clean within a minute of a job ending,
        # infrequent enough to be negligible load.
        interval = float(os.environ.get("PAPRIKA_IDLE_TAB_REAP_INTERVAL_S") or 60.0)
        try:
            while True:
                await asyncio.sleep(interval)
                if self.lane_pool is None:
                    continue
                for lane in self.lane_pool.lanes:
                    # Only touch genuinely-idle lanes. busy=True covers
                    # in-flight jobs AND held keep_session sessions, so
                    # we never close a tab someone is actually using.
                    if lane.busy:
                        continue
                    try:
                        await self._reap_lane_tabs(lane)
                    except Exception as e:
                        _logger.info(
                            f"[worker {self.worker_id}] idle tab reap "
                            f"lane #{lane.lane_idx} failed: "
                            f"{type(e).__name__}: {e}",
                        )
        except asyncio.CancelledError:
            return

    async def _reap_lane_tabs(self, lane) -> None:
        """Close every ``page`` tab on an idle lane except the first.

        Pure HTTP DevTools -- no nodriver attach. Chrome exposes:
          GET /json/list            -> [{id, type, url, ...}, ...]
          GET /json/close/{id}      -> closes that target
        ``type`` values other than ``"page"`` (service_worker,
        background_page, iframe, ...) are left alone so extension
        workers from an ambient profile survive.
        """
        import httpx

        base = f"http://localhost:{lane.chrome_port}"
        async with httpx.AsyncClient(timeout=5.0) as cli:
            try:
                r = await cli.get(f"{base}/json/list")
                r.raise_for_status()
                targets = r.json()
            except Exception:
                # Chrome not reachable (restarting / mid-profile-swap)
                # -- skip this lane this round.
                return
            pages = [t for t in targets if t.get("type") == "page"]
            if len(pages) <= 1:
                return
            # Keep the first page target (usually the lane's original
            # about:blank from startup); close the rest.
            keep_id = pages[0].get("id")
            closed = 0
            for t in pages[1:]:
                tid = t.get("id")
                if not tid or tid == keep_id:
                    continue
                try:
                    await cli.get(f"{base}/json/close/{tid}")
                    closed += 1
                except Exception:
                    pass
            if closed:
                _logger.info(
                    f"[worker {self.worker_id}] idle tab reap "
                    f"lane #{lane.lane_idx}: closed {closed} leftover "
                    f"tab(s) (kept 1)",
                )

    async def _fetch_cookies_txt_for(self, target_url, state, log=None):
        """Fetch a Netscape cookies.txt for ``target_url``'s host from
        the hub's /hosts registry and write it to a temp file.

        Returns the temp Path on success, ``None`` when there are no
        cookies / the hub is unreachable / anything else goes wrong
        (caller then runs yt-dlp unauthenticated). The hub base URL
        is derived from ``state.asset_upload_base`` (which the hub
        set to ``http://<hub>/jobs/{id}/assets`` at session start).
        """
        import re as _re
        import tempfile
        from urllib.parse import urlparse

        import httpx

        try:
            host = (urlparse(target_url).hostname or "").lower()
        except Exception:
            host = ""
        if not host:
            return None
        if host.startswith("www."):
            host = host[4:]
        # Derive hub base from the asset upload base
        # (http://hub:8000/jobs/{id}/assets -> http://hub:8000).
        base = getattr(state, "asset_upload_base", None) or ""
        m = _re.match(r"^(https?://[^/]+)", base)
        if not m:
            return None
        hub_base = m.group(1)
        url = f"{hub_base}/hosts/{host}/cookies.txt"
        try:
            async with httpx.AsyncClient(timeout=10.0) as cli:
                r = await cli.get(url)
                if r.status_code != 200:
                    return None
                body = r.text
        except Exception as e:
            if log:
                log(f"[download_video] cookies fetch failed for {host}: {e}")
            return None
        if not body or not body.strip() or body.strip() == "# Netscape HTTP Cookie File":
            return None
        try:
            fd, path = tempfile.mkstemp(prefix="ytdlp_cookies_", suffix=".txt")
            import os as _os

            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            return Path(path)
        except Exception:
            return None

    async def _handle_hub_message(self, msg) -> None:
        if isinstance(msg, HubAssignJob):
            asyncio.create_task(self._run_assigned_job(msg))
            return
        if isinstance(msg, HubScreenshotRequest):
            # Don't block the recv loop on ffmpeg; fan out to a task.
            asyncio.create_task(self._handle_screenshot(msg))
            return
        if isinstance(msg, HubSessionStart):
            asyncio.create_task(self._handle_session_start(msg))
            return
        if isinstance(msg, HubSessionAction):
            # One task per action; the per-session Lock serialises them
            # so concurrent ops on the same session can't interleave.
            asyncio.create_task(self._handle_session_action(msg))
            return
        if isinstance(msg, HubSessionEnd):
            asyncio.create_task(self._handle_session_end(msg))
            return
        if isinstance(msg, HubSessionAgent):
            asyncio.create_task(self._handle_session_agent(msg))
            return
        if isinstance(msg, HubProfileSync):
            # Prefetch into the local cache without blocking the WS
            # loop. Same async pattern as HubAssignJob; failures are
            # logged but never propagate (the on-demand fetch path
            # is the fallback).
            asyncio.create_task(self._handle_profile_sync(msg))
            return
        if isinstance(msg, HubProfileDelete):
            asyncio.create_task(self._handle_profile_delete(msg))
            return
        # HubCancelJob: not yet implemented (Phase 3.x)
        # HubPing: respond via heartbeat already

    async def _handle_screenshot(self, req: HubScreenshotRequest) -> None:
        """Take a screenshot of the requested lane and reply over the WS."""
        import base64

        reply = WorkerScreenshotReply(
            req_id=req.req_id,
            lane_idx=req.lane_idx,
        )
        try:
            if self.lane_pool is None:
                raise RuntimeError("worker has no lane pool")
            if not (0 <= req.lane_idx < len(self.lane_pool.lanes)):
                raise RuntimeError(
                    f"lane_idx {req.lane_idx} out of range (have {len(self.lane_pool.lanes)} lanes)"
                )
            lane = self.lane_pool.lanes[req.lane_idx]
            jpeg = await lane.screenshot(
                max_width=req.max_width,
                quality=req.quality,
            )
            reply.jpeg_b64 = base64.b64encode(jpeg).decode("ascii")
        except Exception as e:
            reply.error = str(e)
        try:
            await self._send(reply)
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] failed to send screenshot reply: {e}"
            )

    # --------------------------------------------------- session handlers

    async def _handle_session_start(self, msg: HubSessionStart) -> None:
        """Reserve a lane, attach nodriver, install tab-killer, ack hub."""
        import nodriver as uc

        sid = msg.session_id
        ack = WorkerSessionStartAck(session_id=sid)
        try:
            if self.lane_pool is None:
                raise RuntimeError("worker has no lane pool")
            if sid in self._sessions:
                raise RuntimeError(f"session {sid} already exists on this worker")

            lane = await self.lane_pool.acquire(lane_hint=msg.lane_hint)
            if lane is None:
                raise RuntimeError(f"no free lane (lane_hint={msg.lane_hint})")

            # Operator-uploaded Chrome profile, if any. Same shape as
            # the /jobs path: download tarball, extract, re-point the
            # lane's user-data-dir, restart Chrome. Failure is
            # non-fatal -- the session still starts, just on the
            # lane's default profile. Restored on session end via
            # _teardown_session_state.
            profile_url = getattr(msg, "profile_url", None)
            if profile_url:
                pdir = await self._get_profile_for_job(
                    profile_url=profile_url,
                    profile_name=getattr(msg, "profile_name", None),
                    profile_etag=getattr(msg, "profile_etag", None),
                    scratch_key=sid,
                )
                if pdir is not None:
                    try:
                        await lane.use_profile(pdir)
                        _logger.info(
                            f"[session {sid}] operator profile installed "
                            f"into lane #{lane.lane_idx}",
                        )
                    except Exception as e:
                        _logger.info(
                            f"[session {sid}] lane.use_profile failed: "
                            f"{type(e).__name__}: {e} -- continuing "
                            f"with lane default",
                        )

            assets_dir = Path(tempfile.mkdtemp(prefix=f"paprika-ses-{sid}-"))
            (assets_dir / "assets").mkdir(parents=True, exist_ok=True)

            # Derive the parent job_id from the asset_upload_base URL
            # so dispatcher actions (fetch_refresh, etc.) that need it
            # can read state.job_id directly without re-parsing the
            # URL every time. Shape:
            #   http://hub:8000/jobs/{job_id}/assets
            # Split on "/jobs/" and take the segment up to "/assets".
            derived_job_id: str | None = None
            if msg.asset_upload_base:
                try:
                    after_jobs = msg.asset_upload_base.split("/jobs/", 1)[1]
                    derived_job_id = after_jobs.split("/", 1)[0] or None
                except Exception:
                    derived_job_id = None

            state = SessionState(
                session_id=sid,
                lane=lane,
                assets_dir=assets_dir / "assets",
                # Inherits from HubSessionStart -- the hub fills this in
                # when the session belongs to a parent job (codegen-loop
                # mode) so we know where to upload page.capture() output.
                asset_upload_base=msg.asset_upload_base,
                job_id=derived_job_id,
            )
            self._sessions[sid] = state

            # Attach nodriver in the same pattern as agent_runner.
            chrome = await uc.start(
                host="localhost",
                port=lane.chrome_port,
                browser_executable_path=sys.executable,
            )
            try:
                from urllib.parse import urlparse

                parsed = urlparse(chrome.websocket_url)
                if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
                    new_netloc = f"localhost:{lane.chrome_port}"
                    chrome.websocket_url = chrome.websocket_url.replace(
                        parsed.netloc,
                        new_netloc,
                        1,
                    )
            except Exception:
                pass
            state.browser = chrome

            # Aggressively reduce the browser to one tab so the next
            # navigation starts from a clean state. Done via CDP
            # Target.getTargets / closeTarget rather than nodriver's
            # cached tabs list, which can lag CDP state.
            try:
                await browser_ops.force_single_tab(
                    chrome,
                    log=lambda s: _logger.info(
                        f"[session {sid}] {s}",
                    ),
                )
            except Exception as e:
                _logger.info(
                    f"[worker {self.worker_id}] session {sid} "
                    f"force_single_tab at start failed: "
                    f"{type(e).__name__}: {e}",
                )

            target_url = msg.initial_url or "about:blank"
            # Grab the existing tab without navigating yet (initial url
            # is "about:blank" -- no-op). We need the tab in hand so we
            # can attach CDP listeners BEFORE the first real navigation,
            # otherwise the initial page's images/videos slip past us.
            state.tab = await chrome.get("about:blank", new_tab=False)

            # NOTE: install_tab_killer was removed in the multi-tab
            # refactor (Phase 1). Previously this enforced "1 lane =
            # 1 tab" by CDP-killing every new ``Target`` Chrome
            # created. We now let new tabs open freely so the
            # upcoming operator API (Phase 2: ``page.new_tab()``,
            # ``session.pages``, popup events) can address them as
            # first-class objects. JS-level same-origin
            # ``target="_blank"`` rewriting (``TAB_HOOKS_ENABLED``)
            # remains in place for one more release cycle so most
            # navigation still collapses into the main tab while the
            # operator API is being designed.
            # Passively persist every image/video/audio response the
            # browser fetches while this session is alive. Each saved
            # file is immediately uploaded to the parent job's /assets
            # so the gallery fills as the script crawls (mirrors what
            # fetch mode does for one-shot jobs).
            async def _on_session_asset_saved(path: Path, info: dict) -> None:
                await self._upload_one_session_asset(
                    state,
                    path,
                    mime=info.get("mime"),
                    source_url=info.get("url"),
                    page_url=info.get("document_url"),
                )

            # Session-wide video downloader. Created here (BEFORE the
            # CDP listener is hooked) so the listener can call its
            # ``maybe_download`` the moment an .m3u8 / .mpd appears
            # in network traffic -- yt-dlp starts merging segments
            # without waiting for the SDK to call page.download_video().
            # The same closures are also reused by _handle_session_agent
            # and the explicit download_video action handler, so all
            # three paths share one downloaded-URLs set (= no double-
            # download when an SDK call follows the passive trigger).
            async def _on_session_video_saved_open(path: Path, info: dict) -> None:
                try:
                    await self._upload_one_session_asset(
                        state,
                        path,
                        mime=info.get("mime"),
                        source_url=info.get("url"),
                        page_url=info.get("document_url"),
                    )
                except Exception as e:
                    _logger.info(
                        f"[session {sid}] session-open video upload "
                        f"failed: {type(e).__name__}: {e}"
                    )

            # job_log routes "operator-visible" downloader lines
            # (detection, progress, save / fail) to the parent
            # job's Live panel via WorkerJobLog -- on top of the
            # usual worker-stderr logging. Only wires up when the
            # session is bound to a parent job (SDK sets this via
            # parent_job_id, set by the codegen-loop runner's
            # PAPRIKA_JOB_ID env var).
            def _maybe_send_job_log(line: str) -> None:
                pjid = getattr(state, "job_id", None)
                if not pjid:
                    return
                try:
                    asyncio.ensure_future(
                        self._send(WorkerJobLog(job_id=pjid, line=line))
                    )
                except Exception:
                    pass

            maybe_download_video_session, drain_video_session = _make_video_downloader(
                assets_dir=state.assets_dir,
                min_asset_size=int(
                    os.environ.get("MIN_ASSET_SIZE_BYTES", "0") or 0
                ),
                on_saved=_on_session_video_saved_open,
                log=lambda s: _logger.info(f"[session {sid}] {s}"),
                job_id_for_logs=f"session-{sid}",
                job_log=_maybe_send_job_log,
                # Top-level page URL referer fallback for cross-origin
                # iframe player streams (e.g. supjav's supremejav iframe).
                # state.last_response tracks the most recent top-level
                # document load, so its url is the page the operator is
                # actually on -- the referer the CDN expects.
                page_url_provider=lambda: (
                    (state.last_response or {}).get("url")
                    if isinstance(getattr(state, "last_response", None), dict)
                    else None
                ),
            )
            state.video_downloader = maybe_download_video_session
            state.video_drainer = drain_video_session

            # Passive "last main-document response" tracker so
            # page.last_response() always reflects whatever the most
            # recent top-level navigation returned -- including
            # click-induced ones where _capture_nav_response can't
            # bracket the call.
            def _set_last_response(info: dict) -> None:
                state.last_response = info
            try:
                await browser_ops.install_last_response_tracker(
                    state.tab,
                    on_response_captured=_set_last_response,
                    log=lambda s: _logger.info(f"[session {sid}] {s}"),
                )
            except Exception as e:
                _logger.info(
                    f"[session {sid}] last_response tracker install failed "
                    f"(non-fatal): {type(e).__name__}: {e}"
                )

            await browser_ops.install_session_asset_capture(
                state.tab,
                state.assets_dir,
                on_saved=_on_session_asset_saved,
                log=lambda s: _logger.info(f"[session {sid}] {s}"),
                # Share the session's dedup set so URLs already captured
                # on an earlier page aren't saved again with _1/_2/...
                # suffixes when the script revisits them.
                seen_urls=state.seen_asset_urls,
                # Hub-managed min-size filter (Settings → "Asset
                # capture"). 0 = save everything; otherwise the
                # passive listener drops anything below the
                # threshold without writing or uploading it.
                min_asset_size_bytes=getattr(msg, "min_asset_size_bytes", 0) or 0,
                # Feed the session's network_log list so the Live
                # panel "Network" tab can display all observed media
                # traffic and let the operator cherry-pick assets.
                network_log=state.network_log,
                # Auto-fire yt-dlp on every .m3u8 / .mpd response so
                # HLS/DASH streams are merged into a playable mp4
                # without an explicit SDK call. Idempotent on URL.
                on_stream_detected=maybe_download_video_session,
                # iframe + nested-iframe deep network trace. ALWAYS on
                # for a capturing session: the asset dir is always
                # present here, and many video sites (supjav, DMM
                # litevideo, embedded players) stream HLS *inside a
                # cross-origin iframe*. Without deep-trace the parent
                # CDP target never sees those .m3u8 requests, so
                # on_stream_detected never fires and a video that
                # visibly plays during the session is captured as
                # zero bytes (job 8a10c9289262: video played, nothing
                # downloaded, because it was submitted download_video=
                # False so deep-trace stayed deferred and the script's
                # goal never called page.download_video()).
                #
                # Mirrors the same always-on decision in core/fetcher
                # (fetch path). The old download_video gate + the
                # page.download_video() late-enable hook left a hole:
                # a session that merely *plays* a video (rather than
                # explicitly downloading it) was invisible. The CDP
                # attach overhead is acceptable for a video-archiving
                # tool where any played stream should be preserved.
                enable_iframe_deep_trace=True,
            )
            # Per-host cookies auto-injected by the hub. Install them via
            # CDP Network.setCookies BEFORE the initial navigation so the
            # very first request already carries the session. Skipping
            # silently when the list is empty/None keeps non-cookie hosts
            # on the existing zero-config path.
            if msg.cookies:
                try:
                    from nodriver import cdp as _cdp

                    from core.fetcher import _to_cdp_cookie_params

                    params = _to_cdp_cookie_params(msg.cookies)
                    if params:
                        await state.tab.send(_cdp.network.set_cookies(cookies=params))
                        _logger.info(
                            f"[worker {self.worker_id}] session {sid} "
                            f"installed {len(params)} cookie(s) before "
                            f"navigation "
                            f"({len(msg.cookies) - len(params)} dropped)",
                        )
                    else:
                        _logger.info(
                            f"[worker {self.worker_id}] session {sid} "
                            f"all cookies dropped as invalid",
                        )
                except Exception as e:
                    # Best-effort: the script can still set_cookies()
                    # manually if the auto-injection failed (e.g. one
                    # of the cookies had an unexpected field).
                    _logger.info(
                        f"[worker {self.worker_id}] session {sid} cookie "
                        f"injection failed: {type(e).__name__}: {e}",
                    )

            # Now do the actual navigation -- hooks are armed. Use the
            # low-level cdp.page.navigate so the tab keeps the same CDP
            # session id; ``state.tab.get()`` (the nodriver high-level
            # helper) can re-attach to a fresh target, which detaches
            # the listeners we just installed and makes
            # network.get_response_body() fail with -32000.
            if target_url != "about:blank":
                try:
                    from nodriver import cdp as _cdp

                    await state.tab.send(_cdp.page.navigate(target_url))
                except Exception:
                    # Best-effort: the script can page.goto() to retry.
                    pass

            ack.lane_idx = lane.lane_idx
            ack.novnc_url = lane.novnc_url
            _logger.info(
                f"[worker {self.worker_id}] session {sid} -> lane #{lane.lane_idx} at {target_url}",
            )
        except Exception as e:
            # Roll back partial state on error.
            ack.error = f"{type(e).__name__}: {e}"
            state = self._sessions.pop(sid, None)
            if state is not None:
                if state.browser is not None:
                    try:
                        await state.browser.stop()
                    except Exception:
                        pass
                if state.lane is not None and self.lane_pool is not None:
                    self.lane_pool.release(state.lane)
            _logger.info(
                f"[worker {self.worker_id}] session {sid} start failed: {ack.error}",
            )
        # ---- abort checkpoint -------------------------------------
        # The hub's start_session has a bounded wait_for() (default
        # 60s). If we took longer than that, the hub will have
        # given up + likely sent a HubSessionEnd to release the
        # lane. That end message couldn't find a session because we
        # were still constructing this one, so _handle_session_end
        # parked the sid in self._aborted_sessions. Notice it now,
        # before we return from session_start, and tear ourselves
        # down so the lane goes back to the pool. Without this, a
        # slow initial navigation leaks a lane every time.
        if sid in self._aborted_sessions:
            self._aborted_sessions.discard(sid)
            aborted = self._sessions.pop(sid, None)
            if aborted is not None:
                _logger.info(
                    f"[worker {self.worker_id}] session {sid} aborted "
                    f"by hub during start; releasing lane",
                )
                await self._teardown_session_state(sid, aborted)
            if not ack.error:
                ack.error = "session aborted by hub during start"
        try:
            await self._send(ack)
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] failed to send session_start_ack: {e}"
            )

    async def _handle_session_action(self, msg: HubSessionAction) -> None:
        """Dispatch one action against a bound session via browser_ops."""
        sid = msg.session_id
        rid = msg.request_id
        reply = WorkerSessionActionResult(
            session_id=sid,
            request_id=rid,
            status="OK",
        )
        state = self._sessions.get(sid)
        if state is None:
            reply.status = f"ERR: session {sid} not found on this worker"
            try:
                await self._send(reply)
            except Exception:
                pass
            return

        t0 = time.time()

        # Quiet log function for the session path -- per-action log
        # lines just go to stderr for now. Sessions don't have an
        # operator log stream like jobs do.
        def _slog(line: str) -> None:
            _logger.info(f"[session {sid}] {line}")

        # Fetch-owned sessions are read-only: the fetch loop is driving
        # the tab and a write action mid-fetch would race (clicks would
        # collide with the scroll loop, navigates would derail asset
        # capture). Reject early with a clear error so the UI shows
        # something sensible instead of a CDP-level explosion.
        _READ_ONLY_KINDS_FOR_FETCH = {
            "outline",
            "state",
            "screenshot",
            "visited",
            "get_cookies",
            "links",
            "exists",
            "ask",
            # Network log: read-only list populated by the fetcher's
            # own CDP handlers. Safe to read mid-fetch.
            "network",
            # Last main-document HTTP response. Updated by a passive
            # listener so reading it never races the fetch loop.
            "last_response",
            # Tab management is read-only-safe: listing / switching
            # default doesn't drive the fetch loop's tab. Creating /
            # closing tabs is still disallowed during fetch (the
            # passive listener is bound to a specific tab and would
            # de-sync). Hence only "pages" / "switch_page" included.
            "pages",
            "switch_page",
            # Page zoom is a viewing aid (visual magnification only --
            # Emulation.setPageScaleFactor). It doesn't navigate or
            # touch the DOM, so it's safe to apply while a fetch drives
            # the tab (operator zooms in to watch / inspect).
            "zoom",
        }
        # Session-level kinds (operate on the session as a whole, not
        # on any specific tab). They run under ``state.lock`` rather
        # than the per-page lock.
        _SESSION_LEVEL_KINDS = {
            "pages",
            "new_page",
            "close_page",
            "switch_page",
        }
        action = msg.action or {}
        kind = action.get("kind") or ""
        if state.is_fetch_owned and kind not in _READ_ONLY_KINDS_FOR_FETCH:
            reply.status = (
                f"ERR: session {sid} is owned by a running fetch job; "
                f"only read-only actions are allowed "
                f"({sorted(_READ_ONLY_KINDS_FOR_FETCH)}). "
                f"Got: {kind!r}"
            )
            reply.elapsed_ms = int((time.time() - t0) * 1000)
            try:
                await self._send(reply)
            except Exception:
                pass
            return

        # Pick the target tab. ``action.get('page_id')`` overrides the
        # session's default; falls back to default_page_id otherwise.
        # Session-level kinds use state.lock; per-tab kinds use the
        # page's own lock so two pages in the same session can run
        # actions in parallel.
        target_pid = action.get("page_id") or state.default_page_id
        if kind in _SESSION_LEVEL_KINDS:
            chosen_lock = state.lock
        else:
            chosen_lock = state.page_locks.get(target_pid) or state.lock

        async with chosen_lock:
            try:
                # For per-tab kinds, look up the target Tab. For
                # session-level kinds, fall back to default (used only
                # for the "snapshot URL" bookkeeping below).
                tab = state.pages.get(target_pid) if target_pid else None
                if tab is None and kind not in _SESSION_LEVEL_KINDS:
                    reply.status = (
                        f"ERR: unknown page_id {target_pid!r} (known: {sorted(state.pages.keys())})"
                    )
                    reply.elapsed_ms = int((time.time() - t0) * 1000)
                    try:
                        await self._send(reply)
                    except Exception:
                        pass
                    return

                # Snapshot the current URL into visited_urls so the
                # visited=true marker works for the next outline. Only
                # meaningful for per-tab kinds (tab exists).
                if tab is not None:
                    try:
                        cur = await tab.evaluate("document.location.href")
                    except Exception:
                        cur = ""
                    if cur:
                        state.note_url(browser_ops.canon_url(cur))

                if kind == "outline":
                    out = await browser_ops.outline(
                        tab,
                        visited_urls=state.visited_urls,
                    )
                    reply.result = out
                elif kind == "state":
                    try:
                        title = await tab.evaluate("document.title")
                    except Exception:
                        title = ""
                    reply.result = {
                        "url": cur,
                        "title": title or "",
                        "lane_idx": state.lane.lane_idx,
                        "visited_count": len(state.visited_urls),
                    }
                elif kind == "screenshot":
                    from nodriver import cdp

                    png_b64 = await tab.send(
                        cdp.page.capture_screenshot(format_="png"),
                    )
                    reply.result = png_b64
                    # Optional: also publish this frame to the parent
                    # job's gallery so it shows in the Live tab's
                    # "Screenshot" sub-tab (filter = name startswith
                    # "screenshot-"). Triggered only when the caller
                    # passed a ``label`` AND the session is bound to a
                    # parent job (asset_upload_base set). Keeps the
                    # plain page.screenshot() byte-return path untouched
                    # for callers that don't want gallery noise.
                    label = action.get("label")
                    if label and state.asset_upload_base is not None:
                        try:
                            import base64 as _b64

                            ts = time.strftime("%Y%m%d-%H%M%S")
                            # millisecond suffix so a 0.5s-interval burst
                            # (e.g. video frame capture) doesn't collide
                            # within the same second.
                            ms = int((time.time() % 1) * 1000)
                            safe = browser_ops.safe_label(str(label)) or "shot"
                            name = f"screenshot-{ts}-{ms:03d}-{safe}.png"
                            shots_dir = state.assets_dir / "screenshots"
                            shots_dir.mkdir(parents=True, exist_ok=True)
                            png_path = shots_dir / name
                            png_path.write_bytes(_b64.b64decode(png_b64))
                            await self._upload_one_session_asset(
                                state,
                                png_path,
                                mime="image/png",
                                asset_name=name,
                            )
                        except Exception as e:
                            _slog(f"screenshot gallery upload failed: {e}")
                elif kind == "visited":
                    reply.result = list(state.visited_urls_ordered)
                elif kind == "last_response":
                    # Return the most recent main-document HTTP response
                    # observed on this session, regardless of whether
                    # the navigation was triggered by goto / back / forward
                    # / reload / history_first or a click that happened
                    # to navigate (form submit, anchor click, JS
                    # location.href = ...). state.last_response is
                    # updated by the passive tracker installed at
                    # session_start (browser_ops.install_last_response_tracker).
                    # None when no document response has been observed yet
                    # (session opened with initial_url=about:blank etc.).
                    reply.result = state.last_response
                elif kind == "network":
                    # Return the session's network traffic log for the
                    # Live panel "Network" tab. Each entry:
                    #   {url, mime, size, saved, document_url, timestamp}
                    # The ``since`` parameter lets the client do
                    # incremental polling (only new entries).
                    since_ts = float(action.get("since", 0) or 0)
                    entries = state.network_log
                    if since_ts:
                        entries = [e for e in entries if e.get("timestamp", 0) > since_ts]
                    reply.result = {
                        "count": len(state.network_log),
                        "entries": entries,
                    }
                elif kind == "links":
                    # Return every <a href> on the current page resolved
                    # to absolute URLs, deduped, with the visible text
                    # truncated to ~120 chars. We use the live DOM
                    # ``document.links`` collection (HTMLCollection of all
                    # anchors with an href, EXCLUDING <area> ping URLs
                    # and bare <a name>). Properties:
                    #   * a.href is already resolved against <base> and
                    #     document URL -- no manual urljoin needed.
                    #   * skipped: javascript: / mailto: / tel: / blob:
                    #     / data: -- they're not navigatable in the same
                    #     sense and clutter the list. Operator opt-in
                    #     can be added later if there's demand.
                    # The worker can be on a page that does not yet
                    # have any anchors (e.g. captcha page). Empty list
                    # is a valid result.
                    # nodriver's ``tab.evaluate`` auto-converts only
                    # scalar return values (string / int / bool); for
                    # arrays/objects we need to JSON.stringify on the
                    # JS side and json.loads here. Same pattern used by
                    # browser_ops.outline(). Source of truth for the JS
                    # is module-scope ``_LINKS_EXTRACT_JS`` so the
                    # session-end dump path uses the same extraction.
                    raw_str = None
                    try:
                        raw_str = await tab.evaluate(_LINKS_EXTRACT_JS)
                    except Exception as e:
                        reply.status = f"ERR: links eval failed: {e}"
                    items: list = []
                    if isinstance(raw_str, str) and raw_str:
                        import json as _json

                        try:
                            parsed = _json.loads(raw_str)
                            if isinstance(parsed, list):
                                items = parsed
                        except Exception:
                            pass
                    elif isinstance(raw_str, list):
                        # Some nodriver versions auto-decode JSON;
                        # accept that path too.
                        items = raw_str
                    reply.result = {
                        "current_url": cur or "",
                        "count": len(items),
                        "links": items,
                    }
                elif kind == "exists":
                    # CSS selector exists check -- cheap, deterministic.
                    # Used by macros / scripts for if/else branching
                    # without involving an LLM.
                    selector = action.get("selector") or ""
                    status, found = await browser_ops.exists(
                        tab,
                        selector,
                        _slog,
                    )
                    reply.status = status
                    reply.result = bool(found)
                elif kind == "evaluate":
                    # Arbitrary JS evaluation in the tab's page context.
                    # The keystone the SDK builds Locator getters /
                    # wait_for_selector / hover / select_option on top of
                    # (all client-side JS, so only this one worker action
                    # is needed).
                    #
                    # nodriver does NOT return arrays/objects by value --
                    # they come back as RemoteObject descriptors
                    # ([{type,value}, ...] / [[key,{value}], ...]). To give
                    # callers clean JSON values (page.evaluate("[1,2,3]")
                    # -> [1,2,3], not descriptors) we wrap the expression so
                    # the browser does JSON.stringify(await (expr)) -- a
                    # string always crosses by value -- and json.loads it
                    # here. Same pattern as the ``links`` handler above.
                    # Promises are awaited unconditionally (await on a
                    # non-promise is a harmless no-op), so await_promise is
                    # implied. DOM nodes / functions stringify to {} /
                    # undefined rather than erroring.
                    import json as _json

                    expr = action.get("expression") or ""
                    if not expr:
                        reply.status = "ERR: evaluate failed: empty expression"
                    else:
                        wrapped = "(async()=>{return JSON.stringify(await (" + expr + "));})()"
                        try:
                            raw = await tab.evaluate(wrapped, await_promise=True)
                            if isinstance(raw, str):
                                try:
                                    reply.result = _json.loads(raw)
                                except Exception:
                                    reply.result = raw
                            else:
                                # undefined / non-serialisable -> null
                                reply.result = None
                        except Exception as e:
                            reply.status = f"ERR: evaluate failed: {browser_ops.short_error(e)}"
                elif kind == "set_input_files":
                    # File upload: the client base64-encodes the file
                    # bytes, we materialise them in a worker tempdir and
                    # point the <input type=file> at the paths via CDP
                    # DOM.setFileInputFiles (a JS expression can't set a
                    # file input -- browsers forbid it). Chrome reads the
                    # paths at form-submit time, so the temp files must
                    # outlive this call; they're cleaned with the lane.
                    import base64 as _b64

                    from nodriver import cdp as _cdp

                    selector = action.get("selector") or ""
                    files = action.get("files") or []
                    if not selector:
                        reply.status = "ERR: set_input_files: empty selector"
                    else:
                        try:
                            updir = tempfile.mkdtemp(prefix="paprika_upload_")
                            paths: list[str] = []
                            for f in files:
                                name = (
                                    os.path.basename(f.get("name") or "upload.bin") or "upload.bin"
                                )
                                data = _b64.b64decode(f.get("content_b64") or "")
                                p = os.path.join(updir, name)
                                with open(p, "wb") as fh:
                                    fh.write(data)
                                paths.append(p)
                            doc = await tab.send(_cdp.dom.get_document())
                            node_id = await tab.send(
                                _cdp.dom.query_selector(
                                    node_id=doc.node_id,
                                    selector=selector,
                                )
                            )
                            if not node_id:
                                reply.status = "NO_MATCH"
                            else:
                                await tab.send(
                                    _cdp.dom.set_file_input_files(
                                        files=paths,
                                        node_id=node_id,
                                    )
                                )
                                reply.result = {
                                    "files": [os.path.basename(p) for p in paths],
                                    "count": len(paths),
                                }
                        except Exception as e:
                            reply.status = (
                                f"ERR: set_input_files failed: {browser_ops.short_error(e)}"
                            )
                elif kind == "ask":
                    # LLM-based yes/no question. Sends current outline
                    # + URL + the question to the configured text LLM
                    # (Qwen 2.5-VL via AGENT_LLM_URL) with a strict
                    # "answer yes or no" prompt. Parses the response
                    # leniently; anything unparseable defaults to
                    # False (the safe / non-acting branch).
                    question = (action.get("question") or "").strip()
                    if not question:
                        reply.status = "ERR: ask failed: empty question"
                        reply.result = False
                    else:
                        # Outline = compact accessibility tree (text +
                        # role + visible-element list). Cap to a few
                        # KB to fit in the prompt.
                        try:
                            outline_text = await browser_ops.outline(
                                tab,
                                visited_urls=state.visited_urls,
                            )
                        except Exception as e:
                            outline_text = f"(outline failed: {e})"
                        outline_text = (outline_text or "")[:3500]

                        # Engine resolution: the script can pick a
                        # specific chat backend via ``engine=`` (e.g.
                        # "chatgpt51"), or "auto" / unset to use the
                        # promoted chat engine on the hub. We hit the
                        # hub's /engines/.../resolve endpoint, which
                        # returns the endpoint + model + API key the
                        # operator configured in the admin UI. Falls
                        # back to AGENT_LLM_URL when the registry has
                        # nothing to say (fresh deploy, hub unreachable).
                        requested_engine = (action.get("engine") or "auto").strip()
                        resolved = await self.resolve_engine(
                            requested_engine,
                            fallback_kind="chat",
                        )
                        if resolved:
                            llm_base = (resolved.get("endpoint") or "").rstrip("/")
                            llm_model = resolved.get("model") or "qwen2.5-vl-72b"
                            llm_api_key = resolved.get("api_key") or ""
                            llm_headers = dict(resolved.get("headers") or {})
                            llm_timeout = float(resolved.get("timeout_s") or 30)
                            llm_protocol = resolved.get("protocol") or "openai"
                        else:
                            llm_base = os.environ.get(
                                "AGENT_LLM_URL",
                                "http://<gpu-host>:15082",
                            ).rstrip("/")
                            llm_model = os.environ.get(
                                "AGENT_MODEL_NAME",
                                "qwen2.5-vl-72b",
                            )
                            llm_api_key = ""
                            llm_headers = {}
                            llm_timeout = 30.0
                            llm_protocol = "openai"

                        prompt = (
                            "You are inspecting a web page. Answer the user's "
                            'question with strictly the single word "yes" or '
                            '"no". No explanation, no quotes, no punctuation. '
                            'If you cannot tell with confidence, answer "no".\n\n'
                            f"Current URL: {cur or '(unknown)'}\n"
                            f"Page outline (excerpt):\n{outline_text}\n\n"
                            f"Question: {question}\n"
                            "Answer (yes or no):"
                        )
                        import httpx as _httpx

                        req_headers = {"Content-Type": "application/json"}
                        if llm_api_key:
                            req_headers["Authorization"] = f"Bearer {llm_api_key}"
                        req_headers.update(llm_headers)
                        body_req = {
                            "model": llm_model,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.0,
                            "max_tokens": 8,
                        }
                        answer_text = ""
                        # ``page.ask`` is documented as a chat-style
                        # check, so we require an OpenAI-compat
                        # protocol. agent-service / cogagent / native
                        # anthropic aren't wired up for arbitrary chat
                        # at this layer yet.
                        if llm_protocol not in ("openai",):
                            _slog(
                                f"ask: engine '{requested_engine}' "
                                f"protocol={llm_protocol!r} not supported "
                                f"for page.ask (need openai-compat); "
                                f"falling back to AGENT_LLM_URL"
                            )
                            llm_base = os.environ.get(
                                "AGENT_LLM_URL",
                                "http://<gpu-host>:15082",
                            ).rstrip("/")
                            llm_model = os.environ.get(
                                "AGENT_MODEL_NAME",
                                "qwen2.5-vl-72b",
                            )
                            req_headers = {"Content-Type": "application/json"}
                            body_req["model"] = llm_model
                        try:
                            async with _httpx.AsyncClient(timeout=llm_timeout) as cli:
                                rr = await cli.post(
                                    f"{llm_base}/v1/chat/completions",
                                    headers=req_headers,
                                    json=body_req,
                                )
                                rr.raise_for_status()
                                data = rr.json()
                                answer_text = (
                                    (data.get("choices") or [{}])[0]
                                    .get("message", {})
                                    .get("content", "")
                                    .strip()
                                )
                        except Exception as e:
                            _slog(
                                f"ask: LLM call failed via "
                                f"engine={requested_engine!r} "
                                f"endpoint={llm_base!r}: "
                                f"{type(e).__name__}: {e}"
                            )
                            reply.status = f"ERR: ask failed: LLM unreachable ({type(e).__name__})"
                            reply.result = False
                        else:
                            # Lenient parsing: strip punctuation / quotes,
                            # check leading word.
                            a = answer_text.strip().lower()
                            a = a.lstrip("'\"`*. ").rstrip("'\"`*. ,!?")
                            head = a.split()[0] if a else ""
                            if head.startswith("yes") or head == "y" or head == "true":
                                reply.result = True
                            elif head.startswith("no") or head == "n" or head == "false":
                                reply.result = False
                            else:
                                _slog(
                                    f"ask: unparseable LLM answer: "
                                    f"{answer_text!r}, defaulting to False"
                                )
                                reply.result = False
                            _slog(f"ask {question!r} -> {reply.result} (LLM said {answer_text!r})")
                elif kind in ("extract", "observe"):
                    # paprika-native structured LLM helpers. Both share
                    # the same engine-resolution + chat-completions
                    # plumbing as ``ask`` above; the difference is the
                    # prompt shape (JSON Schema for extract, candidate
                    # list for observe) and the response parsing (the
                    # SDK does Pydantic validation on extract; the hub
                    # passes observe's array back as-is for the SDK to
                    # wrap in Candidate objects).
                    instruction = (
                        action.get("instruction") or action.get("intent") or ""
                    ).strip()
                    if not instruction:
                        reply.status = f"ERR: {kind}: empty instruction"
                        reply.result = [] if kind == "observe" else None
                    else:
                        # Collect the page context. ``extract`` lets the
                        # caller pick outline vs html via context=; defaults
                        # to outline (compact, [@N]-annotated, plenty for
                        # most extraction tasks). ``observe`` is always
                        # outline-based -- it specifically maps intent to
                        # the [@N] markers.
                        ctx_mode = "outline"
                        if kind == "extract":
                            ctx_mode = (action.get("context") or "outline").lower()
                            if ctx_mode not in ("outline", "html"):
                                ctx_mode = "outline"
                        max_chars = int(action.get("max_chars") or 12000)
                        try:
                            if ctx_mode == "html":
                                page_ctx = await browser_ops.html_excerpt(
                                    tab,
                                    max_chars=max_chars,
                                ) if hasattr(browser_ops, "html_excerpt") else ""
                                if not page_ctx:
                                    page_ctx = await browser_ops.outline(
                                        tab,
                                        visited_urls=state.visited_urls,
                                    )
                            else:
                                page_ctx = await browser_ops.outline(
                                    tab,
                                    visited_urls=state.visited_urls,
                                )
                        except Exception as e:
                            page_ctx = f"(context fetch failed: {e})"
                        page_ctx = (page_ctx or "")[:max_chars]

                        # Engine resolve -- same pattern as ``ask``.
                        requested_engine = (action.get("engine") or "auto").strip()
                        resolved = await self.resolve_engine(
                            requested_engine,
                            fallback_kind="chat",
                        )
                        if resolved:
                            llm_base = (resolved.get("endpoint") or "").rstrip("/")
                            llm_model = resolved.get("model") or "qwen2.5-vl-72b"
                            llm_api_key = resolved.get("api_key") or ""
                            llm_headers = dict(resolved.get("headers") or {})
                            llm_timeout = float(resolved.get("timeout_s") or 60)
                            llm_protocol = resolved.get("protocol") or "openai"
                        else:
                            llm_base = os.environ.get(
                                "AGENT_LLM_URL",
                                "http://<gpu-host>:15082",
                            ).rstrip("/")
                            llm_model = os.environ.get(
                                "AGENT_MODEL_NAME",
                                "qwen2.5-vl-72b",
                            )
                            llm_api_key = ""
                            llm_headers = {}
                            llm_timeout = 60.0
                            llm_protocol = "openai"
                        if llm_protocol not in ("openai",):
                            _slog(
                                f"{kind}: engine {requested_engine!r} "
                                f"protocol={llm_protocol!r} not supported "
                                f"(need openai-compat); falling back to "
                                f"AGENT_LLM_URL"
                            )
                            llm_base = os.environ.get(
                                "AGENT_LLM_URL",
                                "http://<gpu-host>:15082",
                            ).rstrip("/")
                            llm_model = os.environ.get(
                                "AGENT_MODEL_NAME",
                                "qwen2.5-vl-72b",
                            )
                            llm_api_key = ""
                            llm_headers = {}

                        # Build the prompt. The schema_json string (for
                        # extract) and the candidate-shape spec (for
                        # observe) are explicit so the LLM has no excuse
                        # to drift from JSON. Variables are NEVER
                        # substituted in the prompt -- the LLM sees the
                        # raw ``${name}`` placeholders, never the real
                        # values; substitution happens only at the CDP
                        # edge (browser_ops.execute).
                        if kind == "extract":
                            schema_json = (action.get("schema_json") or "").strip()
                            sys_prompt = (
                                "You are a precise structured-data extractor. "
                                "Read the page context below and return data "
                                "that matches the JSON Schema. Output JSON ONLY "
                                "-- no markdown fences, no prose, no comments. "
                                "If a field cannot be determined from the page, "
                                "use null (or omit when the schema allows). "
                                "Do not invent values."
                            )
                            user_prompt = (
                                f"Current URL: {cur or '(unknown)'}\n"
                                f"Page context ({ctx_mode}):\n{page_ctx}\n\n"
                                f"JSON Schema:\n{schema_json}\n\n"
                                f"Instruction: {instruction}\n\n"
                                "Output (JSON only):"
                            )
                        else:  # observe
                            max_results = int(action.get("max_results") or 5)
                            sys_prompt = (
                                "You identify interactive elements on a web "
                                "page that match the user's intent. The page "
                                "outline labels each element with [@N] markers. "
                                "Return up to N candidates as a JSON array. "
                                "Each candidate is an object with these keys:\n"
                                '  "paprika_id"  integer matching an [@N]\n'
                                '  "selector"    "[data-paprika-id=\\"N\\"]" '
                                "(same N as paprika_id)\n"
                                '  "description" short JP/EN label for the '
                                "element\n"
                                '  "method"      one of "click", "fill", '
                                '"press", "type", "hover", "select_option" '
                                "or null when unsure\n"
                                '  "arguments"   array of strings when the '
                                "method needs args (e.g. fill value), else "
                                "null. ${name} placeholders are allowed and "
                                "will be substituted later.\n"
                                '  "confidence"  float 0..1 (your own '
                                "estimate)\n"
                                "Output JSON ONLY (the array). No markdown, "
                                "no prose, no trailing text."
                            )
                            user_prompt = (
                                f"Current URL: {cur or '(unknown)'}\n"
                                f"Page outline:\n{page_ctx}\n\n"
                                f"Intent: {instruction}\n"
                                f"Max results: {max_results}\n\n"
                                "Output (JSON array only):"
                            )

                        import httpx as _httpx
                        req_headers = {"Content-Type": "application/json"}
                        if llm_api_key:
                            req_headers["Authorization"] = f"Bearer {llm_api_key}"
                        req_headers.update(llm_headers)
                        body_req = {
                            "model": llm_model,
                            "messages": [
                                {"role": "system", "content": sys_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            "temperature": 0.0,
                            # extract/observe can need more room than ask's
                            # 8 tokens; the LLM emits a JSON object/array.
                            "max_tokens": 1500,
                        }
                        answer_text = ""
                        try:
                            async with _httpx.AsyncClient(timeout=llm_timeout) as cli:
                                rr = await cli.post(
                                    f"{llm_base}/v1/chat/completions",
                                    headers=req_headers,
                                    json=body_req,
                                )
                                rr.raise_for_status()
                                data = rr.json()
                                answer_text = (
                                    (data.get("choices") or [{}])[0]
                                    .get("message", {})
                                    .get("content", "")
                                    .strip()
                                )
                        except Exception as e:
                            _slog(
                                f"{kind}: LLM call failed via "
                                f"engine={requested_engine!r} "
                                f"endpoint={llm_base!r}: "
                                f"{type(e).__name__}: {e}"
                            )
                            reply.status = (
                                f"ERR: {kind} failed: LLM unreachable "
                                f"({type(e).__name__})"
                            )
                            reply.result = [] if kind == "observe" else None
                        else:
                            # Strip common LLM-decorations (```json fences,
                            # leading "Here is the JSON:" prose, etc.) so
                            # plain json.loads succeeds without a regex zoo.
                            raw = answer_text.strip()
                            if raw.startswith("```"):
                                # Drop opening fence + optional language tag.
                                nl = raw.find("\n")
                                if nl != -1:
                                    raw = raw[nl + 1:]
                                # Drop closing fence.
                                if raw.endswith("```"):
                                    raw = raw[:-3]
                                raw = raw.strip()
                            try:
                                parsed = _json.loads(raw)
                            except Exception as e:
                                _slog(
                                    f"{kind}: LLM response was not JSON: "
                                    f"{raw[:200]!r}"
                                )
                                reply.status = (
                                    f"ERR: {kind} failed: LLM response "
                                    f"was not valid JSON ({type(e).__name__})"
                                )
                                reply.result = [] if kind == "observe" else None
                            else:
                                reply.result = parsed
                                _slog(
                                    f"{kind} {instruction!r} -> "
                                    f"{type(parsed).__name__} "
                                    f"({len(parsed) if hasattr(parsed, '__len__') else '-'})"
                                )
                elif kind == "get_cookies":
                    # Dump every cookie the browser currently has via CDP
                    # Network.getAllCookies. Used by the admin UI's
                    # "save cookies to host" button so the operator can
                    # log into a site once in the noVNC viewer and then
                    # promote that session's cookies into the per-host
                    # registry. ``urls`` (optional) narrows the dump to
                    # cookies that would be sent to those URLs (matches
                    # CDP Network.getCookies semantics).
                    from nodriver import cdp as _cdp

                    urls = action.get("urls")
                    if urls:
                        cookies = await tab.send(_cdp.network.get_cookies(urls=list(urls)))
                    else:
                        cookies = await tab.send(_cdp.network.get_all_cookies())
                    # CDP returns Cookie objects (with extra read-only
                    # fields like size/session). Project to a plain dict
                    # the host registry will accept. cookies_for_cdp
                    # later drops the still-unknown keys before sending
                    # them back to a future session.
                    out: list[dict] = []
                    for c in cookies or []:
                        # nodriver returns dataclass-like objects; fall
                        # back to vars(c) when no to_json helper exists.
                        try:
                            d = c.to_json() if hasattr(c, "to_json") else dict(vars(c))
                        except Exception:
                            d = {}
                        if not d:
                            continue
                        out.append(d)
                    reply.result = {
                        "current_url": cur or "",
                        "count": len(out),
                        "cookies": out,
                    }
                elif kind == "capture":
                    label = action.get("label") or "capture"
                    step = int(action.get("step") or 0)
                    snap = await browser_ops.capture(
                        tab,
                        label=label,
                        step=step,
                        assets_dir=state.assets_dir,
                        log=_slog,
                    )
                    # Upload the PNG (only) to the parent job's /assets
                    # endpoint so the Live tab's "Screenshot" sub-tab
                    # can show it. The Live tab filter requires the
                    # filename to start with "screenshot-" -- so we
                    # rename on upload, keeping the local copy at its
                    # original path for the script's own reference.
                    # HTML and axtree stay local: the user-facing gallery
                    # is for real page resources (image/video/audio)
                    # that the passive CDP listener captures
                    # automatically; HTML/axtree are mainly for
                    # debugging and not worth shipping.
                    if state.asset_upload_base is not None and snap.png_name:
                        png_path = state.assets_dir / snap.label / snap.png_name
                        if png_path.exists() and png_path.stat().st_size > 0:
                            ts = time.strftime("%Y%m%d-%H%M%S")
                            uploaded_name = f"screenshot-{ts}-{snap.label}.png"
                            await self._upload_one_session_asset(
                                state,
                                png_path,
                                mime="image/png",
                                page_url=snap.url or None,
                                asset_name=uploaded_name,
                            )
                    reply.result = {
                        "label": snap.label,
                        "url": snap.url,
                        "html_name": snap.html_name,
                        "png_name": snap.png_name,
                        "axtree_name": snap.axtree_name,
                    }
                elif kind == "download_video":
                    # Late-enable iframe + nested-iframe deep network
                    # trace, if the session was opened with
                    # download_video=False. Cross-origin video players
                    # live inside iframes; without this hook their HLS
                    # / DASH manifest URLs never enter state.network_log
                    # and the iframe-walk fallback below has nothing to
                    # find. Idempotent (the helper short-circuits when
                    # the tab is already marked traced).
                    try:
                        await browser_ops.install_iframe_deep_trace(
                            tab,
                            log=lambda s: _logger.info(f"[session {sid}] {s}"),
                        )
                    except Exception as e:
                        _logger.info(
                            f"[session {sid}] late iframe trace "
                            f"enable failed (non-fatal): "
                            f"{type(e).__name__}: {e}"
                        )
                    # Shell to yt-dlp against the requested URL (or the
                    # current page URL if omitted), saving outputs to
                    # state.assets_dir/videos/. Each newly-saved file is
                    # then uploaded to the parent job's /assets via the
                    # same path the passive CDP listener uses. This is
                    # the bulk video pipeline: for streaming sites the
                    # passive listener only catches m3u8/.ts fragments
                    # whereas yt-dlp produces a single playable .mp4.
                    #
                    # Enhancement (job 2d2e99c3829c): many video sites
                    # embed their player in a 3rd-party iframe whose
                    # OUTER URL yt-dlp doesn't recognise (e.g.
                    # bird.openhub.tv/frame?pi=<opaque-token>). The
                    # actual HLS playlist lives INSIDE the iframe and
                    # gets surfaced in this session's network_log when
                    # playback fires. So: before falling back to yt-dlp
                    # on the page URL, sniff network_log for any
                    # .m3u8 / .mpd entry, nudge <video>/<audio> to
                    # autoplay to populate it, and use the sniffed URL
                    # as the higher-priority candidate. If sniff fails,
                    # behaviour reverts to the original page-URL path.
                    target_url = action.get("url") or ""
                    user_pinned_url = bool(target_url)
                    # ``iframe_walk`` controls Tier 4 below. Default True
                    # for the SDK call (operators want the best-effort
                    # fallback); explicit False lets a caller skip the
                    # invasive navigation step.
                    iframe_walk_enabled = bool(
                        action.get("iframe_walk", True)
                    )
                    if not target_url:
                        try:
                            st = await tab.evaluate("document.location.href")
                            target_url = st or ""
                        except Exception:
                            target_url = ""
                    if not target_url:
                        reply.status = "ERR: no url for download_video"
                    else:
                        from core.fetcher import run_ytdlp

                        videos_dir = state.assets_dir / "videos"
                        videos_dir.mkdir(parents=True, exist_ok=True)
                        timeout_s = int(action.get("timeout_s") or 1800)
                        referer = action.get("referer")

                        # ---- candidate URL list (priority ordered) ----
                        # Tier 1: user-pinned ``url=`` (caller knows best)
                        # Tier 2: deterministic DOM/network discovery
                        #         - <video src> / <source src>
                        #         - .m3u8 / .mpd in network_log
                        # Tier 3: trigger playback + re-sniff
                        # Tier 4: iframe walk (navigate into player iframes)
                        # Tier 5: page URL (original fallback)
                        #
                        # All heuristics are VENDOR-NEUTRAL -- URL shape
                        # and DOM structure, no hostnames hardcoded.
                        # See _looks_like_player_iframe / _PLAYER_PATH_KEYWORDS.
                        candidates: list[dict] = []
                        sniffed_stream: Optional[str] = None
                        dom_video_urls: list[str] = []
                        iframe_walk_done = False

                        if user_pinned_url or _VIDEO_STREAM_RE.search(target_url) \
                                or _VIDEO_DIRECT_RE.search(target_url):
                            # Caller knows what they want -- skip discovery.
                            candidates.append({
                                "url": target_url,
                                "referer": referer,
                                "label": (
                                    "user-pinned url" if user_pinned_url
                                    else "page url (is a stream)"
                                ),
                            })
                        else:
                            # ---- Tier 2: cheap discovery (no waits / no nav) ----
                            dom_video_urls = await _extract_dom_video_urls(tab)
                            for u in dom_video_urls:
                                candidates.append({
                                    "url": u,
                                    "referer": referer or target_url,
                                    "label": "DOM <video|source>[src]",
                                })
                            for u in _sniff_stream_urls_from_log(
                                state.network_log
                            ):
                                if not sniffed_stream:
                                    sniffed_stream = u
                                candidates.append({
                                    "url": u,
                                    "referer": referer or target_url,
                                    "label": "network_log .m3u8/.mpd",
                                })

                            # ---- Tier 3: trigger playback, re-sniff ----
                            # Only if Tier 2 yielded nothing; otherwise we
                            # already have something to try. Modern
                            # browsers block programmatic .play() without
                            # a user gesture, so we ALSO synthesise a
                            # click on the most play-like visible element
                            # (vendor-neutral heuristic).
                            if not candidates:
                                await _trigger_video_playback(tab)
                                clicked = await _try_click_play_button(tab)
                                if clicked:
                                    _slog(
                                        "[download_video] tier3: clicked "
                                        "play-like element"
                                    )
                                # Short wait -- the operator usually
                                # navigated here ages ago; playback +
                                # 3-5s is plenty to surface a playlist.
                                await asyncio.sleep(
                                    5.0 if clicked else 3.0
                                )
                                for u in _sniff_stream_urls_from_log(
                                    state.network_log
                                ):
                                    if not sniffed_stream:
                                        sniffed_stream = u
                                    candidates.append({
                                        "url": u,
                                        "referer": referer or target_url,
                                        "label": "post-play network sniff",
                                    })

                            # Last resort within the original page:
                            # let yt-dlp try the page URL itself before
                            # we go invasive (iframe walk). It works for
                            # the many sites whose page IS a yt-dlp
                            # extractor target.
                            candidates.append({
                                "url": target_url,
                                "referer": referer,
                                "label": "page url",
                            })

                        # ---- yt-dlp + upload loop over candidates ----
                        # Stop after the first candidate that actually
                        # produces uploaded files; otherwise fall
                        # through to the next. Each candidate gets its
                        # own cookies.txt (host-scoped, see ``ask``).
                        upload_timeout = 30 * 60.0
                        uploaded: list[str] = []
                        upload_errors: list[str] = []
                        new_files_all: list[str] = []
                        ok = False
                        msg = ""
                        tried_labels: list[str] = []
                        for cand in candidates:
                            cand_url = cand["url"]
                            cand_ref = cand["referer"]
                            label = cand["label"]
                            tried_labels.append(label)
                            before = {
                                p.name for p in videos_dir.iterdir() if p.is_file()
                            }
                            cookies_file = await self._fetch_cookies_txt_for(
                                cand_url,
                                state,
                                _slog,
                            )
                            _slog(
                                f"[download_video] yt-dlp [{label}] "
                                f"{cand_url[:120]} "
                                f"(timeout {timeout_s}s"
                                + (", +cookies" if cookies_file else "")
                                + ")"
                            )
                            # yt-dlp is sync (subprocess.run); offload to
                            # a worker thread so the event loop keeps
                            # pumping the WS heartbeat etc.
                            ok, msg = await asyncio.to_thread(
                                run_ytdlp,
                                cand_url,
                                videos_dir,
                                cand_ref,
                                None,  # cookies_from_browser
                                timeout_s,
                                _slog,
                                cookies_file,  # cookies_file (Netscape)
                            )
                            if cookies_file:
                                try:
                                    cookies_file.unlink()
                                except OSError:
                                    pass
                            after = {
                                p.name for p in videos_dir.iterdir() if p.is_file()
                            }
                            cand_new = sorted(after - before)
                            new_files_all.extend(cand_new)
                            # Upload each new artefact to the parent job.
                            # Per-file timeout = 30 min: yt-dlp output
                            # for an HD video can be hundreds of MB and
                            # the shared httpx client uses 60s by
                            # default -- not nearly enough. Without this
                            # override the upload silently ReadTimeouts
                            # and the file is lost. (Job ad1846fbbcbc.)
                            for name in cand_new:
                                path = videos_dir / name
                                mime = (
                                    "video/mp4" if path.suffix == ".mp4" else None
                                )
                                try:
                                    ok_up = await self._upload_one_session_asset(
                                        state,
                                        path,
                                        mime=mime,
                                        source_url=cand_url,
                                        page_url=target_url,
                                        timeout=upload_timeout,
                                    )
                                    if ok_up:
                                        uploaded.append(name)
                                    else:
                                        size_b = 0
                                        try:
                                            size_b = path.stat().st_size
                                        except Exception:
                                            pass
                                        upload_errors.append(
                                            f"{name} ({size_b // 1024} KB): "
                                            f"upload did not complete "
                                            f"(asset_upload_base missing, "
                                            f"already-uploaded, or HTTP / "
                                            f"timeout error -- see worker "
                                            f"stderr)"
                                        )
                                except Exception as e:
                                    upload_errors.append(
                                        f"{name}: {type(e).__name__}: {e}"
                                    )
                                    _slog(
                                        f"[download_video] upload {name} "
                                        f"failed: {e}"
                                    )
                            # First candidate that lands a file in the
                            # gallery wins; skip remaining fallbacks.
                            if uploaded:
                                break

                        # ---- Tier 3.5: post-failure re-sniff ----
                        # When every candidate so far returned "Unsupported
                        # URL" (typical signature of yt-dlp probing a page
                        # whose extractor it doesn't have) AND the user
                        # didn't pin a URL, give the playlist a last chance
                        # to surface. Two things happen during the
                        # candidate loop that the original Tier 2/3 sniff
                        # can't catch:
                        #   1) yt-dlp's HTTP probe of the page URL often
                        #      causes the page's player JS to start
                        #      loading the real .m3u8 (analytics ping,
                        #      autoplay kicks in after DOMContentLoaded).
                        #   2) The user-gesture click in Tier 3 might
                        #      only have effect after a few hundred ms
                        #      of JS work that exceeded the original
                        #      3-5s wait.
                        # So: pause briefly to let the network log catch
                        # up, re-sniff, and retry anything new.
                        unsupported = "Unsupported URL" in (msg or "")
                        if (
                            not uploaded
                            and not user_pinned_url
                            and unsupported
                        ):
                            tried_urls = {c["url"] for c in candidates}
                            await asyncio.sleep(3.0)
                            new_streams = [
                                u for u in _sniff_stream_urls_from_log(
                                    state.network_log
                                )
                                if u not in tried_urls
                            ]
                            if new_streams:
                                _slog(
                                    f"[download_video] post-failure re-sniff: "
                                    f"{len(new_streams)} new stream URL(s) "
                                    f"appeared after first pass exhausted with "
                                    f"'Unsupported URL'"
                                )
                                # Bound the retry count -- if 3 attempts on
                                # newly-discovered playlists still fail, the
                                # site probably needs the iframe walk (Tier 4)
                                # to enter the player frame proper.
                                for stream_url in new_streams[:3]:
                                    tried_urls.add(stream_url)
                                    before = {
                                        p.name for p in videos_dir.iterdir()
                                        if p.is_file()
                                    }
                                    cookies_file = (
                                        await self._fetch_cookies_txt_for(
                                            stream_url, state, _slog,
                                        )
                                    )
                                    _slog(
                                        f"[download_video] yt-dlp "
                                        f"[re-sniffed .m3u8/.mpd] "
                                        f"{stream_url[:120]} "
                                        f"(timeout {timeout_s}s"
                                        + (", +cookies" if cookies_file else "")
                                        + ")"
                                    )
                                    ok, msg = await asyncio.to_thread(
                                        run_ytdlp,
                                        stream_url,
                                        videos_dir,
                                        referer or target_url,
                                        None,
                                        timeout_s,
                                        _slog,
                                        cookies_file,
                                    )
                                    if cookies_file:
                                        try:
                                            cookies_file.unlink()
                                        except OSError:
                                            pass
                                    after = {
                                        p.name for p in videos_dir.iterdir()
                                        if p.is_file()
                                    }
                                    cand_new = sorted(after - before)
                                    new_files_all.extend(cand_new)
                                    for name in cand_new:
                                        path = videos_dir / name
                                        mime = (
                                            "video/mp4"
                                            if path.suffix == ".mp4" else None
                                        )
                                        try:
                                            ok_up = (
                                                await self._upload_one_session_asset(
                                                    state,
                                                    path,
                                                    mime=mime,
                                                    source_url=stream_url,
                                                    page_url=target_url,
                                                    timeout=upload_timeout,
                                                )
                                            )
                                            if ok_up:
                                                uploaded.append(name)
                                        except Exception as e:
                                            upload_errors.append(
                                                f"{name}: "
                                                f"{type(e).__name__}: {e}"
                                            )
                                    tried_labels.append(
                                        "re-sniffed .m3u8/.mpd"
                                    )
                                    if uploaded:
                                        break

                        # ---- Tier 4: iframe walk (Phase 3a) ----
                        # Two phases per frame:
                        #
                        #   Phase A (NEW, in-place CDP): for each frame,
                        #     use Page.createIsolatedWorld(frameId) +
                        #     Runtime.evaluate(contextId=...) to harvest
                        #     <video>/<source> URLs AND synthesise a
                        #     user-gesture play click WITHOUT replacing
                        #     the top frame. Works on players that
                        #     refuse to load when not framed (window.top
                        #     === window.self refusal).
                        #
                        #   Phase B (legacy, full navigate): for any
                        #     frame Phase A yielded nothing usable on,
                        #     fall back to the existing
                        #     ``page.navigate(iframe_src)`` approach so
                        #     we don't lose ground on sites where the
                        #     iframe REQUIRES top-level loading.
                        #
                        # Frames discovered via CDP Page.getFrameTree
                        # (recursive, depth=3) so JS-injected and
                        # nested iframes are also visited.
                        # All heuristics vendor-neutral.
                        if (
                            not uploaded
                            and not user_pinned_url
                            and iframe_walk_enabled
                            and not iframe_walk_done
                        ):
                            iframe_walk_done = True
                            try:
                                all_frames = await _enumerate_all_frames(tab)
                            except Exception as e:
                                _slog(
                                    f"[download_video] frame enumeration "
                                    f"failed: {type(e).__name__}: {e}"
                                )
                                all_frames = []
                            # Filter + prioritise: player-shaped URLs
                            # first (heuristic match), then anything
                            # else (catch-all in case the heuristic
                            # underrates). Within each bucket, shallow
                            # depth first.
                            prio_frames: list[tuple[int, int, dict]] = []
                            for fr in all_frames:
                                bucket = (
                                    0 if _looks_like_player_iframe(fr["url"])
                                    else 1
                                )
                                prio_frames.append((bucket, fr["depth"], fr))
                            prio_frames.sort(key=lambda t: (t[0], t[1]))
                            if prio_frames:
                                _slog(
                                    f"[download_video] in-page candidates "
                                    f"exhausted; entering iframe walk "
                                    f"({len(prio_frames)} frame(s) total, "
                                    f"{sum(1 for t in prio_frames if t[0] == 0)} "
                                    f"player-shaped)"
                                )
                            # Capture original URL ONCE so Phase B can
                            # restore the operator's view after a
                            # fallback navigate (Phase A doesn't
                            # navigate, so the restore is a no-op for
                            # in-place hits).
                            orig_url_for_restore = target_url
                            try:
                                orig_url_for_restore = (
                                    await tab.evaluate("document.location.href")
                                    or target_url
                                )
                            except Exception:
                                pass

                            # ---------- Phase A: in-place per-frame ----------
                            # Don't navigate. Just probe each frame via
                            # isolated worlds. If we get a usable URL,
                            # try yt-dlp with the frame's URL as referer.
                            phase_a_winners: set[str] = set()
                            for bucket, depth, fr in prio_frames:
                                if uploaded:
                                    break
                                frame_id = fr["frame_id"]
                                frame_url = fr["url"] or ""
                                _slog(
                                    f"[download_video] frame in-place "
                                    f"@depth={depth} bucket={bucket}: "
                                    f"{frame_url[:120]}"
                                )
                                # Snapshot network_log size BEFORE any
                                # click so we can tell "this manifest
                                # is from THIS frame's click attempt"
                                # vs "manifest was already there".
                                # Note: shared log, no per-frame split;
                                # we just use the new entries as a
                                # weak attribution signal.
                                try:
                                    log_size_before = len(state.network_log or [])
                                except Exception:
                                    log_size_before = 0
                                in_place_cands: list[dict] = []
                                # 1) DOM extraction inside the frame.
                                try:
                                    pre_click_dom = (
                                        await _extract_dom_video_urls_in_frame(
                                            tab, frame_id,
                                        )
                                    )
                                except Exception as e:
                                    _slog(
                                        f"[download_video] frame DOM probe "
                                        f"failed: {type(e).__name__}: {e}"
                                    )
                                    pre_click_dom = []
                                for u in pre_click_dom:
                                    in_place_cands.append({
                                        "url": u,
                                        "referer": frame_url,
                                        "label": (
                                            f"frame[d{depth}] DOM in-place"
                                        ),
                                    })
                                # 2) Try synthesising a user-gesture
                                # click inside the frame. This is the
                                # step that unlocks autoplay-blocked
                                # HLS without replacing the top frame.
                                try:
                                    clicked = (
                                        await _try_click_play_button_in_frame(
                                            tab, frame_id,
                                        )
                                    )
                                except Exception as e:
                                    _slog(
                                        f"[download_video] frame click "
                                        f"failed: {type(e).__name__}: {e}"
                                    )
                                    clicked = False
                                if clicked:
                                    _slog(
                                        f"[download_video] frame in-place "
                                        f"[d{depth}]: clicked play-like "
                                        f"element"
                                    )
                                    await asyncio.sleep(5.0)
                                    # 3) Re-extract after click in case
                                    # the player added a <video> tag
                                    # post-init.
                                    try:
                                        post_click_dom = (
                                            await _extract_dom_video_urls_in_frame(
                                                tab, frame_id,
                                            )
                                        )
                                    except Exception:
                                        post_click_dom = []
                                    for u in post_click_dom:
                                        if not any(c["url"] == u for c in in_place_cands):
                                            in_place_cands.append({
                                                "url": u,
                                                "referer": frame_url,
                                                "label": (
                                                    f"frame[d{depth}] DOM "
                                                    f"in-place (post-click)"
                                                ),
                                            })
                                # 4) New network log entries since
                                # before the click -- shared log, but
                                # the temporal correlation is a useful
                                # weak signal.
                                try:
                                    log_tail = (
                                        (state.network_log or [])[log_size_before:]
                                    )
                                    fresh_sniffs = _sniff_stream_urls_from_log(
                                        log_tail
                                    )
                                except Exception:
                                    fresh_sniffs = []
                                for u in fresh_sniffs:
                                    if not any(c["url"] == u for c in in_place_cands):
                                        in_place_cands.append({
                                            "url": u,
                                            "referer": frame_url,
                                            "label": (
                                                f"frame[d{depth}] sniff "
                                                f"(after in-place click)"
                                            ),
                                        })
                                # 5) Run yt-dlp on the in-place
                                # candidates.
                                for cand in in_place_cands:
                                    cand_url = cand["url"]
                                    cand_ref = cand["referer"]
                                    label = cand["label"]
                                    tried_labels.append(label)
                                    before = {
                                        p.name
                                        for p in videos_dir.iterdir()
                                        if p.is_file()
                                    }
                                    cookies_file = await self._fetch_cookies_txt_for(
                                        cand_url, state, _slog,
                                    )
                                    _slog(
                                        f"[download_video] yt-dlp "
                                        f"[{label}] {cand_url[:120]}"
                                    )
                                    ok, msg = await asyncio.to_thread(
                                        run_ytdlp,
                                        cand_url, videos_dir, cand_ref,
                                        None, timeout_s, _slog, cookies_file,
                                    )
                                    if cookies_file:
                                        try:
                                            cookies_file.unlink()
                                        except OSError:
                                            pass
                                    after = {
                                        p.name
                                        for p in videos_dir.iterdir()
                                        if p.is_file()
                                    }
                                    cand_new = sorted(after - before)
                                    new_files_all.extend(cand_new)
                                    for name in cand_new:
                                        path = videos_dir / name
                                        mime = (
                                            "video/mp4"
                                            if path.suffix == ".mp4"
                                            else None
                                        )
                                        try:
                                            ok_up = (
                                                await self._upload_one_session_asset(
                                                    state,
                                                    path,
                                                    mime=mime,
                                                    source_url=cand_url,
                                                    page_url=orig_url_for_restore,
                                                    timeout=upload_timeout,
                                                )
                                            )
                                            if ok_up:
                                                uploaded.append(name)
                                                phase_a_winners.add(frame_id)
                                            else:
                                                upload_errors.append(
                                                    f"{name}: upload did not "
                                                    f"complete"
                                                )
                                        except Exception as e:
                                            upload_errors.append(
                                                f"{name}: {type(e).__name__}: {e}"
                                            )
                                    if uploaded:
                                        break

                            # ---------- Phase B: legacy navigate ----------
                            # For frames Phase A didn't crack, fall
                            # back to the original "navigate top frame
                            # to iframe URL" approach. Only do this
                            # when nothing landed in uploaded yet.
                            # Reuse the same frame ordering.
                            phase_b_frames = [
                                (b, d, fr)
                                for (b, d, fr) in prio_frames
                                if fr["frame_id"] not in phase_a_winners
                                and _looks_like_player_iframe(fr["url"])
                            ]
                            for ifr_idx, (_b, _d, _fr) in enumerate(phase_b_frames, 1):
                                if uploaded:
                                    break
                                ifr_src = _fr["url"]
                                if uploaded:
                                    break
                                _slog(
                                    f"[download_video] iframe walk Phase B "
                                    f"[{ifr_idx}/{len(phase_b_frames)}]: "
                                    f"{ifr_src[:120]}"
                                )
                                try:
                                    from nodriver import cdp as _cdp_nav
                                    # Spoof the Referer so iframe player
                                    # endpoints that require the parent
                                    # origin (typical 3rd-party players
                                    # serve nothing without it) get one.
                                    # Vendor-neutral: we pass the URL we
                                    # navigated from, which is exactly
                                    # what the browser would have sent
                                    # if the iframe loaded normally.
                                    try:
                                        await tab.send(
                                            _cdp_nav.network.set_extra_http_headers(
                                                headers=_cdp_nav.network.Headers(
                                                    {"Referer": orig_url_for_restore}
                                                ),
                                            )
                                        )
                                    except Exception as e:
                                        _slog(
                                            f"[download_video] iframe set "
                                            f"Referer header failed: "
                                            f"{type(e).__name__}: {e}"
                                        )
                                    await tab.send(
                                        _cdp_nav.page.navigate(ifr_src)
                                    )
                                except Exception as e:
                                    _slog(
                                        f"[download_video] iframe nav "
                                        f"failed: {type(e).__name__}: {e}"
                                    )
                                    continue
                                # Settle: HTTP + script load + initial
                                # autoplay. 4s is a compromise between
                                # "give HLS time" and "don't hang".
                                await asyncio.sleep(4.0)
                                await _trigger_video_playback(tab)
                                # Modern players block autoplay without
                                # a user gesture -- synthesise a click
                                # on the most play-like visible element
                                # (vendor-neutral). This is the key step
                                # that unlocks the HLS manifest request
                                # the iframe walk depends on.
                                ifr_clicked = await _try_click_play_button(tab)
                                if ifr_clicked:
                                    _slog(
                                        f"[download_video] iframe[{ifr_idx}]: "
                                        f"clicked play-like element"
                                    )
                                # Longer wait when we clicked -- gives
                                # the player time to initialise + load
                                # the playlist before sniff.
                                await asyncio.sleep(
                                    6.0 if ifr_clicked else 3.0
                                )
                                # Re-gather candidates from inside the
                                # iframe's now-main-tab context.
                                iframe_cands: list[dict] = []
                                seen_in_walk = set()
                                for u in await _extract_dom_video_urls(tab):
                                    if u in seen_in_walk:
                                        continue
                                    seen_in_walk.add(u)
                                    iframe_cands.append({
                                        "url": u,
                                        "referer": ifr_src,
                                        "label": (
                                            f"iframe[{ifr_idx}] "
                                            f"DOM <video|source>"
                                        ),
                                    })
                                for u in _sniff_stream_urls_from_log(
                                    state.network_log
                                ):
                                    if u in seen_in_walk:
                                        continue
                                    seen_in_walk.add(u)
                                    if not sniffed_stream:
                                        sniffed_stream = u
                                    iframe_cands.append({
                                        "url": u,
                                        "referer": ifr_src,
                                        "label": (
                                            f"iframe[{ifr_idx}] "
                                            f"network .m3u8/.mpd"
                                        ),
                                    })
                                # Also try the iframe URL itself --
                                # some hosts route yt-dlp recognisable
                                # extractors at the player page.
                                iframe_cands.append({
                                    "url": ifr_src,
                                    "referer": orig_url_for_restore,
                                    "label": f"iframe[{ifr_idx}] url",
                                })
                                for cand in iframe_cands:
                                    cand_url = cand["url"]
                                    cand_ref = cand["referer"]
                                    label = cand["label"]
                                    tried_labels.append(label)
                                    before = {
                                        p.name
                                        for p in videos_dir.iterdir()
                                        if p.is_file()
                                    }
                                    cookies_file = await self._fetch_cookies_txt_for(
                                        cand_url, state, _slog,
                                    )
                                    _slog(
                                        f"[download_video] yt-dlp "
                                        f"[{label}] {cand_url[:120]}"
                                    )
                                    ok, msg = await asyncio.to_thread(
                                        run_ytdlp,
                                        cand_url, videos_dir, cand_ref,
                                        None, timeout_s, _slog, cookies_file,
                                    )
                                    if cookies_file:
                                        try:
                                            cookies_file.unlink()
                                        except OSError:
                                            pass
                                    after = {
                                        p.name
                                        for p in videos_dir.iterdir()
                                        if p.is_file()
                                    }
                                    cand_new = sorted(after - before)
                                    new_files_all.extend(cand_new)
                                    for name in cand_new:
                                        path = videos_dir / name
                                        mime = (
                                            "video/mp4"
                                            if path.suffix == ".mp4"
                                            else None
                                        )
                                        try:
                                            ok_up = (
                                                await self._upload_one_session_asset(
                                                    state,
                                                    path,
                                                    mime=mime,
                                                    source_url=cand_url,
                                                    page_url=orig_url_for_restore,
                                                    timeout=upload_timeout,
                                                )
                                            )
                                            if ok_up:
                                                uploaded.append(name)
                                            else:
                                                upload_errors.append(
                                                    f"{name}: upload did not "
                                                    f"complete"
                                                )
                                        except Exception as e:
                                            upload_errors.append(
                                                f"{name}: {type(e).__name__}: {e}"
                                            )
                                    if uploaded:
                                        break
                            # Restore the operator's original view.
                            # Best-effort: never fail the action if
                            # this navigate-back errors (keep_session
                            # users see the post-walk page in noVNC
                            # which is acceptable). Also clear the
                            # Referer override set during the walk so
                            # subsequent operator browsing is normal.
                            if iframe_walk_done and orig_url_for_restore:
                                try:
                                    from nodriver import cdp as _cdp_back
                                    try:
                                        await tab.send(
                                            _cdp_back.network.set_extra_http_headers(
                                                headers=_cdp_back.network.Headers({})
                                            )
                                        )
                                    except Exception:
                                        pass
                                    await tab.send(
                                        _cdp_back.page.navigate(orig_url_for_restore)
                                    )
                                    await asyncio.sleep(1.5)
                                except Exception:
                                    pass

                        # Surface failed uploads in the reply message
                        # so the operator UI can tell apart "yt-dlp
                        # produced nothing" from "yt-dlp produced files
                        # but they didn't ship".
                        if upload_errors and ok:
                            msg = msg + "\n[upload] " + "\n[upload] ".join(upload_errors)
                            ok = bool(uploaded)
                        _slog(
                            f"[download_video] done ok={ok} "
                            f"candidates={len(candidates)} "
                            f"tried={tried_labels} "
                            f"new_files={len(new_files_all)} "
                            f"uploaded={len(uploaded)}"
                        )
                        reply.result = {
                            "ok": ok,
                            "url": target_url,
                            "message": msg,
                            "files": uploaded,
                            "file_count": len(uploaded),
                            # Diagnostic fields so the operator / codegen
                            # LLM can see WHICH path produced the file
                            # (or why it failed).
                            "sniffed_stream": sniffed_stream,
                            "dom_video_urls": dom_video_urls,
                            "iframe_walk_done": iframe_walk_done,
                            "candidates_tried": tried_labels,
                        }
                elif kind == "fetch_refresh":
                    # Operator-triggered refresh on a keep_session
                    # post-fetch session. Captures the current page
                    # HTML (the operator may have navigated via
                    # noVNC) and pushes it to /jobs/{jid}/files/
                    # page.html so /jobs/{jid}/links re-extracts
                    # against the latest DOM. Then walks the worker
                    # tempdir and uploads any files the passive CDP
                    # listener wrote AFTER the original fetch returned
                    # (e.g. .ts segments from a video the operator
                    # played manually). Idempotent: re-running an
                    # already-flushed refresh is cheap and just
                    # returns added=[].
                    added: list[str] = []
                    html_uploaded = False
                    current_url = ""
                    try:
                        current_url = (
                            await tab.evaluate(
                                "document.location.href",
                            )
                            or ""
                        )
                    except Exception:
                        current_url = ""
                    # ---- page.html refresh ----
                    if state.asset_upload_base and state.job_id:
                        try:
                            html = await tab.evaluate(
                                "document.documentElement.outerHTML",
                            )
                            if isinstance(html, str) and html:
                                base = state.asset_upload_base.split("/jobs/", 1)[0]
                                page_url = f"{base}/jobs/{state.job_id}/files/page.html"
                                files = {
                                    "file": (
                                        "page.html",
                                        html.encode("utf-8"),
                                        "text/html",
                                    )
                                }
                                data: dict[str, str] = {}
                                if self.worker_secret:
                                    data["secret"] = self.worker_secret
                                r = await self._http.post(
                                    page_url,
                                    files=files,
                                    data=data,
                                )
                                r.raise_for_status()
                                html_uploaded = True
                        except Exception as e:
                            _slog(
                                f"[fetch_refresh] page.html upload failed: {type(e).__name__}: {e}"
                            )
                    # ---- new-asset flush ----
                    if state.assets_dir is not None:
                        try:
                            for p in sorted(state.assets_dir.rglob("*")):
                                if not p.is_file():
                                    continue
                                if p.name in state.uploaded_assets:
                                    continue
                                ok = await self._upload_one_session_asset(
                                    state,
                                    p,
                                    page_url=current_url or None,
                                )
                                if ok:
                                    added.append(p.name)
                        except Exception as e:
                            _slog(f"[fetch_refresh] asset flush failed: {type(e).__name__}: {e}")
                    _slog(
                        f"[fetch_refresh] current_url={current_url!r} "
                        f"html_uploaded={html_uploaded} "
                        f"added_assets={len(added)}"
                    )
                    reply.result = {
                        "current_url": current_url,
                        "html_uploaded": html_uploaded,
                        "added": added,
                        "added_count": len(added),
                    }
                elif kind == "resize_window":
                    # Resize the Chrome OS window to (width, height).
                    # Used by the admin UI's "iframe サイズに合わせる"
                    # button so the operator can match the browser to
                    # the noVNC viewport. CDP path:
                    #   Browser.getWindowForTarget -> windowId
                    #   Browser.setWindowBounds(windowId, bounds)
                    # The X display itself stays its native size --
                    # we only resize Chrome inside it. Edge cases
                    # (X display smaller than requested window) are
                    # left to Chrome to clamp.
                    try:
                        width = int(action.get("width") or 0)
                        height = int(action.get("height") or 0)
                    except Exception:
                        width = height = 0
                    if width < 200 or height < 200:
                        reply.status = (
                            f"ERR: resize_window: width / height must "
                            f"be >= 200 (got {width}x{height})"
                        )
                    elif width > 4096 or height > 4096:
                        reply.status = (
                            f"ERR: resize_window: width / height must "
                            f"be <= 4096 (got {width}x{height})"
                        )
                    else:
                        try:
                            from nodriver import cdp

                            wfor = await tab.send(
                                cdp.browser.get_window_for_target(),
                            )
                            # nodriver returns (window_id, bounds) tuple.
                            if isinstance(wfor, tuple) and len(wfor) >= 1:
                                window_id = wfor[0]
                            else:
                                window_id = getattr(wfor, "window_id", wfor)
                            await tab.send(
                                cdp.browser.set_window_bounds(
                                    window_id=window_id,
                                    bounds=cdp.browser.Bounds(
                                        width=width,
                                        height=height,
                                        window_state=cdp.browser.WindowState.NORMAL,
                                    ),
                                ),
                            )
                            reply.result = {
                                "width": width,
                                "height": height,
                                "window_id": int(window_id)
                                if isinstance(window_id, (int, str)) and str(window_id).isdigit()
                                else None,
                            }
                            _slog(f"[resize_window] {width}x{height}")
                        except Exception as e:
                            reply.status = (
                                f"ERR: resize_window CDP call failed: {type(e).__name__}: {e}"
                            )
                elif kind == "zoom":
                    # In-browser PAGE zoom. Preferred path: the Paprika
                    # Agent extension's chrome.tabs.setZoom -- the GENUINE
                    # browser zoom (reflows, == the Ctrl+/Ctrl- menu
                    # zoom), which also works on full-viewport
                    # cross-origin iframe players. Fallback when the
                    # agent SW isn't reachable: CDP
                    # Emulation.setPageScaleFactor (pinch-magnify; no
                    # reflow but still visibly zooms). 1.0 = 100%.
                    try:
                        z = float(action.get("factor") or 1.0)
                    except Exception:
                        z = 1.0
                    if z < 0.25:
                        z = 0.25
                    elif z > 5.0:
                        z = 5.0
                    agent_out = None
                    try:
                        _port = getattr(
                            getattr(state, "lane", None), "chrome_port", None,
                        )
                        if _port:
                            agent_out = await _paprika_agent_run(
                                _port, "setZoom", {"factor": z},
                                timeout=8.0, log=_slog,
                            )
                    except Exception as e:
                        _slog(f"[zoom] agent path errored: {type(e).__name__}: {e}")
                        agent_out = None
                    if agent_out and agent_out.get("ok"):
                        reply.result = {
                            "factor": z,
                            "method": "chrome.tabs.setZoom",
                        }
                        _slog(f"[zoom] genuine zoom via agent = {z}")
                    else:
                        # Fallback: CDP pinch-zoom.
                        try:
                            from nodriver import cdp

                            await tab.send(
                                cdp.emulation.set_page_scale_factor(
                                    page_scale_factor=z,
                                ),
                            )
                            reply.result = {
                                "factor": z,
                                "method": "setPageScaleFactor(fallback)",
                            }
                            _slog(
                                f"[zoom] fallback setPageScaleFactor = {z} "
                                f"(agent unavailable)"
                            )
                        except Exception as e:
                            reply.status = (
                                f"ERR: zoom failed (agent + CDP): "
                                f"{type(e).__name__}: {e}"
                            )
                # ---- tab management (session-level) ---------------------
                elif kind == "pages":
                    # List all tabs in this session.
                    # Each entry: {page_id, url, title, is_default}.
                    # URL / title are best-effort -- a tab that just
                    # started navigating may not have either ready.
                    items: list[dict] = []
                    for pid, t in list(state.pages.items()):
                        url = ""
                        title = ""
                        try:
                            url = await t.evaluate("document.location.href") or ""
                        except Exception:
                            pass
                        try:
                            title = await t.evaluate("document.title") or ""
                        except Exception:
                            pass
                        items.append(
                            {
                                "page_id": pid,
                                "url": url,
                                "title": title,
                                "is_default": pid == state.default_page_id,
                            }
                        )
                    reply.result = {
                        "count": len(items),
                        "default_page_id": state.default_page_id,
                        "pages": items,
                    }
                elif kind == "new_page":
                    # Open a new tab in this session. Body params:
                    #   url:    initial URL (default about:blank)
                    #   switch: if True, also flip default_page_id to
                    #           the new tab so subsequent un-keyed
                    #           primitives target it
                    import uuid as _uuid

                    new_url = (action.get("url") or "about:blank").strip()
                    switch = bool(action.get("switch", False))
                    browser_handle = state.browser
                    if browser_handle is None:
                        reply.status = "ERR: session has no browser handle"
                    else:
                        try:
                            new_tab = await browser_handle.get(
                                new_url,
                                new_tab=True,
                            )
                        except Exception as e:
                            reply.status = f"ERR: new_page failed: {type(e).__name__}: {e}"
                        else:
                            # Wait briefly for the navigation to
                            # commit. ``browser.get(url, new_tab=True)``
                            # returns as soon as the new target exists,
                            # NOT once Page.navigate has run. Without
                            # this poll, a subsequent state() / reload()
                            # call samples the tab while it's still on
                            # about:blank -- which is exactly what
                            # broke the YouTube + Google sample (job
                            # a26e4651d538): the new Google tab's
                            # reload() round-tripped to about:blank,
                            # leaving the foreground page unusable
                            # for yt-dlp.
                            if new_url and not new_url.startswith("about:"):
                                for _ in range(30):  # ~3s ceiling
                                    try:
                                        cur = await new_tab.evaluate(
                                            "document.location.href",
                                        )
                                    except Exception:
                                        cur = None
                                    if (
                                        isinstance(cur, str)
                                        and cur
                                        and not cur.startswith("about:")
                                    ):
                                        break
                                    await asyncio.sleep(0.1)
                            pid = "p_" + _uuid.uuid4().hex[:8]
                            state.pages[pid] = new_tab
                            state.page_locks[pid] = asyncio.Lock()
                            if switch or state.default_page_id is None:
                                state.default_page_id = pid
                            _slog(f"new_page: opened {pid} -> {new_url} (switch={switch})")
                            reply.result = {
                                "page_id": pid,
                                "url": new_url,
                                "is_default": pid == state.default_page_id,
                            }
                elif kind == "close_page":
                    # Close one tab. Body params:
                    #   page_id: which tab to close (required)
                    # Closing the default page is allowed but only if
                    # there's at least one other page to fall back to;
                    # the default_page_id is auto-moved to a remaining
                    # page (the most-recently-added one).
                    pid = action.get("page_id") or ""
                    if not pid:
                        reply.status = "ERR: close_page requires page_id"
                    elif pid not in state.pages:
                        reply.status = (
                            f"ERR: unknown page_id {pid!r} (known: {sorted(state.pages.keys())})"
                        )
                    elif len(state.pages) <= 1:
                        reply.status = (
                            f"ERR: cannot close the last remaining "
                            f"page ({pid}); end the session instead"
                        )
                    else:
                        t = state.pages.pop(pid)
                        state.page_locks.pop(pid, None)
                        if pid == state.default_page_id:
                            # Fall back to most-recently-added page.
                            state.default_page_id = next(reversed(list(state.pages.keys())))
                            _slog(f"close_page: default moved to {state.default_page_id}")
                        try:
                            await t.close()
                        except Exception as e:
                            _slog(
                                f"close_page: tab.close raised "
                                f"{type(e).__name__}: {e} (already gone?)"
                            )
                        _slog(f"close_page: closed {pid}")
                        reply.result = {
                            "closed_page_id": pid,
                            "default_page_id": state.default_page_id,
                        }
                elif kind == "switch_page":
                    # Change the default tab (where un-keyed primitives
                    # land). Body: {page_id}.
                    pid = action.get("page_id") or ""
                    if not pid:
                        reply.status = "ERR: switch_page requires page_id"
                    elif pid not in state.pages:
                        reply.status = (
                            f"ERR: unknown page_id {pid!r} (known: {sorted(state.pages.keys())})"
                        )
                    else:
                        state.default_page_id = pid
                        # Best-effort: bring it to the visual front in
                        # noVNC so the operator can see what the script
                        # is now operating on.
                        try:
                            t = state.pages[pid]
                            if hasattr(t, "activate"):
                                await t.activate()
                            elif hasattr(t, "bring_to_front"):
                                await t.bring_to_front()
                        except Exception:
                            pass
                        reply.result = {"default_page_id": pid}

                elif kind == "solve_cloudflare":
                    # Get past a Cloudflare "Just a moment..." challenge.
                    #
                    # Two phases:
                    #   1. WAIT: nodriver is an undetected real Chrome, so
                    #      the common *managed* challenge auto-passes
                    #      within a few seconds of executing the
                    #      challenge JS. Poll the title until the marker
                    #      disappears.
                    #   2. CLICK (opt-in, default on): if the wait times
                    #      out the challenge probably wants an explicit
                    #      Turnstile checkbox click. Use nodriver's
                    #      verify_cf() -- it template-matches the
                    #      checkbox in a screenshot (opencv) and clicks
                    #      it by coordinate, since the widget lives in a
                    #      cross-origin iframe / shadow DOM unreachable
                    #      via the DOM. Then poll again.
                    #
                    # Body: {timeout_s?: float, click_checkbox?: bool}
                    # Result: {cleared, title, waited_s, clicked_checkbox}
                    #
                    # IMPORTANT: verify_cf() clicks the best template
                    # match unconditionally (no confidence threshold in
                    # nodriver), so we ONLY invoke it while the title
                    # still shows a challenge marker -- never on a
                    # normal page, which would mis-click random content.
                    import asyncio as _asyncio

                    timeout_s = float(action.get("timeout_s") or 25.0)
                    click_checkbox = action.get("click_checkbox", True)
                    poll = 1.0
                    start = time.time()

                    # Language-independent challenge detection. The
                    # inline challenge page sets ``window._cf_chl_opt``
                    # (gold signal) + loads a /challenge-platform/
                    # script + a challenges.cloudflare.com iframe. A
                    # multilingual title-marker list is the fallback --
                    # the JA challenge title is "しばらくお待ちください..."
                    # which an English-only check missed (the bug that
                    # made the first test falsely report cleared=True).
                    # On a cleared page none of these are present.
                    _CF_DETECT_JS = (
                        "(function(){try{"
                        "if(window._cf_chl_opt)return true;"
                        "if(document.getElementById('challenge-running'))return true;"
                        "if(document.querySelector('script[src*=\"challenge-platform\"]'))return true;"
                        "if(document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]'))return true;"
                        "var t=(document.title||'').toLowerCase();"
                        "var m=['just a moment','checking your browser',"
                        "'attention required','\\u3057\\u3070\\u3089\\u304f\\u304a\\u5f85\\u3061',"
                        "'\\u5c11\\u3005\\u304a\\u5f85\\u3061','\\u304a\\u5f85\\u3061\\u304f\\u3060\\u3055\\u3044',"
                        "'un momento','einen moment'];"
                        "for(var i=0;i<m.length;i++){if(t.indexOf(m[i])>=0)return true;}"
                        "return false;"
                        "}catch(e){return true;}})()"
                    )

                    async def _title() -> str:
                        try:
                            return (await tab.evaluate("document.title")) or ""
                        except Exception:
                            return ""

                    async def _challenged() -> bool:
                        try:
                            return bool(await tab.evaluate(_CF_DETECT_JS))
                        except Exception:
                            # Can't evaluate (navigation in flight etc.)
                            # -> assume still challenged, keep waiting.
                            return True

                    # Locate the Turnstile checkbox by the CF iframe's
                    # page-coordinate bounding box. Language-independent
                    # (unlike verify_cf's English template match, which
                    # mis-clicks the JA "私はロボットではありません" widget).
                    # The checkbox sits at the iframe's left edge,
                    # vertically centred -- ~30px in from the left.
                    _CF_IFRAME_RECT_JS = (
                        "(function(){var f=document.querySelector("
                        "'iframe[src*=\"challenges.cloudflare.com\"]');"
                        "if(!f)return null;var r=f.getBoundingClientRect();"
                        "if(!r||!r.width||!r.height)return null;"
                        "return {x:r.left,y:r.top,w:r.width,h:r.height};})()"
                    )

                    async def _click_cf_checkbox() -> bool:
                        # Primary: click by iframe rect (any language).
                        rect = None
                        try:
                            rect = await tab.evaluate(_CF_IFRAME_RECT_JS)
                        except Exception:
                            rect = None
                        if isinstance(rect, dict) and rect.get("w"):
                            x = float(rect.get("x") or 0)
                            y = float(rect.get("y") or 0)
                            w = float(rect.get("w") or 0)
                            h = float(rect.get("h") or 0)
                            cx = x + min(30.0, w * 0.12)
                            cy = y + h / 2.0
                            try:
                                await tab.mouse_click(cx, cy)
                                _slog(
                                    f"solve_cloudflare: clicked CF iframe "
                                    f"checkbox at ({cx:.0f},{cy:.0f})"
                                )
                                return True
                            except Exception as e:
                                _slog(
                                    f"solve_cloudflare: iframe-rect "
                                    f"click failed: {type(e).__name__}: {e}"
                                )
                        # Fallback: nodriver template match (EN only).
                        try:
                            await tab.verify_cf()
                            _slog(
                                "solve_cloudflare: verify_cf() template click attempted (fallback)"
                            )
                            return True
                        except Exception as e:
                            _slog(
                                f"solve_cloudflare: verify_cf fallback "
                                f"failed: {type(e).__name__}: {e}"
                            )
                            return False

                    # Phase 1: passive wait (auto-pass window).
                    cleared = False
                    deadline = start + timeout_s
                    while time.time() < deadline:
                        if not await _challenged():
                            cleared = True
                            break
                        await _asyncio.sleep(poll)

                    # Phase 2: checkbox click. Only while still
                    # challenged + opted in. Retry a couple times --
                    # the Turnstile widget can take a beat to render
                    # its clickable checkbox after the page settles.
                    clicked = False
                    if not cleared and click_checkbox:
                        for _attempt in range(3):
                            if not await _challenged():
                                cleared = True
                                break
                            if await _click_cf_checkbox():
                                clicked = True
                            # Re-poll ~8s after each click attempt.
                            post_deadline = time.time() + 8.0
                            while time.time() < post_deadline:
                                if not await _challenged():
                                    cleared = True
                                    break
                                await _asyncio.sleep(poll)
                            if cleared:
                                break

                    waited = round(time.time() - start, 1)
                    last_title = await _title()
                    reply.result = {
                        "cleared": cleared,
                        "title": last_title,
                        "waited_s": waited,
                        "clicked_checkbox": clicked,
                    }
                    _slog(
                        f"solve_cloudflare: cleared={cleared} "
                        f"clicked={clicked} title={last_title!r} "
                        f"waited={waited}s"
                    )
                    # Status stays OK regardless -- the caller branches
                    # on result.cleared. A non-cleared challenge isn't a
                    # protocol error, it's a "site still gated" signal
                    # the script can act on (retry / hand to operator).

                else:
                    # click / fill / press_key / scroll / navigate /
                    # back / wait -- delegate to browser_ops.execute
                    # which understands these kinds.
                    #
                    # For nav-kind actions (navigate / back / forward /
                    # history_first) we also capture the main document's
                    # HTTP response (status, headers, final URL) so the
                    # SDK can surface a Playwright-compatible Response
                    # object to the caller. Non-nav kinds return resp=None.
                    nav_kinds = ("navigate", "back", "forward", "history_first")
                    if action.get("kind") in nav_kinds:
                        status_str, resp_info = await browser_ops.execute_nav_with_response(
                            tab, action, _slog,
                        )
                        reply.status = status_str
                        if resp_info:
                            # Put the HTTP response info under reply.result
                            # as a dict; existing nav callers expect
                            # result=None so we use {"response": {...}}
                            # to keep the new key namespaced and easy to
                            # spot.
                            reply.result = {"response": resp_info}
                    else:
                        reply.status = await browser_ops.execute(
                            tab,
                            action,
                            _slog,
                        )
            except Exception as e:
                reply.status = f"ERR: {type(e).__name__}: {e}"
                _slog(f"action {msg.action.get('kind')!r} crashed: {e}")

        reply.elapsed_ms = int((time.time() - t0) * 1000)
        try:
            await self._send(reply)
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] failed to send session_action_result: {e}",
            )

    async def _force_end_all_sessions(self, reason: str) -> int:
        """Tear down every session currently held by this worker and
        return their lanes to the pool. Called from the WS-reconnect
        path so that a hub bounce can't leave the worker holding lanes
        the hub no longer knows about.

        Without this, `commit e5c0f35`-style deploys produced the
        scenario diagnosed on job fb84e2a6da7b: hub's in-memory session
        registry got wiped on restart but the worker still believed
        both lanes were occupied -> every subsequent attempt at
        session_start was rejected with "no free lane" until the
        operator manually `docker compose restart worker`'d.

        Returns the number of sessions force-closed. Best-effort:
        per-session cleanup errors are swallowed since the goal is
        "get back to a known-clean state", not to surface tab-close
        races.
        """
        if not self._sessions:
            return 0
        sids = list(self._sessions.keys())
        _logger.info(
            f"[worker {self.worker_id}] force-ending {len(sids)} session(s) "
            f"({reason}): {', '.join(sids)}",
        )
        for sid in sids:
            state = self._sessions.pop(sid, None)
            if state is None:
                continue
            try:
                if state.browser is not None:
                    try:
                        await state.browser.stop()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                if state.lane is not None and self.lane_pool is not None:
                    self.lane_pool.release(state.lane)
            except Exception:
                pass
        return len(sids)

    async def _dump_session_to_parent_job(self, sid: str, state) -> None:
        """Persist a session's network log + final-page link list to
        the parent job's directory so the Live panel still has data
        after the session closes.

        Sibling URLs of ``state.asset_upload_base``:

          * ``POST {hub}/jobs/{pid}/network``         (the network log)
          * ``POST {hub}/jobs/{pid}/links_snapshot``  (the final links)

        Both endpoints are best-effort -- a failure here is logged but
        not raised so the lane still gets released. No-op when the
        session has no parent (``asset_upload_base`` unset)."""
        if not state.asset_upload_base:
            return
        # Derive sibling URLs by stripping the trailing /assets. Robust
        # against base URLs that already include a job_id path (which
        # asset_upload_base always does -- see _asset_upload_url).
        base = state.asset_upload_base
        if base.endswith("/assets"):
            base = base[: -len("/assets")]

        # ---- network log ------------------------------------------------
        entries = list(state.network_log or [])
        if entries:
            try:
                await self._http.post(
                    f"{base}/network",
                    json={
                        "secret": self.worker_secret or "",
                        "session_id": sid,
                        "entries": entries,
                    },
                    timeout=10.0,
                )
            except Exception as e:
                _logger.info(
                    f"[session {sid}] network dump POST failed: {type(e).__name__}: {e}",
                )

        # ---- last-page links snapshot -----------------------------------
        # state.tab may already be None for sessions that died early; the
        # evaluate call also can't reach a tab that crashed. Both paths
        # are tolerated -- an empty link list is a valid snapshot.
        tab = getattr(state, "tab", None)
        if tab is None:
            return
        try:
            raw = await asyncio.wait_for(
                tab.evaluate(_LINKS_EXTRACT_JS),
                timeout=5.0,
            )
        except Exception:
            raw = None
        items: list = []
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    items = parsed
            except Exception:
                pass
        elif isinstance(raw, list):
            items = raw
        # Best-effort current URL -- the snapshot is more useful with it.
        cur = ""
        try:
            cur_raw = await asyncio.wait_for(
                tab.evaluate("location.href"),
                timeout=2.0,
            )
            if isinstance(cur_raw, str):
                cur = cur_raw
        except Exception:
            pass
        try:
            await self._http.post(
                f"{base}/links_snapshot",
                json={
                    "secret": self.worker_secret or "",
                    "session_id": sid,
                    "current_url": cur,
                    "links": items,
                },
                timeout=10.0,
            )
        except Exception as e:
            _logger.info(
                f"[session {sid}] links snapshot POST failed: {type(e).__name__}: {e}",
            )

    async def _teardown_session_state(self, sid: str, state) -> None:
        """Common teardown logic: reset tabs to about:blank, stop the
        browser handle, release the lane. Extracted so both the
        normal session_end path AND the abort-on-arrival path
        (_aborted_sessions checkpoint in session_start) can reuse it.
        Best-effort throughout -- per-step failures are swallowed
        because the lane MUST be released no matter what."""
        # Drain pending video downloads first. Sessions that triggered
        # an HLS yt-dlp or a direct mp4 fetch via the passive m3u8
        # listener may still have ffmpeg merging segments / httpx
        # streaming bytes when the operator (or upstream code) ends
        # the session -- tearing Chrome down would kill the
        # subprocess / cancel the stream mid-write and leave a
        # corrupt partial file (or nothing at all).
        #
        # drain() itself implements an idle-window policy: it keeps
        # waiting as long as bytes / yt-dlp log lines keep flowing
        # (default 45s of zero progress before abandoning) up to a
        # hard wall-clock cap (default 30 min). The outer
        # asyncio.wait_for here is the matching safety net at the
        # same cap -- if drain() somehow hangs without polling its
        # own deadline, this releases the lane.
        if state.video_drainer is not None:
            hard_cap = float(
                os.environ.get("PAPRIKA_VIDEO_DRAIN_HARD_S", "1800.0")
            )
            try:
                await asyncio.wait_for(
                    state.video_drainer(), timeout=hard_cap + 30.0,
                )
            except asyncio.TimeoutError:
                _logger.info(
                    f"[worker {self.worker_id}] session {sid} video "
                    f"drain hit outer safety timeout after "
                    f"{hard_cap + 30:.0f}s; tearing down anyway"
                )
            except Exception as e:
                _logger.info(
                    f"[worker {self.worker_id}] session {sid} video "
                    f"drain failed: {type(e).__name__}: {e}"
                )

        # Persist session-end snapshots BEFORE teardown closes the tab
        # and discards state.network_log. The Live panel's Network /
        # Links tabs read these files when the session is gone --
        # without this dump, opening Live on a completed job shows
        # empty Network and (for non-fetch jobs) empty Links.
        # Bounded by an overall timeout so a flaky hub can't stall the
        # lane release path.
        try:
            await asyncio.wait_for(
                self._dump_session_to_parent_job(sid, state),
                timeout=15.0,
            )
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] session {sid} dump "
                f"to parent job failed: {type(e).__name__}: {e}",
            )
        try:
            if state.browser is not None:
                try:
                    await browser_ops.force_single_tab(
                        state.browser,
                        log=lambda s: _logger.info(
                            f"[session {sid}] {s}",
                        ),
                    )
                except Exception as e:
                    _logger.info(
                        f"[worker {self.worker_id}] session {sid} "
                        f"force_single_tab teardown failed: "
                        f"{type(e).__name__}: {e}",
                    )
                if state.tab is not None:
                    try:
                        await state.tab.get("about:blank", new_tab=False)
                    except Exception:
                        pass
                try:
                    await state.browser.stop()
                except Exception:
                    pass
        finally:
            # Restore the lane's default profile if this session
            # was running with an operator-uploaded profile. Idempotent
            # via Lane._profile_swap_active -- the call is a no-op
            # when no swap is active.
            if state.lane is not None:
                try:
                    await state.lane.restore_default_profile()
                except Exception as e:
                    _logger.info(
                        f"[worker {self.worker_id}] session {sid} "
                        f"restore_default_profile failed: "
                        f"{type(e).__name__}: {e}",
                    )
            if state.lane is not None and self.lane_pool is not None:
                self.lane_pool.release(state.lane)
            # Mirror the lane release into self._in_flight so the
            # next heartbeat reports the worker as free. _in_flight
            # was kept incremented past the original
            # _run_assigned_job finish for keep_session sessions so
            # the scheduler wouldn't over-dispatch onto a lane the
            # session was holding. Decrement here closes that loop.
            # Best-effort: clamped at 0 since we can't easily tell
            # whether THIS session is one that kept in_flight bumped
            # (vs. a regular session_start-style session that never
            # bumped it).
            if hasattr(state, "job_id") and state.job_id:
                try:
                    self._in_flight = max(0, self._in_flight - 1)
                except Exception:
                    pass
            # keep_session sessions own a worker-side workdir that
            # outlived the fetch. Clean it up now that the browser is
            # really gone -- otherwise it'd leak on disk forever.
            try:
                wd = getattr(state, "workdir", None)
                if wd is not None:
                    shutil.rmtree(wd, ignore_errors=True)
            except Exception:
                pass

    async def _handle_session_end(self, msg: HubSessionEnd) -> None:
        """Release the lane and detach nodriver. Tabs reset to about:blank
        so the next session sees a clean browser.

        If the session isn't in ``self._sessions`` yet, the end message
        likely arrived while ``_handle_session_start`` is still in
        flight (e.g. the hub's session-start ``wait_for`` timed out
        and is now telling us to abort). Mark the sid in
        ``self._aborted_sessions`` so the in-flight start tears itself
        down right before sending its ack.
        """
        sid = msg.session_id
        ack = WorkerSessionEndAck(session_id=sid)
        state = self._sessions.pop(sid, None)
        if state is None:
            # Mark for abort -- in-flight session_start will see this.
            self._aborted_sessions.add(sid)
            ack.error = f"session {sid} not present (queued for abort if start is still in flight)"
        else:
            await self._teardown_session_state(sid, state)
            _logger.info(f"[worker {self.worker_id}] session {sid} ended")
        try:
            await self._send(ack)
        except Exception as e:
            _logger.info(f"[worker {self.worker_id}] failed to send session_end_ack: {e}")

    async def _handle_session_agent(self, msg: HubSessionAgent) -> None:
        """Run a localised agent loop on the session's bound tab.

        Equivalent to ``await page.agent(goal, max_steps, engine)`` in
        the SDK. Each ``step`` is observe -> ask engine -> execute, up
        to ``max_steps`` iterations or until the engine emits
        ``done`` / ``end``. The session's existing nodriver tab is
        reused (no separate Lane needed).

        ``msg.engine`` selects the driver:

          - ``"qwen"``:     Qwen-VL via agent_service /act (selectors).
          - ``"cogagent"``: CogAgent via cogagent_service /act (pixel
                            boxes -> execute_vision_action).
          - ``"auto"``:     CogAgent first; if its action looks suspect
                            (corner / repeat / out-of-viewport), retry
                            this step via Qwen-VL.
        """
        sid = msg.session_id
        rid = msg.request_id
        result = WorkerSessionAgentResult(
            session_id=sid,
            request_id=rid,
        )
        state = self._sessions.get(sid)
        if state is None:
            result.error = f"session {sid} not found"
            try:
                await self._send(result)
            except Exception:
                pass
            return

        # Lazy imports so the session_agent path doesn't pay for them
        # on workers that never call it.
        import httpx
        from nodriver import cdp as _cdp

        from server.worker import browser_ops as bops

        engine = (getattr(msg, "engine", None) or "auto").lower()
        if engine not in ("auto", "qwen", "cogagent"):
            engine = "auto"

        # Env knobs (read per-request so changes are hot-reloadable
        # without a worker restart).
        agent_url = os.environ.get(
            "AGENT_URL",
            "http://<worker-host>:8001",
        ).rstrip("/")
        agent_timeout_s = float(
            os.environ.get("AGENT_REQUEST_TIMEOUT_S", "180"),
        )
        send_screenshots = os.environ.get("AGENT_SEND_SCREENSHOTS", "0") not in ("0", "false", "no")
        cogagent_url = os.environ.get(
            "COGAGENT_URL",
            "http://<gpu-host>:15083",
        ).rstrip("/")
        cogagent_timeout_s = float(
            os.environ.get("COGAGENT_REQUEST_TIMEOUT_S", "120"),
        )
        agent_llm_url = os.environ.get(
            "AGENT_LLM_URL",
            "http://<gpu-host>:15082",
        ).rstrip("/")
        agent_llm_model = os.environ.get(
            "AGENT_MODEL_NAME",
            "qwen2.5-vl-72b",
        )

        def _slog(line: str) -> None:
            _logger.info(f"[session {sid} agent] {line}")

        # ---- JP/CN -> EN goal translation -----------------------------
        # CogAgent (and to a lesser extent Qwen-VL prompt templates) has
        # known weakness with Japanese task descriptions. We translate
        # once up front via the configured chat LLM (Qwen2.5-VL is
        # bilingual). Always-English goals short-circuit the cheap
        # _looks_non_english check.
        translated_goal = msg.goal
        if _looks_non_english(msg.goal):
            _slog(f"goal looks non-english; translating: {msg.goal!r}")
            translated_goal = await _translate_to_english(
                msg.goal,
                agent_llm_url=agent_llm_url,
                model_name=agent_llm_model,
                timeout_s=30.0,
                log=_slog,
            )
            if translated_goal != msg.goal:
                _slog(f"translated -> {translated_goal!r}")

        # ---- per-engine step helpers ----------------------------------
        # Both return a (action_dict, outcome_hint) pair where
        # outcome_hint is "OK" on success or "ERR: ..." / "NO_MATCH"
        # otherwise. action_dict has the same shape across engines so
        # the executor below doesn't care which produced it.

        # Shared history of compact strings; both engines see the same
        # tail. Qwen wants "kind -> outcome", CogAgent wants
        # "CLICK(...) <desc>" -- we just send "kind -> outcome" to both
        # for simplicity; CogAgent tolerates the shorter format.
        history: list[str] = []
        # Parallel structured trace of (n, engine, kind, outcome) shipped
        # back in WorkerSessionAgentResult.steps so the SDK can print
        # them to the job log. ``history`` stays as compact strings
        # because the engines see them in their /act prompts and the
        # format matters there; ``step_events`` is just for the
        # operator-facing trace.
        step_events: list[dict[str, Any]] = []
        # Last CogAgent box for the suspect-detector's "loop" check.
        last_cogagent_box: dict | None = None
        last_action: dict | None = None
        completed = False
        steps_taken = 0
        summary: str | None = None
        # Track visited URLs so the video downloader can detect
        # "the page just navigated to *.mp4" between steps. Without
        # this, popup_policy=follow on gallery / streaming sites would
        # leave the click successful but the actual MP4 unsaved
        # (Chrome's media player swallows the response before our
        # CDP listeners see it).
        session_visited_urls: list[str] = []

        # Video downloader: prefer the session-wide closures hooked up
        # at session_open (they share the downloaded-URLs set with the
        # passive on_response m3u8 trigger and the explicit
        # download_video action -- so the same URL isn't re-downloaded
        # by three different code paths). Fall back to a local pair
        # only when the session pre-dates the wiring (e.g. fetch-owned
        # sessions that never went through _handle_session_open).
        if state.video_downloader is not None and state.video_drainer is not None:
            maybe_download_video = state.video_downloader
            drain_video_downloads = state.video_drainer
        else:
            async def _on_session_video_saved(path: Path, info: dict) -> None:
                try:
                    await self._upload_one_session_asset(
                        state,
                        path,
                        mime=info.get("mime"),
                        source_url=info.get("url"),
                        page_url=info.get("document_url"),
                    )
                except Exception as e:
                    _slog(f"video upload failed: {type(e).__name__}: {e}")

            maybe_download_video, drain_video_downloads = _make_video_downloader(
                assets_dir=state.assets_dir,
                min_asset_size=int(os.environ.get("MIN_ASSET_SIZE_BYTES", "0") or 0),
                on_saved=_on_session_video_saved,
                log=lambda s: _slog(s),
                job_id_for_logs=f"session-{sid}",
                page_url_provider=lambda: (
                    (state.last_response or {}).get("url")
                    if isinstance(getattr(state, "last_response", None), dict)
                    else None
                ),
            )

        async def _viewport(tab) -> tuple[int, int]:
            try:
                vp_str = await tab.evaluate(
                    "JSON.stringify({w: window.innerWidth, h: window.innerHeight})"
                )
                import json as _json

                vp = _json.loads(vp_str or "{}")
                return int(vp.get("w") or 1280), int(vp.get("h") or 720)
            except Exception:
                return 1280, 720

        async def _ask_qwen(
            client, current_url: str, outline: str, screenshot_b64: str | None, step: int
        ) -> tuple[dict, str | None]:
            req = {
                "goal": translated_goal,
                "url": current_url or "",
                "ax_tree": outline,
                "history": history[-10:],
                "step": step,
                "max_steps": msg.max_steps,
            }
            if screenshot_b64:
                req["image_b64"] = screenshot_b64
            try:
                r = await client.post(f"{agent_url}/act", json=req, timeout=agent_timeout_s)
                r.raise_for_status()
                payload = r.json()
            except Exception as e:
                return ({"kind": "unknown"}, f"ERR: qwen /act: {e}")
            return (payload.get("action") or {"kind": "unknown"}, None)

        async def _ask_cogagent(
            client,
            screenshot_b64: str,
            viewport_w: int,
            viewport_h: int,
        ) -> tuple[dict, str | None]:
            req = {
                "task": translated_goal,
                "image_b64": screenshot_b64,
                "image_width": viewport_w,
                "image_height": viewport_h,
                "history": history[-20:],
                "platform": "WIN",
                "answer_format": "Action-Operation",
                "max_new_tokens": 512,
                "temperature": 0.0,
            }
            try:
                r = await client.post(f"{cogagent_url}/act", json=req, timeout=cogagent_timeout_s)
                r.raise_for_status()
                payload = r.json()
            except Exception as e:
                return ({"kind": "unknown"}, f"ERR: cogagent /act: {e}")
            return (payload.get("action") or {"kind": "unknown"}, None)

        async def _execute(
            action: dict,
            viewport_w: int,
            viewport_h: int,
        ) -> str:
            kind = action.get("kind") or "unknown"
            # Selector-shaped actions (from Qwen) go through bops.execute.
            # Box-shaped actions (from CogAgent) go through
            # execute_vision_action. Detect by which fields are present.
            if action.get("box"):
                try:
                    return await bops.execute_vision_action(
                        state.tab,
                        action,
                        _slog,
                        viewport_width=viewport_w,
                        viewport_height=viewport_h,
                    )
                except Exception as e:
                    return f"ERR: {type(e).__name__}: {e}"
            else:
                try:
                    return await bops.execute(state.tab, action, _slog)
                except Exception as e:
                    return f"ERR: {type(e).__name__}: {e}"

        async with state.lock:
            try:
                async with httpx.AsyncClient(timeout=agent_timeout_s) as client:
                    for step in range(1, max(1, msg.max_steps) + 1):
                        # ---- observe ---------------------------------
                        try:
                            current_url = await state.tab.evaluate("document.location.href")
                        except Exception:
                            current_url = ""
                        if current_url:
                            canon = bops.canon_url(current_url)
                            state.note_url(canon)
                            # Detect URL change since last step and
                            # fire off a video downloader if we landed
                            # on a media URL. mirrors the same logic
                            # in _run_vision_agent_job so page.agent()
                            # gets the same MP4-from-popup-follow
                            # behaviour as mode=vision-agent jobs.
                            if not session_visited_urls or session_visited_urls[-1] != canon:
                                prev_url = session_visited_urls[-1] if session_visited_urls else ""
                                session_visited_urls.append(canon)
                                maybe_download_video(current_url, prev_url)

                        viewport_w, viewport_h = await _viewport(state.tab)

                        # CogAgent always needs a screenshot. Qwen only
                        # when configured. Grab once and share.
                        png_b64: str | None = None
                        if engine != "qwen" or send_screenshots:
                            try:
                                png_b64 = await state.tab.send(
                                    _cdp.page.capture_screenshot(format_="png"),
                                )
                            except Exception as e:
                                _slog(f"screenshot failed: {e}")

                        # Qwen also wants the page outline.
                        outline: str | None = None
                        if engine in ("qwen", "auto"):
                            try:
                                outline = await bops.outline(
                                    state.tab,
                                    visited_urls=state.visited_urls,
                                )
                            except Exception as e:
                                outline = ""
                                _slog(f"outline failed: {e}")

                        # ---- choose engine + ask ---------------------
                        action: dict = {"kind": "unknown"}
                        ask_err: str | None = None
                        used_engine = engine

                        if engine == "qwen":
                            action, ask_err = await _ask_qwen(
                                client,
                                current_url,
                                outline or "",
                                png_b64 if send_screenshots else None,
                                step,
                            )
                        elif engine == "cogagent":
                            if png_b64 is None:
                                ask_err = "cogagent needs screenshot but capture failed"
                            else:
                                action, ask_err = await _ask_cogagent(
                                    client,
                                    png_b64,
                                    viewport_w,
                                    viewport_h,
                                )
                        else:  # auto
                            # 1) Try CogAgent first.
                            if png_b64 is None:
                                _slog("auto: no screenshot; falling back to qwen directly")
                                action, ask_err = await _ask_qwen(
                                    client,
                                    current_url,
                                    outline or "",
                                    None,
                                    step,
                                )
                                used_engine = "qwen"
                            else:
                                cog_action, cog_err = await _ask_cogagent(
                                    client,
                                    png_b64,
                                    viewport_w,
                                    viewport_h,
                                )
                                suspect: str | None = None
                                if cog_err:
                                    suspect = cog_err
                                else:
                                    suspect = _looks_suspect(
                                        cog_action,
                                        viewport_w=viewport_w,
                                        viewport_h=viewport_h,
                                        last_box=last_cogagent_box,
                                    )
                                if suspect:
                                    _slog(
                                        f"[auto step {step}] cogagent "
                                        f"suspect ({suspect}); falling back to qwen"
                                    )
                                    action, ask_err = await _ask_qwen(
                                        client,
                                        current_url,
                                        outline or "",
                                        png_b64 if send_screenshots else None,
                                        step,
                                    )
                                    used_engine = "qwen"
                                else:
                                    action = cog_action
                                    used_engine = "cogagent"
                                    # Remember box for next step's
                                    # loop-detection.
                                    last_cogagent_box = (
                                        cog_action.get("box")
                                        if isinstance(cog_action, dict)
                                        else None
                                    )

                        if ask_err:
                            result.error = ask_err
                            _slog(f"abort step {step}: {ask_err}")
                            break

                        kind = action.get("kind") or "unknown"
                        last_action = action

                        # ---- execute --------------------------------
                        if kind in ("done", "end"):
                            completed = True
                            summary = action.get("summary") or action.get("action_text") or "done"
                            steps_taken = step
                            _slog(f"[step {step}/{msg.max_steps} {used_engine}] done: {summary}")
                            step_events.append(
                                {
                                    "n": step,
                                    "engine": used_engine,
                                    "kind": "done",
                                    "outcome": summary,
                                }
                            )
                            break
                        if kind == "unknown":
                            _slog(
                                f"[step {step}/{msg.max_steps} {used_engine}] "
                                f"unknown action; aborting"
                            )
                            history.append("unknown -> abort")
                            step_events.append(
                                {
                                    "n": step,
                                    "engine": used_engine,
                                    "kind": "unknown",
                                    "outcome": "abort",
                                }
                            )
                            steps_taken = step
                            break
                        if kind == "capture":
                            _slog(
                                f"[step {step}/{msg.max_steps} {used_engine}] "
                                f"capture inside page.agent() ignored"
                            )
                            history.append("capture(skipped)")
                            step_events.append(
                                {
                                    "n": step,
                                    "engine": used_engine,
                                    "kind": "capture",
                                    "outcome": "skipped (capture inside agent ignored)",
                                }
                            )
                            steps_taken = step
                            continue

                        outcome = await _execute(action, viewport_w, viewport_h)
                        _slog(f"[step {step}/{msg.max_steps} {used_engine}] {kind} -> {outcome}")
                        history.append(f"{kind} -> {outcome}")
                        step_events.append(
                            {
                                "n": step,
                                "engine": used_engine,
                                "kind": kind,
                                "outcome": outcome,
                            }
                        )
                        steps_taken = step

                if not completed and steps_taken == 0 and not result.error:
                    result.error = "agent emitted no actions"
            except Exception as e:
                result.error = f"{type(e).__name__}: {e}"
                _slog(f"crashed: {e}")
            finally:
                # Wait for any pending video downloads (httpx single
                # file or yt-dlp HLS) BEFORE returning control to the
                # caller's script. The script's next ``await
                # page.wait_for(...)`` or page.goto() expects to see
                # the video already in the gallery; without this
                # drain a yt-dlp invocation could still be merging
                # segments when the script moves on or the worker
                # tears the lane down.
                try:
                    await drain_video_downloads()
                except Exception as e:
                    _slog(f"video-drain crashed: {e}")

        result.completed = completed
        result.steps_taken = steps_taken
        result.summary = summary
        result.last_action = last_action
        result.steps = step_events
        try:
            await self._send(result)
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] failed to send session_agent_result: {e}",
            )

    # ----------------------------------------------------------- profile

    async def _handle_profile_sync(self, msg: HubProfileSync) -> None:
        """Prefetch (or update) the cached extraction for one profile.

        On etag match the download is a no-op, BUT we still pass
        through to ``_reconcile_ambient_default`` because the same
        broadcast is used by the hub to flip is_default for an
        already-cached profile (e.g. operator hits "set as default"
        on a profile that's been on the worker for days). A fresh
        etag triggers a fresh download + atomic swap before the
        ambient reconcile.
        """
        async with self._profile_cache_lock:
            cur = self._profile_cache.get(msg.name)
            already_current = (
                cur and cur.get("etag") == msg.etag and Path(cur.get("dir", "")).exists()
            )
            if not already_current:
                _logger.info(
                    f"[worker {self.worker_id}] profile cache: "
                    f"prefetching {msg.name!r} ({msg.size_bytes} bytes)",
                )
        if already_current:
            # Skip the download, still reconcile the ambient state
            # so a "set as default" on an already-cached profile
            # actually installs into lanes.
            await self._reconcile_ambient_default(msg.name, msg.is_default)
            return
        # Download outside the cache lock so other syncs (different
        # names) can proceed in parallel. We re-acquire the lock to
        # swap the cache entry once the new extraction is ready.
        new_dir = await self._fetch_to_temp(msg.url, scratch_key=f"sync-{msg.name}")
        if new_dir is None:
            return  # _fetch_to_temp logged the failure
        # Move into the canonical cache location atomically (rename
        # is atomic on the same filesystem; both paths are in /tmp).
        self._profile_cache_root.mkdir(parents=True, exist_ok=True)
        canonical = self._profile_cache_root / msg.name
        async with self._profile_cache_lock:
            old_dir = canonical.with_suffix(".old")
            if canonical.exists():
                try:
                    if old_dir.exists():
                        shutil.rmtree(old_dir, ignore_errors=True)
                    canonical.rename(old_dir)
                except OSError:
                    shutil.rmtree(canonical, ignore_errors=True)
            try:
                new_dir.rename(canonical)
            except OSError:
                shutil.copytree(new_dir, canonical, dirs_exist_ok=True)
                shutil.rmtree(new_dir, ignore_errors=True)
            # Drop the previous version now that the new one is in
            # place. In-flight jobs that already snapshotted the old
            # dir are unaffected because Lane.use_profile copies out
            # before launching Chrome.
            shutil.rmtree(old_dir, ignore_errors=True)
            self._profile_cache[msg.name] = {
                "etag": msg.etag,
                "dir": str(canonical),
                "size_bytes": msg.size_bytes,
            }
        _logger.info(
            f"[worker {self.worker_id}] profile cache: ready {msg.name!r} (etag={msg.etag})",
        )
        # Operator-set default handling. The hub tags the broadcast
        # with is_default=True for the current default profile and
        # is_default=False for everything else. Workers maintain a
        # tiny state machine so noVNC viewers see the operator's
        # logged-in Chrome on EVERY lane (not just lanes that
        # happened to run a job recently).
        await self._reconcile_ambient_default(msg.name, msg.is_default)

    # ---------------------------------------------------------- extensions

    async def _sync_extensions_from_hub(self) -> None:
        """Pull the hub's enabled-extension set into this worker's
        local cache. Called once on register so every lane started
        afterwards picks them up via --load-extension. Idempotent:
        skips downloads whose etag matches the cached one, and
        evicts cache entries that no longer exist on the hub (so a
        deleted extension stops loading on the next Chrome bounce).
        """
        import tarfile as _tarfile

        import httpx

        # Use the http(s) hub base derived from the WS URL at startup.
        base = (self.hub_http_url or "").rstrip("/")
        if not base:
            _logger.info(
                f"[worker {self.worker_id}] extension sync: no hub_http_url set; skipping",
            )
            return
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{base}/extensions")
                if r.status_code != 200:
                    _logger.info(
                        f"[worker {self.worker_id}] extension sync: "
                        f"hub returned HTTP {r.status_code}; "
                        f"skipping",
                    )
                    return
                payload = r.json()
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] extension sync: "
                f"list fetch failed ({type(e).__name__}: {e})",
            )
            return
        items = [it for it in (payload.get("extensions") or []) if it.get("enabled", True)]
        wanted: dict[str, dict] = {it["slug"]: it for it in items if it.get("slug")}
        self._extension_cache_root.mkdir(parents=True, exist_ok=True)
        # Evict cache entries for extensions removed on the hub.
        async with self._extension_cache_lock:
            stale = [s for s in self._extension_cache if s not in wanted]
        for slug in stale:
            target = self._extension_cache_root / slug
            try:
                shutil.rmtree(target, ignore_errors=True)
            except Exception:
                pass
            async with self._extension_cache_lock:
                self._extension_cache.pop(slug, None)
            _logger.info(
                f"[worker {self.worker_id}] extension cache: evicted {slug!r}",
            )
        # Sync each wanted extension.
        for slug, meta in wanted.items():
            etag = str(meta.get("etag") or "")
            async with self._extension_cache_lock:
                cur = self._extension_cache.get(slug)
            target = self._extension_cache_root / slug
            up_to_date = (
                cur is not None
                and cur.get("etag") == etag
                and target.exists()
                and (target / "manifest.json").exists()
            )
            if up_to_date:
                continue
            # Download the tarball + extract atomically (extract to
            # a sibling .new dir, then rename into place).
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    async with client.stream(
                        "GET",
                        f"{base}/extensions/{slug}/download",
                    ) as resp:
                        if resp.status_code != 200:
                            _logger.info(
                                f"[worker {self.worker_id}] extension "
                                f"{slug!r}: download HTTP "
                                f"{resp.status_code}",
                            )
                            continue
                        tmp_tar = self._extension_cache_root / f".{slug}.tar.gz.dl"
                        with open(tmp_tar, "wb") as f:
                            async for chunk in resp.aiter_bytes(64 * 1024):
                                f.write(chunk)
            except Exception as e:
                _logger.info(
                    f"[worker {self.worker_id}] extension {slug!r}: "
                    f"download failed ({type(e).__name__}: {e})",
                )
                continue
            # Extract.
            new_target = self._extension_cache_root / f".{slug}.new"
            shutil.rmtree(new_target, ignore_errors=True)
            new_target.mkdir(parents=True, exist_ok=True)
            try:
                with _tarfile.open(tmp_tar, "r:gz") as tf:
                    # Hub already validated against path traversal,
                    # but defence in depth.
                    for m in tf.getmembers():
                        if m.issym() or m.islnk():
                            continue
                        full = (new_target / m.name).resolve()
                        try:
                            full.relative_to(new_target.resolve())
                        except ValueError:
                            _logger.info(
                                f"[worker {self.worker_id}] extension "
                                f"{slug!r}: skipped traversal entry "
                                f"{m.name!r}",
                            )
                            continue
                    tf.extractall(new_target)
            except Exception as e:
                _logger.info(
                    f"[worker {self.worker_id}] extension {slug!r}: "
                    f"extract failed ({type(e).__name__}: {e})",
                )
                shutil.rmtree(new_target, ignore_errors=True)
                try:
                    tmp_tar.unlink()
                except OSError:
                    pass
                continue
            try:
                tmp_tar.unlink()
            except OSError:
                pass
            # The tarball top-level entry is the slug-named dir, so
            # the extracted layout is new_target/<slug>/manifest.json.
            # Move that inner dir into the canonical target.
            inner = new_target / slug
            if not (inner / "manifest.json").exists():
                # Be tolerant if the hub-side packing changed shape.
                if (new_target / "manifest.json").exists():
                    inner = new_target
                else:
                    _logger.info(
                        f"[worker {self.worker_id}] extension {slug!r}: "
                        f"manifest.json not found post-extract",
                    )
                    shutil.rmtree(new_target, ignore_errors=True)
                    continue
            # Atomic swap into place.
            async with self._extension_cache_lock:
                if target.exists():
                    old = target.with_suffix(".old")
                    shutil.rmtree(old, ignore_errors=True)
                    try:
                        target.rename(old)
                    except OSError:
                        shutil.rmtree(target, ignore_errors=True)
                try:
                    inner.rename(target)
                except OSError:
                    shutil.copytree(inner, target, dirs_exist_ok=True)
                if new_target.exists():
                    shutil.rmtree(new_target, ignore_errors=True)
                self._extension_cache[slug] = {
                    "etag": etag,
                    "dir": str(target),
                    "size_bytes": int(meta.get("size_bytes") or 0),
                    "version": str(meta.get("version") or ""),
                    "name": str(meta.get("name") or slug),
                }
            _logger.info(
                f"[worker {self.worker_id}] extension cache: "
                f"ready {slug!r} (v{meta.get('version') or '?'}, "
                f"etag={etag})",
            )

    def loaded_extension_paths(self) -> list[str]:
        """Return the on-disk paths to every cached hub-managed
        extension. Called by the lane code on Chrome launch to build
        the ``--load-extension`` arg list. Order is stable (sorted by
        slug) so chrome's load order is deterministic for debugging.
        """
        out: list[str] = []
        # Read under the lock for a consistent snapshot, then release.
        snap: list[tuple[str, dict]] = []
        # The lock is asyncio.Lock so we can't use it from a sync method;
        # accessing the dict is safe enough -- writers swap whole keys
        # atomically and we only read.
        for slug in sorted(self._extension_cache.keys()):
            snap.append((slug, self._extension_cache[slug]))
        for slug, entry in snap:
            d = entry.get("dir")
            if not d:
                continue
            p = Path(d)
            if not (p / "manifest.json").exists():
                continue
            out.append(str(p))
        return out

    async def _reconcile_ambient_default(
        self,
        name: str,
        is_default: bool,
    ) -> None:
        """Install / clear the ambient default profile based on a
        HubProfileSync signal. Idempotent.

        * is_default=True, name == current ambient -> no-op
        * is_default=True, name != current ambient -> install ``name``
          on every idle lane (lanes with an in-flight per-job swap
          are skipped; they'll inherit the new ambient on next
          release via the existing .lane-default restore path)
        * is_default=False, name == current ambient -> clear ambient
          on every idle lane (lane reverts to stock)
        * is_default=False, name != current ambient -> no-op (the
          broadcast is just telling us this profile isn't default;
          nothing to do)
        """
        cur = self._ambient_default_name
        if is_default:
            if cur == name:
                return
            cached = self._profile_cache.get(name)
            if not cached or not Path(cached.get("dir", "")).exists():
                # cache didn't make it to disk somehow; bail out
                # so we don't try to install nothing.
                return
            cdir = Path(cached["dir"])
            if self.lane_pool is None:
                return
            installed = 0
            skipped = 0
            for lane in self.lane_pool.lanes:
                if lane.busy:
                    skipped += 1
                    continue
                ok = await lane.set_ambient_profile(cdir, name)
                if ok:
                    installed += 1
                else:
                    skipped += 1
            self._ambient_default_name = name
            _logger.info(
                f"[worker {self.worker_id}] ambient default -> "
                f"{name!r}: installed on {installed} lane(s), "
                f"{skipped} skipped (busy)",
            )
        else:
            if cur != name:
                return
            if self.lane_pool is None:
                return
            cleared = 0
            for lane in self.lane_pool.lanes:
                if lane.busy:
                    continue
                ok = await lane.clear_ambient_profile()
                if ok:
                    cleared += 1
            self._ambient_default_name = None
            _logger.info(
                f"[worker {self.worker_id}] ambient default "
                f"cleared (was {name!r}): {cleared} lane(s) "
                f"reverted to stock",
            )

    async def _handle_profile_delete(self, msg: HubProfileDelete) -> None:
        """Drop the cached copy of a profile. No-op if not cached.
        Also clears the ambient install if this was the default
        profile (lanes revert to stock).
        """
        # If the deleted profile was the ambient default, clear it
        # on lanes FIRST so we don't leave an orphaned reference to
        # a cache dir that's about to be rmtree'd.
        await self._reconcile_ambient_default(msg.name, is_default=False)
        async with self._profile_cache_lock:
            self._profile_cache.pop(msg.name, None)
        canonical = self._profile_cache_root / msg.name
        if canonical.exists():
            shutil.rmtree(canonical, ignore_errors=True)
            _logger.info(
                f"[worker {self.worker_id}] profile cache: evicted {msg.name!r}",
            )

    async def _fetch_to_temp(
        self,
        profile_url: str,
        *,
        scratch_key: str,
        log=None,
    ) -> Path | None:
        """Download + extract a profile tarball to a NEW temp dir.

        Returns the extracted dir on success, ``None`` on any
        network / archive failure. Caller decides whether to install
        it directly (job path: rename into the lane), promote it to
        the cache (sync path: rename into _profile_cache_root), or
        clean it up.
        """
        import tarfile

        import httpx

        scratch = Path(tempfile.mkdtemp(prefix=f"paprika-profile-{scratch_key}-"))
        tar_path = scratch / "profile.tar.gz"
        extract_dir = scratch / "User Data"
        try:
            async with httpx.AsyncClient(timeout=60.0) as cli:
                async with cli.stream("GET", profile_url) as r:
                    r.raise_for_status()
                    with open(tar_path, "wb") as f:
                        async for chunk in r.aiter_bytes(64 * 1024):
                            f.write(chunk)
        except Exception as e:
            msg = f"  ... profile fetch failed: {type(e).__name__}: {e} (URL: {profile_url})"
            if log:
                log(msg)
            else:
                _logger.info(msg)
            shutil.rmtree(scratch, ignore_errors=True)
            return None
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                safe_root = extract_dir.resolve()
                for m in tar.getmembers():
                    dest = (extract_dir / m.name).resolve()
                    if not str(dest).startswith(str(safe_root)):
                        raise ValueError(f"tarball member escapes extract dir: {m.name}")
                tar.extractall(extract_dir)
        except Exception as e:
            msg = f"  ... profile extract failed: {type(e).__name__}: {e}"
            if log:
                log(msg)
            else:
                _logger.info(msg)
            shutil.rmtree(scratch, ignore_errors=True)
            return None
        finally:
            try:
                tar_path.unlink()
            except OSError:
                pass

        # Belt-and-suspenders normalisation: the hub now normalises
        # archives at upload time, but caches from before that
        # change (= tarballs accepted by an old hub build) keep
        # whatever top-level structure the operator originally
        # uploaded -- typically "Profile 10/" or similar named
        # profile. Chrome at startup reads <user-data-dir>/Default/,
        # so an un-remapped tarball means Chrome silently sees no
        # profile content. We detect + fix here too so existing
        # caches don't have to be re-uploaded.
        try:
            _normalise_extracted_profile(extract_dir, log=log)
        except Exception as e:
            msg = f"  ... profile extract: post-normalisation failed: {type(e).__name__}: {e}"
            if log:
                log(msg)
            else:
                _logger.info(msg)
            # Don't fail outright -- the unnormalised tree might
            # still be partially useful (Chrome generates a fresh
            # Default/ on first run and the operator can re-upload).
        return extract_dir

    async def _get_profile_for_job(
        self,
        *,
        profile_url: str,
        profile_name: str | None,
        profile_etag: str | None,
        scratch_key: str,
        log=None,
    ) -> Path | None:
        """Resolve a profile to an extracted directory ready for
        Lane.use_profile().

        Lookup order:
          1. Local cache (HubProfileSync prefetch): if name + etag
             match, copy the cached tree to a per-job scratch dir
             and return that. The cache stays untouched so other
             concurrent jobs can also use it.
          2. On-demand fetch from ``profile_url`` (existing path).

        ``None`` return means both failed -- the caller should log
        and continue with the lane's default profile rather than
        failing the job (operator can tell from missing cookies +
        worker stderr).
        """
        # 1) Cache hit?
        if profile_name and profile_etag:
            async with self._profile_cache_lock:
                cur = self._profile_cache.get(profile_name)
            if cur and cur.get("etag") == profile_etag:
                cached_dir = Path(cur["dir"])
                if cached_dir.exists():
                    out = (
                        Path(
                            tempfile.mkdtemp(
                                prefix=f"paprika-profile-{scratch_key}-",
                            )
                        )
                        / "User Data"
                    )
                    try:
                        shutil.copytree(cached_dir, out)
                        if log:
                            log(f"  ... profile {profile_name!r} from local cache")
                        else:
                            _logger.info(
                                f"[worker {self.worker_id}] profile cache HIT: {profile_name!r}",
                            )
                        return out
                    except Exception as e:
                        # Cache copy failed -- shouldn't normally
                        # happen on a same-fs copytree, but fall
                        # through to the network path rather than
                        # failing the job.
                        _logger.info(
                            f"[worker {self.worker_id}] profile cache "
                            f"copy failed: {type(e).__name__}: {e} -- "
                            f"falling back to fetch",
                        )
                        shutil.rmtree(out.parent, ignore_errors=True)
        # 2) On-demand fetch.
        return await self._fetch_to_temp(
            profile_url,
            scratch_key=scratch_key,
            log=log,
        )

    # _fetch_profile_tarball was the pre-cache fetch helper; superseded
    # by _get_profile_for_job + _fetch_to_temp above. Removed in
    # the profile-cache feature commit.

    # ----------------------------------------------------------- run a job

    async def _run_assigned_job(self, assign: HubAssignJob) -> None:
        job_id = assign.job_id
        async with self._sem:
            self._in_flight += 1
            lane = None
            if self.lane_pool is not None:
                # If hub specified a lane_hint (attach_to_job), wait for THAT
                # specific lane to be free. Otherwise grab any free lane.
                lane = await self.lane_pool.acquire(lane_hint=assign.lane_hint)
                if lane is None:
                    await self._send(
                        WorkerJobFailed(
                            job_id=job_id,
                            error=(
                                f"lane_hint {assign.lane_hint} out of range"
                                if assign.lane_hint is not None
                                else "no free lane in pool"
                            ),
                        )
                    )
                    self._in_flight -= 1
                    return
            # Track whether we swapped the lane's profile in so the
            # finally block can restore it on every code path.
            _profile_swapped = False
            # Pre-bind the names the finally block reads. If something
            # explodes BEFORE the original assignment further down
            # (where these come from assign.options) the finally would
            # otherwise hit UnboundLocalError and mask the real error.
            keep_session = False
            inspect_sid = assign.session_id
            try:
                await self._send(
                    WorkerJobAccepted(
                        job_id=job_id,
                        novnc_url=lane.novnc_url if lane else None,
                        lane_idx=lane.lane_idx if lane else None,
                    )
                )

                # If the hub told us to use an operator-uploaded
                # Chrome profile, fetch + extract it and re-point the
                # lane's user-data-dir at it BEFORE we hand the lane
                # to nodriver. Best-effort: a failed fetch falls back
                # to the lane default rather than failing the job --
                # the operator will notice the missing cookies on
                # their own faster than the LLM would interpret a
                # "could not pull profile" error.
                profile_url = getattr(assign, "profile_url", None)
                if profile_url and lane is not None:
                    pdir = await self._get_profile_for_job(
                        profile_url=profile_url,
                        profile_name=getattr(assign, "profile_name", None),
                        profile_etag=getattr(assign, "profile_etag", None),
                        scratch_key=job_id,
                    )
                    if pdir is not None:
                        try:
                            await lane.use_profile(pdir)
                            _profile_swapped = True
                            _logger.info(
                                f"[{job_id}] operator profile installed into lane #{lane.lane_idx}",
                            )
                        except Exception as e:
                            _logger.info(
                                f"[{job_id}] lane.use_profile failed: "
                                f"{type(e).__name__}: {e} -- "
                                f"continuing with lane default",
                            )

                # Local tempdir for assets + page.html + log.txt
                workdir = Path(tempfile.mkdtemp(prefix=f"paprika-{job_id}-"))
                assets_dir = workdir / "assets"
                assets_dir.mkdir(parents=True, exist_ok=True)
                log_path = workdir / "log.txt"
                log_fp = open(log_path, "a", encoding="utf-8", buffering=1)

                # Log callback: write file + ship to hub via WorkerJobLog
                send_lock = self._send_lock

                def _log(line: str) -> None:
                    line = line.rstrip()
                    # log_fp gets closed before the keep_session
                    # post-fetch block runs ("... is now interactive"
                    # etc.); writes to a closed file raise ValueError
                    # which previously took the whole job down with
                    # "worker crashed: ValueError: I/O operation on
                    # closed file." Stderr + WS log keep working so
                    # the operator-facing log doesn't lose lines.
                    try:
                        if not log_fp.closed:
                            log_fp.write(line + "\n")
                    except (ValueError, OSError):
                        pass
                    _logger.info(f"[{job_id}] {line}")
                    asyncio.ensure_future(self._send(WorkerJobLog(job_id=job_id, line=line)))

                # ---- Job banner ---------------------------------------
                # Surface the requested URL + key options at the very top
                # of every job log. Without this, fetch logs jump straight
                # into "downloaded N cookies" / "saved page.html" and the
                # operator scrolling back through Live can't tell which
                # URL the job was actually pointed at (a real complaint
                # for jobs with many redirects or job-ID-only filenames).
                _mode = (getattr(assign.options, "mode", None) or "fetch")
                _url = getattr(assign, "url", "") or "(no url)"
                _log(f"=== job {job_id}  mode={_mode} ===")
                _log(f"==> URL: {_url}")
                _opt = assign.options
                _opt_bits: list[str] = []
                _mw = getattr(_opt, "max_wait_seconds", None)
                if _mw is not None:
                    _opt_bits.append(f"max_wait={_mw}s")
                _ca = getattr(_opt, "capture_assets", None)
                if _ca is not None:
                    _opt_bits.append(f"capture_assets={bool(_ca)}")
                if getattr(_opt, "scroll", None):
                    _opt_bits.append("scroll=True")
                _up = getattr(_opt, "use_profile", None)
                if _up:
                    _opt_bits.append(f"profile={_up!r}")
                if getattr(_opt, "keep_session", None):
                    _opt_bits.append("keep_session=True")
                if getattr(_opt, "goal", None):
                    _g = str(_opt.goal)
                    _opt_bits.append(
                        f"goal={(_g[:80] + '…') if len(_g) > 80 else _g!r}"
                    )
                if _opt_bits:
                    _log("    options: " + ", ".join(_opt_bits))

                # NOTE: the v1 "vision-agent" mode (CogAgent screenshot
                # loop + pixel-space actions) was removed in the v2
                # cleanup. The hub rejects mode="vision-agent" at the
                # protocol layer (Pydantic) and the dispatcher never
                # routes such jobs here. Worker code that drove it
                # (_run_vision_agent_job, _handle_session_agent's
                # cogagent branch, _ask_cogagent helpers) is left as
                # dead code reachable only via legacy paths that no
                # longer fire; a follow-up cleanup can rip it out
                # without protocol or behavioural changes.

                # Fetch mode: single-shot HTML + assets capture.
                # LLM-driven jobs (mode=codegen-loop) are orchestrated by
                # the hub and never reach this code path -- they spawn a
                # sandboxed paprika-runner that drives the browser via
                # /sessions/* HTTP, not via the worker's job pipeline.
                # The old per-step agent loop was removed in PR-14a.

                # Build the "after fetch, save cookies back to host"
                # callback. The hub gives us ``assign.save_cookies_host``
                # already normalised; we host-filter the dumped jar
                # client-side so we don't store noise (cross-site
                # tracker cookies) and PUT to /hosts/{host}. The
                # hub URL is derived from asset_upload_base (the only
                # absolute hub URL the worker already knows).
                save_cb = None
                save_host_for_cb = assign.save_cookies_host
                if save_host_for_cb:
                    save_cb = self._make_cookie_save_callback(
                        assign,
                        save_host_for_cb,
                        _log,
                    )

                # Register this fetch as a read-only inspectable
                # session so the admin UI can call /sessions/{id}/
                # cookies / outline / screenshot / state while the
                # fetch is running. on_ready fires after CDP is set
                # up but before navigation; on_closing fires in
                # fetch's finally before browser.stop(). Together
                # they guarantee the session is alive for exactly
                # the inspectable window.
                ready_cb = None
                closing_cb = None
                inspect_sid = assign.session_id
                keep_session = bool(getattr(assign.options, "keep_session", False))
                # Network log for the fetch-mode path: tracked here
                # and shared with the inspect session so the Live
                # panel "Network" tab can display real-time traffic.
                fetch_network_log: list = []
                if inspect_sid and lane is not None:
                    ready_cb, closing_cb = self._make_fetch_session_callbacks(
                        inspect_sid,
                        lane,
                        assets_dir,
                        _log,
                        job_id=job_id,
                        keep_session=keep_session,
                        network_log=fetch_network_log,
                    )

                # Pre-baked per-host recipe (HostRecord.fetch_recipes).
                # The hub stamps the picked recipe onto options.fetch_recipe
                # before dispatch; we wrap it in a callback that fires
                # right after the initial Page.navigate(). Best-effort:
                # recipe failures are logged but don't fail the fetch.
                _picked_recipe = getattr(assign.options, "fetch_recipe", None)
                async def _recipe_cb(tab):
                    if _picked_recipe:
                        await _apply_fetch_recipe(tab, _picked_recipe, _log)
                fetch_opts = self._build_fetch_options(
                    assign.url,
                    assign.options,
                    assets_dir,
                    _log,
                    lane=lane,
                    cookies_to_install=assign.cookies,
                    on_complete_dump_cookies=save_cb,
                    on_browser_ready=ready_cb,
                    on_browser_closing=closing_cb,
                    on_after_navigate=_recipe_cb if _picked_recipe else None,
                    network_log=fetch_network_log,
                )
                try:
                    result = await fetch(fetch_opts)
                except Exception as e:
                    _log(f"  !! fetch crashed: {type(e).__name__}: {e}")
                    log_fp.close()
                    await self._upload_log(assign, log_path)
                    await self._send(
                        WorkerJobFailed(
                            job_id=job_id,
                            error=f"{type(e).__name__}: {e}",
                        )
                    )
                    shutil.rmtree(workdir, ignore_errors=True)
                    return

                # Persist page.html + log to local workdir
                page_path = workdir / "page.html"
                page_path.write_text(result.html, encoding="utf-8")
                log_fp.close()

                # Upload all outputs to hub
                await self._upload_files(assign, workdir, result)

                # Build the JobResult Pydantic object with hub-side hrefs.
                # page_url: every fetch-mode asset belongs to the single
                # page the operator asked us to grab (assign.url). The
                # fetcher tracks per-asset source URL + mime but not the
                # initiating document URL, so we stamp the job URL on
                # every entry -- same shape the assets.json + .meta/
                # sidecar pipeline uses.
                page_url_for_assets = assign.url or None
                asset_infos = [
                    AssetInfo(
                        name=a["name"],
                        size=a["size"],
                        mime=a.get("mime"),
                        url=a.get("url"),
                        page_url=page_url_for_assets,
                        href=f"/jobs/{job_id}/assets/{a['name']}",
                    )
                    for a in result.assets_saved
                ]
                job_result = JobResult(
                    job_id=job_id,
                    status=JobStatus.completed,
                    html_href=f"/jobs/{job_id}/page.html",
                    log_href=f"/jobs/{job_id}/log.txt",
                    assets=asset_infos,
                    assets_failed=result.assets_failed,
                    video_detection=getattr(result, "video_detection", {}) or {},
                    video_urls_seen=list(getattr(result, "video_urls_seen", []) or []),
                    iframe_srcs=list(getattr(result, "iframe_srcs", []) or []),
                    ytdlp_results=[
                        YtdlpResult(**r) for r in getattr(result, "ytdlp_results", []) or []
                    ],
                    visited_urls=list(getattr(result, "visited_urls", []) or []),
                )
                await self._send(
                    WorkerJobComplete(
                        job_id=job_id,
                        result=job_result,
                    )
                )
                # keep_session: hand the (now post-fetch) browser /
                # session over to the operator instead of tearing down.
                # Concretely:
                #   * flip is_fetch_owned=False so write actions are
                #     allowed via /sessions/{sid}/action,
                #   * stash the upload base + workdir on the state so
                #     POST /jobs/{id}/refresh can flush new assets and
                #     so session_end can rmtree the right directory,
                #   * seed uploaded_assets with the names the fetcher
                #     already shipped, so the next refresh only picks
                #     up assets captured during operator interaction,
                #   * skip the workdir rmtree + lane release (both run
                #     when the operator DELETEs the session instead).
                if keep_session and inspect_sid:
                    sess = self._sessions.get(inspect_sid)
                    if sess is not None:
                        sess.is_fetch_owned = False
                        sess.asset_upload_base = assign.asset_upload_base
                        sess.job_id = job_id
                        sess.workdir = workdir
                        # The network_log is already shared via
                        # _make_fetch_session_callbacks (same list
                        # object), so no transfer needed here.
                        try:
                            for p in Path(assets_dir).rglob("*"):
                                if p.is_file():
                                    sess.uploaded_assets.add(p.name)
                        except Exception:
                            pass
                        # Stderr only -- log_fp was closed a few lines
                        # above (post-fetch cleanup) and writing to it
                        # would raise ValueError("I/O operation on
                        # closed file") which propagates as
                        # "worker crashed: ValueError: ..." and kills
                        # the keepalive transition. The operator-facing
                        # log already got the fetch's own completion
                        # lines; this banner is for the worker stderr
                        # (visible in docker logs) only.
                        _logger.info(
                            f"[{job_id}]   ... keep_session: session "
                            f"{inspect_sid} is now interactive "
                            f"(use POST /jobs/{job_id}/refresh to "
                            f"flush new assets / refresh links)",
                        )
                else:
                    shutil.rmtree(workdir, ignore_errors=True)
            except Exception as e:
                try:
                    await self._send(
                        WorkerJobFailed(
                            job_id=job_id,
                            error=f"worker crashed: {type(e).__name__}: {e}",
                        )
                    )
                except Exception:
                    pass
            finally:
                # Restore the lane's default profile when we swapped
                # one in. Skipped for keep_session+use_profile because
                # the session is still using the operator profile via
                # the same Chrome; _teardown_session_state restores it
                # when the session actually ends.
                if (
                    _profile_swapped
                    and lane is not None
                    and not (keep_session and inspect_sid in self._sessions)
                ):
                    try:
                        await lane.restore_default_profile()
                    except Exception as e:
                        _logger.info(
                            f"[{job_id}] lane.restore_default_profile "
                            f"failed: {type(e).__name__}: {e}",
                        )
                # In keep_session mode the lane is held by the live
                # session -- it's released when the operator DELETEs
                # /sessions/{sid} (which calls _teardown_session_state).
                if (
                    lane is not None
                    and self.lane_pool is not None
                    and not (keep_session and inspect_sid in self._sessions)
                ):
                    self.lane_pool.release(lane)
                # For keep_session jobs the lane stays held by the
                # live session; keeping _in_flight incremented mirrors
                # that to the hub via the next WorkerHeartbeat, so the
                # scheduler doesn't see this worker as fully idle and
                # over-dispatch onto a lane that's actually pinned
                # (= the "no free lane in pool" cascade after burst
                # tests + keep_session). _in_flight is finally
                # decremented when the session ends in
                # _teardown_session_state() below.
                if not (keep_session and inspect_sid in self._sessions):
                    self._in_flight = max(0, self._in_flight - 1)

    async def _run_vision_agent_job(
        self,
        *,
        assign: HubAssignJob,
        lane,
        log,
        log_fp,
        log_path: Path,
        workdir: Path,
    ) -> None:
        """Drive the browser through CogAgent's observe -> /act -> exec loop.

        Lane management, log streaming, and the surrounding
        WorkerJobAccepted/Complete/Failed envelopes are handled by the
        caller (``_run_assigned_job``); this method is responsible for:

          1. Attaching nodriver to the lane's Chrome.
          2. Installing the tab-killer so popups don't escape.
          3. Navigating to ``assign.url``.
          4. Looping screenshot -> POST /act -> execute up to
             ``vision_max_steps`` times or until CogAgent emits END().
          5. Sending WorkerJobComplete with a JobResult.
        """
        import json as _json

        import nodriver as uc
        from nodriver import cdp as _cdp

        job_id = assign.job_id
        cogagent_url = os.environ.get(
            "COGAGENT_URL",
            "http://<gpu-host>:15083",
        ).rstrip("/")
        cogagent_timeout_s = float(
            os.environ.get("COGAGENT_REQUEST_TIMEOUT_S", "120"),
        )
        max_steps = max(
            1,
            int(getattr(assign.options, "vision_max_steps", 30) or 30),
        )
        goal = (assign.options.goal or "").strip()
        if not goal:
            raise RuntimeError("vision-agent mode requires JobOptions.goal (natural-language task)")

        # ---- JP/CN -> EN goal translation -------------------------
        # CogAgent has known weakness with Japanese task descriptions
        # (it confabulates them into Chinese Google-Images-style tasks
        # -- see job b6472e7c0da6). Translate via the chat LLM once
        # up front. The same mechanism already runs in
        # _handle_session_agent for page.agent() calls; this brings
        # the Simple-mode (job-direct) path in line.
        agent_llm_url = os.environ.get(
            "AGENT_LLM_URL",
            "http://<gpu-host>:15082",
        ).rstrip("/")
        agent_llm_model = os.environ.get(
            "AGENT_MODEL_NAME",
            "qwen2.5-vl-72b",
        )
        if _looks_non_english(goal):
            log(f"  ... goal looks non-english; translating: {goal[:80]!r}")
            translated_goal = await _translate_to_english(
                goal,
                agent_llm_url=agent_llm_url,
                model_name=agent_llm_model,
                timeout_s=30.0,
                log=log,
            )
            if translated_goal != goal:
                log(f"  ... translated -> {translated_goal!r}")
                goal = translated_goal

        log(f"  ... vision-agent: cogagent_url={cogagent_url} max_steps={max_steps}")

        # ---- attach to chrome ------------------------------------------
        if lane is None:
            raise RuntimeError("vision-agent requires a lane-pool worker (no lane assigned)")
        chrome = await uc.start(
            host="localhost",
            port=lane.chrome_port,
            browser_executable_path=sys.executable,
        )
        # Rewrite the websocket URL when nodriver picked up a bogus
        # internal hostname (same dance as _handle_session_start).
        try:
            from urllib.parse import urlparse as _urlparse

            parsed = _urlparse(chrome.websocket_url)
            if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
                new_netloc = f"localhost:{lane.chrome_port}"
                chrome.websocket_url = chrome.websocket_url.replace(
                    parsed.netloc,
                    new_netloc,
                    1,
                )
        except Exception:
            pass

        try:
            # Single-tab + tab-killer so popups can't redirect the
            # agent's attention away from the working page.
            try:
                await browser_ops.force_single_tab(chrome, log=log)
            except Exception as e:
                log(f"  ... force_single_tab warned: {type(e).__name__}: {e}")

            tab = await chrome.get("about:blank", new_tab=False)
            # install_tab_killer removed in multi-tab refactor (Phase 1).
            # See sessions_start path for the explanation -- the
            # vision-agent path was the only other call site, removed
            # symmetrically. Per-host ``popup_policy`` ("kill" / "follow")
            # is preserved on JobInfo for the upcoming Phase 2 operator
            # API to consult; meanwhile JS-level same-origin
            # ``target="_blank"`` rewriting still collapses most
            # navigation into the main tab.
            assign_popup_policy = getattr(assign, "popup_policy", "kill") or "kill"
            log(f"  ... popup_policy={assign_popup_policy} (kill enforcement removed)")

            # ---- passive asset capture --------------------------------
            # Without this the loop only ever does what the model says,
            # which means clicks that pop a video URL into the main
            # tab (popup_policy=follow) load the MP4 but never save
            # it. Mirror the session_start setup: write each
            # image/video/audio response to ``assets_dir`` and
            # POST it to /jobs/{id}/assets so the admin gallery
            # picks it up.
            assets_dir = workdir / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            captured_urls: set = set()
            captured_paths: set = set()
            asset_count = 0
            # Network log for the fetch-mode path: tracked here and
            # transferred to SessionState if keep_session is active.
            fetch_network_log: list = []

            async def _on_asset_saved(path: Path, info: dict) -> None:
                nonlocal asset_count
                if path.name in captured_paths:
                    return
                captured_paths.add(path.name)
                try:
                    with open(path, "rb") as f:
                        files = {
                            "file": (
                                path.name,
                                f,
                                info.get("mime") or "application/octet-stream",
                            )
                        }
                        data = {
                            "asset_name": path.name,
                        }
                        if info.get("url"):
                            data["source_url"] = info["url"]
                        if info.get("mime"):
                            data["mime"] = info["mime"]
                        if info.get("document_url"):
                            data["page_url"] = info["document_url"]
                        if self.worker_secret:
                            data["secret"] = self.worker_secret
                        r = await self._http.post(
                            assign.asset_upload_base,
                            files=files,
                            data=data,
                        )
                        r.raise_for_status()
                    asset_count += 1
                    log(
                        f"  📦 asset #{asset_count}: {path.name} "
                        f"({info.get('mime') or '?'}, "
                        f"{path.stat().st_size} bytes)"
                    )
                except Exception as e:
                    log(f"  !! asset upload failed for {path.name}: {type(e).__name__}: {e}")

            min_asset = int(getattr(assign.options, "min_asset_size_bytes", 0) or 0)
            try:
                await browser_ops.install_session_asset_capture(
                    tab,
                    assets_dir,
                    on_saved=_on_asset_saved,
                    log=log,
                    seen_urls=captured_urls,
                    min_asset_size_bytes=min_asset,
                    # vision-agent's popup-follow flow lands the main
                    # tab directly on naked video URLs (e.g. twitter's
                    # video.twimg.com/.../avc1/...). Without this the
                    # default image+audio filter would silently drop
                    # the response we explicitly clicked to get.
                    # min_asset_size_bytes (default 10KB) keeps the
                    # MSE/DASH fragment flood under control.
                    extra_mime_prefixes=("video/",),
                    network_log=fetch_network_log,
                    # ALWAYS on (see the matching session_start call and
                    # core/fetcher): vision-agent flows frequently land
                    # on cross-origin iframe players whose HLS stream is
                    # invisible to the parent CDP target without deep-
                    # trace. Gating this on download_video meant a video
                    # the agent visibly opened could be captured as zero
                    # bytes. The attach overhead is acceptable for a
                    # video-archiving tool.
                    enable_iframe_deep_trace=True,
                )
                log(f"  ... asset capture armed (min_size={min_asset} bytes, +video/*)")
            except Exception as e:
                log(f"  ... install_session_asset_capture warned: {type(e).__name__}: {e}")

            # ---- initial navigation -----------------------------------
            if assign.url and assign.url != "about:blank":
                log(f"  ... navigating to {assign.url}")
                try:
                    await tab.send(_cdp.page.navigate(assign.url))
                except Exception as e:
                    raise RuntimeError(f"initial navigation failed: {type(e).__name__}: {e}")
                # Match fetch's NAVIGATION_SETTLE_S so the first
                # screenshot doesn't catch a half-loaded paint.
                await asyncio.sleep(float(os.environ.get("AGENT_NAVIGATION_SETTLE_S", "3.0")))

            # ---- viewport probe (CSS pixels) --------------------------
            # We need to tell CogAgent the screenshot dimensions so its
            # 0-1000 box gets de-normalised to the same pixel grid CDP
            # mouse events live in. window.innerWidth/Height are the
            # right CSS-pixel values; the PNG bytes are at the same
            # resolution (CDP Page.captureScreenshot in default mode).
            try:
                vp_str = await tab.evaluate(
                    "JSON.stringify({w: window.innerWidth, h: window.innerHeight})"
                )
                vp = _json.loads(vp_str or "{}")
                viewport_w = int(vp.get("w") or 1280)
                viewport_h = int(vp.get("h") or 720)
            except Exception:
                viewport_w, viewport_h = 1280, 720
            log(f"  ... viewport probe: {viewport_w}x{viewport_h}")

            visited_urls: list[str] = []
            history_lines: list[str] = []  # for CogAgent's "History steps:"
            completed = False
            end_summary: str | None = None
            last_action: dict | None = None
            steps_taken = 0
            # Top-level video URL downloader -- shared with the
            # session-mode page.agent() path via _make_video_downloader.
            # Detects URL navigations that land on .mp4/.webm/... or
            # .m3u8/.mpd and fetches the file (httpx for direct,
            # yt-dlp for streams). Tasks live in pending_downloads so
            # we can drain() before tearing down the browser.
            maybe_download_video, drain_video_downloads = _make_video_downloader(
                assets_dir=assets_dir,
                min_asset_size=min_asset,
                on_saved=_on_asset_saved,
                log=log,
                job_id_for_logs=assign.job_id,
                # Top-level page URL referer fallback for cross-origin
                # iframe player streams (see _make_video_downloader).
                page_url_provider=lambda: getattr(assign, "url", None),
            )

            # ---- main loop --------------------------------------------
            async with httpx.AsyncClient(timeout=cogagent_timeout_s) as client:
                for step in range(1, max_steps + 1):
                    # observe: URL + screenshot
                    try:
                        current_url = await tab.evaluate("document.location.href")
                    except Exception:
                        current_url = ""
                    if current_url:
                        canon = browser_ops.canon_url(current_url)
                        if not visited_urls or visited_urls[-1] != canon:
                            prev_url = visited_urls[-1] if visited_urls else ""
                            visited_urls.append(canon)
                            # If we just landed on what looks like a
                            # direct video file (gallery popup-follow
                            # flow), download it ourselves --
                            # Chrome's media player doesn't fire the
                            # CDP response event we'd otherwise catch.
                            maybe_download_video(current_url, prev_url)

                    try:
                        png_b64 = await tab.send(
                            _cdp.page.capture_screenshot(format_="png"),
                        )
                    except Exception as e:
                        raise RuntimeError(f"capture_screenshot failed: {type(e).__name__}: {e}")

                    payload = {
                        "task": goal,
                        "image_b64": png_b64,
                        "image_width": viewport_w,
                        "image_height": viewport_h,
                        # CogAgent expects history newest-LAST. Keep the
                        # last 20 -- the model was trained with much
                        # longer windows but our prompt budget is small.
                        "history": history_lines[-20:],
                        "platform": "WIN",
                        "answer_format": "Action-Operation",
                        "max_new_tokens": 512,
                        "temperature": 0.0,
                    }
                    log(f"  [{step}/{max_steps}] @ {current_url[:80]} -> {cogagent_url}/act")
                    try:
                        r = await client.post(f"{cogagent_url}/act", json=payload)
                        r.raise_for_status()
                        reply = r.json()
                    except Exception as e:
                        raise RuntimeError(
                            f"cogagent /act failed at step {step}: {type(e).__name__}: {e}"
                        )

                    action = reply.get("action") or {}
                    raw = reply.get("raw") or ""
                    kind = action.get("kind") or "unknown"
                    last_action = action
                    steps_taken = step

                    # Construct a CogAgent-shaped history line. We
                    # ship the model's own Grounded Operation string
                    # if it parsed cleanly, else fall back to "kind".
                    op_summary = raw.splitlines()[-1] if raw else kind
                    desc = action.get("action_text") or ""
                    history_lines.append(f"{op_summary}\t{desc}" if desc else op_summary)

                    log(
                        f"  [{step}] -> {kind}"
                        + (f" box={action['box']}" if action.get("box") else "")
                        + (f" text={action.get('text')!r}" if action.get("text") else "")
                        + (f" key={action.get('key')!r}" if action.get("key") else "")
                    )

                    if kind == "end":
                        completed = True
                        end_summary = action.get("action_text") or "task complete"
                        log(f"  [vagent] END: {end_summary}")
                        break
                    if kind == "unknown":
                        log(f"  [vagent] unknown action; raw: {raw[:200]}")
                        break

                    try:
                        outcome = await browser_ops.execute_vision_action(
                            tab,
                            action,
                            log,
                            viewport_width=viewport_w,
                            viewport_height=viewport_h,
                        )
                    except Exception as e:
                        outcome = f"ERR: {type(e).__name__}: {e}"
                        log(f"  [vagent] execute crashed: {outcome}")
                    log(f"  [vagent] outcome: {outcome}")

            # ---- finalise ---------------------------------------------
            # 1) Wait for any pending video downloads (httpx single
            #    file or yt-dlp HLS) to finish. yt-dlp can run for
            #    minutes on a long HLS source; without this wait the
            #    chrome.stop() in our finally block tears down the
            #    browser mid-download and we'd ship a half-merged
            #    file. Tasks have their own internal timeouts (httpx
            #    120s, yt-dlp VISION_YTDLP_TIMEOUT_S=1800s default)
            #    so this gather is naturally bounded.
            await drain_video_downloads()

            # 2) Give any in-flight passive-capture responses time to
            #    land in the asset capture before we tear down the
            #    browser. Catches "the last click triggered a small
            #    image / mp4 chunk that hadn't finished by the time
            #    CogAgent said END()". 5s covers the typical CDN
            #    latency; bump via env if needed.
            final_drain = float(
                os.environ.get(
                    "VISION_FINAL_DRAIN_S",
                    "5.0",
                )
            )
            if final_drain > 0:
                log(
                    f"  ... draining network for {final_drain}s "
                    f"(catch in-flight responses from the last action)"
                )
                await asyncio.sleep(final_drain)

            if completed:
                log(f"  ✓ vision-agent completed in {steps_taken} step(s): {end_summary}")
            elif steps_taken >= max_steps:
                log(
                    f"  ⚠ vision-agent reached max_steps={max_steps} "
                    f"without END(); last action={last_action}"
                )
            else:
                log(
                    f"  ⚠ vision-agent stopped early at step {steps_taken} "
                    f"(last action={last_action})"
                )

            log_fp.close()
            await self._upload_log(assign, log_path)

            job_result = JobResult(
                job_id=job_id,
                status=JobStatus.completed if completed else JobStatus.failed,
                html_href=None,
                log_href=f"/jobs/{job_id}/log.txt",
                assets=[],
                assets_failed=0,
                video_detection={},
                video_urls_seen=[],
                iframe_srcs=[],
                ytdlp_results=[],
                visited_urls=visited_urls,
                error=(
                    None
                    if completed
                    else f"vision-agent stopped without END() after {steps_taken} step(s)"
                ),
            )
            if completed:
                await self._send(
                    WorkerJobComplete(
                        job_id=job_id,
                        result=job_result,
                    )
                )
            else:
                # Treat "ran out of steps" as a soft failure -- the
                # operator can resubmit with a higher
                # vision_max_steps, or look at the log + visited_urls
                # to understand where the model got stuck.
                await self._send(
                    WorkerJobFailed(
                        job_id=job_id,
                        error=job_result.error or "vision-agent did not complete",
                    )
                )
            shutil.rmtree(workdir, ignore_errors=True)
        finally:
            # Lane stays acquired -- _run_assigned_job's finally releases
            # it. We only close the browser handle here so a crash
            # mid-loop doesn't leak the CDP connection.
            try:
                await chrome.stop()
            except Exception:
                pass

    def _build_fetch_options(
        self,
        url: str,
        opts: JobOptions,
        assets_dir: Path,
        log,
        lane=None,
        cookies_to_install: list[dict] | None = None,
        on_complete_dump_cookies=None,
        on_browser_ready=None,
        on_browser_closing=None,
        on_after_navigate=None,
        network_log: list | None = None,
    ) -> FetchOptions:
        # Server-side normalization (Swagger 'string' guard etc)
        def _norm(v):
            if v is None or not isinstance(v, str):
                return v
            s = v.strip()
            return None if (not s or s.lower() == "string") else s

        attach = _norm(opts.attach)
        clone_profile = _norm(opts.clone_chrome_profile)

        attach_host: str | None = None
        attach_port: int | None = None
        user_data_dir: Path | None = None
        # Lane-pool mode wins: each job uses its dedicated Chrome.
        if lane is not None:
            attach_host = "localhost"
            attach_port = lane.chrome_port
            log(
                f"  ... lane #{lane.lane_idx} acquired  "
                f"chrome=localhost:{lane.chrome_port}  "
                f"noVNC={lane.novnc_url}"
            )
        elif attach:
            attach_host, attach_port = parse_attach(attach)
        elif clone_profile:
            user_data_dir = clone_chrome_profile(clone_profile, log=log)
        elif self.chrome_host and self.chrome_port:
            attach_host = self.chrome_host
            attach_port = self.chrome_port
            log(f"  ... using worker's pre-running Chrome at {attach_host}:{attach_port}")

        return FetchOptions(
            url=url,
            wait_seconds=opts.wait_seconds,
            settle_seconds=opts.settle_seconds,
            idle_seconds=opts.idle_seconds,
            max_wait_seconds=opts.max_wait_seconds,
            scroll=opts.scroll,
            scroll_step=opts.scroll_step,
            scroll_max=opts.scroll_max,
            scroll_early_after=opts.scroll_early_after,
            post_click_seconds=opts.post_click_seconds,
            download_video=bool(getattr(opts, "download_video", False)),
            cookies_from=_norm(opts.cookies_from),
            referer=_norm(opts.referer),
            user_data_dir=user_data_dir,
            attach_host=attach_host,
            attach_port=attach_port,
            # In lane-pool mode the Chrome is dedicated; reuse its tab.
            attach_new_tab=(lane is None),
            # keep_open=True only in keep_session mode. The worker
            # then transitions the fetch-owned session into an
            # interactive one (is_fetch_owned=False) and leaves the
            # browser running so the operator can drive it via noVNC.
            keep_open=bool(getattr(opts, "keep_session", False)),
            headless=opts.headless,
            assets_dir=assets_dir if opts.capture_assets else None,
            log=log,
            cookies_to_install=cookies_to_install,
            on_complete_dump_cookies=on_complete_dump_cookies,
            on_browser_ready=on_browser_ready,
            on_browser_closing=on_browser_closing,
            on_after_navigate=on_after_navigate,
            # Hub-managed min-size filter (Settings → "Asset capture").
            min_asset_size_bytes=int(getattr(opts, "min_asset_size_bytes", 0) or 0),
            network_log=network_log,
        )

    def _make_fetch_session_callbacks(
        self,
        session_id: str,
        lane,
        assets_dir: Path,
        log,
        *,
        job_id: str | None = None,
        keep_session: bool = False,
        network_log: list | None = None,
    ):
        """Build (on_browser_ready, on_browser_closing) callbacks that
        register a SessionState for the duration of a fetch.

        Sharing the lane's existing tab is fine: nodriver allows
        multiple CDP clients per Chrome, and our session_action
        handlers only do read-only CDP calls when ``is_fetch_owned``.
        The session is removed in the on-closing callback BEFORE
        ``browser.stop()`` so subsequent /sessions/{id}/* requests
        get a clean 404 instead of operating on a torn-down tab.

        keep_session=True: the on_closing skips the unregister so the
        session lives past fetch return. The fetch job handler is
        responsible for then flipping is_fetch_owned=False and seeding
        the upload metadata so /jobs/{job_id}/refresh can flush new
        assets captured during operator interaction.

        network_log: shared list that the CDP asset-capture listener
        populates. Wired into the SessionState so ``kind="network"``
        session actions return live data while the fetch is running.
        """

        async def _on_ready(browser, tab) -> None:
            try:
                state = SessionState(
                    session_id=session_id,
                    lane=lane,
                    assets_dir=assets_dir,
                    is_fetch_owned=True,
                    job_id=job_id,
                )
                # Share the caller's network_log list so the Network
                # tab can read live data while the fetch is running.
                if network_log is not None:
                    state.network_log = network_log
                state.browser = browser
                state.tab = tab
                self._sessions[session_id] = state
                # network_log is populated by the fetcher's own CDP
                # handlers (core.fetcher on_response / on_finished).
                # No separate install_session_asset_capture needed --
                # the shared list reference means the Network tab gets
                # entries as the fetcher processes each response.
                log(f"  ... registered fetch-owned session {session_id} (read-only inspection)")
            except Exception as e:
                log(f"  !! could not register fetch session ({type(e).__name__}: {e})")

        async def _on_closing() -> None:
            # keep_session: the fetch finishes but the session lives
            # on. The fetch job handler will mutate the state in place
            # (is_fetch_owned=False, asset_upload_base=...) right after
            # this callback returns. Don't pop or browser.stop() will
            # run on an empty self._sessions entry next teardown.
            if keep_session:
                log(f"  ... keeping fetch session {session_id} alive (keep_session=True)")
                return
            try:
                gone = self._sessions.pop(session_id, None)
                if gone is not None:
                    log(f"  ... unregistered fetch-owned session {session_id}")
            except Exception as e:
                log(f"  !! could not unregister fetch session ({type(e).__name__}: {e})")

        return _on_ready, _on_closing

    def _make_cookie_save_callback(self, assign, save_host: str, log):
        """Return an async callable suitable for FetchOptions.on_complete_dump_cookies.

        The callback receives the post-fetch cookie jar (list of dicts),
        filters it down to cookies whose domain matches ``save_host``
        (so we don't store cross-site tracker noise under this host),
        and PUTs the resulting list to the hub's host registry. The
        hub's existing /hosts/{host} endpoint handles upsert,
        timestamp updates, and projection at injection time -- we
        just hand it the raw jar slice.
        """
        # Derive the hub base URL from asset_upload_base, which is the
        # only absolute hub URL we already know on the worker. Shape:
        # http://hub:8000/jobs/{id}/assets  ->  http://hub:8000
        # Splitting on "/jobs/" is stable across the /api/ rename
        # (was "/api/" before) and keeps working if assets ever live
        # behind a sub-path proxy.
        try:
            base = assign.asset_upload_base.split("/jobs/", 1)[0]
        except Exception:
            base = None

        async def _save_cb(jar: list[dict]) -> None:
            if not base:
                log("  ... cookie save skipped: cannot derive hub base url")
                return
            host_norm = (save_host or "").strip().lower()
            if host_norm.startswith("www."):
                host_norm = host_norm[4:]
            if not host_norm:
                log("  ... cookie save skipped: no host")
                return
            # Host-filter: cookies whose domain matches the registry
            # host (exact, suffix, or parent). Without this we'd store
            # 100+ third-party tracker cookies in every record.
            filtered: list[dict] = []
            for c in jar or []:
                if not isinstance(c, dict):
                    continue
                dom = (c.get("domain") or "").lower().lstrip(".")
                if not dom:
                    continue
                if dom.startswith("www."):
                    dom = dom[4:]
                if (
                    dom == host_norm
                    or dom.endswith("." + host_norm)
                    or host_norm.endswith("." + dom)
                ):
                    filtered.append(c)

            # Always upsert -- the operator wants every visited host
            # to surface in the Hosts tab, even sites that set no
            # first-party cookies (so they can curate manually later).
            # Safety net: when the new dump has ZERO matching cookies
            # but the existing record has some, preserve them so a
            # casual revisit doesn't wipe a saved login.
            url = f"{base}/hosts/{host_norm}"
            existing_notes = None
            existing_cookies: list[dict] = []
            existed = False
            try:
                async with httpx.AsyncClient(timeout=10.0) as cli:
                    g = await cli.get(url)
                    if g.status_code == 200:
                        rec = g.json() or {}
                        existing_notes = rec.get("notes")
                        existing_cookies = list(rec.get("cookies") or [])
                        existed = True
            except Exception:
                pass

            if filtered:
                cookies_to_save = filtered
                kind_label = (
                    f"replaced ({len(filtered)} cookie(s))"
                    if existed
                    else f"created ({len(filtered)} cookie(s))"
                )
            elif existing_cookies:
                # Existing record + no new matches → keep what we had.
                cookies_to_save = existing_cookies
                kind_label = (
                    f"refreshed timestamp only "
                    f"(kept {len(existing_cookies)} existing cookie(s); "
                    f"none matched in this fetch)"
                )
            else:
                # Brand-new host with no matching cookies → empty
                # marker entry so the Hosts tab shows "I visited
                # this" without forcing the operator to do anything.
                cookies_to_save = []
                kind_label = "marker created (0 cookie(s) matched this host)"

            notes = existing_notes
            if not notes:
                notes = f"auto-saved by fetch job {assign.job_id}"

            try:
                async with httpx.AsyncClient(timeout=15.0) as cli:
                    r = await cli.put(
                        url,
                        json={
                            "cookies": cookies_to_save,
                            "notes": notes,
                        },
                    )
                if r.status_code in (200, 201):
                    log(
                        f"  ... cookie save: PUT /hosts/{host_norm} "
                        f"-- {kind_label} [http {r.status_code}]"
                    )
                else:
                    log(f"  !! cookie save failed: http {r.status_code} {r.text[:200]}")
            except Exception as e:
                log(f"  !! cookie save crashed: {type(e).__name__}: {e}")

        return _save_cb

    # ----------------------------------------------------------- uploads

    async def _upload_files(
        self,
        assign: HubAssignJob,
        workdir: Path,
        fetch_result,
    ) -> None:
        """Upload page.html, log.txt, and all asset files to the hub."""
        await self._upload_log(assign, workdir / "log.txt")
        await self._upload_special(assign, workdir / "page.html", "page.html")
        # Page URL = the URL the user told us to fetch. Every captured
        # asset is "on" this page from the gallery's point of view.
        page_url = assign.url or None
        # Assets
        for a in fetch_result.assets_saved:
            path = Path(a["path"])
            if path.exists():
                await self._upload_asset(
                    assign,
                    path,
                    a["name"],
                    source_url=a.get("url"),
                    mime=a.get("mime"),
                    page_url=page_url,
                )
        # yt-dlp may have produced extra files in assets_dir not in
        # assets_saved (since fetcher tracks only its own captures, not
        # yt-dlp downloads). Walk the dir and upload anything we missed.
        # For these we don't have a per-asset source URL, but we can
        # still attach the page_url so the gallery knows where they
        # came from.
        assets_dir = workdir / "assets"
        if assets_dir.exists():
            known = {a["name"] for a in fetch_result.assets_saved}
            for p in assets_dir.iterdir():
                if p.is_file() and p.name not in known:
                    await self._upload_asset(
                        assign,
                        p,
                        p.name,
                        page_url=page_url,
                    )

    async def _upload_one_session_asset(
        self,
        state: SessionState,
        path: Path,
        *,
        mime: str | None = None,
        source_url: str | None = None,
        page_url: str | None = None,
        timeout: float | None = None,
        asset_name: str | None = None,
    ) -> bool:
        """Upload one file from a session's assets dir to the parent
        job's /assets endpoint. Used by:

          - the passive CDP listener installed at session_start (for
            browser-side image/video/audio responses), and
          - the ``download_video`` action handler (for yt-dlp output).

        Dedupes via ``state.uploaded_assets`` so the same path doesn't
        get re-shipped. Best-effort: errors are logged to stderr but
        don't raise -- a failed asset upload shouldn't kill the
        whole session.

        ``timeout`` overrides the AsyncClient's default 60s ceiling.
        Pass a generous value for big files (yt-dlp output, etc.):
        ``self._http`` is shared across the worker so the default
        can't be raised globally without affecting smaller, latency-
        sensitive calls like screenshot or state polling.

        Returns True if the upload was attempted-and-succeeded, False
        if it was skipped (no upload base, already uploaded) or failed.
        """
        if state.asset_upload_base is None:
            return False
        name = asset_name or path.name
        if name in state.uploaded_assets:
            return False
        try:
            with open(path, "rb") as f:
                files = {
                    "file": (
                        name,
                        f,
                        mime or "application/octet-stream",
                    )
                }
                data: dict[str, str] = {"asset_name": name}
                if source_url:
                    data["source_url"] = source_url
                if mime:
                    data["mime"] = mime
                if page_url:
                    data["page_url"] = page_url
                if self.worker_secret:
                    data["secret"] = self.worker_secret
                post_kwargs: dict[str, Any] = {
                    "files": files,
                    "data": data,
                }
                if timeout is not None:
                    post_kwargs["timeout"] = float(timeout)
                r = await self._http.post(
                    state.asset_upload_base,
                    **post_kwargs,
                )
                r.raise_for_status()
            state.uploaded_assets.add(name)
            return True
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] session asset upload failed: {name}: {e}",
            )
            return False

    async def _upload_asset(
        self,
        assign: HubAssignJob,
        path: Path,
        name: str,
        *,
        source_url: str | None = None,
        mime: str | None = None,
        page_url: str | None = None,
    ) -> None:
        url = assign.asset_upload_base
        try:
            with open(path, "rb") as f:
                files = {
                    "file": (
                        name,
                        f,
                        mime or "application/octet-stream",
                    )
                }
                data: dict[str, str] = {"asset_name": name}
                if source_url:
                    data["source_url"] = source_url
                if mime:
                    data["mime"] = mime
                if page_url:
                    data["page_url"] = page_url
                if self.worker_secret:
                    data["secret"] = self.worker_secret
                r = await self._http.post(url, files=files, data=data)
                r.raise_for_status()
        except Exception as e:
            await self._send(
                WorkerJobLog(
                    job_id=assign.job_id,
                    line=f"  !! asset upload failed: {name}: {e}",
                )
            )

    async def _upload_special(self, assign: HubAssignJob, path: Path, kind: str) -> None:
        # /jobs/{id}/files/{kind} replaces the asset_upload_base path.
        url = assign.asset_upload_base.rsplit("/assets", 1)[0] + f"/files/{kind}"
        try:
            with open(path, "rb") as f:
                files = {"file": (kind, f, "application/octet-stream")}
                data = {}
                if self.worker_secret:
                    data["secret"] = self.worker_secret
                r = await self._http.post(url, files=files, data=data)
                r.raise_for_status()
        except Exception as e:
            await self._send(
                WorkerJobLog(
                    job_id=assign.job_id,
                    line=f"  !! {kind} upload failed: {e}",
                )
            )

    async def _upload_log(self, assign: HubAssignJob, path: Path) -> None:
        if path.exists():
            await self._upload_special(assign, path, "log.txt")
