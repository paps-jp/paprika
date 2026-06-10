"""Per-engine GPU thermal throttle (local-GPU engines).

An :class:`~server.hub.engines.EngineRecord` can declare a thermal window:

  * ``gpu_temp_stop_c``   (受付停止温度) -- stop accepting requests at >= this.
  * ``gpu_temp_resume_c`` (開始温度)     -- resume accepting at <= this.

Throttling is OFF when ``gpu_temp_stop_c <= 0`` (the default; every cloud
engine). The GPU temperature is read from ``gpu_temp_url``, or derived from
the engine ``endpoint`` host on the exporter port (9402) when blank -- one
``scripts/gpu_temp_exporter.py`` runs per GPU box and reports its hottest GPU.

A per-engine HYSTERESIS latch (stop high / resume low) prevents flapping.
When a window IS configured but the temperature can't be read we fail
CLOSED (throttled): protecting the GPU beats serving blind.

Selection helpers (``first_accepting``) let the engine-resolve paths fail
OVER to a cooler engine of the same kind, and surface "all throttled" so
the caller can return an error (acceptable per operator: when every local
GPU is throttling, Agent/LLM calls may error).
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import httpx

# Exporter port + read cache. The exporter caches nvidia-smi itself, and we
# cache the HTTP read here, so many call sites cost ~one GET per box per
# _CACHE_S regardless of request volume.
_DEFAULT_PORT = int(os.environ.get("PAPRIKA_GPU_EXPORTER_PORT", "9402"))
_CACHE_S = float(os.environ.get("PAPRIKA_GPU_TEMP_CACHE_S", "5.0"))
_STALE_OK_S = 30.0       # reuse a recent reading across a brief exporter blip
_DEFAULT_BAND_C = 15.0   # resume = stop - band when resume isn't sensible

# url -> {"ts": float, "temp": float}
_temp_cache: dict = {}
# slug -> bool (True = accepting). Hysteresis latch, per hub process. All
# hubs read the same exporter per box, so their latches converge.
_latch: dict = {}

# url -> {"ts": float, "hist": list}. The rolling 1-hour temp history is
# owned by the exporter (one process per box = one consistent series every
# hub reads); we just cache the HTTP read briefly.
_HIST_CACHE_S = float(os.environ.get("PAPRIKA_GPU_HIST_CACHE_S", "4.0"))
_hist_cache: dict = {}


def exporter_url_for(rec) -> str:
    """Temp-exporter URL for an engine: explicit ``gpu_temp_url`` else
    derived from the endpoint host on the exporter port. "" when neither
    resolves."""
    url = (getattr(rec, "gpu_temp_url", "") or "").strip()
    if url:
        return url
    ep = (getattr(rec, "endpoint", "") or "").strip()
    if not ep:
        return ""
    host = urlparse(ep if "://" in ep else f"http://{ep}").hostname or ""
    if not host:
        return ""
    return f"http://{host}:{_DEFAULT_PORT}/"


def is_throttle_configured(rec) -> bool:
    """True when this engine has a thermal window (gpu_temp_stop_c > 0)."""
    try:
        return rec is not None and float(getattr(rec, "gpu_temp_stop_c", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


async def read_temp(url: str) -> float | None:
    """Hottest-GPU temperature (deg C) from the exporter at ``url``, cached
    ``_CACHE_S``. None when no URL or unreachable (and no recent cache)."""
    if not url:
        return None
    now = time.time()
    c = _temp_cache.get(url)
    if c and (now - c["ts"]) < _CACHE_S:
        return c["temp"]
    try:
        async with httpx.AsyncClient(timeout=2.0) as cli:
            r = await cli.get(url)
            t = float((r.json() or {}).get("max_temp_c"))
        _temp_cache[url] = {"ts": now, "temp": t}
        return t
    except Exception:
        if c and (now - c["ts"]) < _STALE_OK_S:
            return c["temp"]
        return None


async def read_history(url: str) -> list:
    """Rolling 1-hour temperature history ``[[ts, temp], ...]`` from the
    exporter's ``/history`` endpoint, cached ``_HIST_CACHE_S``. Empty list
    when there's no URL, the exporter is unreachable, or it predates the
    history endpoint (older exporter -> UI falls back to live accumulation)."""
    if not url:
        return []
    now = time.time()
    c = _hist_cache.get(url)
    if c and (now - c["ts"]) < _HIST_CACHE_S:
        return c["hist"]
    hurl = url.rstrip("/") + "/history"
    try:
        async with httpx.AsyncClient(timeout=3.0) as cli:
            r = await cli.get(hurl)
            hist = (r.json() or {}).get("history") or []
        if not isinstance(hist, list):
            hist = []
        _hist_cache[url] = {"ts": now, "hist": hist}
        return hist
    except Exception:
        if c and (now - c["ts"]) < _STALE_OK_S:
            return c["hist"]
        return []


def _resume_for(stop: float, resume: float) -> float:
    """Effective resume threshold: the configured value when sane, else a
    default band below ``stop`` (so a mis-set resume can't deadlock)."""
    if resume and 0 < resume < stop:
        return resume
    return max(0.0, stop - _DEFAULT_BAND_C)


async def engine_thermal_ok(rec) -> bool:
    """Hysteresis thermal gate for one engine. True = accepting requests.
    Engines without a window (stop_c<=0) always return True. Fail-CLOSED
    (False) when a window is configured but the temperature can't be read."""
    if not is_throttle_configured(rec):
        return True
    slug = getattr(rec, "slug", "") or getattr(rec, "model", "") or "?"
    stop = float(getattr(rec, "gpu_temp_stop_c", 0) or 0)
    resume = _resume_for(stop, float(getattr(rec, "gpu_temp_resume_c", 0) or 0))
    t = await read_temp(exporter_url_for(rec))
    if t is None:
        _latch[slug] = False
        return False
    if t >= stop:
        _latch[slug] = False
    elif t <= resume:
        _latch[slug] = True
    # else: within the band -> hold the latch (default True on first sight).
    return _latch.get(slug, True)


async def first_accepting(recs):
    """Return the first thermally-accepting engine in ``recs`` (evaluated in
    order), or None when every one is throttled. Engines without a window
    count as accepting. Lets a resolve path fail over between engines of the
    same kind and detect the all-throttled case."""
    for rec in recs:
        # Manual stop (停止中): skip operator-stopped engines during failover,
        # same as a throttled one (engines_disabled setting).
        try:
            slug = getattr(rec, "slug", "")
            from server.hub._state import state
            _dis = (state.settings.get("engines_disabled", "") or "") if state.settings is not None else ""
            if slug and slug in {s.strip() for s in _dis.split(",") if s.strip()}:
                continue
        except Exception:
            pass
        try:
            ok = await engine_thermal_ok(rec)
        except Exception:
            continue
        if ok:
            return rec
    return None


async def engine_thermal_snapshot(rec) -> dict:
    """UI/health view of one engine's thermal state. Advances the
    hysteresis latch (keeps it current) as a side effect."""
    if not is_throttle_configured(rec):
        return {
            "configured": False, "temp_c": None, "accepting": True,
            "stop_c": 0.0, "resume_c": 0.0, "exporter_url": "",
        }
    stop = float(getattr(rec, "gpu_temp_stop_c", 0) or 0)
    resume = _resume_for(stop, float(getattr(rec, "gpu_temp_resume_c", 0) or 0))
    url = exporter_url_for(rec)
    temp = await read_temp(url)
    accepting = await engine_thermal_ok(rec)
    history = await read_history(url)
    return {
        "configured": True, "temp_c": temp, "accepting": accepting,
        "stop_c": stop, "resume_c": resume, "exporter_url": url,
        # Rolling 1-hour series [[ts, temp], ...] for the live UI graph.
        "history": history,
    }
