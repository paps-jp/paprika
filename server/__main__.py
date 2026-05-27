"""`python -m server` entrypoint.

Modes:
  --mode all    : single-process hub + (optional) in-process worker
  --mode hub    : hub only (API + WS endpoint)
  --mode worker : worker only — connects to hub, runs jobs
                  Add --lane-pool N to pre-spawn N dedicated browser lanes
                  (per-job Chrome + noVNC). Without --lane-pool the worker
                  uses --chrome-host/--chrome-port (or nodriver-launched
                  Chrome) for a single shared browser.
                  (--slot-pool is accepted as a deprecated alias.)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from server._logging import setup_logging

log = logging.getLogger("server")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m server")
    parser.add_argument(
        "--mode",
        choices=["all", "hub", "worker"],
        default="all",
        help="Run mode (default: all = single-process hub + worker).",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address for the HTTP server (hub/all). Default: 0.0.0.0",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the HTTP server (hub/all). Default: 8000",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("./data/jobs"),
        help="Where to store per-job working directories (default: ./data/jobs)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=2,
        help="Max concurrently running jobs in this process (default: 2)",
    )
    parser.add_argument(
        "--redis-url",
        type=str,
        default=None,
        metavar="URL",
        help="Redis DSN (e.g. redis://localhost:6379). For hub/all: enables "
        "persistent JobStore + Pub/Sub log streaming. Without it the "
        "hub falls back to in-memory store.",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Uvicorn auto-reload on code changes (--mode all/hub only).",
    )

    # ---- worker options ----
    parser.add_argument(
        "--hub-url",
        type=str,
        default="ws://paprika.lan:8000",
        help="(worker) WebSocket URL of the hub to connect to. "
        "Default: ws://paprika.lan:8000 (assumes mDNS / DNS / hosts).",
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default=None,
        help="(worker) Identifier for this worker (default: hostname-<rand>, "
        "persisted to ~/.paprika/worker_id).",
    )
    parser.add_argument(
        "--chrome-host",
        type=str,
        default=None,
        help="(worker, no lane pool) Host of a pre-running Chrome to attach "
        "to. Default: let nodriver launch its own Chrome.",
    )
    parser.add_argument(
        "--chrome-port",
        type=int,
        default=None,
        help="(worker, no lane pool) Port of the pre-running Chrome.",
    )
    parser.add_argument(
        "--labels",
        type=str,
        default=None,
        metavar="K=V,K=V",
        help="(worker) Capabilities labels for hub-side routing. "
        "Example: --labels region=jp,gpu=false",
    )
    parser.add_argument(
        "--novnc-url",
        type=str,
        default=None,
        metavar="URL",
        help="(worker, no lane pool) Public URL of this worker's noVNC "
        "viewer. Use --lane-pool for per-job dedicated browsers.",
    )
    parser.add_argument(
        "--lane-pool",
        type=int,
        default=0,
        metavar="N",
        help="(worker) Run N pre-spawned browser lanes in this process "
        "(per-job dedicated Chrome + noVNC). Each lane gets its own "
        "Xvfb display, Chrome port, and noVNC port.",
    )
    # Backwards-compat: --slot-pool is the old name. Accept it silently
    # and merge into --lane-pool below. Drop one release after the rename.
    parser.add_argument(
        "--slot-pool",
        type=int,
        default=0,
        metavar="N",
        help=argparse.SUPPRESS,  # deprecated alias of --lane-pool
    )
    parser.add_argument(
        "--novnc-public-host",
        type=str,
        default="localhost",
        help="(worker + --lane-pool) Public hostname for noVNC URLs",
    )
    parser.add_argument(
        "--novnc-base-port",
        type=int,
        default=6080,
        help="(worker + --lane-pool) First noVNC port (lane i uses base_port + i)",
    )
    parser.add_argument(
        "--worker-secret",
        type=str,
        default=None,
        help="(worker/hub) Shared secret for worker<->hub auth.",
    )
    parser.add_argument(
        "--public-base-url",
        type=str,
        default=None,
        help="(hub) Public URL workers use to reach this hub. Example: http://hub.example.com:8000",
    )
    return parser


def _run_hub_only(args) -> int:
    import uvicorn

    from server.hub import app as hub_app_module

    hub_app_module.config.data_dir = args.data_dir
    hub_app_module.config.max_concurrent_jobs = args.max_concurrent
    hub_app_module.config.redis_url = args.redis_url
    hub_app_module.config.public_base_url = args.public_base_url
    hub_app_module.config.worker_secret = args.worker_secret

    log.info(
        "mode=hub  http://%s:%d  data=%s  redis=%s",
        args.host,
        args.port,
        args.data_dir.resolve(),
        args.redis_url or "(none — in-memory)",
    )
    uvicorn.run(
        "server.hub.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        # WS ping/pong tolerance for worker control channels. Workers
        # may briefly block their event loop when yt-dlp / heavy subprocess
        # work runs (mitigated client-side via asyncio.to_thread, but a
        # generous timeout is a defensive second layer). Defaults are
        # 20s/20s which trip on a single multi-minute HLS download.
        ws_ping_interval=30.0,
        ws_ping_timeout=120.0,
    )
    return 0


def _run_all(args) -> int:
    """Same as --mode hub. (Phase 3+ doesn't need an in-process worker;
    jobs run via local fallback when no remote worker is connected.)"""
    return _run_hub_only(args)


def _run_worker(args) -> int:
    from server.worker.agent import WorkerAgent, default_worker_id

    worker_id = args.worker_id or default_worker_id()
    labels: dict[str, str] = {}
    if args.labels:
        for pair in args.labels.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                labels[k.strip()] = v.strip()

    # Honour both --lane-pool and the deprecated --slot-pool alias.
    n_lanes = args.lane_pool or args.slot_pool
    if args.slot_pool and not args.lane_pool:
        log.warning("--slot-pool is deprecated, use --lane-pool")

    lane_pool = None
    if n_lanes > 0:
        from server.worker.lanes import LanePool

        lane_pool = LanePool(
            n=n_lanes,
            public_host=args.novnc_public_host,
            base_novnc_port=args.novnc_base_port,
        )

    agent = WorkerAgent(
        hub_ws_url=args.hub_url,
        worker_id=worker_id,
        max_concurrent=n_lanes or args.max_concurrent,
        labels=labels,
        chrome_host=args.chrome_host,
        chrome_port=args.chrome_port,
        worker_secret=args.worker_secret,
        novnc_url=args.novnc_url,
        lane_pool=lane_pool,
    )
    log.info(
        "mode=worker  worker_id=%s  hub=%s  max_concurrent=%d  labels=%s%s",
        worker_id,
        args.hub_url,
        n_lanes or args.max_concurrent,
        labels,
        f"  lanes={n_lanes}" if n_lanes else "",
    )
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    setup_logging()
    parser = _build_parser()
    args = parser.parse_args()

    if args.mode == "all":
        return _run_all(args)
    if args.mode == "hub":
        return _run_hub_only(args)
    if args.mode == "worker":
        return _run_worker(args)
    parser.error(f"unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
