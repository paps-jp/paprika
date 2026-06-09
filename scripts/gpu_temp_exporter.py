#!/usr/bin/env python3
"""paprika GPU temp exporter.

Tiny stdlib-only HTTP server that exposes the local GPU temperature/util
(read via ``nvidia-smi``) as JSON, so the paprika hubs' engine *thermal
gate* can pace AI calls to the GPU's thermal headroom instead of
saturating a single card (see ``server/hub/thermal.py``).

It also keeps a rolling **1-hour history** of the hottest-GPU temperature,
sampled continuously in the background (independent of HTTP traffic), so
the admin UI can draw the past-hour graph the moment an engine is opened
and keep extending it live. The exporter is a single process per GPU box,
so this history is the one consistent source every hub reads -- no Redis,
no per-hub buffer drift under nginx round-robin.

Runs on each GPU/vLLM box (e.g. 10.10.50.26, 10.10.50.31). No deps.

  GET /         -> {"max_temp_c", "gpus":[{temp_c,util_pct,power_w,power_limit_w}], "ts"}
  GET /history  -> {"history":[[ts,temp],...], "interval_s", "retain_s", "now"}
  GET /healthz  -> {"ok": true}

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

_PORT = int(os.environ.get("PAPRIKA_GPU_EXPORTER_PORT", "9402"))
_CACHE_S = float(os.environ.get("PAPRIKA_GPU_EXPORTER_CACHE_S", "2.0"))
_SAMPLE_S = float(os.environ.get("PAPRIKA_GPU_EXPORTER_SAMPLE_S", "10.0"))
_RETAIN_S = float(os.environ.get("PAPRIKA_GPU_EXPORTER_RETAIN_S", "3600.0"))

_cache: dict = {"ts": 0.0, "data": None}
# Rolling history of (ts, max_temp_c). Time-trimmed to _RETAIN_S; the maxlen
# is a hard safety cap (~1h at the sample interval, plus slack).
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


def _sampler() -> None:
    """Background loop: record the hottest-GPU temp every _SAMPLE_S into the
    rolling history, independent of HTTP traffic ("常時ウォッチ")."""
    while True:
        try:
            d = _cached()
            ts = float(d.get("ts") or time.time())
            temp = float(d.get("max_temp_c") or 0.0)
            with _hist_lock:
                _hist.append((round(ts, 1), temp))
                cutoff = ts - _RETAIN_S
                while _hist and _hist[0][0] < cutoff:
                    _hist.popleft()
        except Exception:
            pass
        time.sleep(_SAMPLE_S)


def _history_payload() -> dict:
    with _hist_lock:
        items = [[ts, temp] for (ts, temp) in _hist]
    return {
        "history": items,
        "interval_s": _SAMPLE_S,
        "retain_s": _RETAIN_S,
        "now": time.time(),
    }


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/healthz":
            self._send(200, {"ok": True})
            return
        if path == "/history":
            self._send(200, _history_payload())
            return
        try:
            self._send(200, _cached())
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
