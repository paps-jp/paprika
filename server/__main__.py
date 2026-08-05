"""`python -m server` entrypoint.

Modes:
  --mode all    : single-process hub + (optional) in-process worker
  --mode hub    : hub only (API + WS endpoint)
  --mode admin  : read-only management UI/API over the shared stores
                  (no worker WS, no job dispatch, no reapers)
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
import os
import sys
from pathlib import Path

from server._logging import setup_logging

log = logging.getLogger("server")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m server")
    parser.add_argument(
        "--mode",
        choices=["all", "hub", "worker", "admin"],
        default="all",
        help="Run mode (default: all). admin = read-only management UI/API "
        "(no worker WS, no job dispatch, no reapers) over the shared stores.",
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


def _purge_stale_part_files(base: Path) -> int:
    """Delete leftover ``*.part*`` download partials under ``base`` at startup.
    An interrupted yt-dlp video download leaves a big ``.mp4.part`` (hundreds of
    MB); across restarts these pile up and fill the disk -- a hub's /data/jobs
    filling with .part is what killed its Redis client and took the whole fleet
    down (2026-07-12 incident). Safe on a CLEAN start: no job runs yet, so every
    .part is from a dead prior run. Best-effort; never fatal. Returns #deleted."""
    n = 0
    freed = 0
    try:
        for f in base.rglob("*.part*"):
            try:
                if f.is_file():
                    freed += f.stat().st_size
                    f.unlink()
                    n += 1
            except OSError:
                pass
    except Exception:
        pass
    if n:
        log.info(
            "startup: purged %d stale .part file(s) (%.0f MB reclaimed) under %s",
            n, freed / 1e6, base,
        )
    return n


def _run_hub_only(args) -> int:
    import uvicorn

    # Reboot-safety: clear interrupted video-download partials from the durable
    # cache before serving. Left unbounded they filled the disk on 2026-07-12
    # and broke the hub's Redis client -> fleet-wide outage.
    _purge_stale_part_files(args.data_dir)

    from server.hub import app as hub_app_module

    hub_app_module.config.data_dir = args.data_dir
    hub_app_module.config.max_concurrent_jobs = args.max_concurrent
    hub_app_module.config.redis_url = args.redis_url
    hub_app_module.config.public_base_url = args.public_base_url
    # Worker<->hub shared secret. CLI flag wins; otherwise fall back to
    # PAPRIKA_WORKER_SECRET so compose / .34 deploy env can enable it
    # fleet-wide without a flag. None (both unset) keeps the check OFF.
    hub_app_module.config.worker_secret = (
        args.worker_secret or os.environ.get("PAPRIKA_WORKER_SECRET") or None
    )

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
        # Bound graceful shutdown so a hub restart doesn't hang forever waiting
        # for long-lived worker control WebSockets to close on their own. With
        # no bound (the default) uvicorn waits indefinitely -> docker's
        # `restart -t 8` SIGKILLs mid-shutdown -> the worker WS is torn down
        # uncleanly and nginx keeps the dead upstream, ghosting the worker
        # ([[worker-ghost-proxied-ws]]). A bounded period makes uvicorn actively
        # close the WS so the worker reconnects to a live hub instead of
        # ghosting. Kept under docker's -t 8 stop-grace so shutdown finishes
        # before SIGKILL. (Root cause of the recurring 60->57 ghost drift.)
        timeout_graceful_shutdown=5,
    )
    return 0


def _run_admin(args) -> int:
    """Dedicated read-only management service. Serves the admin UI + API over
    the SHARED stores (MariaDB jobs+registries, Redis workers/sessions, MinIO
    assets) but runs NO worker WS, NO job dispatch, NO reapers/leases/orphan-
    recovery. Gives one stable admin URL decoupled from the compute hubs.
    PAPRIKA_ROLE=admin is read at app-import time (server/hub/app.py)."""
    import os

    os.environ["PAPRIKA_ROLE"] = "admin"
    log.info(
        "mode=admin  http://%s:%d  (read-only management service)",
        args.host,
        args.port,
    )
    return _run_hub_only(args)


def _run_all(args) -> int:
    """Same as --mode hub. (Phase 3+ doesn't need an in-process worker;
    jobs run via local fallback when no remote worker is connected.)"""
    return _run_hub_only(args)


def _run_worker(args) -> int:
    # Bypass docker's flaky embedded DNS (127.0.0.11) BEFORE any hostname
    # resolution or Chrome lane spawns -- see server/worker/dns_fix.py. The
    # embedded resolver intermittently HANGS on cold lookups (~10-20% at
    # 4-16s), tripping the SSRF guard's pre-navigate resolve and slowing
    # Chrome's navigation. No-op unless the container is on 127.0.0.11;
    # disable with PAPRIKA_WORKER_DNS=off, customise with =ip1,ip2.
    try:
        from server.worker import dns_fix
        dns_fix.apply()
    except Exception as e:
        log.warning("dns_fix: init failed (continuing): %s", e)

    # Reboot-safety: wipe leftover per-job tempdirs from a prior run. Each holds
    # an interrupted yt-dlp ``.mp4.part`` (hundreds of MB) under a
    # ``paprika-<job>-*`` mkdtemp; across crashes/restarts these pile up and
    # fill the CT disk (cf. the hub-side .part fill that took the fleet down
    # 2026-07-12). On a clean start no job is running, so every such tempdir is
    # abandoned -> safe to remove wholesale. Best-effort; never fatal.
    try:
        import tempfile as _tf
        import shutil as _sh
        _wiped = 0
        for _d in Path(_tf.gettempdir()).glob("paprika-*"):
            # ONLY per-job workdirs (mkdtemp prefix "paprika-<job_id>-"). Do NOT
            # touch the live prefetched infra that also lives under /tmp:
            # paprika-profile-cache (~80 MB), paprika-profile-* lane clones,
            # paprika-extensions -- wiping those just forces a needless re-fetch
            # on every restart. A hex job_id can't collide with these prefixes.
            if _d.name.startswith(("paprika-profile", "paprika-extensions")):
                continue
            if _d.is_dir():
                _sh.rmtree(_d, ignore_errors=True)
                _wiped += 1
        if _wiped:
            log.info(
                "startup: wiped %d abandoned per-job tempdir(s) (interrupted "
                "downloads/.part) under %s", _wiped, _tf.gettempdir(),
            )
    except Exception as e:
        log.warning("startup tempdir purge failed (continuing): %s", e)

    from server.worker.agent import WorkerAgent, default_worker_id

    # hub_url is passed so the id can be derived from this CT's LAN IP as the
    # HUB reports it (GET /health -> client_ip). From inside the bridge-
    # networked container the kernel's own answer is 172.18.0.2 on EVERY
    # worker, which is how the whole fleet ended up claiming one identity and
    # fighting over one Chrome profile dir on the shared ramdisk (2026-08-05).
    worker_id = args.worker_id or default_worker_id(args.hub_url)

    # Same reboot-safety for the shared node-tmpfs pool, but it has to wait
    # for worker_id: the pool is shared with every other worker CT on this
    # Proxmox node, so the purge is OWNER-SCOPED. Our own leftovers are from
    # a dead prior process (no job runs yet); a neighbour's directory may be
    # a live 2-hour download and is never touched. No-op when the pool isn't
    # mounted. See server/worker/scratch_pool.py.
    try:
        from server.worker import scratch_pool as _scratch_pool

        log.info("%s", _scratch_pool.status_line())
        _n, _freed = _scratch_pool.purge_own(worker_id)
        if _n:
            log.info(
                "startup: purged %d abandoned scratch-pool dir(s) "
                "(%.0f MB reclaimed from the shared ramdisk)",
                _n, _freed / 1e6,
            )
    except Exception as e:
        log.warning("startup scratch-pool purge failed (continuing): %s", e)

    # Second node tmpfs, same idea applied to Chrome's user-data-dirs -- the
    # dominant remaining writer to the CT's thin pool once video downloads are
    # on the pool above. Owner-scoped by worker_id for the same reason the
    # pool is: one ramdisk is shared by every worker CT on the node, so an
    # unscoped directory would collide. Resolved once here, before any lane
    # spawns, so the decision (and the reason for any fallback) is visible in
    # the startup log. See docs/ramdisk-chrome-lane.md.
    try:
        from server.worker import lanes as _lanes

        _lanes.init_chrome_lane_root(worker_id)
        log.info("%s", _lanes.chrome_lane_status_line())
    except Exception as e:
        log.warning("chrome lane root probe failed (continuing): %s", e)

    # Phase 3 E (Approach B): apply the self-maintaining egress firewall BEFORE
    # any Chrome lane spawns, so private-IP egress (redirects / fetch() /
    # metadata) is blocked from the first navigation. No-op unless
    # PAPRIKA_EGRESS_GUARD=1; fetches the allowlist from the hub
    # (/fleet/egress-allow) + the worker's own HUB_URL host.
    try:
        from server.worker import egress_guard
        egress_guard.apply(args.hub_url)
    except Exception as e:
        log.warning("egress-guard: init failed (continuing without firewall): %s", e)

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

    # Downloader-role workers (PAPRIKA_WORKER_ROLE=downloader) serve ONLY
    # HubAssignVideoDownload handoffs, writing to the shared host-tmpfs
    # ramdisk. They run no Xvfb/Chrome/x11vnc, so the lane pool is skipped
    # entirely and concurrency comes from the pool's slot budget instead of
    # the lane count. See docs/ramdisk-video-tier.md.
    from server.worker.agent._mix_videodl import (
        download_slots_from_env,
        role_from_env,
    )

    worker_role = role_from_env()
    if worker_role == "downloader":
        n_lanes = 0

    lane_pool = None
    if n_lanes > 0:
        from server.worker.lanes import LanePool

        lane_pool = LanePool(
            n=n_lanes,
            public_host=args.novnc_public_host,
            base_novnc_port=args.novnc_base_port,
        )

    if worker_role == "downloader":
        _dl_slots = download_slots_from_env()
        max_concurrent = _dl_slots or args.max_concurrent
    else:
        max_concurrent = n_lanes or args.max_concurrent

    agent = WorkerAgent(
        hub_ws_url=args.hub_url,
        worker_id=worker_id,
        max_concurrent=max_concurrent,
        labels=labels,
        chrome_host=args.chrome_host,
        chrome_port=args.chrome_port,
        worker_secret=args.worker_secret or os.environ.get("PAPRIKA_WORKER_SECRET"),
        novnc_url=args.novnc_url,
        lane_pool=lane_pool,
    )
    log.info(
        "mode=worker  worker_id=%s  role=%s  hub=%s  max_concurrent=%d  labels=%s%s",
        worker_id,
        worker_role,
        args.hub_url,
        max_concurrent,
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
    if args.mode == "admin":
        return _run_admin(args)
    if args.mode == "worker":
        return _run_worker(args)
    parser.error(f"unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    rc = main()
    # os._exit bypasses interpreter shutdown (atexit / executor-thread join /
    # daemon-thread cleanup). Worker mode spawns chrome/xvfb/x11vnc subprocesses
    # and asyncio's default ThreadPoolExecutor; a job that exits abnormally can
    # leave one of those threads in a state where Python's shutdown sequence
    # logs "executor did not finishing joining its threads within 300 seconds"
    # and never returns -- the process is alive to PID-1 but never reconnects
    # to the hub. Four fleet workers stayed wedged like that across a 75c93db
    # version bump (2026-06-06) because docker's restart policy only fires on
    # exit, and the python interpreter never exited. Hard-exit forecloses that
    # failure class so the supervisor always sees a clean death and relaunches.
    os._exit(rc if isinstance(rc, int) else 0)
