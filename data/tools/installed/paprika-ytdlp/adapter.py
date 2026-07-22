"""paprika-ytdlp adapter — yt-dlp video download plugin.

Entry point used by the hub plugin system (kind=python_lib):
    download(**params) -> dict

Entry point used by core/fetcher.py via direct import:
    download(..., _log_fn=callback) -> dict   (streaming log output)

Design notes
------------
* Self-contained: no imports from core/ or server/ so this file
  works both when bootstrapped as an isolated subprocess (hub plugin
  system) and when imported directly from the worker's fetcher.py.
* live HLS detection: fetches the first 8 KB of a .m3u8 URL and
  checks for #EXT-X-ENDLIST.  Live streams are recorded with
  --no-live-from-start + --download-sections for N seconds.
* The caller (fetcher.py / _jobrunner.py) is responsible for the
  fMP4 merge pass after download; this adapter only handles the
  yt-dlp subprocess and live detection.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable


LogFn = Callable[[str], None]

_DEFAULT_LIVE_RECORD_S = 30

_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Live HLS detection
# ---------------------------------------------------------------------------

def _hls_is_live(
    url: str,
    referer: str | None = None,
    user_agent: str | None = None,
) -> bool | None:
    """Fetch the HLS manifest and check for liveness.

    Returns:
        True   – live stream (explicit PLAYLIST-TYPE:EVENT, or
                 media playlist with no #EXT-X-ENDLIST anywhere)
        False  – VOD / finite recording
        None   – not HLS, master playlist, or couldn't determine
                 (network error etc.)
    """
    if not re.search(r"\.m3u8($|\?)", url, re.I):
        return None
    import urllib.request as _ur
    try:
        headers: dict[str, str] = {
            "User-Agent": user_agent or _FALLBACK_USER_AGENT,
        }
        if referer:
            headers["Referer"] = referer
        req = _ur.Request(url, headers=headers)
        # Read up to 256 KB so we don't truncate long VOD variant
        # playlists. A typical 30-minute VOD at 10s segments has
        # ~180 #EXTINF lines + URLs ≈ 20-40 KB; 256 KB safely covers
        # 4-hour movies. 8 KB used to mis-classify these as live
        # because #EXT-X-ENDLIST sits at the very end of the file.
        with _ur.urlopen(req, timeout=8) as resp:
            content = resp.read(262144).decode("utf-8", errors="replace")
    except Exception:
        return None
    # Master playlists (multi-variant) list sub-streams via
    # EXT-X-STREAM-INF but never contain EXT-X-ENDLIST.  They are
    # NOT live -- yt-dlp resolves variants itself.  Returning True
    # here would inject --hls-use-mpegts / --download-sections flags
    # that break ffmpeg on CDNs with JPEG thumbnails in the variant
    # manifest (e.g. surrit.com).
    if "#EXT-X-STREAM-INF" in content:
        return None
    if "#EXT-X-ENDLIST" in content:
        return False
    if "#EXT-X-PLAYLIST-TYPE:VOD" in content:
        return False
    # Explicit live markers from HLS spec.
    if "#EXT-X-PLAYLIST-TYPE:EVENT" in content:
        return True
    # No ENDLIST seen even after 256 KB.  Two cases:
    #   (a) genuinely live stream -- usually has only a handful of
    #       segments at any moment (sliding window).
    #   (b) VERY long VOD whose manifest exceeds 256 KB -- e.g.
    #       8h+ movies at short segments.  Distinguish by counting
    #       #EXTINF: a sliding-window live playlist rarely has more
    #       than ~10 segments; a VOD that doesn't fit in 256 KB has
    #       hundreds.
    extinf_count = content.count("#EXTINF")
    if extinf_count >= 50:
        # Almost certainly a long VOD whose ENDLIST is past the
        # 256 KB read.  Safer to treat as VOD than to inject live
        # flags that force MPEG-TS output.
        return False
    return True


# ---------------------------------------------------------------------------
# Main download entry point
# ---------------------------------------------------------------------------

def download(
    *,
    url: str,
    output_dir: str,
    referer: str | None = None,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    timeout: int = 600,
    live_record_s: int | None = None,
    extra_args: list[str] | None = None,
    user_agent: str | None = None,
    # Not in plugin.json schema — only used when imported directly from
    # fetcher.py so the caller gets live streaming log lines.
    _log_fn: LogFn | None = None,
    # noVNC operator priority callback. core/fetcher.py forwards a
    # closure here that returns True while an operator is actively
    # driving the parent session via noVNC; when True, the stall +
    # min-rate kill gates DEFER (= reset timers) instead of killing
    # yt-dlp. Evidence preservation outranks the automatic verdict
    # when a human is at the keyboard. None = legacy behaviour.
    _is_protected_fn: "Callable[[], bool] | None" = None,
) -> dict:
    """Download a video via yt-dlp.

    Returns::

        {
            "ok":        bool,
            "message":   str,    # last log line on success / error on failure
            "log_lines": list[str],
        }
    """
    lines: list[str] = []

    def _log(line: str) -> None:
        lines.append(line)
        if _log_fn is not None:
            _log_fn(line)

    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        msg = "yt-dlp not found on PATH (try: pip install yt-dlp)"
        _log(msg)
        return {"ok": False, "message": msg, "log_lines": lines}

    out_dir = Path(output_dir)

    # ------------------------------------------------------------------
    # Live HLS detection FIRST so output template + remux flags reflect
    # the real container we're going to produce.  --hls-use-mpegts
    # forces a TS stream; saving it with a .mp4 extension produces
    # files that browsers / QuickTime cannot play even though ffprobe
    # reads them (operator confusion in c057912fa777 / 9bfce06f1553).
    # ------------------------------------------------------------------
    is_live = _hls_is_live(url, referer, user_agent=user_agent)
    live_flags: list[str] = []
    if is_live is True:
        rec_s = live_record_s
        if rec_s is None:
            rec_s = int(os.environ.get("PAPRIKA_LIVE_HLS_RECORD_S", str(_DEFAULT_LIVE_RECORD_S)))
        if rec_s <= 0:
            msg = "live stream skipped"
            _log(
                "  ⏭ live HLS stream detected (no #EXT-X-ENDLIST) — "
                "skipping yt-dlp (PAPRIKA_LIVE_HLS_RECORD_S=0)"
            )
            return {"ok": False, "message": msg, "log_lines": lines}
        _log(
            f"  🔴 live HLS stream detected — recording first "
            f"{rec_s}s (PAPRIKA_LIVE_HLS_RECORD_S={rec_s}, container=.ts)"
        )
        live_flags = [
            "--no-live-from-start",
            "--download-sections", f"*0-{rec_s}",
            "--hls-use-mpegts",
        ]

    # Output extension: ``.ts`` for live (matches --hls-use-mpegts),
    # ``.mp4`` for VOD (yt-dlp will remux/merge into ISO BMFF).
    if live_flags:
        output_template = str(out_dir / "%(title).80s [%(id)s].ts")
        merge_format = "mpegts"
    else:
        output_template = str(out_dir / "%(title).80s [%(id)s].%(ext)s")
        merge_format = "mp4"

    cmd: list[str] = [
        ytdlp,
        "-f", "bv*+ba/b",
        "--merge-output-format", merge_format,
        "--no-playlist",
        "--no-warnings",
        "--no-overwrites",
        "-o", output_template,
    ]
    if referer:
        cmd += ["--referer", referer]
    if cookies_file:
        cmd += ["--cookies", cookies_file]
    elif cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    if extra_args:
        cmd += list(extra_args)

    # Log the invocation (hide cookies_file path for brevity)
    extras: list[str] = []
    if referer:
        extras.append(f"referer={referer}")
    if cookies_file:
        extras.append(f"cookies={Path(cookies_file).name}")
    elif cookies_from_browser:
        extras.append(f"cookies-from-browser={cookies_from_browser}")
    extra_str = f" ({', '.join(extras)})" if extras else ""
    _log(f"  $ yt-dlp ... {url}{extra_str}")

    if live_flags:
        cmd += live_flags

    cmd.append(url)

    # ------------------------------------------------------------------
    # Run yt-dlp, streaming output line by line
    # ------------------------------------------------------------------
    # Hard cap for LIVE recording.  --download-sections is a VOD-only
    # seek and is silently ignored on a live HLS sliding-window
    # playlist, so yt-dlp/ffmpeg would keep recording until the full
    # `timeout` (default 3600s) -- an hour of an ad-preview live stream,
    # flooding the log.  When we injected live_flags, clamp the
    # subprocess deadline to the intended record window + a small
    # margin so the recording actually stops near rec_s.
    eff_timeout = timeout
    if live_flags:
        _rec = int(os.environ.get(
            "PAPRIKA_LIVE_HLS_RECORD_S", str(_DEFAULT_LIVE_RECORD_S)))
        eff_timeout = min(timeout, max(15, _rec) + 45)
    deadline = time.monotonic() + eff_timeout

    # ------------------------------------------------------------------
    # Stall + min-rate kill gates (mirror of core/fetcher.py inline).
    #
    # Observed on .143/.152/.151/.153/.154/.156: yt-dlp dribbling video
    # at 20 KiB/s -> ETA 24 minutes monopolises the worker's asyncio
    # thread-pool slot, the event loop can't fire its heartbeat task,
    # hub TTL's the worker as offline.  ``timeout`` doesn't trip because
    # yt-dlp KEEPS emitting progress lines -- it's not hung, just
    # glacial.  Need stall AND min-rate gates to abort early.
    #
    # PAPRIKA_YTDLP_NO_PROGRESS_S
    #   Kill if download % has not advanced (by >=0.1%) for this long.
    #   Default 90s.  Set 0 to disable.
    # PAPRIKA_YTDLP_MIN_RATE_KIBS
    #   Floor download rate in KiB/s.  Default 50.  Set 0 to disable.
    # PAPRIKA_YTDLP_MIN_RATE_GRACE_S
    #   Don't kill on low rate until it's been below the floor for at
    #   least this many continuous seconds.  Default 60s.
    #
    # Live recordings are exempt -- the rate gate would constantly fire
    # on a sliding-window manifest that genuinely outputs slowly, and
    # the deadline above already clamps live to rec_s + 45s.
    # 45s no-progress abandon (operator rule 2026-07-04): a video whose
    # download % has not advanced for 45s is treated as stuck -- kill yt-dlp
    # and hand the stream to the ffmpeg fallback below (parallel-hls segment
    # fetch + local ffmpeg mux), which beats the per-connection CDN rate cap
    # that yt-dlp's single reader gets throttled by. Env-overridable.
    _no_progress_s = float(os.environ.get("PAPRIKA_YTDLP_NO_PROGRESS_S", "45"))
    _min_rate_kibs = float(os.environ.get("PAPRIKA_YTDLP_MIN_RATE_KIBS", "50"))
    _min_rate_grace_s = float(os.environ.get("PAPRIKA_YTDLP_MIN_RATE_GRACE_S", "60"))
    if live_flags:
        # Live HLS: never trip stall/rate gates -- the deadline already
        # bounds the recording window to rec_s + a small margin.
        _no_progress_s = 0.0
        _min_rate_kibs = 0.0
    _last_pct: float | None = None
    _last_pct_at = time.monotonic()
    _slow_rate_since: float | None = None
    # Set when the stall / min-rate gate SIGTERMs yt-dlp. Routes the failure
    # into the ffmpeg fallback below (instead of just returning failure) so a
    # stalled/glacial HLS stream gets a second chance via parallel-segment
    # fetch + ffmpeg mux.
    _kill_reason: str | None = None

    returncode = -1
    try:
        with subprocess.Popen(
            cmd + ["--newline"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as proc:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                _log(line)
                now_m = time.monotonic()
                if now_m > deadline:
                    proc.kill()
                    msg = f"timeout after {timeout}s"
                    return {"ok": False, "message": msg, "log_lines": lines}

                # Parse progress + rate from this line, if any.
                pct, rate_kibs = _parse_ytdlp_progress_line(line)

                # noVNC operator priority. If the caller wired up an
                # _is_protected_fn that currently returns True, an
                # operator is actively driving the session via noVNC
                # and we DEFER any kill -- reset the relevant gate
                # timers and keep going. Evidence preservation outranks
                # the automatic "too slow / stalled" verdict when a
                # human is literally watching.
                _is_protected_now = False
                if _is_protected_fn is not None:
                    try:
                        _is_protected_now = bool(_is_protected_fn())
                    except Exception:
                        _is_protected_now = False

                # ---- Stall gate (percentage didn't advance) ----
                if _no_progress_s > 0 and pct is not None:
                    if _last_pct is None or pct - _last_pct >= 0.1:
                        _last_pct = pct
                        _last_pct_at = now_m
                    elif now_m - _last_pct_at > _no_progress_s:
                        if _is_protected_now:
                            _log(
                                f"  -- stall gate: deferred kill "
                                f"({_last_pct:.1f}% for "
                                f"{now_m - _last_pct_at:.0f}s) — "
                                f"noVNC operator is interacting"
                            )
                            _last_pct_at = now_m
                        else:
                            proc.kill()
                            msg = (
                                f"stalled: download stuck at {_last_pct:.1f}% "
                                f"for {_no_progress_s:.0f}s "
                                f"(PAPRIKA_YTDLP_NO_PROGRESS_S)"
                            )
                            _log(f"  !! {msg} -- handing to ffmpeg fallback")
                            _kill_reason = "stalled"
                            break

                # ---- Min-rate gate (download too slow for too long) ----
                if _min_rate_kibs > 0 and rate_kibs is not None:
                    if rate_kibs < _min_rate_kibs:
                        if _slow_rate_since is None:
                            _slow_rate_since = now_m
                        elif now_m - _slow_rate_since > _min_rate_grace_s:
                            if _is_protected_now:
                                _log(
                                    f"  -- rate gate: deferred kill "
                                    f"({rate_kibs:.1f} KiB/s for "
                                    f"{now_m - _slow_rate_since:.0f}s) — "
                                    f"noVNC operator is interacting"
                                )
                                _slow_rate_since = now_m
                            else:
                                proc.kill()
                                msg = (
                                    f"too slow: {rate_kibs:.1f} KiB/s < "
                                    f"{_min_rate_kibs:.0f} KiB/s for "
                                    f"{_min_rate_grace_s:.0f}s "
                                    f"(PAPRIKA_YTDLP_MIN_RATE_KIBS / GRACE_S)"
                                )
                                _log(f"  !! {msg} -- handing to ffmpeg fallback")
                                _kill_reason = "slow"
                                break
                    else:
                        # Rate recovered above the floor; reset the grace.
                        _slow_rate_since = None
            proc.wait()
            returncode = proc.returncode
    except Exception as exc:
        msg = f"failed to spawn yt-dlp: {exc}"
        return {"ok": False, "message": msg, "log_lines": lines}

    if returncode == 0:
        last = lines[-1] if lines else "(ok)"
        return {"ok": True, "message": last, "log_lines": lines}

    # ------------------------------------------------------------------
    # ffmpeg-direct fallback for "extension-disguised AES-128 HLS".
    #
    # Some video hosts (index page → third-party stream CDN) serve an
    # HLS manifest whose segments use a ``.js`` extension AND are
    # AES-128 encrypted.  yt-dlp delegates such streams to ffmpeg,
    # which by default rejects non-media segment extensions:
    #
    #   URL .../segment_000.js is not in allowed_segment_extensions
    #   ffmpeg exited with code 183
    #
    # The fix is to call ffmpeg directly with the segment-extension
    # whitelist disabled and the Referer header injected so the AES
    # key + segments fetch succeeds.  ffmpeg's ``crypto+https://``
    # protocol then transparently decrypts the AES-128 stream.
    # Proven on the stream CDN: produces a clean h264/aac MP4.
    _looks_like_ext_blocked = any(
        "allowed_segment_extensions" in ln
        or "Invalid data found when processing input" in ln
        for ln in lines
    )
    _is_hls = bool(re.search(r"\.m3u8($|\?)", url, re.I))
    # Try the ffmpeg fallback for any HLS non-live failure that the
    # parallel-segment path can actually help with:
    #   * ext-blocked  -- disguised .js/AES-128 segments yt-dlp/ffmpeg reject
    #   * stalled/slow -- the stall (45s no-progress) or min-rate gate SIGTERM'd
    #     yt-dlp; a 16-way parallel segment fetch beats the per-connection CDN
    #     rate cap that was throttling yt-dlp's single reader (operator rule
    #     2026-07-04: 45s no progress -> terminate -> ffmpeg).
    _stalled_or_slow = _kill_reason in ("stalled", "slow")
    if _is_hls and not live_flags and (_looks_like_ext_blocked or _stalled_or_slow):
        # First try the PARALLEL downloader: fetch all segments + key
        # concurrently to local disk (ffmpeg-direct's single connection
        # is rate-limited by the CDN to ~1x realtime; 16-way parallel
        # measured 21x on the stream CDN), then let ffmpeg decrypt +
        # mux from local files.  This beats the CDN's per-connection
        # rate cap AND finishes before short-lived segment tokens
        # expire.  Falls back to ffmpeg-direct if anything goes wrong.
        _reason_txt = (
            "disguised segment extensions" if _looks_like_ext_blocked
            else f"yt-dlp {_kill_reason} (gate SIGTERM)"
        )
        _log(
            f"  ↻ {_reason_txt}; trying parallel segment download "
            f"+ local ffmpeg mux"
        )
        _pd_timeout = max(60, int(deadline - time.monotonic()))
        pd_result = _parallel_hls_to_mp4(
            url=url,
            out_dir=out_dir,
            referer=referer,
            user_agent=user_agent,
            timeout=_pd_timeout,
            log=_log,
        )
        if pd_result["ok"]:
            return {
                "ok": True,
                "message": pd_result["message"],
                "log_lines": lines,
            }
        _log(
            f"  parallel downloader failed ({pd_result['message']}); "
            f"falling back to ffmpeg-direct"
        )
        _ff_timeout = max(30, int(deadline - time.monotonic()))
        ff_result = _ffmpeg_direct_hls(
            url=url,
            out_dir=out_dir,
            referer=referer,
            user_agent=user_agent,
            timeout=_ff_timeout,
            log=_log,
        )
        if ff_result["ok"]:
            return {
                "ok": True,
                "message": ff_result["message"],
                "log_lines": lines,
            }
        _log(f"  ffmpeg-direct fallback also failed: {ff_result['message']}")

    err_tail = lines[-3:]
    msg = "\n".join(err_tail) if err_tail else f"exit={returncode}"
    return {"ok": False, "message": msg, "log_lines": lines}


def _parallel_hls_to_mp4(
    *,
    url: str,
    out_dir: Path,
    referer: str | None,
    user_agent: str | None,
    timeout: int,
    log: LogFn,
    max_workers: int = 16,
) -> dict:
    """Download every HLS segment (and the AES key) CONCURRENTLY to a
    local temp dir, rewrite the manifest to point at the local files,
    then let ffmpeg decrypt + mux from disk.

    Why: CDNs like the stream CDN rate-limit each connection to ~1x
    realtime, so ffmpeg's single-connection HLS read crawls (a 79-min
    video takes ~79 min and often outlives the segment token).  A
    16-way parallel fetch saturates the link instead of the per-stream
    cap -- measured 21x on the stream CDN -- so the whole stream lands
    in a couple minutes, well inside the token TTL.  ffmpeg then muxes
    from local files in seconds (no network), reusing its built-in
    AES-128 + arbitrary-extension handling so we need no crypto lib.

    Returns ``{ok, message}``.
    """
    import urllib.request as _ur
    import concurrent.futures as _cf
    import tempfile
    from urllib.parse import urljoin, urlparse, unquote

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg not found on PATH"}

    hdrs: dict[str, str] = {
        "User-Agent": user_agent or _FALLBACK_USER_AGENT,
    }
    if referer:
        hdrs["Referer"] = referer

    def _fetch(u: str, timeout_s: float = 20.0) -> bytes:
        req = _ur.Request(u, headers=hdrs)
        with _ur.urlopen(req, timeout=timeout_s) as r:
            return r.read()

    # 1. Fetch + parse the manifest.
    try:
        manifest = _fetch(url, 15.0).decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "message": f"manifest fetch failed: {e}"}

    if "#EXT-X-STREAM-INF" in manifest:
        # Master playlist -- pick the highest-bandwidth variant and
        # recurse once.  (Most disguised-HLS players hand us a media
        # playlist directly, but handle the master case too.)
        best_url = None
        best_bw = -1
        lines_m = manifest.splitlines()
        for i, ln in enumerate(lines_m):
            if ln.startswith("#EXT-X-STREAM-INF"):
                mbw = re.search(r"BANDWIDTH=(\d+)", ln)
                bw = int(mbw.group(1)) if mbw else 0
                # next non-comment line is the variant URI
                for j in range(i + 1, len(lines_m)):
                    if lines_m[j].strip() and not lines_m[j].startswith("#"):
                        if bw > best_bw:
                            best_bw = bw
                            best_url = urljoin(url, lines_m[j].strip())
                        break
        if not best_url:
            return {"ok": False, "message": "master playlist had no variant"}
        log(f"  [parallel-hls] master playlist -> variant @ {best_bw} bps")
        return _parallel_hls_to_mp4(
            url=best_url, out_dir=out_dir, referer=referer,
            user_agent=user_agent, timeout=timeout, log=log,
            max_workers=max_workers,
        )

    # Segment URIs (every non-comment line), resolved against the
    # manifest URL so post-redirect subdomains are correct.
    seg_uris = [
        ln.strip() for ln in manifest.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    if not seg_uris:
        return {"ok": False, "message": "no segments in manifest"}
    seg_urls = [urljoin(url, s) for s in seg_uris]

    # AES key (if any).  We download it once and rewrite the KEY line
    # to a local file so ffmpeg reads the key from disk.
    key_bytes: bytes | None = None
    key_line_idx = -1
    manifest_lines = manifest.splitlines()
    for idx, ln in enumerate(manifest_lines):
        if ln.startswith("#EXT-X-KEY") and "URI=" in ln:
            m = re.search(r'URI="([^"]+)"', ln)
            if m:
                key_url = urljoin(url, m.group(1))
                try:
                    key_bytes = _fetch(key_url, 15.0)
                    key_line_idx = idx
                except Exception as e:
                    return {"ok": False, "message": f"key fetch failed: {e}"}
            break

    # 2. Parallel-download all segments to a temp dir.
    work = Path(tempfile.mkdtemp(prefix="paprika-phls-", dir=str(out_dir)))
    deadline = time.monotonic() + timeout
    n = len(seg_urls)
    log(f"  [parallel-hls] {n} segments, {max_workers}-way parallel")

    errors: list[str] = []

    def _dl(i_u):
        i, u = i_u
        if time.monotonic() > deadline:
            return (i, False, "deadline")
        try:
            data = _fetch(u, 30.0)
            (work / f"seg_{i:05d}.ts").write_bytes(data)
            return (i, True, None)
        except Exception as e:
            return (i, False, str(e)[:80])

    done = 0
    try:
        with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for (i, ok_, err) in ex.map(_dl, list(enumerate(seg_urls))):
                done += 1
                if not ok_:
                    errors.append(f"seg{i}:{err}")
                if done % 200 == 0:
                    log(f"  [parallel-hls] {done}/{n} segments")
    except Exception as e:
        return {"ok": False, "message": f"parallel fetch crashed: {e}"}

    ok_count = n - len(errors)
    if ok_count == 0:
        return {"ok": False, "message": f"all {n} segments failed; e.g. {errors[:1]}"}
    if errors:
        log(f"  [parallel-hls] WARNING {len(errors)}/{n} segments failed "
            f"(continuing with {ok_count})")

    # 3. Write the local key + a rewritten manifest.
    if key_bytes is not None and key_line_idx >= 0:
        (work / "key.bin").write_bytes(key_bytes)
        manifest_lines[key_line_idx] = re.sub(
            r'URI="[^"]+"', 'URI="key.bin"', manifest_lines[key_line_idx]
        )

    out_lines: list[str] = []
    seg_i = 0
    for ln in manifest_lines:
        if ln.strip() and not ln.startswith("#"):
            # Only reference segments that actually downloaded.
            local = work / f"seg_{seg_i:05d}.ts"
            if local.exists():
                out_lines.append(f"seg_{seg_i:05d}.ts")
            seg_i += 1
        else:
            out_lines.append(ln)
    local_manifest = work / "index.m3u8"
    local_manifest.write_text("\n".join(out_lines), encoding="utf-8")

    # 4. ffmpeg mux from local files (fast: no network).
    from urllib.parse import urlparse, unquote
    stem = "video"
    try:
        base = Path(unquote(urlparse(url).path)).stem or "video"
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:80] or "video"
    except Exception:
        pass
    out_path = out_dir / f"{stem}_parallel.mp4"

    cmd = [
        ffmpeg, "-y",
        "-allowed_extensions", "ALL",
        "-extension_picky", "0",
        "-i", str(local_manifest),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        # Progressive MP4 with the moov moved to the FRONT (+faststart).
        # All segments are already on local disk here, so the mux always
        # completes -- no truncation risk -- which means we do NOT need
        # the fragmented (empty_moov) layout.  A fragmented MP4 has only
        # a tiny empty moov + moof/mdat fragments; ffprobe/VLC read it,
        # but Chrome's progressive (non-MSE) player can't play it via a
        # direct <video src> / tab navigation.  +faststart produces a
        # standard sample-table moov at the front that plays everywhere.
        "-movflags", "+faststart",
        str(out_path),
    ]
    log(f"  [parallel-hls] muxing {ok_count} local segments -> {out_path.name}")
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=max(30, int(deadline - time.monotonic())) + 120,
        )
        mux_ok = proc.returncode == 0
        if not mux_ok:
            tail = "\n".join(proc.stdout.splitlines()[-3:]) if proc.stdout else ""
            log(f"  [parallel-hls] ffmpeg mux rc={proc.returncode}: {tail[:200]}")
    except Exception as e:
        return {"ok": False, "message": f"local mux failed: {e}"}
    finally:
        # Clean up the segment temp dir (keep only the final mp4).
        try:
            import shutil as _sh
            _sh.rmtree(work, ignore_errors=True)
        except Exception:
            pass

    if out_path.exists() and out_path.stat().st_size > 0:
        sz = out_path.stat().st_size
        partial = " (partial: some segments failed)" if errors else ""
        return {
            "ok": True,
            "message": f"parallel-hls OK: {out_path.name} "
                       f"({sz // 1024 // 1024} MB, {ok_count}/{n} segs){partial}",
        }
    return {"ok": False, "message": "mux produced no usable output"}


def _ffmpeg_direct_hls(
    *,
    url: str,
    out_dir: Path,
    referer: str | None,
    user_agent: str | None,
    timeout: int,
    log: LogFn,
) -> dict:
    """Download an HLS stream by invoking ffmpeg directly.

    Handles two anti-scraping tricks yt-dlp's ffmpeg delegation can't:
      * segments with disguised extensions (.js, .png, ...) -- via
        ``-allowed_extensions ALL``
      * AES-128 encryption needing a Referer to fetch the key -- via
        ``-headers "Referer: ...\\r\\n"`` (ffmpeg's crypto+https
        protocol then decrypts transparently)

    Output filename: ``<m3u8-stem> [ffdirect].mp4`` in ``out_dir``.
    Returns ``{ok, message}``.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg not found on PATH"}

    # Derive a stable output name from the manifest path.
    from urllib.parse import urlparse, unquote
    stem = "video"
    try:
        p = unquote(urlparse(url).path)
        base = Path(p).stem or "video"
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:80] or "video"
    except Exception:
        pass
    out_path = out_dir / f"{stem}_ffdirect.mp4"

    cmd: list[str] = [ffmpeg, "-y"]
    # Input-side options (MUST precede -i).
    hdrs = []
    if referer:
        hdrs.append(f"Referer: {referer}")
    if user_agent:
        hdrs.append(f"User-Agent: {user_agent}")
    if hdrs:
        cmd += ["-headers", "".join(h + "\r\n" for h in hdrs)]
    cmd += [
        "-allowed_extensions", "ALL",
        "-extension_picky", "0",
        "-i", url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        # Fragmented MP4: the moov atom is written at the FRONT and
        # each fragment is self-contained, so the output is playable
        # even if ffmpeg is killed mid-download (timeout, token
        # expiry, network drop).  A plain MP4 writes moov at the END,
        # so a truncated file is unplayable ("moov atom not found").
        # Critical for evidence preservation: a partial recording is
        # still usable.
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        str(out_path),
    ]
    log(f"  $ ffmpeg-direct -> {out_path.name}")

    deadline = time.monotonic() + timeout
    rc = -1
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        last_progress = ""
        timed_out = False
        import select as _select
        while True:
            if time.monotonic() > deadline:
                # Graceful stop: send 'q' so ffmpeg finalises the
                # current fragment + container before exiting, then
                # give it a few seconds before a hard kill.
                timed_out = True
                try:
                    if proc.stdin:
                        proc.stdin.write("q")
                        proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=8)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                break
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip("\r\n")
            if line.startswith("frame=") or "time=" in line:
                last_progress = line
                continue
            if line and ("error" in line.lower() or "Opening" in line):
                log(f"    [ffmpeg] {line[:160]}")
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        rc = proc.returncode if proc.returncode is not None else -1
        if last_progress:
            log(f"    [ffmpeg] {last_progress[:120]}")
    except Exception as exc:
        return {"ok": False, "message": f"ffmpeg spawn failed: {exc}"}

    # Success = a non-empty fragmented MP4 exists.  A graceful-'q'
    # exit on timeout still produces a playable file, so treat that
    # as success too (the file is real evidence, just truncated).
    if out_path.exists() and out_path.stat().st_size > 0:
        sz = out_path.stat().st_size
        tag = " (truncated at timeout)" if timed_out else ""
        return {
            "ok": True,
            "message": f"ffmpeg-direct OK: {out_path.name} ({sz // 1024} KB){tag}",
        }
    return {"ok": False, "message": f"ffmpeg exited {rc}, no usable output"}


# ---------------------------------------------------------------------------
# yt-dlp progress line parser (used by the stall + min-rate kill gates above)
# ---------------------------------------------------------------------------
# DUPLICATED from core/fetcher.py on purpose: this file is self-contained
# (see module docstring) so the hub-plugin bootstrap can import it as an
# isolated subprocess without a core/ dependency.  Keep the two copies in
# sync.  Format matches yt-dlp's --newline progress output:
#
#   [download]  45.2% of  1.20GiB at  5.00MiB/s ETA 00:30
#   [download]  20.3% of   34.48MiB at   21.67KiB/s ETA 21:38
#
# Returns ``(percent, rate_in_KiB_per_second)`` -- both ``None`` on a
# non-progress line.  Rate is normalised to KiB/s regardless of yt-dlp's
# reported unit (B / KiB / MiB / GiB).
_YTDLP_PROGRESS_RE = re.compile(
    r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+"
    r"[\d.]+\s*[KMGT]?i?B(?:[^\s]*)\s+at\s+"
    r"([\d.]+)\s*([KMGT]?)i?B/s"
)
_RATE_UNIT_TO_KIBS: dict[str, float] = {
    "":  1.0 / 1024.0,  # bytes/s -> KiB/s
    "K": 1.0,
    "M": 1024.0,
    "G": 1024.0 * 1024.0,
    "T": 1024.0 * 1024.0 * 1024.0,
}


def _parse_ytdlp_progress_line(line: str) -> tuple[float | None, float | None]:
    """Extract ``(percent, rate_KiB/s)`` from a yt-dlp progress line.

    Returns ``(None, None)`` for non-progress lines so callers can use
    plain ``is not None`` checks. Defensive: any parsing error falls
    back to ``(None, None)`` rather than raising.
    """
    try:
        m = _YTDLP_PROGRESS_RE.search(line)
        if not m:
            return None, None
        pct = float(m.group(1))
        rate_val = float(m.group(2))
        unit = m.group(3)
        rate_kibs = rate_val * _RATE_UNIT_TO_KIBS.get(unit, 1.0)
        return pct, rate_kibs
    except Exception:
        return None, None
