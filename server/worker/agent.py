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
from typing import Any, Optional
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
    JOB_PROGRESS_MARKER,
    WorkerJobLog,
    WorkerJobProgress,
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


_logger = logging.getLogger(__name__)


async def _get_browser_user_agent(browser) -> str | None:
    """Ask Chrome for its real User-Agent via CDP.  Returns None on failure."""
    try:
        from nodriver import cdp as _cdp
        ver = await browser.send(_cdp.browser.get_version())
        return ver.user_agent
    except Exception:
        return None




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


# ---------------------------------------------------------------------------
# Vendor-neutral video-discovery heuristics used by page.download_video().
# Deliberately reference NO specific hostnames -- the patterns target URL
# shape (path keywords + opaque-token query params) and DOM structure so
# they generalise across video-host sites paprika has crawled.
# ---------------------------------------------------------------------------








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


# --- download-progress parsing for the Live panel progress widget --------
# Recognise the common live-progress shapes emitted by yt-dlp, ffmpeg, and
# our parallel-HLS adapter, and normalise them into a small dict the admin
# Live panel renders as a per-download progress bar.  Returns None for any
# line that isn't a progress update.
_DLP_DEST_RE = _re.compile(r"\[download\]\s+Destination:\s+(.+?)\s*$")
_DLP_PCT_RE = _re.compile(r"\[download\]\s+([0-9.]+)%")
_DLP_SPEED_RE = _re.compile(r"\bat\s+([0-9.]+\s*[KMGT]?i?B/s)")
_DLP_ETA_RE = _re.compile(r"\bETA\s+([0-9:]+)")
_DLP_PHLS_SEG_RE = _re.compile(r"\[parallel-hls\]\s+([0-9]+)/([0-9]+)\s+segments")
_DLP_FF_TIME_RE = _re.compile(r"\btime=\s*([0-9:.]+)")
_DLP_FF_SPEED_RE = _re.compile(r"\bspeed=\s*([0-9.]+x)")
_DLP_FF_SIZE_RE = _re.compile(r"\bsize=\s*([0-9.]+\s*[KMGT]?i?B)")


def _parse_dl_progress(line: str) -> dict | None:
    """Normalise one yt-dlp/ffmpeg/parallel-hls progress line into
    ``{state, pct?, speed?, eta?, size?, time?, detail?, label?}`` or
    None.  ``state`` is one of: start | downloading | muxing | done."""
    s = line
    # yt-dlp: "[download] Destination: /path/file.mp4"  (download begins)
    m = _DLP_DEST_RE.search(s)
    if m:
        name = m.group(1).strip().replace("\\", "/").rsplit("/", 1)[-1]
        return {"state": "start", "label": name}
    # parallel-hls adapter: "[parallel-hls] 400/950 segments"
    m = _DLP_PHLS_SEG_RE.search(s)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        pct = round(done * 100.0 / total, 1) if total else None
        return {"state": "downloading", "pct": pct, "detail": f"{done}/{total} segs"}
    # yt-dlp: "[download]  45.2% of 1.20GiB at 5.00MiB/s ETA 00:30"
    m = _DLP_PCT_RE.search(s)
    if m:
        pct = float(m.group(1))
        out: dict = {"state": "done" if pct >= 100.0 else "downloading", "pct": pct}
        sp = _DLP_SPEED_RE.search(s)
        if sp:
            out["speed"] = sp.group(1).replace(" ", "")
        eta = _DLP_ETA_RE.search(s)
        if eta:
            out["eta"] = eta.group(1)
        return out
    # ffmpeg: "frame= 6896 ... size=17920KiB time=00:03:49.99 ... speed=1.02x"
    # (HLS muxing -- no overall %, so an indeterminate "muxing" state)
    if s.startswith("frame=") or ("time=" in s and "speed=" in s):
        t = _DLP_FF_TIME_RE.search(s)
        sp = _DLP_FF_SPEED_RE.search(s)
        if t or sp:
            out = {"state": "muxing"}
            if t:
                out["time"] = t.group(1)
            if sp:
                out["speed"] = sp.group(1)
            sz = _DLP_FF_SIZE_RE.search(s)
            if sz:
                out["size"] = sz.group(1).replace(" ", "")
            return out
    if "[parallel-hls] muxing" in s:
        return {"state": "muxing"}
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
    user_agent: str | None = None,  # real Chrome UA from cdp.browser.get_version()
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

    _fallback_ua = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    )

    async def _download_direct(target_url: str, referer: str) -> None:
        base_headers = {
            "User-Agent": user_agent or _fallback_ua,
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
        from core.fetcher import run_ytdlp, _hls_is_live

        # Passive sniffer skips live HLS streams.  Many AV preview
        # sites (7mmtv -> saawsedge, similar) deliver short looping
        # CMAF/HLS live previews that are NOT the actual video the
        # operator wants.  If we record one, yt-dlp adds
        # --hls-use-mpegts so the file is a TS stream saved with a
        # .mp4 extension -- not playable in browsers, confusing in
        # the gallery, and 30 s of irrelevant preview footage.
        # User-initiated page.download_video() takes the regular
        # path (action handler in session_actions/handlers/media.py),
        # which still allows live recording when explicitly asked.
        try:
            if _hls_is_live(target_url, referer) is True:
                _both(
                    f"  ⏭ skipping live HLS preview "
                    f"(passive sniffer, not a real video): {target_url[:90]}"
                )
                downloaded_urls.add(target_url)
                return
        except Exception:
            pass

        # 3600s (1h) default.  The parallel HLS downloader (adapter)
        # is ~21x realtime so most videos finish in minutes, but a
        # genuinely huge file behind a slow CDN needs headroom; the
        # old 1800s cut a 79-min stream off at 30 min when the
        # single-connection ffmpeg path crawled at 1x.  Env-overridable.
        ytdlp_timeout = int(
            os.environ.get(
                "VISION_YTDLP_TIMEOUT_S",
                "3600",
            )
        )
        log(f"  🎬 detected HLS/DASH URL, running yt-dlp (timeout={ytdlp_timeout}s)")
        before = {p.name for p in assets_dir.iterdir() if p.is_file()}
        # Mark the URL as actively progressing before yt-dlp starts
        # so drain() doesn't immediately abandon it during the
        # subprocess spawn / first-byte window.
        last_progress[target_url] = time.time()

        _loop = asyncio.get_running_loop()

        # Per-download identity for the Live panel progress widget.  The
        # key is stable for this download's lifetime; the label starts as
        # the URL basename and upgrades to the real output filename once
        # yt-dlp logs its "Destination:" line.
        _dl_key = target_url
        _dl_label = [
            (target_url.split("?", 1)[0].rsplit("/", 1)[-1] or target_url)[:64]
        ]
        # monotonic time of the last throttled progress marker (see below)
        _dl_last_marker = [0.0]

        def _ytdlp_log(line: str) -> None:
            # Runs in asyncio.to_thread (a plain OS thread), so we
            # cannot call log()/_both() directly -- they use
            # ensure_future which requires the calling thread to own
            # the event loop.  call_soon_threadsafe is the correct
            # cross-thread bridge.
            last_progress[target_url] = time.time()
            _logger.info(f"[{job_id_for_logs} yt-dlp] {line}")
            # ffmpeg/yt-dlp emit a torrent of low-level demuxer chatter
            # (per-frame progress, per-segment "Opening ..." / "[hls]
            # Skip ..." / "[https @ 0x..]" lines).  On a live HLS this
            # never stops, drowning the LiveLog WS.  Keep ALL of that in
            # the worker container log only; surface ONLY the
            # operator-meaningful lines -- our own [parallel-hls] /
            # [ffmpeg-direct] status, download %, completion, errors --
            # to the Live panel via _both.
            _stripped = line.lstrip()
            # Per-download progress widget.  Parse live progress and emit
            # an EPHEMERAL marker the hub broadcasts (but never persists)
            # and the Live panel renders as a progress bar.  The
            # %/segment/muxing updates are widget-only -- returning here
            # keeps them out of the scroll log -- while the "Destination:"
            # start line falls through so the operator still sees which
            # file began downloading.
            if job_log is not None:
                _prog = _parse_dl_progress(_stripped)
                if _prog is not None:
                    if _prog.get("label"):
                        _dl_label[0] = _prog["label"]
                    _st = _prog.get("state")
                    # Throttle the high-frequency downloading/muxing markers
                    # to ~1/s.  yt-dlp can emit 20+ progress lines/sec on a
                    # direct file; one WorkerJobLog per line floods the hub
                    # WS (observed destabilising the worker connection).
                    # start/done always pass through so the bar appears and
                    # resolves promptly.
                    _now = time.monotonic()
                    if (_st not in ("downloading", "muxing")
                            or _now - _dl_last_marker[0] >= 1.0):
                        _dl_last_marker[0] = _now
                        _payload = {"key": _dl_key, "label": _dl_label[0]}
                        _payload.update(_prog)
                        try:
                            _loop.call_soon_threadsafe(
                                job_log,
                                JOB_PROGRESS_MARKER + json.dumps(_payload),
                            )
                        except RuntimeError:
                            pass
                    if _st in ("downloading", "muxing", "done"):
                        return
            _spam = (
                _stripped.startswith("frame=")
                or ("time=" in line and "bitrate=" in line)
                or _stripped.startswith("[hls @")
                or _stripped.startswith("[https @")
                or _stripped.startswith("[tcp @")
                or _stripped.startswith("[generic]")
                # MP4 demuxer chatter, e.g. the per-segment
                # "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x..] Found duplicated MOOV
                # Atom. Skipped it" notice on fMP4 HLS -- harmless ffmpeg
                # info, worker-log only.
                or _stripped.startswith("[mov,mp4")
                or _stripped.startswith("Opening ")
                # NB: "[download] ..." (Destination / progress % /
                # completion) is intentionally NOT spam -- operator wants
                # download progress visible in the LiveLog.
                or "Skip ('#EXT" in line
                or "#EXT-X-PROGRAM-DATE-TIME" in line
            )
            sink = log if _spam else _both
            try:
                _loop.call_soon_threadsafe(sink, f"  [yt-dlp] {line}")
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
        _dl_task = asyncio.create_task(_run(target_url, referer, is_stream))
        # Resolve the Live panel progress bar when the download settles
        # (success, failure, or cancel) so a row never sticks at 99%.
        # The done-callback runs on the event loop, so job_log is safe to
        # call directly here.
        if job_log is not None:
            def _emit_dl_done(_t, _u=target_url):
                try:
                    job_log(
                        JOB_PROGRESS_MARKER
                        + json.dumps({"key": _u, "state": "done"})
                    )
                except Exception:
                    pass
            _dl_task.add_done_callback(_emit_dl_done)
        pending.append(_dl_task)

    async def drain() -> None:
        """Block until every pending download completes OR has been
        idle (no chunks / no yt-dlp output) for ``idle_window``
        seconds. Lets a 1 GB+ video finish even if it takes 5
        minutes, while still abandoning a download that hangs.

        Tunables read from env:
          PAPRIKA_VIDEO_DRAIN_IDLE_S (default 45) -- give up after
            this many seconds of zero progress.
          PAPRIKA_VIDEO_DRAIN_HARD_S (default 3600 = 60 min) -- hard
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
            os.environ.get("PAPRIKA_VIDEO_DRAIN_HARD_S", "3600.0")
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

    # Deterministic id of the built-in Paprika Agent extension (must
    # match server/hub/routes/extensions.py PAPRIKA_AGENT_ID, derived
    # from the committed signing key).
    PAPRIKA_AGENT_ID = "gmhfgiloilioklcofcinlemifjjaeppe"

    def _write_agent_extension_policy(self) -> None:
        """Write the Chrome managed policy that force-installs the
        built-in Paprika Agent extension from the hub-served CRX +
        update manifest. Best-effort; idempotent."""
        import json as _json
        from pathlib import Path as _Path

        hub = (self.hub_http_url or "").rstrip("/")
        if not hub:
            return
        update_url = f"{hub}/agent-ext/updates.xml"
        policy = {
            "ExtensionInstallForcelist": [
                f"{self.PAPRIKA_AGENT_ID};{update_url}"
            ],
        }
        pol_dir = _Path("/etc/opt/chrome/policies/managed")
        pol_dir.mkdir(parents=True, exist_ok=True)
        pol_file = pol_dir / "paprika-agent.json"
        pol_file.write_text(_json.dumps(policy, indent=2), encoding="utf-8")
        _logger.info(
            f"[worker {self.worker_id}] wrote agent force-install "
            f"policy ({self.PAPRIKA_AGENT_ID} <- {update_url})",
        )

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

        # Write the Chrome managed policy that force-installs the
        # built-in Paprika Agent extension. Chrome 148 ignores
        # --load-extension for unpacked extensions and the CDP
        # Extensions.loadUnpacked is pipe-only, so a force-install
        # enterprise policy (read from /etc/opt/chrome/policies/managed)
        # is the supported path. MUST run before lanes spawn Chrome so
        # the first launch already picks it up.
        try:
            self._write_agent_extension_policy()
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] agent extension policy "
                f"write failed (non-fatal): {type(e).__name__}: {e}",
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

        # Run heartbeat + idle-tab reaper + disk-leak sweeper +
        # message loop concurrently. The sweeper is the production
        # backstop for stranded /tmp/paprika-* dirs from crashes /
        # ungraceful teardown -- see _disk_cleanup_loop docstring.
        hb_task = asyncio.create_task(self._heartbeat_loop())
        reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
        disk_task = asyncio.create_task(self._disk_cleanup_loop())
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
            disk_task.cancel()

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

    # ----------------------------------------------------------------------
    # Disk-leak sweeper
    # ----------------------------------------------------------------------
    # Even with the per-call cleanup paths fixed (profile sync drops its
    # scratch parent, session teardown drops state.assets_dir.parent),
    # ungraceful worker crashes (OOM kill, container restart, hub WS
    # disconnect mid-fetch) still strand /tmp/paprika-*/ dirs from
    # in-flight work. Production already saw a worker root FS hit 100%
    # this way -- 313 leaked paprika-profile-sync-* entries and 4-5 GB
    # yt-dlp partials inside orphaned paprika-ses-* dirs. Chrome and the
    # worker process then can't write logs/state -> Lane WebSocket dies
    # -> every fetch landing on that node crashes (manifests as
    # "ConnectionClosedError: no close frame received or sent").
    #
    # This periodic sweep is the defense-in-depth net. It runs in the
    # worker process (NOT a host cron) so it inherently knows which
    # sessions are STILL live via self._sessions and never touches their
    # tmpdirs. Conservative defaults: only entries older than
    # PAPRIKA_TMP_SWEEP_MIN_AGE_S (default 30 min) are eligible, and a
    # set of protected names (the persistent profile cache, the
    # extensions cache) is hard-coded out of scope.

    # Transient-scratch prefixes the worker creates under /tmp:
    #   paprika-ses-<sid>-<rand>             assets_dir parent  (_handle_session_start)
    #   paprika-profile-<scratch_key>-<rand> extracted profile  (_fetch_to_temp)
    #     where scratch_key ∈ {sid, job_id, "sync-<profile_name>"}
    #   paprika-vid-<job_id>-<rand>          deferred yt-dlp tmp (_spawn_deferred_video_download)
    #   paprika-<job_id>-<rand>              legacy job workdir
    #
    # The age threshold catches mid-flight EXTRACTS (which finish in
    # seconds), but DEFERRED VIDEO DOWNLOADS run up to PAPRIKA_VIDEO_
    # DOWNLOAD_TIMEOUT_S (default 7200s = 2h) AFTER the lane / session
    # are gone -- and a long single-file mp4 stream doesn't bump the
    # tmp dir's mtime (only file mtime is touched on append). Without
    # an explicit live-job set, the sweeper would race and rmtree a
    # paprika-vid-<jobid>-* dir out from under the active yt-dlp.
    # _bg_video_tasks (dict[task,job_id]) is the source of truth for
    # "still downloading"; its .values() feeds the keep-set below.
    _TMP_SWEEP_PROTECTED = {
        "paprika-profile-cache",     # canonical sync cache root
        "paprika-extensions",        # CRX cache, populated on register
    }
    _TMP_SWEEP_PREFIXES = (
        "paprika-ses-",
        "paprika-profile-",
        "paprika-vid-",
        "paprika-",                  # legacy / job tmpdirs (paprika-<jobid>-<rand>)
    )

    async def _disk_cleanup_loop(self) -> None:
        """Periodically prune stale /tmp/paprika-* dirs left behind by
        crashes / ungraceful teardown. Idempotent; safe to run while
        new sessions are starting (age threshold + active-session check)."""
        interval = float(
            os.environ.get("PAPRIKA_TMP_SWEEP_INTERVAL_S") or 1800.0,  # 30 min
        )
        min_age = float(
            os.environ.get("PAPRIKA_TMP_SWEEP_MIN_AGE_S") or 1800.0,  # 30 min
        )
        # One pass immediately on startup so a worker that just came up
        # from a previous crash starts clean. Subsequent passes are spaced
        # by ``interval``.
        first = True
        try:
            while True:
                if first:
                    first = False
                else:
                    await asyncio.sleep(interval)
                try:
                    n, bytes_freed = await asyncio.to_thread(
                        self._sweep_tmp_orphans, min_age,
                    )
                    if n:
                        _logger.info(
                            f"[worker {self.worker_id}] tmp sweep: "
                            f"removed {n} orphan dir(s), "
                            f"~{bytes_freed // (1024*1024)} MiB freed",
                        )
                except Exception as e:
                    _logger.info(
                        f"[worker {self.worker_id}] tmp sweep failed: "
                        f"{type(e).__name__}: {e}",
                    )
        except asyncio.CancelledError:
            return

    def _sweep_tmp_orphans(self, min_age_s: float) -> tuple[int, int]:
        """One sweep pass. Synchronous (run in a thread because rmtree of
        multi-GB dirs is blocking). Returns ``(removed_count, bytes_freed)``."""
        import time as _t

        tmp = Path("/tmp")
        if not tmp.exists():
            return (0, 0)
        # Snapshot of live session IDs so we don't race a session_end
        # that fires mid-sweep. Slight race risk on a session that just
        # started but hasn't populated self._sessions yet -- the
        # min_age_s threshold (default 30 min) is the belt to the
        # active-set's suspenders.
        live_sids: set[str] = set(self._sessions.keys())
        # Job IDs whose deferred yt-dlp download is STILL RUNNING (lane
        # is freed, session is gone from self._sessions, but the bg
        # task is appending bytes to paprika-vid-<jobid>-*/). Without
        # this, a long single-file mp4 download with the 7200s timeout
        # would be swept out from under the live yt-dlp around the 30
        # min mark. See _TMP_SWEEP_PREFIXES comment.
        bg_tasks = getattr(self, "_bg_video_tasks", None) or {}
        live_jobs: set[str] = (
            set(bg_tasks.values()) if isinstance(bg_tasks, dict) else set()
        )
        # Combined keep-set: any tmpdir whose name contains one of
        # these strings is preserved this pass.
        keep_tokens: set[str] = {x for x in (live_sids | live_jobs) if x}
        now = _t.time()
        removed = 0
        freed = 0
        try:
            entries = list(tmp.iterdir())
        except Exception:
            return (0, 0)
        for entry in entries:
            name = entry.name
            if name in self._TMP_SWEEP_PROTECTED:
                continue
            if not any(name.startswith(p) for p in self._TMP_SWEEP_PREFIXES):
                continue
            try:
                if not entry.is_dir():
                    continue
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age < min_age_s:
                continue
            # If the dir name embeds a live session id OR an in-flight
            # background-download job id, keep it. Names follow
            # paprika-ses-<sid>-<rand> / paprika-profile-<key>-<rand> /
            # paprika-vid-<jobid>-<rand> / paprika-<jobid>-<rand>. The
            # substring match is loose but safe-direction: false
            # positives only KEEP a stale dir an extra cycle; the
            # protection NEVER deletes a live dir.
            if any(tok in name for tok in keep_tokens):
                continue
            try:
                # Best-effort directory size for the log line, capped so
                # we don't os.walk a huge tree just for telemetry.
                size = 0
                for root, _, files in os.walk(entry):
                    for f in files:
                        try:
                            size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                        if size > 100 * 1024 * 1024 * 1024:  # cap at 100 GiB
                            break
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
                freed += size
            except Exception:
                # Best-effort: skip and try again next sweep.
                continue
        return (removed, freed)

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
        # Tracked outside the try so the except can release the lane even
        # when the failure happens AFTER acquire() but BEFORE the
        # SessionState is registered in self._sessions (the rollback
        # below pops self._sessions, which is empty in that window).
        lane = None
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
            _session_ua = await _get_browser_user_agent(chrome)

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

            _raw_downloader, drain_video_session = _make_video_downloader(
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
                user_agent=_session_ua,
            )
            # Asset URL blacklist wrapper (V + Y): glob/regex deny list.
            # passive on_stream_detected inside install_session_asset_capture
            # already filters at the capture layer; this wrapper covers the
            # explicit SDK call path (page.download_video(url=...)) so the
            # same list governs both. See core/url_blacklist.py for syntax.
            from core.url_blacklist import compile_blacklist as _compile_blacklist_yt
            _video_bl_matcher = _compile_blacklist_yt(
                getattr(msg, "asset_url_blacklist", []) or ()
            )

            def maybe_download_video_session(url, referer=""):
                if url:
                    hit = _video_bl_matcher.match(url)
                    if hit is not None:
                        _logger.info(
                            f"[session {sid}] yt-dlp BLOCK (blacklist={hit!r}) {url[:120]}"
                        )
                        return None
                return _raw_downloader(url, referer)

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
                # Asset URL blacklist (V): operator-managed substring
                # deny list pulled from Settings. The same list is also
                # checked in maybe_download_video_session below so
                # blocked URLs don't trigger yt-dlp.
                url_blacklist=tuple(getattr(msg, "asset_url_blacklist", []) or ()),
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
            elif lane is not None and self.lane_pool is not None:
                # Lane was acquired but the failure happened before the
                # SessionState was registered (operator-profile install
                # or mkdtemp threw between acquire() and
                # self._sessions[sid] = state). Without releasing it
                # here the lane leaks permanently -- the classic
                # "no free lane / fleet at capacity" after a transient
                # profile-fetch error.
                self.lane_pool.release(lane)
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
        # the tab and a write action mid-fetch would race. Whether a kind
        # is allowed mid-fetch (read-only) or is session-level is read off
        # its registry ``_ActionSpec`` -- the single source of truth.
        action = msg.action or {}
        kind = action.get("kind") or ""
        spec = _SESSION_ACTIONS.get(kind)
        # A read-only evaluate (forensics probe) is also permitted
        # mid-fetch. The hub's forensics loop pre-flights every probe
        # against a safety regex (rejects navigate / click / submit /
        # cookie+storage writes / POST fetch / DOM mutation), so the JS
        # only READS the page -- as safe as outline / screenshot / state,
        # which are already allowed. The ``read_only`` flag is set ONLY
        # by server/hub/routes/forensics.py; the normal /evaluate route
        # never sets it, so operator writes mid-fetch stay blocked.
        _is_ro_evaluate = kind == "evaluate" and bool(action.get("read_only"))
        if (
            state.is_fetch_owned
            and not (spec is not None and spec.read_only)
            and not _is_ro_evaluate
        ):
            _ro_kinds = sorted(k for k, s in _SESSION_ACTIONS.items() if s.read_only)
            reply.status = (
                f"ERR: session {sid} is owned by a running fetch job; "
                f"only read-only actions are allowed "
                f"({_ro_kinds} + read-only evaluate). "
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
        if spec is not None and spec.session_level:
            chosen_lock = state.lock
        else:
            chosen_lock = state.page_locks.get(target_pid) or state.lock

        async with chosen_lock:
            try:
                # For per-tab kinds, look up the target Tab. For
                # session-level kinds, fall back to default (used only
                # for the "snapshot URL" bookkeeping below).
                tab = state.pages.get(target_pid) if target_pid else None
                if tab is None and not (spec is not None and spec.session_level):
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
                # meaningful for per-tab kinds (tab exists). ``cur`` is
                # initialised first so the ctx below always has it (a
                # session-level kind has tab=None and never assigns it).
                cur = ""
                if tab is not None:
                    try:
                        cur = await tab.evaluate("document.location.href")
                    except Exception:
                        cur = ""
                    if cur:
                        state.note_url(browser_ops.canon_url(cur))

                # Plugin-style dispatch: every explicit action kind is
                # handled by its decorated ``_act_<kind>`` method in the
                # _SESSION_ACTIONS registry. The generic mutating kinds
                # (click / fill / scroll / navigate / back / ...) have no
                # per-kind handler -- they fall to the catch-all ``else``
                # which delegates uniformly to ``browser_ops.execute``.
                ctx = _ActionCtx(
                    state=state, tab=tab, action=action, reply=reply,
                    cur=cur, slog=_slog, t0=t0, msg=msg,
                )
                if spec is not None:
                    await spec.fn(self, ctx)
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
        # Cancel any URL-capture polling tasks installed by
        # browser_ops.install_session_asset_capture. They run in a
        # tight while loop reading window.__paprika_url_capture; if we
        # don't cancel them BEFORE the tab is killed they'll keep
        # raising "tab closed" errors until the event loop notices.
        try:
            tab = getattr(state, "tab", None)
            tasks = getattr(tab, "_paprika_url_capture_tasks", None) if tab else None
            if tasks:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                tasks.clear()
        except Exception:
            pass

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
                os.environ.get("PAPRIKA_VIDEO_DRAIN_HARD_S", "3600.0")
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
            # Session assets_dir is rooted at /tmp/paprika-ses-<sid>-<rand>/
            # (see _handle_session_start). Until now nothing cleaned it,
            # so every closed session leaked its yt-dlp partials / HLS
            # segment buffers / screenshot dumps -- on busy workers this
            # accumulated multi-GB per orphan and filled the root FS,
            # causing Chrome+WebSocket crashes. Drop the whole mkdtemp
            # root (parent of state.assets_dir) now that teardown is
            # done. assets_dir was uploaded to the hub during the fetch
            # so on-disk copy is no longer needed.
            try:
                ad = getattr(state, "assets_dir", None)
                if ad is not None:
                    # state.assets_dir is <scratch>/assets; drop the
                    # whole <scratch> the mkdtemp made.
                    shutil.rmtree(Path(ad).parent, ignore_errors=True)
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
        # v2 cleanup: cogagent / vision-agent retired. The auto + cogagent
        # branches inside this function are now dead code reachable only
        # via the engine literal which Pydantic now rejects ("qwen"/"auto"
        # only). Force-route both to qwen so even a hand-crafted WS
        # message can't dive into the CogAgent codepath that depends on
        # the deleted ``browser_ops.execute_vision_action``. Full
        # surgical extraction of the cogagent branches is a follow-up
        # cleanup that requires careful test coverage.
        if engine in ("auto", "cogagent"):
            engine = "qwen"

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

            _agent_ua = await _get_browser_user_agent(state.browser) if state.browser else None
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
                user_agent=_agent_ua,
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
        # _fetch_to_temp returns ``<scratch>/"User Data"`` -- after we
        # move the "User Data" subdir into the canonical cache below,
        # the parent scratch dir is left orphaned in /tmp. Track it
        # here so the post-rename cleanup can drop it (otherwise every
        # sync leaks one empty paprika-profile-sync-<name>-<rand>/ in
        # /tmp; over days the dentry count fills small VM root FSes).
        scratch_parent = new_dir.parent
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
            # Drop the now-empty scratch parent (see comment above).
            shutil.rmtree(scratch_parent, ignore_errors=True)
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
        # Skip built-in extensions (e.g. paprika-agent): they ship in
        # the repo and are loaded straight from the code tree by the
        # lane -- there's no tarball to download (the /extensions list
        # only surfaces them for operator visibility), so trying to
        # fetch one just logs a spurious 404.
        items = [
            it for it in (payload.get("extensions") or [])
            if it.get("enabled", True) and not it.get("builtin")
        ]
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

                # Incremental asset upload (resilience). The fetcher fires
                # this once per asset right after it's written to
                # assets_dir; we ship each one to the hub immediately so a
                # mid-fetch failure (worker disconnect, crash, hub restart)
                # leaves the already-captured assets in the gallery instead
                # of discarding the whole batch -- the legacy behaviour
                # uploaded nothing until fetch() returned successfully.
                # The end-of-fetch _upload_files() pass then reconciles
                # (page.html, log, late yt-dlp output, and anything whose
                # inline upload failed). uploaded_names dedupes the two
                # passes so a file is never shipped twice.
                uploaded_names: set[str] = set()
                page_url_for_assets = assign.url or None

                async def _on_asset_saved(path, info):
                    try:
                        name = (info or {}).get("name") or path.name
                    except Exception:
                        return
                    if name in uploaded_names:
                        return
                    ok = await self._upload_asset(
                        assign,
                        path,
                        name,
                        source_url=(info or {}).get("url"),
                        mime=(info or {}).get("mime"),
                        page_url=page_url_for_assets,
                        timeout=300.0,
                    )
                    if ok:
                        uploaded_names.add(name)

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
                    # V: operator-managed URL deny list from Settings.
                    asset_url_blacklist=list(getattr(assign, "asset_url_blacklist", []) or []),
                    on_asset_saved=_on_asset_saved,
                )
                # Detach big-video downloads from the lane. When the
                # operator asked for video AND this isn't a keep_session
                # job, DETECT streams during capture but defer the
                # (often 10+ min) yt-dlp download to a background task so
                # the lane is freed immediately. The job sits in phase
                # "downloading" until the background task uploads the
                # video and sends the final WorkerJobComplete. keep_session
                # is excluded -- there the operator drives download_video()
                # interactively inside the live session.
                _defer_video = (
                    bool(getattr(assign.options, "download_video", False))
                    and not keep_session
                )
                fetch_opts.defer_video_download = _defer_video
                try:
                    result = await fetch(fetch_opts)
                except Exception as e:
                    _log(f"  !! fetch crashed: {type(e).__name__}: {e}")
                    # Salvage: ship any assets captured before the crash
                    # that the incremental on_asset_saved callback didn't
                    # already upload (the file mid-write when fetch raised,
                    # or one whose inline upload failed). Without this they
                    # would be rmtree'd below, unsent -- a contributor to
                    # "errored job has empty assets". Best-effort; never
                    # let salvage failures mask the original error.
                    salvaged = 0
                    try:
                        if assets_dir and assets_dir.exists():
                            for _p in sorted(assets_dir.iterdir()):
                                if not _p.is_file() or _p.name in uploaded_names:
                                    continue
                                if await self._upload_asset(
                                    assign,
                                    _p,
                                    _p.name,
                                    page_url=page_url_for_assets,
                                    timeout=300.0,
                                ):
                                    uploaded_names.add(_p.name)
                                    salvaged += 1
                        if salvaged:
                            _log(
                                f"  ... salvaged {salvaged} captured "
                                f"asset(s) despite fetch error"
                            )
                    except Exception as _sx:
                        _log(
                            f"  (salvage pass failed: "
                            f"{type(_sx).__name__}: {_sx})"
                        )
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

                # Upload all outputs to hub. Assets already shipped by the
                # incremental on_asset_saved callback are skipped; this
                # pass reconciles page.html, log, late yt-dlp output and
                # any inline-upload failures.
                await self._upload_files(assign, workdir, result, uploaded_names)

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
                # Deferred video download: capture is done and the
                # image assets are uploaded, but a (big) video was
                # detected. Mark the job "downloading", then run yt-dlp
                # in a detached background task that uploads the video
                # and sends the FINAL WorkerJobComplete. The lane is
                # released by the finally below (the download doesn't
                # need the browser), so other jobs can use it meanwhile.
                _deferred_targets = list(
                    getattr(result, "deferred_video_targets", []) or []
                )
                if _defer_video and _deferred_targets:
                    await self._send(
                        WorkerJobProgress(job_id=job_id, phase="downloading")
                    )
                    _logger.info(
                        f"[{job_id}] video deferred to background "
                        f"({len(_deferred_targets)} target(s)); lane released, "
                        f"phase=downloading",
                    )
                    self._spawn_deferred_video_download(
                        assign,
                        _deferred_targets,
                        job_result,
                        page_url_for_assets,
                    )
                else:
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
        asset_url_blacklist: list[str] | None = None,
        on_asset_saved=None,
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
            # Asset URL blacklist (V). Caller passes the list it pulled
            # from HubAssignJob; applied at fetcher's on_response so
            # blocked URLs never reach disk or yt-dlp.
            asset_url_blacklist=list(asset_url_blacklist or []),
            network_log=network_log,
            # Incremental upload: fire per asset as it lands so a
            # mid-fetch failure (worker disconnect / crash / hub restart)
            # doesn't discard everything captured so far. None disables.
            on_asset_saved=on_asset_saved,
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

    def _spawn_deferred_video_download(
        self,
        assign: HubAssignJob,
        targets: list[dict],
        base_result,
        page_url: str | None,
    ) -> None:
        """Run a fetch job's deferred yt-dlp download(s) in a DETACHED
        background task, upload the resulting video(s) to the job's
        /assets, then send the FINAL WorkerJobComplete.

        Called after the lane has been released (the download only needs
        the stream URL + referer, not the live browser), so the job sits
        in phase "downloading" without pinning a Chrome lane. Uses its
        OWN temp dir (not the job workdir, which the caller rmtree's) and
        a generous per-download timeout so big VODs aren't killed
        mid-stream (the old inline path capped at 600s and died ~50%).
        """
        import os as _os
        import shutil as _shutil
        import tempfile as _tempfile

        job_id = assign.job_id

        async def _run() -> None:
            from core.fetcher import run_ytdlp

            tmp = Path(_tempfile.mkdtemp(prefix=f"paprika-vid-{job_id}-"))
            dl_timeout = int(
                _os.environ.get("PAPRIKA_VIDEO_DOWNLOAD_TIMEOUT_S", "7200")
            )
            _loop = asyncio.get_running_loop()
            # Current download identity for the Live panel progress bar
            # (set before each target below).  run_ytdlp processes the
            # targets sequentially, so a single holder is unambiguous.
            _cur = {"key": None, "label": None}
            # monotonic time of the last throttled progress marker; reset
            # per target so each download's first update emits promptly.
            _cur_last = [0.0]

            def _emit_progress(line: str) -> None:
                # Schedule a WorkerJobLog send from this worker THREAD.
                # The hub treats JOB_PROGRESS_MARKER lines as ephemeral
                # (broadcast to live viewers, never persisted), so this
                # drives the Live panel's progress bars without flooding
                # log.txt.
                try:
                    _loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(
                            self._send(WorkerJobLog(job_id=job_id, line=line))
                        )
                    )
                except RuntimeError:
                    pass

            def _dl_log(line: str) -> None:
                # Worker stderr for the raw line; ALSO parse live progress
                # and drive the Live panel's per-download progress bar via
                # an ephemeral marker.  fetch / recipe mode has no
                # _make_video_downloader, so this deferred path is where
                # its progress bars come from.
                _logger.info("[%s] [downloading] %s", job_id, line)
                try:
                    _prog = _parse_dl_progress(line.lstrip())
                except Exception:
                    _prog = None
                if _prog is None:
                    return
                if _prog.get("label"):
                    _cur["label"] = _prog["label"]
                _st = _prog.get("state")
                # Throttle downloading/muxing markers to ~1/s -- yt-dlp emits
                # 20+ progress lines/sec on a direct file, and one
                # WorkerJobLog per line floods the hub WS (observed
                # destabilising the worker connection).  start/done always
                # pass through.
                _now = time.monotonic()
                if (_st in ("downloading", "muxing")
                        and _now - _cur_last[0] < 1.0):
                    return
                _cur_last[0] = _now
                _payload = {
                    "key": _cur["key"] or "video",
                    "label": _cur["label"] or _cur["key"] or "video",
                }
                _payload.update(_prog)
                _emit_progress(JOB_PROGRESS_MARKER + json.dumps(_payload))

            added: list = []
            try:
                await self._send(WorkerJobLog(
                    job_id=job_id,
                    line=f"  [downloading] {len(targets)} video target(s) "
                         f"in background…",
                ))
                for t in targets:
                    u = t.get("url")
                    ref = t.get("referer")
                    if not u:
                        continue
                    _cur["key"] = u
                    _cur["label"] = (
                        u.split("?", 1)[0].rsplit("/", 1)[-1] or u
                    )[:64]
                    _cur_last[0] = 0.0  # let this target's first marker emit now
                    ok, msg = await asyncio.to_thread(
                        run_ytdlp, u, tmp,
                        referer=ref, timeout=dl_timeout, log=_dl_log,
                    )
                    # Resolve this target's progress bar (success or fail)
                    # so it doesn't stick at the last %.
                    try:
                        await self._send(WorkerJobLog(
                            job_id=job_id,
                            line=JOB_PROGRESS_MARKER + json.dumps(
                                {"key": u, "state": "done"}),
                        ))
                    except Exception:
                        pass
                    if not ok:
                        await self._send(WorkerJobLog(
                            job_id=job_id,
                            line=f"  [downloading] FAIL {u}: {str(msg)[:200]}",
                        ))
                # Upload every completed file (skip in-progress parts).
                _video_ext = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".ts"}
                for p in sorted(tmp.iterdir()):
                    if not p.is_file():
                        continue
                    low = p.name.lower()
                    if low.endswith((".part", ".ytdl")) or ".part-" in low:
                        continue
                    mime = "video/mp4" if p.suffix.lower() in _video_ext else None
                    await self._upload_asset(
                        assign, p, p.name,
                        mime=mime, page_url=page_url, timeout=900.0,
                    )
                    try:
                        sz = p.stat().st_size
                    except Exception:
                        sz = 0
                    added.append(AssetInfo(
                        name=p.name, size=sz, mime=mime,
                        url=None, page_url=page_url,
                        href=f"/jobs/{job_id}/assets/{p.name}",
                    ))
                await self._send(WorkerJobLog(
                    job_id=job_id,
                    line=f"  [downloading] done: {len(added)} video asset(s) "
                         f"uploaded",
                ))
            except Exception as e:
                await self._send(WorkerJobLog(
                    job_id=job_id,
                    line=f"  [downloading] error: {type(e).__name__}: {e}",
                ))
            finally:
                _shutil.rmtree(tmp, ignore_errors=True)
                # ALWAYS finish the job so it can never hang in
                # "downloading" -- include whatever video assets landed.
                try:
                    base_result.assets = list(base_result.assets) + added
                except Exception:
                    pass
                try:
                    await self._send(WorkerJobComplete(
                        job_id=job_id, result=base_result,
                    ))
                except Exception:
                    pass

        task = asyncio.create_task(_run())
        # Track {task: job_id} so two consumers can find the in-flight
        # downloads:
        #   * the worker shutdown path can await them,
        #   * _disk_cleanup_loop can keep paprika-vid-<jobid>-* dirs
        #     alive for as long as the download is running (the lane
        #     is already released and the session is gone from
        #     self._sessions, so without this protection a long single-
        #     file mp4 -- 2h cap, no per-segment dir-mtime bump -- could
        #     get swept out from under the live yt-dlp process).
        if not hasattr(self, "_bg_video_tasks") or not isinstance(
            getattr(self, "_bg_video_tasks", None), dict
        ):
            self._bg_video_tasks = {}
        self._bg_video_tasks[task] = job_id
        task.add_done_callback(lambda t: self._bg_video_tasks.pop(t, None))

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
        already_uploaded: set[str] | None = None,
    ) -> None:
        """Upload page.html, log.txt, and all asset files to the hub.

        ``already_uploaded`` is the set of asset names the incremental
        on_asset_saved callback already shipped during capture; those are
        skipped here so the end-of-fetch reconcile pass only uploads the
        stragglers (page.html, log, late yt-dlp output, and any asset
        whose inline upload failed)."""
        done = already_uploaded if already_uploaded is not None else set()
        await self._upload_log(assign, workdir / "log.txt")
        await self._upload_special(assign, workdir / "page.html", "page.html")
        # Page URL = the URL the user told us to fetch. Every captured
        # asset is "on" this page from the gallery's point of view.
        page_url = assign.url or None
        # Assets
        for a in fetch_result.assets_saved:
            if a["name"] in done:
                continue
            path = Path(a["path"])
            if path.exists():
                if await self._upload_asset(
                    assign,
                    path,
                    a["name"],
                    source_url=a.get("url"),
                    mime=a.get("mime"),
                    page_url=page_url,
                ):
                    done.add(a["name"])
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
                if p.is_file() and p.name not in known and p.name not in done:
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
        timeout: float | None = None,
    ) -> bool:
        """Upload one fetch-mode asset to the hub. Returns True on
        success, False on failure. The bool lets the incremental
        on_asset_saved callback track which names actually landed so the
        end-of-fetch reconcile pass can re-try only the ones that didn't.
        """
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
                post_kwargs: dict[str, Any] = {"files": files, "data": data}
                if timeout is not None:
                    post_kwargs["timeout"] = float(timeout)
                r = await self._http.post(url, **post_kwargs)
                r.raise_for_status()
            return True
        except Exception as e:
            await self._send(
                WorkerJobLog(
                    job_id=assign.job_id,
                    line=f"  !! asset upload failed: {name}: {e}",
                )
            )
            return False

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
