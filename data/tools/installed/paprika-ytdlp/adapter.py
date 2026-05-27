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


# ---------------------------------------------------------------------------
# Live HLS detection
# ---------------------------------------------------------------------------

def _hls_is_live(url: str, referer: str | None = None) -> bool | None:
    """Fetch the first 8 KB of an HLS manifest and check for liveness.

    Returns:
        True   – live stream (no #EXT-X-ENDLIST / PLAYLIST-TYPE:VOD)
        False  – VOD / finite recording
        None   – not HLS, or couldn't determine (network error etc.)
    """
    if not re.search(r"\.m3u8($|\?)", url, re.I):
        return None
    import urllib.request as _ur
    try:
        headers: dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/137.0.0.0 Safari/537.36"
            )
        }
        if referer:
            headers["Referer"] = referer
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=8) as resp:
            content = resp.read(8192).decode("utf-8", errors="replace")
    except Exception:
        return None
    if "#EXT-X-ENDLIST" in content:
        return False
    if "#EXT-X-PLAYLIST-TYPE:VOD" in content:
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
    output_template = str(out_dir / "%(title).80s [%(id)s].%(ext)s")

    cmd: list[str] = [
        ytdlp,
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
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

    # ------------------------------------------------------------------
    # Live HLS detection
    # ------------------------------------------------------------------
    is_live = _hls_is_live(url, referer)
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
            f"{rec_s}s (PAPRIKA_LIVE_HLS_RECORD_S={rec_s})"
        )
        # Insert live flags BEFORE the URL.
        # --hls-use-mpegts: reliable container for live recording;
        # avoids seeking issues mid-stream.
        cmd += [
            "--no-live-from-start",
            "--download-sections", f"*0-{rec_s}",
            "--hls-use-mpegts",
        ]

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

    err_tail = lines[-3:]
    msg = "\n".join(err_tail) if err_tail else f"exit={returncode}"
    return {"ok": False, "message": msg, "log_lines": lines}
