#!/usr/bin/env python3
"""paprika GPU temp exporter.

Tiny stdlib-only HTTP server that exposes the local GPU temperature/util
(read via ``nvidia-smi``) as JSON, so the paprika hubs' engine *thermal
gate* can pace AI calls to the GPU's thermal headroom instead of
saturating a single card (see ``server/hub/thermal.py``).

It also keeps a rolling **1-hour history** of temperature, sampled
continuously in the background (independent of HTTP traffic), so the
admin UI can draw the past-hour graph the moment an engine is opened and
keep extending it live. The exporter is a single process per GPU box, so
this history is the one consistent source every hub reads -- no Redis, no
per-hub buffer drift under nginx round-robin.

**Per-GPU addressing** (for boxes with >1 card, e.g. balcony=192.168.50.31
runs 2x RTX 3090, one vLLM engine pinned per GPU): pass ``?gpu=N`` to
target a single card. ``/?gpu=1`` reports that card's temp *as*
``max_temp_c`` (so ``thermal.read_temp`` targets it unchanged) and
``/history?gpu=1`` returns that card's own 1-hour series. Without the
param the endpoints keep their original box-wide ("hottest GPU")
behaviour, so single-GPU boxes and old callers are unaffected.

Runs on each GPU/vLLM box (e.g. 192.168.50.26, 192.168.50.31). No deps.

  GET /            -> {"max_temp_c", "gpus":[{temp_c,util_pct,power_w,power_limit_w}], "ts"}
  GET /?gpu=N      -> same shape but max_temp_c = GPU N's temp, gpus=[GPU N]
  GET /history     -> {"history":[[ts,temp],...], "interval_s", "retain_s", "now"}
  GET /history?gpu=N -> per-GPU history series for card N
  GET /healthz     -> {"ok": true}

Env:
  PAPRIKA_GPU_EXPORTER_PORT     (default 9402)
  PAPRIKA_GPU_EXPORTER_CACHE_S  (default 2.0)   -- nvidia-smi read cache
  PAPRIKA_GPU_EXPORTER_SAMPLE_S (default 10.0)  -- history sample interval
  PAPRIKA_GPU_EXPORTER_RETAIN_S (default 3600)  -- history retention (1h)

Deploy: see scripts/paprika-gpu-exporter.service (systemd unit) or run via
a self-restarting wrapper + cron @reboot.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_PORT = int(os.environ.get("PAPRIKA_GPU_EXPORTER_PORT", "9402"))
_CACHE_S = float(os.environ.get("PAPRIKA_GPU_EXPORTER_CACHE_S", "2.0"))
_SAMPLE_S = float(os.environ.get("PAPRIKA_GPU_EXPORTER_SAMPLE_S", "10.0"))
_RETAIN_S = float(os.environ.get("PAPRIKA_GPU_EXPORTER_RETAIN_S", "3600.0"))

_cache: dict = {"ts": 0.0, "data": None}
# Rolling history of (ts, max_temp_c, per_gpu_temps). Time-trimmed to
# _RETAIN_S; the maxlen is a hard safety cap (~1h at the sample interval,
# plus slack). ``per_gpu_temps`` is a tuple indexed by GPU order so a
# ``?gpu=N`` query can replay a single card's own series.
_hist: deque = deque(maxlen=int(_RETAIN_S / max(_SAMPLE_S, 1.0)) + 32)
_hist_lock = threading.Lock()


def _read_gpu() -> dict:
    out = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=temperature.gpu,utilization.gpu,power.draw,power.limit",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    gpus: list = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            gpus.append(
                {
                    "temp_c": float(parts[0]),
                    "util_pct": float(parts[1]),
                    "power_w": float(parts[2]),
                    "power_limit_w": float(parts[3]),
                }
            )
        except ValueError:
            continue
    max_temp = max((g["temp_c"] for g in gpus), default=0.0)
    return {"max_temp_c": max_temp, "gpus": gpus, "ts": time.time()}


def _cached() -> dict:
    now = time.time()
    if _cache["data"] is None or (now - _cache["ts"]) >= _CACHE_S:
        _cache["data"] = _read_gpu()
        _cache["ts"] = now
    return _cache["data"]


def _gpu_view(data: dict, gpu: int | None) -> dict:
    """Project the box reading to a single card when ``gpu`` is a valid
    index: report that card's temp *as* ``max_temp_c`` so the hub's
    ``thermal.read_temp`` (which reads ``max_temp_c``) targets it without
    changes. Out-of-range / None -> the original box-wide reading (safe
    default: a mis-set index still throttles on the box's hottest card)."""
    if gpu is None:
        return data
    gpus = data.get("gpus") or []
    if 0 <= gpu < len(gpus):
        g = gpus[gpu]
        return {
            "max_temp_c": g.get("temp_c"),
            "gpus": [g],
            "gpu": gpu,
            "ts": data.get("ts"),
        }
    return data


def _sampler() -> None:
    """Background loop: record each GPU's temp (and the box max) every
    _SAMPLE_S into the rolling history, independent of HTTP traffic
    ("常時ウォッチ")."""
    while True:
        try:
            d = _cached()
            ts = float(d.get("ts") or time.time())
            temp = float(d.get("max_temp_c") or 0.0)
            per = tuple(float(g.get("temp_c") or 0.0) for g in (d.get("gpus") or []))
            with _hist_lock:
                _hist.append((round(ts, 1), temp, per))
                cutoff = ts - _RETAIN_S
                while _hist and _hist[0][0] < cutoff:
                    _hist.popleft()
        except Exception:
            pass
        time.sleep(_SAMPLE_S)


def _history_payload(gpu: int | None = None) -> dict:
    with _hist_lock:
        rows = list(_hist)
    if gpu is None:
        items = [[ts, temp] for (ts, temp, _per) in rows]
    else:
        # Only rows that actually captured this card's temp (steady once
        # the box has >gpu cards; guards against any short startup gap).
        items = [[ts, per[gpu]] for (ts, _temp, per) in rows if gpu < len(per)]
    return {
        "history": items,
        "interval_s": _SAMPLE_S,
        "retain_s": _RETAIN_S,
        "now": time.time(),
        "gpu": gpu,
    }


def _parse_gpu(query: str) -> int | None:
    """Parse ``?gpu=N`` into a non-negative int, or None when absent/bad."""
    try:
        vals = parse_qs(query or "").get("gpu")
        if not vals:
            return None
        gi = int(vals[0])
        return gi if gi >= 0 else None
    except (ValueError, TypeError):
        return None


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        gpu = _parse_gpu(parsed.query)
        if path == "/healthz":
            self._send(200, {"ok": True})
            return
        if path == "/history":
            self._send(200, _history_payload(gpu))
            return
        try:
            self._send(200, _gpu_view(_cached(), gpu))
        except Exception as e:  # nvidia-smi missing / timeout / parse error
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, *args) -> None:  # quiet -- no access log spam
        return


def main() -> None:
    t = threading.Thread(target=_sampler, name="gpu-sampler", daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", _PORT), _Handler)
    print(
        f"[gpu-exporter] serving on :{_PORT} "
        f"(nvidia-smi cache {_CACHE_S}s, history {_SAMPLE_S}s x {_RETAIN_S}s)",
        flush=True,
    )
    srv.serve_forever()


if __name__ == "__main__":
    main()
