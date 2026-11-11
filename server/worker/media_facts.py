"""Objective media facts for the outcome verifier (worker side).

The worker only MEASURES -- it runs ffprobe on a downloaded file while it
is still on local disk and reports what it saw. Whether that amounts to a
"success" is decided elsewhere (the learner's verify()), the same split the
occlusion probe uses (core/fetcher.probe_occlusion measures, the hub
classifies).

Kept dependency-free and separate from session_actions/handlers/media.py:
that probe also computes a perceptual hash with a second ffmpeg run and
applies caller-specific duration/identity rules, neither of which belongs
in a plain measurement.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".ts")

#: ffprobe is header-only work, but a truncated / hostile file can make it
#: scan. Bound it so a bad file can never hold a job's completion hostage.
PROBE_TIMEOUT_S = float(os.environ.get("PAPRIKA_MEDIA_FACTS_TIMEOUT_S") or 20)

#: Upper bound on files probed per job. A page that sprays dozens of preview
#: clips must not turn the end of a fetch into dozens of subprocesses; the
#: largest files are the ones that decide the outcome.
MAX_FILES = int(os.environ.get("PAPRIKA_MEDIA_FACTS_MAX_FILES") or 8)


def enabled() -> bool:
    return (os.environ.get("PAPRIKA_MEDIA_FACTS") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def is_video_name(name: str) -> bool:
    return (name or "").lower().endswith(VIDEO_EXTS)


def probe_file(path: str | os.PathLike) -> dict:
    """ffprobe one file -> facts. Sync; never raises.

    ``probed`` is False when no verdict could be formed (ffprobe missing,
    timeout): the consumer must treat that as "unknown", never as "invalid".
    """
    p = str(path)
    info: dict = {
        "name": os.path.basename(p), "bytes": 0, "probed": False,
        "playable": False, "has_video": False, "has_audio": False,
        "duration_s": 0.0, "width": 0, "height": 0, "codec": "",
        "container": "", "reason": "",
    }
    try:
        info["bytes"] = os.path.getsize(p)
    except OSError:
        info["reason"] = "missing"
        return info
    if info["bytes"] <= 0:
        info["probed"] = True
        info["reason"] = "empty"
        return info
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", p],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
        )
    except FileNotFoundError:
        info["reason"] = "ffprobe_missing"
        return info
    except subprocess.TimeoutExpired:
        info["reason"] = "probe_timeout"
        return info
    except Exception as e:  # pragma: no cover - defensive
        info["reason"] = f"probe_error:{type(e).__name__}"
        return info
    info["probed"] = True
    if proc.returncode != 0:
        info["reason"] = "not_media"
        return info
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:
        info["reason"] = "unparseable_probe"
        return info
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    vstreams = [
        s for s in streams
        if s.get("codec_type") == "video"
        # A cover-art / still "video" stream is not a video capture.
        and not (s.get("disposition") or {}).get("attached_pic")
    ]
    info["has_audio"] = any(s.get("codec_type") == "audio" for s in streams)
    info["container"] = str(fmt.get("format_name") or "")[:40]
    dur = 0.0
    try:
        dur = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    if vstreams:
        v0 = vstreams[0]
        info["has_video"] = True
        info["width"] = int(v0.get("width") or 0)
        info["height"] = int(v0.get("height") or 0)
        info["codec"] = str(v0.get("codec_name") or "")[:24]
        if dur <= 0.0:
            try:
                dur = float(v0.get("duration") or 0.0)
            except (TypeError, ValueError):
                dur = 0.0
    info["duration_s"] = round(max(dur, 0.0), 2)
    if not info["has_video"]:
        info["reason"] = "no_video_stream"
        return info
    info["playable"] = True
    info["reason"] = "ok"
    return info


def pick_paths(paths: Iterable[str | os.PathLike], limit: int = MAX_FILES) -> list[Path]:
    """Video-looking files, largest first, capped at ``limit``."""
    sized: list[tuple[int, Path]] = []
    for raw in paths:
        p = Path(raw)
        if not is_video_name(p.name):
            continue
        try:
            sized.append((p.stat().st_size, p))
        except OSError:
            continue
    sized.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in sized[: max(int(limit), 0)]]


async def probe_paths(paths: Iterable[str | os.PathLike], limit: int = MAX_FILES) -> list[dict]:
    """Probe up to ``limit`` video files off the event loop. Never raises."""
    if not enabled():
        return []
    out: list[dict] = []
    for p in pick_paths(paths, limit):
        try:
            out.append(await asyncio.to_thread(probe_file, p))
        except Exception:  # pragma: no cover - defensive
            continue
    return out
