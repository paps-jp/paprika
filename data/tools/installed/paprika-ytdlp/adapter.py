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
    deadline = time.monotonic() + timeout
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
                if time.monotonic() > deadline:
                    proc.kill()
                    msg = f"timeout after {timeout}s"
                    return {"ok": False, "message": msg, "log_lines": lines}
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
    # Some video hosts (e.g. 7mmtv.sx → streamsuperpro.com) serve an
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
    # Proven on streamsuperpro: produces a clean h264/aac MP4.
    _looks_like_ext_blocked = any(
        "allowed_segment_extensions" in ln
        or "Invalid data found when processing input" in ln
        for ln in lines
    )
    _is_hls = bool(re.search(r"\.m3u8($|\?)", url, re.I))
    if _is_hls and _looks_like_ext_blocked and not live_flags:
        _log(
            "  ↻ yt-dlp/ffmpeg rejected disguised segment extensions; "
            "retrying with ffmpeg-direct (-allowed_extensions ALL)"
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
    out_path = out_dir / f"{stem} [ffdirect].mp4"

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
