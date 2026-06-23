"""yt-dlp video download + progress parsing + process cleanup. (worker agent package; shared bits in _base.py)."""

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
from ._base import _DLP_DEST_RE, _DLP_ETA_RE, _DLP_FF_SIZE_RE, _DLP_FF_SPEED_RE, _DLP_FF_TIME_RE, _DLP_PCT_RE, _DLP_PHLS_SEG_RE, _DLP_SPEED_RE, _NOVNC_PROTECTION_S, _logger, _session_interaction_at

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


def is_session_protected(session_id: str | None) -> bool:
    """Return True if the operator interacted with ``session_id`` via
    noVNC within the protection window. Used by all three stall gates
    to defer a kill.

    Defensive: a None session_id, an unknown session, or a disabled
    window (= 0) all return False -- meaning "no protection, behave
    as the regular timers say".
    """
    if not session_id:
        return False
    if _NOVNC_PROTECTION_S <= 0:
        return False
    last = _session_interaction_at.get(session_id, 0.0)
    if last <= 0:
        return False
    return (time.time() - last) < _NOVNC_PROTECTION_S


def _terminate_ytdlp_descendants_for_job(job_id: str) -> int:
    """Same as :func:`_terminate_ytdlp_descendants` but ONLY SIGTERMs
    yt-dlp / ffmpeg processes whose cmdline argv mentions the given
    ``job_id`` (via the temp dir prefix ``paprika-vid-{job_id}-``).
    Lets HubForceCompleteJob surgically wrap up one stuck download
    without nuking other jobs the same worker may be running."""
    if not os.path.exists("/proc") or not job_id:
        return 0
    import signal as _sig
    needle = f"paprika-vid-{job_id}".encode("utf-8")
    my_pid = os.getpid()
    procs: dict[int, tuple[int, bytes]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read()
            rp = raw.rfind(b")")
            after = raw[rp + 2:].split()
            ppid = int(after[1])
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read()
            procs[pid] = (ppid, cmd)
        except (OSError, ValueError, IndexError):
            continue
    children_of: dict[int, list[int]] = {}
    for p, (pp, _) in procs.items():
        children_of.setdefault(pp, []).append(p)
    descendants: list[int] = []
    seen: set[int] = set()
    stack: list[int] = [my_pid]
    while stack:
        p = stack.pop()
        for c in children_of.get(p, []):
            if c in seen:
                continue
            seen.add(c)
            descendants.append(c)
            stack.append(c)
    killed = 0
    for pid in descendants:
        _, cmd = procs.get(pid, (0, b""))
        if not cmd or needle not in cmd:
            continue
        cmd_str = cmd.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        if ("yt-dlp" in cmd_str) or ("/ffmpeg" in cmd_str) or cmd_str.split(" ", 1)[0].endswith("ffmpeg"):
            try:
                os.kill(pid, _sig.SIGTERM)
                killed += 1
            except OSError:
                pass
    return killed


def _terminate_ytdlp_descendants() -> int:
    """SIGTERM all yt-dlp/ffmpeg descendant processes of this worker.

    Used by the _download_stream stall watchdog as a belt-and-braces
    fallback when the inline/adapter Popen-loop kill gates fail to fire
    (e.g. yt-dlp stops emitting progress lines entirely, so the in-
    process parser never gets a chance to decide).  Killing the child
    unblocks the asyncio.to_thread thread, which lets the worker's
    event loop fire its heartbeat task again.

    Linux-only (relies on /proc).  Returns the number of processes
    signalled; returns 0 on non-Linux or when /proc is unavailable.
    Defensive: any individual /proc read or os.kill failure is silently
    ignored — best-effort by design.
    """
    if not os.path.exists("/proc"):
        return 0
    import signal as _sig
    my_pid = os.getpid()
    # PID -> (PPid, cmdline) for every process currently running.
    procs: dict[int, tuple[int, str]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read()
            # comm is in (...) and can contain spaces / parens. The
            # safest parse: take everything after the LAST ')'.
            rp = raw.rfind(b")")
            after = raw[rp + 2:].split()
            ppid = int(after[1])
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode(
                    "utf-8", errors="replace"
                ).strip()
            procs[pid] = (ppid, cmd)
        except (OSError, ValueError, IndexError):
            continue
    # Build child index, then BFS descendants of my_pid.
    children_of: dict[int, list[int]] = {}
    for p, (pp, _) in procs.items():
        children_of.setdefault(pp, []).append(p)
    descendants: list[int] = []
    seen: set[int] = set()
    stack: list[int] = [my_pid]
    while stack:
        p = stack.pop()
        for c in children_of.get(p, []):
            if c in seen:
                continue
            seen.add(c)
            descendants.append(c)
            stack.append(c)
    # SIGTERM ones whose cmdline names yt-dlp or ffmpeg. We don't touch
    # chrome / chromedriver / curl — those are not the stall source and
    # killing chrome would cascade the whole session.
    killed = 0
    for pid in descendants:
        _, cmd = procs.get(pid, (0, ""))
        if not cmd:
            continue
        if ("yt-dlp" in cmd) or ("/ffmpeg" in cmd) or cmd.split()[0].endswith("ffmpeg"):
            try:
                os.kill(pid, _sig.SIGTERM)
                killed += 1
            except OSError:
                pass
    return killed


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
    session_id: str | None = None,  # optional: enables noVNC operator-
    #              interaction protection. When set, the closure builds
    #              an is_protected callback the inline/adapter/watchdog
    #              gates consult before killing yt-dlp -- so an operator
    #              actively driving the lane via noVNC outranks the
    #              automatic "too slow / stalled" verdict. None falls
    #              back to the regular timer behaviour.
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
        async with make_async_client(
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

        # noVNC operator-priority override. When an operator is actively
        # driving this session via noVNC (KeyEvent / PointerEvent /
        # ClientCutText seen on the hub-side RFB tap within the last
        # PAPRIKA_NOVNC_PROTECTION_S seconds, default 60s), all three
        # stall gates -- this watchdog plus the inline + adapter Popen
        # gates -- defer the kill. Evidence preservation outranks the
        # automatic "too slow" verdict when a human is literally watching.
        # ``session_id`` is the closure capture from _make_video_downloader.
        def _is_session_protected() -> bool:
            return is_session_protected(session_id)

        # Stall watchdog (outer last-resort gate).  The inline run_ytdlp
        # and the adapter both have their OWN in-process stall + min-
        # rate gates parsing yt-dlp progress lines (see core/fetcher.py
        # and data/tools/installed/paprika-ytdlp/adapter.py).  This
        # watchdog catches cases those miss: progress lines stop
        # arriving entirely (CDN dropped the connection but TCP read is
        # still pending), or the adapter delegates to ffmpeg-direct
        # which emits frame=/time= lines the yt-dlp regex doesn't cover.
        # We don't replace the in-process gates -- they kill SOONER and
        # produce a precise error message -- this is the safety net.
        _wd_no_progress_s = float(
            os.environ.get("PAPRIKA_YTDLP_WATCHDOG_S", "120")
        )
        # Initial grace before the watchdog starts firing: yt-dlp's
        # "Resolving / Extracting" phase produces NO progress lines but
        # is legitimate work.  Skip the first 30 s entirely.
        _wd_grace_s = 30.0

        async def _stall_watchdog() -> None:
            # Sleep through the spawn / extractor grace period first.
            try:
                await asyncio.sleep(_wd_grace_s)
            except asyncio.CancelledError:
                return
            while True:
                try:
                    await asyncio.sleep(15)
                except asyncio.CancelledError:
                    return
                last = last_progress.get(target_url, 0.0)
                if not last:
                    # URL was popped (download finished or moved on).
                    return
                idle = time.time() - last
                if idle <= _wd_no_progress_s:
                    continue
                # noVNC operator priority: someone is at the keyboard;
                # don't override their wishes with the automatic kill.
                # Log once so the operator sees the deferral in the Live
                # panel and knows the watchdog isn't broken.
                if _is_session_protected():
                    _both(
                        f"  -- stall watchdog: deferred kill ({idle:.0f}s "
                        f"idle) — noVNC operator is interacting"
                    )
                    # Reset the comparison anchor so the next deferral
                    # log doesn't fire every 15s while interaction
                    # continues. As soon as operator stops touching,
                    # protection lapses and the next tick (after another
                    # _wd_no_progress_s seconds of true idle) will kill.
                    last_progress[target_url] = time.time()
                    continue
                killed = _terminate_ytdlp_descendants()
                _both(
                    f"  !! stall watchdog: no progress for {idle:.0f}s "
                    f"(threshold {_wd_no_progress_s:.0f}s) -- "
                    f"SIGTERM'd {killed} yt-dlp/ffmpeg descendant(s)"
                )
                # Stop after firing once per attempt.  If yt-dlp respawns
                # itself the next per-referer iteration will spin a fresh
                # watchdog.
                return

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
                wd_task = asyncio.create_task(_stall_watchdog())
                try:
                    # is_protected is keyword-only; older core/fetcher.py
                    # (pre-noVNC-priority) ignores it, newer one uses it
                    # in the inline gates and forwards to the adapter.
                    ok, msg = await asyncio.to_thread(
                        functools.partial(
                            run_ytdlp,
                            target_url,
                            assets_dir,
                            _cand_ref or None,
                            None,
                            ytdlp_timeout,
                            _ytdlp_log,
                            is_protected=_is_session_protected,
                        )
                    )
                finally:
                    wd_task.cancel()
                    try:
                        await wd_task
                    except (asyncio.CancelledError, Exception):
                        pass
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


def detect_yt_dlp() -> bool:
    return shutil.which("yt-dlp") is not None

