"""Leaf utility helpers shared by the hub's route modules.

These four functions used to live in ``server/hub/app.py``; every route
module that needed them had to lazy-import them *through* app.py
(``from server.hub.app import _hub_base_url`` inside a function body) to
dodge the ``app.py -> routes.* -> app.py`` import cycle. They have no
dependency on the routers or on app.py itself -- only on ``_state``
(config / storage dir) + FastAPI + stdlib -- so they belong in a leaf
module everything can import at the top level.

app.py re-exports them for backwards compatibility, but new code should
``from server.hub._helpers import ...`` directly.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from server.hub._state import config, get_storage_dir


def _safe_job_file(job_id: str, *parts: str) -> Path:
    """Resolve ``{storage_dir}/{job_id}/{*parts}`` with path-traversal
    guards. Raises HTTPException(400/404) on a bad component, a missing
    job dir, an escape attempt, or a missing file."""
    if any(p in ("", "..", ".") or "\\" in p or "/" in p for p in parts):
        raise HTTPException(400, "invalid path component")
    job_dir = get_storage_dir() / job_id
    if not job_dir.exists():
        raise HTTPException(404, f"job '{job_id}' not found")
    p = job_dir.joinpath(*parts)
    try:
        p.resolve().relative_to(job_dir.resolve())
    except ValueError:
        raise HTTPException(400, "path escapes job dir")
    if not p.exists():
        raise HTTPException(404, f"file not found: {'/'.join(parts)}")
    return p


def _hub_base_url(request: Request) -> str:
    """The URL workers should use to reach this hub. Override via config
    or fall back to the incoming request's base."""
    if config.public_base_url:
        return config.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _asset_upload_url(base: str, job_id: str) -> str:
    return f"{base}/jobs/{job_id}/assets"


def _ffmpeg_q_from_quality_pct(pct: int) -> int:
    """Translate ``quality`` (0-100 perceptual) to ffmpeg's mjpeg
    ``q:v`` (2-31, lower = higher quality). Linear interpolation:

        quality=100 -> q=2  (best)
        quality=50  -> q=16 (medium)
        quality=0   -> q=31 (worst)

    Clamps out-of-range inputs to the endpoint scale before
    translating. This is the only place the inversion happens; the
    worker just consumes the final ffmpeg q value.
    """
    p = max(0, min(100, int(pct)))
    # 100 -> 2, 0 -> 31. ffmpeg_q = round(31 - p * 29 / 100)
    return max(2, min(31, round(31 - p * 29 / 100)))
