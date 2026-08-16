"""WorkerAgent mixin: run loop + WS handshake/heartbeat/watchdog + hub-message dispatch.

Part of the agent/ package; methods reach siblings via self (MRO).
Shared helpers + Phase-1 functions come from the imports below."""

from __future__ import annotations
import asyncio
import functools
import json
import math
import os
import random
import shutil
import socket
import logging
import string
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
import httpx
from core.httpclient import make_async_client
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from core.fetcher import (
    FetchOptions,
    clone_chrome_profile,
    fetch,
)
from server.protocol import (
    AssetInfo,
    HubAssignJob,
    HubAssignVideoDownload,
    HubExpectedVersion,
    HubForceCompleteJob,
    HubProfileDelete,
    HubProfileSync,
    HubRegistered,
    HubPreviewSubscribe,
    HubScreenshotRequest,
    HubSessionAction,
    HubSessionAgent,
    HubSessionEnd,
    HubSessionInteraction,
    HubSessionStart,
    HubUpdateGate,
    JobOptions,
    JobResult,
    JobStatus,
    ProfileCacheEntry,
    SessionStateSnapshot,
    WorkerCapabilities,
    WorkerDraining,
    WorkerHeartbeat,
    WorkerJobAccepted,
    WorkerJobComplete,
    WorkerJobFailed,
    ASSET_CAPTURE_MARKER,
    JOB_PROGRESS_MARKER,
    LINKS_CAPTURE_MARKER,
    NET_CAPTURE_MARKER,
    WorkerJobLog,
    WorkerJobProgress,
    WorkerRegister,
    WorkerPreviewFrame,
    WorkerScreenshotReply,
    WorkerSessionActionResult,
    WorkerSessionAgentResult,
    WorkerSessionAnnounce,
    WorkerSessionEndAck,
    WorkerSessionStartAck,
    YtdlpResult,
    decode_hub_msg,
    encode_msg,
)
from server.scheduler import HEARTBEAT_INTERVAL
from server.worker import browser_ops
from server.worker.sessions import SessionState
from server.worker._browser_helpers import (
    _LINKS_EXTRACT_JS,
    _VIDEO_DIRECT_RE,
    _VIDEO_STREAM_RE,
    _evaluate_in_frame,
    _looks_like_player_iframe,
)
from server.worker.session_actions import (
    _ActionCtx,
    _SESSION_ACTIONS,
)
import re as _re
from ._base import *  # noqa: F401,F403
from ._base import WORKER_EXIT_CODE_VERSION_MISMATCH, _get_browser_user_agent, _logger, _session_interaction_at
from .profile import _normalise_extracted_profile, parse_attach
from .recipe import _apply_fetch_recipe, _looks_suspect
from .selfupdate import _auto_exit_on_version_mismatch, _auto_fetch_source, _check_github_release_once, _fetch_and_apply_source_from_hub, _fetch_worker_plugins_from_hub, _print_version_mismatch_banner, _versions_meaningfully_differ, default_worker_version
from .translate import _looks_non_english, _translate_to_english
from .video import _make_video_downloader, _parse_dl_progress, detect_yt_dlp
from .workerid import WORKER_ID_FILE, _WorkerIdReassigned, hub_http_base


# Module-level CPU sample state. The first heartbeat returns 0.0% because
# we have no prior baseline; from then on each call computes the delta
# (busy / total cpu jiffies) against the previous sample. Module-level is
# safe: one WorkerAgent per process.
_cpu_last_sample: tuple[int, int] | None = None


# Proactive disk-cleanup thresholds (see _heartbeat_loop). A worker whose
# disk crosses _DISK_PRESSURE_FAIL_PCT (90%) is skipped by the hub's
# pick_worker, so it can no longer be handed a job -- and the per-job
# preflight was the ONLY trigger for _emergency_disk_cleanup(). That left an
# IDLE, full CT frozen out of dispatch with nothing left to fire the
# cleanup: it sat at ~100% disk indefinitely (w51149, 2026-07-04). The
# heartbeat runs the same cleanup a little BELOW the 90% cliff so a pressured
# but idle worker sheds transient bloat and re-earns dispatch on its own.
# Only fires while in_flight <= 0 so it can never yank cache/scratch out from
# under an active download (a BUSY pressured worker is still covered by the
# next per-job preflight). Off the event loop (blocking FS walk), throttled.
_DISK_PROACTIVE_CLEANUP_PCT = float(
    os.environ.get("PAPRIKA_DISK_PROACTIVE_CLEANUP_PCT", "85")
)
_DISK_CLEANUP_MIN_INTERVAL_S = float(
    os.environ.get("PAPRIKA_DISK_CLEANUP_MIN_INTERVAL_S", "120")
)


# Host CPU core count, sampled once at import (it doesn't change) and sent in
# every heartbeat so the hub can normalise the host-level load1 into "load per
# core" for I/O-aware dispatch. os.cpu_count() returns the SYSTEM total, which
# for an LXC CT is the Proxmox node's core count (matching load1's host scope)
# and for bare-metal is the box's own -- exactly the denominator we want in both.
_NPROC = os.cpu_count() or 0


def _num_env(name: str, default: float) -> float:
    """Numeric env override. Unset / unparseable -> the caller's default, so a
    typo in a compose file degrades to the shipped behaviour instead of
    disabling a guard silently. A deliberate 0 IS honoured (turns a threshold
    off), which is why this doesn't use the ``or default`` idiom."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _sample_resources() -> tuple[float, float, float, float, float]:
    """Return (cpu_pct, mem_pct, disk_pct, disk_free_gb, load1) for this CT.

    Best-effort. A missing or unparseable /proc entry returns 0.0 for that
    field instead of raising, so the heartbeat loop stays tight and a
    funky kernel doesn't take the worker down. Designed to be called from
    the heartbeat thread (~10s cadence) so the CPU% delta window matches.

    cpu_pct + load1 are LXC-host (Proxmox node) signals because the CT
    shares /proc/stat + getloadavg with its host. disk_* are CT-local
    (overlayfs root). The split matches what an operator needs to triage
    "this CT is full" vs "this whole node is overloaded across all CTs
    sharing it".

    The mem_pct returned here is HOST-scoped and only a fallback -- see
    ``_sample_memory``. This used to be documented as "CT-local (cgroup
    memory)", which was wrong: the workers are a docker container inside an
    LXC CT, and while lxcfs virtualises /proc/meminfo for the CT, the
    container mounts a fresh procfs showing the bare kernel's numbers. Read
    inside paprika-worker-1 on boiler CT382 (an 8GB CT): MemTotal
    395,718,540 kB = the 377GB Proxmox node. Every worker on a node
    therefore reported that node's memory, identically -- which is why the
    2026-08-02 refault storm was invisible in the admin Workers tab.
    """
    global _cpu_last_sample
    cpu_pct = 0.0
    try:
        with open("/proc/stat") as f:
            fields = f.readline().split()
        # cpu user nice system idle iowait irq softirq steal guest guest_nice
        idle = int(fields[4]) + int(fields[5])
        total = sum(int(x) for x in fields[1:8])
        if _cpu_last_sample is not None:
            d_idle = idle - _cpu_last_sample[0]
            d_total = total - _cpu_last_sample[1]
            if d_total > 0:
                cpu_pct = max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total)))
        _cpu_last_sample = (idle, total)
    except (OSError, ValueError, IndexError):
        pass

    mem_pct = 0.0
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0])
        total_kb = info.get("MemTotal", 0) or 1
        # MemAvailable is the right field on kernels >=3.14 (accounts for
        # reclaimable cache); fall back to MemFree on ancient kernels.
        avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
        mem_pct = max(0.0, min(100.0, 100.0 * (1.0 - avail_kb / total_kb)))
    except (OSError, ValueError, KeyError, IndexError):
        pass

    disk_pct = 0.0
    disk_free_gb = 0.0
    try:
        du = shutil.disk_usage("/")
        if du.total > 0:
            disk_pct = max(0.0, min(100.0, 100.0 * du.used / du.total))
        disk_free_gb = du.free / (1024.0 ** 3)
    except OSError:
        pass

    load1 = 0.0
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        pass

    return cpu_pct, mem_pct, disk_pct, disk_free_gb, load1


def _sample_memory() -> tuple[str, float, "Any"]:
    """Return ``(scope, pct, sample)`` for THIS container's memory cgroup.

    ``scope`` is the honest label for what ``pct`` measures:

    ``"cgroup"``  the cgroup has a real limit, so pct is a percentage of it;
    ``"host"``    no discoverable limit -- pct is meaningless here and the
                  caller should keep its /proc/meminfo (node-wide) number,
                  clearly labelled as such;
    ``""``        no cgroup v2 memory controller at all.

    On the production fleet the usual answer is ``"host"``: the docker
    container has no limit of its own (``memory.max`` = ``max``) because the
    8GB cap lives on the parent CT cgroup, which a cgroup namespace hides.
    An operator who wants a real percentage in the Workers tab can declare it
    with ``PAPRIKA_WORKER_MEM_LIMIT_MB``. This is presentation only -- the
    memory guard's thresholds are absolute precisely so they don't depend on
    a limit nobody can read.
    """
    try:
        from server.worker import cgroup_mem
        s = cgroup_mem.sample()
    except Exception:
        return ("", 0.0, None)
    if not s.ok:
        return ("", 0.0, None)
    pct = s.limit_pct
    if pct is None:
        return ("host", 0.0, s)
    return ("cgroup", pct, s)


class _RunMixin:
    async def run(self) -> None:
        """Reconnect loop. Reconnects with backoff on disconnect."""
        # Optional GitHub-releases version check. Fires before any heavy
        # setup so a stale worker can exit fast and let its supervisor
        # pull a fresh image. Disabled unless PAPRIKA_GITHUB_REPO is set;
        # network failures are swallowed so an offline worker still
        # boots. Behaves identically to the hub-driven check on
        # mismatch (banner + sys.exit(42) when auto-exit is enabled).
        await _check_github_release_once(
            log_prefix=f"[worker {self.worker_id}]",
        )

        # Write the Chrome managed policy that force-installs the
        # built-in Paprika Agent extension. Chrome 148 ignores
        # --load-extension for unpacked extensions and the CDP
        # Extensions.loadUnpacked is pipe-only, so a force-install
        # enterprise policy (read from /etc/opt/chrome/policies/managed)
        # is the supported path. MUST run before lanes spawn Chrome so
        # the first launch already picks it up.
        try:
            self._write_agent_extension_policy()
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] agent extension policy "
                f"write failed (non-fatal): {type(e).__name__}: {e}",
            )

        # Pre-spawn pool if configured
        if self.lane_pool is not None:
            _logger.info(
                f"[worker {self.worker_id}] starting lane pool "
                f"({len(self.lane_pool.lanes)} lanes)...",
            )
            await self.lane_pool.start_all()

        backoff = 1.0
        # Seed the link-alive clock so the shutdown-on-failure window
        # covers the very first connect attempts too.
        self._last_link_ok = time.monotonic()
        # Arm the hung-loop watchdog now that the event loop is running (the
        # thread captures this loop for its call_soon pokes). Daemon thread,
        # off the loop -- see __init__ for the design + why it won't storm.
        if self._wd_enabled:
            import threading
            self._wd_last_pong = time.monotonic()
            threading.Thread(
                target=self._watchdog_loop,
                args=(asyncio.get_running_loop(),),
                name=f"wd-{self.worker_id}",
                daemon=True,
            ).start()
            _logger.info(
                f"[worker {self.worker_id}] loop-watchdog armed "
                f"(wedge {self._wd_threshold_s:.0f}s, link-stuck "
                f"{self._wd_link_threshold_s:.0f}s, inbound "
                f"{self._wd_inbound_threshold_s:.0f}s, check {self._wd_check_s:.0f}s)"
            )
        # self-restart HTTP endpoint (hub salvage path): when a worker ghosts
        # (proxied WS alive but no hub consumes it) the hub can't reach us over
        # the WS, so it POSTs /self-restart here -> we exit(42) -> docker
        # restarts us clean. Daemon thread, so it answers even while the asyncio
        # loop is idle/ghosted; a fully-wedged box won't answer -> hub SSH fallback.
        self._start_selfrestart_server()
        # Memory guard: recycle ourselves before the CT's memory cgroup goes
        # into a refault storm. Also a daemon thread, and for the same reason
        # the watchdog is one -- the failure it exists to catch is exactly the
        # one that stops the asyncio loop from getting scheduled. Reading
        # sysfs + os._exit need NO disk IO, which is what makes this survivable
        # when the node's SSD is saturated and `docker restart` would block.
        self._start_memory_guard(asyncio.get_running_loop())
        async with make_async_client(timeout=60.0) as http:
            self._http = http
            while True:
                # Recomputed each iteration: a clone-collision reassignment
                # mutates self.worker_id mid-loop so the next dial uses
                # the freshly-minted id.
                url = f"{self.hub_ws_url}/workers/{self.worker_id}/link"
                try:
                    _logger.info(f"[worker {self.worker_id}] connecting to {url}")
                    # ping_interval / ping_timeout MUST match the hub-side
                    # values in server/__main__.py (ws_ping_interval=30,
                    # ws_ping_timeout=120). Without this, the worker's
                    # client library uses the websockets-default 20s pong
                    # timeout while the hub uses 120s; whenever the HUB
                    # event loop blocks momentarily (e.g. a heavy session
                    # reconcile, a sync DB write, a large JSON dump) all
                    # workers fire their 20s pong timeout simultaneously,
                    # closing every WS with "keepalive ping timeout" and
                    # producing a fleet-wide reconnect storm. The
                    # symmetric setting lets the hub stall up to 120s
                    # before any worker gives up -- enough to absorb
                    # normal back-pressure.
                    async with websockets.connect(
                        url,
                        max_size=2**24,
                        ping_interval=30,
                        ping_timeout=120,
                    ) as ws:
                        self._ws = ws
                        await self._handshake_and_loop()
                        backoff = 1.0
                except _WorkerIdReassigned as e:
                    # Fast-path reconnect with the new id; no penalty
                    # backoff since this isn't an error condition.
                    _logger.info(
                        f"[worker] reconnecting immediately with new id={e}",
                    )
                    backoff = 0.5
                except (WebSocketException, OSError) as e:
                    # WebSocketException is the parent of ConnectionClosed AND
                    # of the handshake-rejection errors (notably InvalidStatus,
                    # raised when nginx returns HTTP 502/503 because the upstream
                    # hub is momentarily down -- e.g. mid `docker compose restart
                    # hub`). Previously only (ConnectionClosed, OSError) were
                    # caught, so a 502 on reconnect raised InvalidStatus straight
                    # out of this loop -> the worker PROCESS exited and docker had
                    # to rebuild every lane (and any in-flight job was orphaned).
                    # Treat any ws-level / socket error as a transient drop:
                    # log + backoff + retry indefinitely (reconnect-in-place).
                    _logger.info(
                        f"[worker {self.worker_id}] hub link down ({e}); "
                        f"reconnecting in {backoff:.1f}s",
                    )
                except KeyboardInterrupt:
                    return
                finally:
                    self._ws = None
                    # Disarm the inbound-liveness arm across the disconnect; it
                    # re-enables on the first frame of the next connection
                    # (self-enabling -> no reconnect-window false-fire).
                    self._last_inbound_ok = 0.0
                    # P2 (session survival): do NOT force-end sessions on a
                    # transient WS drop. This loop reconnects in place, and the
                    # hub now PERSISTS full session state in Redis and REBUILDS a
                    # worker's sessions from its reconnect announce
                    # (_reconcile_worker_sessions). Our Chrome tabs + lanes are
                    # unaffected by a dropped hub WS, so we KEEP every live
                    # session and re-announce it on reconnect -- detached /
                    # keepalive / interactive sessions then survive a hub restart
                    # instead of being torn down here (the old behaviour, from
                    # when the hub forgot all sessions on restart). On reconnect
                    # the hub's reconcile rebuilds what it can (JobInfo or the
                    # Redis owner map), orphan-ends anything it genuinely can't
                    # account for -- freeing those lanes -- and Pass-3 re-syncs
                    # in_flight so the scheduler won't over-dispatch. The announce
                    # itself skips any session it can't snapshot, so a tab that
                    # died during the drop won't be rebuilt. A worker PROCESS exit
                    # (self-update / Ctrl-C / give-up) still tears Chrome + lanes
                    # down via process death, so nothing leaks across a restart.
                    held = len(self._sessions)
                    if held:
                        _logger.info(
                            f"[worker {self.worker_id}] hub WS dropped; keeping "
                            f"{held} live session(s) for reconnect recovery",
                        )
                # NOTE: a "shutdown-on-failure" self-exit (exit after
                # WORKER_RECONNECT_GIVEUP_S of no hub link) was removed. It
                # also fired on transient event-loop starvation under heavy
                # load -- a busy worker can miss heartbeats for 120s while
                # the WS is otherwise fine -- turning a recoverable
                # reconnect into a destructive process restart, and an
                # all-at-once deploy made it storm fleet-wide. Reconnect-in-
                # place is the safer default. A future version may re-add it
                # gated ONLY on genuine connect failures (never-registered),
                # like Selenium's SE_NODE_REGISTER_PERIOD + SHUTDOWN_ON_FAILURE.
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handshake_and_loop(self) -> None:
        # Send register
        await self._send(
            WorkerRegister(
                worker_id=self.worker_id,
                capabilities=self.capabilities,
                secret=self.worker_secret,
            )
        )
        # Wait for hub's HubRegistered ack
        raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        ack = decode_hub_msg(raw)
        if not isinstance(ack, HubRegistered):
            raise RuntimeError(f"unexpected ack: {ack}")
        # The HubRegistered ack is a real inbound frame from the hub. Stamp the
        # inbound-liveness clock NOW so the watchdog's ghost arm is enabled from
        # the moment the link is up. Without this, if the proxied WS ghosts
        # (stays ESTABLISHED to nginx but no hub consumes us) BEFORE the first
        # frame of the async-for recv loop below, _last_inbound_ok stays 0.0
        # (reset on the prior disconnect) and the `> 0` guard disables the arm
        # forever -> the worker lingers as a ghost: absent from /workers yet
        # never self-exiting (observed fleet-wide 2026-06-08).
        self._last_inbound_ok = time.monotonic()

        # Clone-collision: the hub detected our worker_id is already
        # held by a different host (different client IP, original still
        # alive). It minted us a new ID; persist it, update our state,
        # and bail out of this connection so the outer loop reconnects
        # with the new URL.
        new_id = ack.assigned_worker_id
        if new_id and new_id != self.worker_id:
            _logger.info(
                f"[worker {self.worker_id}] hub reassigned id -> {new_id} "
                f"(clone collision detected); persisting and reconnecting",
            )
            try:
                WORKER_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
                WORKER_ID_FILE.write_text(new_id)
            except Exception as e:
                _logger.info(
                    f"[worker {self.worker_id}] WARNING: could not persist "
                    f"reassigned id to {WORKER_ID_FILE}: {e}. Will still use "
                    f"new id this session, but a restart may collide again.",
                )
            self.worker_id = new_id
            raise _WorkerIdReassigned(new_id)

        # Hub-driven version check. The hub's expected_worker_version is
        # whatever its bind-mounted /app/VERSION reports; if our local
        # build is older, we either log a banner (warn-only mode) or
        # exit with WORKER_EXIT_CODE_VERSION_MISMATCH so the docker
        # restart policy can pick up a freshly-pulled image. Dev builds
        # on either side disable the check (see
        # _versions_meaningfully_differ).
        # Rolling self-update on a hub-advertised version mismatch. Also
        # triggerable mid-connection via HubExpectedVersion (heartbeat) so a
        # worker-code deploy rolls out without a hub restart -- see
        # _maybe_begin_self_update + _handle_hub_message.
        await self._maybe_begin_self_update(
            ack.expected_worker_version, source=f"Hub ({self.hub_http_url})",
        )

        _logger.info(
            f"[worker {self.worker_id}] registered. server_time={ack.server_time}"
        )

        # Sync the plugin tree from the hub on every successful register.
        # Best-effort -- failures are logged but never block the worker.
        # See _fetch_worker_plugins_from_hub for the design rationale
        # (the 2026-05-27 fleet outage that prompted splitting source
        # and plugin tarballs into separate endpoints).
        try:
            await _fetch_worker_plugins_from_hub(
                hub_http_url=self.hub_http_url,
                log_prefix=f"[worker {self.worker_id}]",
            )
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] plugin sync crashed unexpectedly "
                f"({type(e).__name__}: {e}); continuing",
            )

        # Defensive lane cleanup: if we have NO sessions registered AND
        # no jobs currently in flight on this worker, lanes marked busy
        # are a stuck reservation from some past failure path (release()
        # missed in a finally, worker code crashed mid-job, etc.). The
        # ``not self._sessions`` check alone wasn't enough because
        # there's a window between lane.acquire() at the top of
        # _run_assigned_job and the session registration that happens
        # later inside fetch()'s on_browser_ready callback -- during
        # that window, freeing the lane caused a future job to acquire
        # the same lane and confuse nodriver into the no-attach path
        # (jobs 6fde9a29166a / others: "could not find a valid chrome
        # browser binary"). ``self._in_flight == 0`` covers that
        # window cleanly because the in_flight counter is incremented
        # at the very top of _run_assigned_job, before lane acquire.
        if self.lane_pool is not None and not self._sessions and self._in_flight == 0:
            stuck = [lane for lane in self.lane_pool.lanes if lane.busy]
            if stuck:
                _logger.info(
                    f"[worker {self.worker_id}] freeing "
                    f"{len(stuck)} stuck busy lane(s) on connect "
                    f"(no sessions registered, in_flight=0): "
                    f"{[lane.lane_idx for lane in stuck]}",
                )
                for lane in stuck:
                    lane.busy = False

        # Announce every session we currently hold so the hub can
        # reconcile its SessionRegistry against worker reality. Covers
        # hub restart (= hub forgot us; we tell it what we have so
        # detached keepalive sessions get rebuilt) AND worker restart
        # (= we have nothing; hub drops stale entries for us). Each
        # session contributes one SessionStateSnapshot with enough
        # fields for the hub to rebuild SessionInfo or 404 it as
        # an orphan.
        try:
            snapshots: list[SessionStateSnapshot] = []
            for sid, sess in list(self._sessions.items()):
                try:
                    lane = sess.lane
                    lane_idx = getattr(lane, "lane_idx", None)
                    if lane_idx is None:
                        continue
                    snapshots.append(
                        SessionStateSnapshot(
                            session_id=sid,
                            lane_idx=int(lane_idx),
                            novnc_url=getattr(lane, "novnc_url", None),
                            job_id=sess.job_id,
                            detached=(not bool(sess.is_fetch_owned)) and bool(sess.job_id),
                            is_fetch_owned=bool(sess.is_fetch_owned),
                        )
                    )
                except Exception as e:
                    _logger.info(
                        f"[worker {self.worker_id}] announce: skipping "
                        f"session {sid} ({type(e).__name__}: {e})",
                    )
            await self._send(WorkerSessionAnnounce(sessions=snapshots))
            _logger.info(
                f"[worker {self.worker_id}] announced {len(snapshots)} session(s) to hub",
            )
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] session announce failed "
                f"({type(e).__name__}: {e}); hub will still see this "
                f"worker but won't know about pre-existing sessions",
            )

        # Pull the hub's current extension set into our local cache.
        # Lanes pass each cached extension dir to Chrome via
        # --load-extension on every restart, so any new extensions
        # uploaded since this worker last started become active on
        # the next lane bounce. Errors are best-effort: a missing
        # extension shouldn't prevent the worker from accepting
        # jobs.
        try:
            await self._sync_extensions_from_hub()
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] extension sync failed "
                f"({type(e).__name__}: {e}); lanes will boot without "
                f"hub-managed extensions until the next reconnect",
            )
        # Push the cache snapshot to every lane so the NEXT Chrome
        # (re)start picks them up via --load-extension. Lanes that
        # are already running with old / no extensions will refresh
        # on their next bounce (watchdog respawn, profile swap, ...).
        try:
            paths = self.loaded_extension_paths()
            if self.lane_pool is not None:
                for lane in self.lane_pool.lanes:
                    try:
                        lane.set_extra_extension_paths(paths)
                    except Exception:
                        pass
                if paths:
                    _logger.info(
                        f"[worker {self.worker_id}] extension cache: "
                        f"pushed {len(paths)} path(s) to "
                        f"{len(self.lane_pool.lanes)} lane(s)",
                    )
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] extension push to lanes "
                f"failed ({type(e).__name__}: {e})",
            )

        # Run heartbeat + idle-tab reaper + disk-leak sweeper +
        # message loop concurrently. The sweeper is the production
        # backstop for stranded /tmp/paprika-* dirs from crashes /
        # ungraceful teardown -- see _disk_cleanup_loop docstring.
        hb_task = asyncio.create_task(self._heartbeat_loop())
        reaper_task = asyncio.create_task(self._idle_tab_reaper_loop())
        disk_task = asyncio.create_task(self._disk_cleanup_loop())
        preview_task = asyncio.create_task(self._preview_capture_loop())
        selfcheck_task = asyncio.create_task(self._self_check_loop())
        # Frees the node ramdisk the instant a deferred download's job is
        # over, instead of at the 2h yt-dlp cap -- see the loop's docstring.
        abandon_task = asyncio.create_task(self._abandoned_download_loop())
        # Pull dispatch (server/worker/agent/_mix_pull.py). Returns
        # immediately unless PAPRIKA_PULL_DISPATCH is set.
        if getattr(self, "_lag_task", None) is None:
            # Never cancelled with the connection -- see _loop_lag.py.
            self._lag_task = asyncio.create_task(self._loop_lag_sampler())
        pull_task = asyncio.create_task(self._pull_loop())
        try:
            async for raw in self._ws:
                # Any frame from the hub -- even an undecodable one -- proves a
                # hub is still consuming/serving this link at the APPLICATION
                # layer (uvicorn/nginx answer protocol pings themselves, so a
                # live WS alone does not). Drives the inbound-liveness arm (v3).
                self._last_inbound_ok = time.monotonic()
                try:
                    msg = decode_hub_msg(raw)
                except Exception as e:
                    _logger.info(f"[worker {self.worker_id}] decode error: {e}")
                    continue
                await self._handle_hub_message(msg)
        finally:
            hb_task.cancel()
            reaper_task.cancel()
            disk_task.cancel()
            preview_task.cancel()
            selfcheck_task.cancel()
            abandon_task.cancel()
            pull_task.cancel()

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                # Race a kick event against the standard heartbeat interval.
                # If anything (session_start / job_exec start or end) sets
                # ``_heartbeat_kick``, we send NOW instead of waiting up to
                # 10s -- the over-dispatch fix for the hub's stale in_flight
                # view (incident 2026-06-16). Falls back to the interval when
                # nothing kicks.
                try:
                    await asyncio.wait_for(
                        self._heartbeat_kick.wait(),
                        timeout=HEARTBEAT_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    pass
                finally:
                    # Clear regardless: if another change happens after we
                    # build the heartbeat below, the next loop iteration
                    # will catch it.
                    self._heartbeat_kick.clear()
                try:
                    # Snapshot the profile cache so the hub can show
                    # "ready on X/N workers" in the Profiles tab and
                    # "has profiles [...]" in the Workers tab. Copy
                    # under the lock so a concurrent sync / delete
                    # can't mutate the list mid-snapshot.
                    async with self._profile_cache_lock:
                        cached = [
                            ProfileCacheEntry(
                                name=n,
                                etag=str(e.get("etag") or ""),
                                size_bytes=int(e.get("size_bytes") or 0),
                            )
                            for n, e in self._profile_cache.items()
                        ]
                    # While draining (recycle), report the worker as full so the
                    # hub stops assigning; real in-flight still drives the exit
                    # check below. Otherwise report the TRUE lane occupancy --
                    # max(job-semaphore counter, busy lanes) -- NOT just
                    # self._in_flight. Lanes are also held by operator-started
                    # sessions (HubSessionStart = noVNC / recorder), which
                    # acquire a lane WITHOUT bumping self._in_flight (and by any
                    # leaked busy lane). Reporting only _in_flight let the hub's
                    # pick_worker see those lanes as free and over-dispatch -- the
                    # worker then couldn't acquire a lane -> "no free lane in
                    # pool" (job 663a3251f4af). max() keeps the brief pre-acquire
                    # window safe: _in_flight is ++'d at the top of job exec,
                    # before lane.acquire(), so during that sliver the job counter
                    # is the higher (safe) number.
                    if self._draining:
                        eff_in_flight = self.max_concurrent
                    else:
                        eff_in_flight = self._in_flight
                        if self.lane_pool is not None:
                            try:
                                eff_in_flight = max(
                                    eff_in_flight,
                                    int(self.lane_pool.stats().get("busy", 0)),
                                )
                            except Exception:
                                pass
                    # Snapshot CT/host resources for the admin Workers list
                    # + the hub-side disk-pressure dispatch gate (pick_worker
                    # skips workers with disk_pct > 90). Stamp onto the
                    # WorkerAgent so _mix_jobexec can read the same sample
                    # in its preflight without re-walking /proc.
                    cpu_pct, mem_pct, disk_pct, disk_free_gb, load1 = (
                        _sample_resources()
                    )
                    self._last_resources = (
                        cpu_pct, mem_pct, disk_pct, disk_free_gb, load1,
                    )
                    # Real, cgroup-scoped memory for the admin UI + the hub's
                    # memory-choke salvage trigger. mem_scope tells the hub
                    # whether mem_pct means anything (see _sample_memory);
                    # the absolute figures below always do.
                    mem_scope, _cg_pct, _cg = _sample_memory()
                    mem_current_mb = mem_anon_mb = 0.0
                    mem_psi_some_avg60 = mem_psi_full_avg60 = 0.0
                    if _cg is not None:
                        mem_current_mb = _cg.current / 1048576.0
                        mem_anon_mb = _cg.anon / 1048576.0
                        mem_psi_some_avg60 = _cg.psi_some_avg60
                        mem_psi_full_avg60 = _cg.psi_full_avg60
                    if mem_scope == "cgroup":
                        mem_pct = _cg_pct
                    # Proactive disk self-heal for an IDLE, disk-pressured
                    # worker. Without this a CT that crosses the 90%
                    # dispatch-exclusion line stops receiving jobs and so
                    # never hits the per-job preflight that runs the cleanup
                    # -- it freezes at ~100% forever (w51149, 2026-07-04).
                    # in_flight<=0 guard: only reclaim when no job is running,
                    # so we never delete cache/scratch mid-download (busy
                    # pressured workers stay covered by the preflight path).
                    if (
                        disk_pct >= _DISK_PROACTIVE_CLEANUP_PCT
                        and self._in_flight <= 0
                    ):
                        _now_m = time.monotonic()
                        if (
                            _now_m - getattr(self, "_last_disk_cleanup_m", 0.0)
                            >= _DISK_CLEANUP_MIN_INTERVAL_S
                        ):
                            self._last_disk_cleanup_m = _now_m
                            _logger.warning(
                                f"[worker {self.worker_id}] disk {disk_pct:.0f}%"
                                f" >= {_DISK_PROACTIVE_CLEANUP_PCT:.0f}% and idle"
                                f" -- running proactive disk cleanup"
                            )
                            try:
                                from ._mix_jobexec import _emergency_disk_cleanup
                                await asyncio.to_thread(_emergency_disk_cleanup)
                            except Exception:
                                _logger.debug(
                                    "proactive disk cleanup raised",
                                    exc_info=True,
                                )
                    await self._send(
                        WorkerHeartbeat(
                            in_flight=eff_in_flight,
                            capacity=self.max_concurrent,
                            profiles_cached=cached,
                            cpu_pct=cpu_pct,
                            mem_pct=mem_pct,
                            disk_pct=disk_pct,
                            disk_free_gb=disk_free_gb,
                            load1=load1,
                            nproc=_NPROC,
                            mem_scope=mem_scope,
                            mem_current_mb=mem_current_mb,
                            mem_anon_mb=mem_anon_mb,
                            mem_psi_some_avg60=mem_psi_some_avg60,
                            mem_psi_full_avg60=mem_psi_full_avg60,
                            mem_majfault_per_s=self._memguard_rates[0],
                            mem_refault_per_s=self._memguard_rates[1],
                            mem_anon_rate_mb_min=self._memguard_anon_rate_mb_min,
                            loop_lag_ms=self.loop_lag_peak_ms(),
                            memguard=self._memguard_reason,
                        )
                    )
                    # A successful heartbeat == the hub link is alive.
                    # Drives the shutdown-on-failure timer in run().
                    self._last_link_ok = time.monotonic()
                    # Recycle: once the drain has emptied in-flight, exit so
                    # docker restarts us fresh.
                    if self._draining and self._in_flight <= 0:
                        _logger.info(
                            f"[worker {self.worker_id}] drained after "
                            f"{self._jobs_done} job(s); exiting for recycle "
                            f"(docker will restart)",
                        )
                        os._exit(0)
                except Exception as e:
                    # Heartbeat send failed -- the WS to nginx is presumed dead.
                    # Used to `return` silently; that left the recv loop blocked
                    # on `async for raw in self._ws:` and the worker ghosted
                    # forever (no python INFO logs, only Chrome dbus errors).
                    # Force the WS closed so the recv loop unblocks, the outer
                    # run() loop falls through finally + sleep + reconnect.
                    _logger.warning(
                        f"[worker {self.worker_id}] heartbeat send failed "
                        f"({type(e).__name__}: {e}); closing WS to force reconnect"
                    )
                    try:
                        if self._ws is not None:
                            await self._ws.close(code=1011, reason="hb-send-failed")
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            return

    async def _self_check_loop(self) -> None:
        """Active hub-side liveness probe. Catches the ghost pattern where:

        - heartbeat sends keep "succeeding" into a half-dead nginx<->hub upstream
          (so _last_link_ok stays fresh -> link arm misses);
        - some auto-traffic keeps inbound fresh too (so v3 inbound arm misses).

        Periodically (default 60s) GET /workers and check whether our own
        worker_id appears in the fleet list. If we are missing for >=3
        consecutive checks (default), the hub is genuinely not serving us
        -> os._exit so docker restarts us and we re-register fresh.

        Belt-and-suspenders next to the passive watchdog arms; both can stay.
        Env knobs:
          PAPRIKA_WORKER_SELFCHECK_DISABLE=1   -> turn off
          PAPRIKA_WORKER_SELFCHECK_INTERVAL_S  -> default 60
          PAPRIKA_WORKER_SELFCHECK_MISS_THRESHOLD -> default 3
        """
        if os.environ.get("PAPRIKA_WORKER_SELFCHECK_DISABLE") == "1":
            return
        try:
            interval = float(
                os.environ.get("PAPRIKA_WORKER_SELFCHECK_INTERVAL_S") or 60
            )
        except (TypeError, ValueError):
            interval = 60.0
        if interval <= 0:
            return
        try:
            miss_threshold = int(
                os.environ.get("PAPRIKA_WORKER_SELFCHECK_MISS_THRESHOLD") or 4
            )
        except (TypeError, ValueError):
            miss_threshold = 4
        miss_threshold = max(2, miss_threshold)
        # Sliding window of recent CONCLUSIVE probe outcomes (True == we were
        # absent-or-not-alive in /workers). Fire when missing in >=
        # miss_threshold of the last window_n probes. Replaces the old
        # CONSECUTIVE streak, which a FLAPPING ghost defeated: its stale redis
        # /workers row intermittently reappeared and reset the streak to 0 so it
        # never reached the threshold. window_n >= miss_threshold so the
        # threshold is reachable.
        try:
            window_n = int(os.environ.get("PAPRIKA_WORKER_SELFCHECK_WINDOW") or 6)
        except (TypeError, ValueError):
            window_n = 6
        window_n = max(miss_threshold, window_n)
        # A probe that ERRORS (ReadTimeout / connection refused) means "I could
        # not even reach the hub to confirm I'm served" -- distinct from a 200
        # that omits me (a genuine presence-miss). The original code counted
        # ONLY presence-misses, never errors, to keep a network blip from
        # becoming a fleet-wide exit storm -- but that left an OVERLOADED worker
        # (event loop starved by a yt-dlp download pile-up, so its OWN probe
        # times out) unable to EVER self-recover: it ghosted for hours (the
        # 2026-07-10 overnight decline 105->78 with zero self-recovery). Count
        # CONSECUTIVE probe errors on a SEPARATE, higher threshold: a brief blip
        # still can't fire (reset on the next reply), but a sustained inability
        # to probe self-heals via the same clean re-register exit.
        try:
            error_threshold = int(
                os.environ.get("PAPRIKA_WORKER_SELFCHECK_ERROR_THRESHOLD") or 8
            )
        except (TypeError, ValueError):
            error_threshold = 8
        error_threshold = max(3, error_threshold)
        url = f"{self.hub_http_url}/workers"
        recent: list[bool] = []
        was_missing = False
        probe_errors = 0  # consecutive probe failures; reset on any HTTP reply
        # Grace: skip the first probe so the freshly-connected WS has time to be
        # picked up across hubs via redis aggregation (~1-2s typically).
        await asyncio.sleep(interval + random.uniform(0.0, 10.0))
        try:
            while True:
                try:
                    assert self._http is not None
                    r = await self._http.get(url, timeout=10.0)
                    probe_errors = 0  # got an HTTP reply -> hub is reachable
                    if r.status_code != 200:
                        # transient nginx/hub blip -> do NOT count as a miss
                        await asyncio.sleep(interval)
                        continue
                    payload = r.json()
                    ws_list = payload.get("workers") or []
                    # Require alive=True, not mere presence: a ghost lingers in
                    # /workers as an alive=False row (stale cross-hub redis
                    # aggregation). The old "present if worker_id appears at all"
                    # check saw that ghost row as ourselves-present -> never
                    # counted a miss -> never self-restarted. We must be LISTED
                    # AND ALIVE to count as genuinely served.
                    me_present = any(
                        (w.get("worker_id") == self.worker_id) and bool(w.get("alive"))
                        for w in ws_list
                    )
                    recent.append(not me_present)   # True == a miss
                    if len(recent) > window_n:
                        recent.pop(0)
                    misses = sum(1 for m in recent if m)
                    if me_present:
                        if was_missing and misses == 0:
                            _logger.info(
                                f"[worker {self.worker_id}] self-check: back in "
                                f"hub registry"
                            )
                        was_missing = False
                    else:
                        was_missing = True
                        _logger.warning(
                            f"[worker {self.worker_id}] self-check: NOT alive in hub "
                            f"/workers ({misses}/{len(recent)} recent probes missing, "
                            f"fire at {miss_threshold}) -- WS believed alive but hub "
                            f"does not serve us"
                        )
                    if misses >= miss_threshold:
                        held = self._memguard_owns_recycle()
                        if held:
                            _logger.warning(
                                f"[worker {self.worker_id}] self-check: missing "
                                f"from hub in {misses}/{len(recent)} recent "
                                f"probes, but standing down -- {held}"
                            )
                            await asyncio.sleep(interval)
                            continue
                        try:
                            _logger.critical(
                                f"[worker {self.worker_id}] self-check: missing from "
                                f"hub in {misses}/{len(recent)} recent probes "
                                f"(~{window_n * interval:.0f}s window) -> "
                                f"exit({WORKER_EXIT_CODE_VERSION_MISMATCH}) for clean "
                                f"re-register"
                            )
                        except Exception:
                            pass
                        os._exit(WORKER_EXIT_CODE_VERSION_MISMATCH)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # A probe error means we could not confirm we're served. A
                    # single blip must NOT exit (that would turn a hub/network
                    # hiccup into a fleet-wide exit storm), so count CONSECUTIVE
                    # errors and only self-restart once they persist past
                    # error_threshold (~error_threshold*interval s) -- long
                    # enough to distinguish a real outage from a blip, bounded
                    # so an overloaded worker whose own probe keeps timing out
                    # finally self-heals instead of ghosting forever. Any HTTP
                    # reply (incl. non-200) resets the counter above.
                    probe_errors += 1
                    if probe_errors >= error_threshold:
                        held = self._memguard_owns_recycle()
                        if held:
                            _logger.warning(
                                f"[worker {self.worker_id}] self-check: probe "
                                f"unreachable {probe_errors}x, but standing "
                                f"down -- {held}"
                            )
                            await asyncio.sleep(interval)
                            continue
                        try:
                            _logger.critical(
                                f"[worker {self.worker_id}] self-check: probe "
                                f"unreachable {probe_errors}x consecutively "
                                f"(~{probe_errors * interval:.0f}s, last "
                                f"{type(e).__name__}: {e}) -> "
                                f"exit({WORKER_EXIT_CODE_VERSION_MISMATCH}) for "
                                f"clean re-register"
                            )
                        except Exception:
                            pass
                        os._exit(WORKER_EXIT_CODE_VERSION_MISMATCH)
                    _logger.info(
                        f"[worker {self.worker_id}] self-check probe transient "
                        f"error ({type(e).__name__}: {e}); "
                        f"{probe_errors}/{error_threshold} before re-register"
                    )
                await asyncio.sleep(interval + random.uniform(0.0, 5.0))
        except asyncio.CancelledError:
            return

    def _start_selfrestart_server(self) -> None:
        """Daemon-thread HTTP server exposing POST /self-restart for the hub's
        salvage path (ghost recovery). Auth = the same worker_secret via the
        X-Worker-Secret header; with no secret configured it accepts LAN-local
        POSTs (same trust level as the rest of the fleet today). Runs in its OWN
        thread so it answers even while the asyncio loop is idle/ghosted; a
        fully-wedged box won't answer -> the hub falls back to SSH. Env:
        PAPRIKA_WORKER_SELFRESTART_DISABLE=1 (off),
        PAPRIKA_WORKER_SELFRESTART_PORT (default 9099)."""
        if os.environ.get("PAPRIKA_WORKER_SELFRESTART_DISABLE") == "1":
            return
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        try:
            port = int(os.environ.get("PAPRIKA_WORKER_SELFRESTART_PORT") or 9099)
        except (TypeError, ValueError):
            port = 9099
        secret = self.worker_secret or ""
        wid = self.worker_id

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence default stderr noise
                pass

            def _reply(self, code: int, body: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                try:
                    self.wfile.write(body.encode())
                except Exception:
                    pass

            def do_GET(self):  # cheap liveness probe for the salvage path
                if self.path.rstrip("/") == "/healthz":
                    return self._reply(200, "ok")
                return self._reply(404, "not found")

            def do_POST(self):
                if self.path.rstrip("/") != "/self-restart":
                    return self._reply(404, "not found")
                if secret and self.headers.get("X-Worker-Secret") != secret:
                    return self._reply(403, "bad worker secret")
                self._reply(200, "restarting")
                try:
                    _logger.critical(
                        f"[worker {wid}] self-restart requested via HTTP "
                        f"-> exit({WORKER_EXIT_CODE_VERSION_MISMATCH})"
                    )
                except Exception:
                    pass
                # Delay slightly so the HTTP response flushes before we exit.
                threading.Timer(
                    0.3, lambda: os._exit(WORKER_EXIT_CODE_VERSION_MISMATCH)
                ).start()

        try:
            srv = HTTPServer(("0.0.0.0", port), _Handler)
        except Exception as e:
            _logger.warning(
                f"[worker {wid}] self-restart server bind failed on :{port}: {e}"
            )
            return
        threading.Thread(
            target=srv.serve_forever,
            name=f"selfrestart-{wid}",
            daemon=True,
        ).start()
        _logger.info(
            f"[worker {wid}] self-restart HTTP server on :{port} "
            f"(auth={'secret' if secret else 'lan-open'})"
        )

    # --- memory guard -----------------------------------------------------
    # Defaults are set from measurements taken on boiler (10.10.50.15) worker
    # CTs 356/365/382 on 2026-08-03, all healthy and running jobs:
    #   anon           1.6-2.1 GB   (climbing 78-215 MB per 31s under load)
    #   pgmajfault     41-75 /s
    #   PSI some avg60 0.00
    # and against the 2026-08-02 incident, where CT356 had accumulated 18.4
    # MILLION major faults and the node did 24k read IOPS of 2KB random IO.
    # Every threshold below therefore sits an order of magnitude above healthy
    # and an order of magnitude below the incident.
    # Was 5500 = ~69% of the fleet's THEN-8GB CT cap. The caps were raised to
    # 12288MB on every node on 2026-08-06 ([[loft-shmem-charged-to-ct-refault-storm]])
    # and this number was not moved with them, leaving it at 45% of the cap --
    # low enough that healthy-but-busy workers crossed it and recovered
    # (measured w5148 2026-08-14: warned at 6676MB, then twelve consecutive
    # clean samples). 7000 is ~57% of the 12288MB cap and still leaves 5.3GB of
    # headroom, which is >5 minutes even at the fastest leak rate observed.
    # The fast leaks are caught earlier by the growth-rate axis below, not by
    # this absolute floor, so raising it costs nothing in detection latency.
    _MEMGUARD_ANON_MB = 7000        # ~57% of the fleet's 12288MB CT cap
    # Growth rate, not level: the axis the absolute threshold above cannot see
    # in time. Measured on balcony w51177 2026-08-14: anon went 5544MB ->
    # 8448MB in 2.7 minutes (~1GB/min), and the kernel OOM-killed the process
    # at 10.4GB before the level-based window could complete. A worker climbing
    # this fast has minutes, not tens of minutes, so it must be caught on the
    # slope. Healthy workers sit at 1.4-2.1GB with no sustained climb; the
    # startup ramp (400MB -> ~2GB as Chrome lanes come up) is steep but ends
    # well below the floor, which is why the rate only counts ABOVE
    # _MEMGUARD_ANON_RATE_FLOOR_MB -- rate alone would recycle every worker
    # during its own boot.
    _MEMGUARD_ANON_RATE_MB_MIN = 300.0
    _MEMGUARD_ANON_RATE_FLOOR_MB = 3500.0
    _MEMGUARD_MAJFAULT_PER_S = 1000.0
    _MEMGUARD_PSI_PCT = 20.0
    # Page-cache refaults/s: pages evicted and immediately read back. THE
    # direct measure of the thrash -- a major fault can be a legitimate first
    # read, a refault cannot. Measured 0.0/s on every healthy CT sampled
    # (boiler 2026-08-03, garage 17 CTs 2026-08-03), so the headroom below is
    # enormous and false positives are structurally unlikely. Sized from the
    # boiler incident: ~48MB/s of 2KB random reads over ~11 CTs is on the
    # order of 1000 pages/s per CT, so 500 trips before it is that bad.
    _MEMGUARD_REFAULT_PER_S = 500.0
    # Was 300s. That window (10-14 samples, tripping at 6-9 of them = ~4
    # minutes) was sized for the slow leak measured in 2026-08: 35-80MB/min,
    # where 4 minutes of deliberation costs ~300MB. Against the 1GB/min bursts
    # measured 2026-08-14 it is longer than the whole runway -- from the
    # threshold to a full 12GB cap is ~5 minutes, so the guard was still
    # counting samples when the kernel OOM killer fired. 120s (4-6 samples,
    # tripping at 3-4) keeps a lone spike from recycling anything while
    # deciding inside the runway.
    _MEMGUARD_SUSTAIN_S = 120.0     # must hold this long -- no spike recycles
    # Fraction of the sustain window that must be breaching to trip. Below 1.0
    # deliberately: see the sliding-window note in _memory_guard_loop. 0.6 of a
    # 10-14 sample window is 6-9 breaching samples, so a lone spike (1-2) still
    # cannot recycle a worker, but the dips that made the old unbroken-run rule
    # unsatisfiable no longer throw the window away.
    _MEMGUARD_WINDOW_FRAC = 0.6
    _MEMGUARD_INTERVAL_S = 30.0
    _MEMGUARD_DRAIN_DEADLINE_S = 900.0
    # Halved with the sustain window: jitter is added to the window length, so
    # leaving it at 120 against a 120s sustain would make the effective window
    # anywhere from 4 to 8 samples -- a 2x spread that puts the slowest workers
    # right back outside the runway. 60 keeps the stagger (its point is that a
    # node-wide event must not drain every CT in the same minute) while
    # bounding the window to 4-6 samples.
    _MEMGUARD_JITTER_S = 60.0
    # How long the self-check loop stands down while the guard is mid-breach,
    # so a graceful drain beats self-check's abrupt exit. Bounded: a worker
    # that is BOTH leaking and genuinely absent from the hub must still recycle
    # rather than sit deferring forever. Sized above the worst-case decision
    # window (sustain + jitter + a couple of samples) with room to spare.
    _MEMGUARD_SELFCHECK_DEFER_S = 300.0

    def _start_memory_guard(self, loop: "asyncio.AbstractEventLoop") -> None:
        """Arm the memory-guard daemon thread. Kill switch:
        ``PAPRIKA_MEMGUARD_DISABLE=1``."""
        if os.environ.get("PAPRIKA_MEMGUARD_DISABLE") == "1":
            _logger.info(
                f"[worker {self.worker_id}] memory guard DISABLED "
                f"(PAPRIKA_MEMGUARD_DISABLE=1)"
            )
            return
        from server.worker import cgroup_mem
        if not cgroup_mem.available():
            # cgroup v1 host, or /sys/fs/cgroup not mounted. Stay inert rather
            # than fall back to /proc/meminfo -- on this fleet that file
            # reports the Proxmox NODE's memory, so a guess built on it would
            # recycle workers for a neighbour's memory pressure.
            _logger.info(
                f"[worker {self.worker_id}] memory guard inert "
                f"(no cgroup v2 memory controller)"
            )
            return
        import threading
        _logger.info(
            f"[worker {self.worker_id}] memory guard armed -- "
            f"{cgroup_mem.status_line(cgroup_mem.sample())}"
        )
        threading.Thread(
            target=self._memory_guard_loop,
            args=(loop,),
            name=f"memguard-{self.worker_id}",
            daemon=True,
        ).start()

    def _memguard_owns_recycle(self) -> str:
        """Why the self-check loop must NOT exit right now, or "" to proceed.

        Both loops recycle a sick worker, but they do it differently: the guard
        sets ``_draining`` and lets in-flight jobs finish (with its own deadline
        as the backstop), while the self-check calls ``os._exit`` immediately
        and those jobs are lost until the redrive path requeues them. When both
        are firing at once the graceful one should win.

        Measured on balcony w51177 2026-08-14, which is what this exists for:
        the worker sat at anon 8.4GB with the guard mid-window, and self-check
        exited it first. The replacement process started at 402MB with an EMPTY
        guard window, leaked back to 8GB, and was exited again -- a ~5 minute
        loop in which the guard could never reach its trip count, so a worker
        that the guard would have recycled cleanly instead lost its in-flight
        work every time. Deferring is bounded (see _MEMGUARD_SELFCHECK_DEFER_S)
        so a worker that is both leaking AND genuinely unserved still exits.
        """
        if self._memguard_reason:
            return f"memory guard already draining ({self._memguard_reason})"
        since = self._memguard_breach_since
        if not since:
            return ""
        held = time.monotonic() - since
        limit = _num_env(
            "PAPRIKA_MEMGUARD_SELFCHECK_DEFER_S", self._MEMGUARD_SELFCHECK_DEFER_S
        )
        if limit > 0 and held <= limit:
            return (
                f"memory guard breaching for {held:.0f}s (limit {limit:.0f}s) "
                f"-- letting it drain gracefully"
            )
        return ""

    def _memguard_breaches(self, prev, cur, dt_s: float) -> list[str]:
        """Which thresholds this sample crosses. Empty list == healthy.

        Four independent signals because each catches something the others
        cannot: ``anon`` sees the slow RSS leak long before any stall shows up;
        the refault rate sees cache thrash directly and earliest; the
        major-fault rate sees it even if reclaim ran in a parent cgroup we
        can't read; PSI sees the stall in the unit that actually matters
        (wall-clock time lost). Any one of them is enough to act on --
        requiring agreement would just delay recovery.

        NOT a signal, deliberately: ``memory.current`` near the limit. A memcg
        fills with clean page cache up to its limit by design, so "current is
        high" is the normal state of a busy worker, not a symptom. Measured on
        garage 2026-08-03: CT351 sat at 6174MB of an 8192MB limit with refault
        0.0/s and PSI 0.02 -- perfectly healthy. A ``current > 80%`` trigger
        (proposed after that day's crashes) would have recycled it, and most
        of the fleet with it. What distinguishes the storm from a full cache
        is whether the cache is being RE-READ, which is what refault measures.
        """
        from server.worker import cgroup_mem
        out: list[str] = []
        anon_mb = _num_env("PAPRIKA_MEMGUARD_ANON_MB", self._MEMGUARD_ANON_MB)
        if anon_mb > 0 and cur.anon >= anon_mb * 1024 * 1024:
            out.append(f"anon {cur.anon // (1024 * 1024)}MB >= {anon_mb:.0f}MB")
        mf_limit = _num_env(
            "PAPRIKA_MEMGUARD_MAJFAULT_PER_S", self._MEMGUARD_MAJFAULT_PER_S
        )
        rf_limit = _num_env(
            "PAPRIKA_MEMGUARD_REFAULT_PER_S", self._MEMGUARD_REFAULT_PER_S
        )
        rate_limit = _num_env(
            "PAPRIKA_MEMGUARD_ANON_RATE_MB_MIN", self._MEMGUARD_ANON_RATE_MB_MIN
        )
        rate_floor = _num_env(
            "PAPRIKA_MEMGUARD_ANON_RATE_FLOOR_MB",
            self._MEMGUARD_ANON_RATE_FLOOR_MB,
        )
        if prev is not None and dt_s > 0:
            mf_rate = cgroup_mem.majfault_rate(prev, cur, dt_s)
            rf_rate = cgroup_mem.refault_rate(prev, cur, dt_s)
            # Stash for the heartbeat: rates need two samples, so the guard
            # thread is the only place that can compute them, and without
            # reporting them the storm stays invisible until it is fatal.
            self._memguard_rates = (mf_rate, rf_rate)
            anon_rate = (cur.anon - prev.anon) / (1024 * 1024) / (dt_s / 60.0)
            self._memguard_anon_rate_mb_min = anon_rate
            if mf_limit > 0 and mf_rate >= mf_limit:
                out.append(f"majfault {mf_rate:.0f}/s >= {mf_limit:.0f}/s")
            if rf_limit > 0 and rf_rate >= rf_limit:
                out.append(f"refault {rf_rate:.0f}/s >= {rf_limit:.0f}/s")
            # Slope, gated on level. Both conditions are load-bearing: without
            # the floor this fires on every worker's own startup ramp, and
            # without the rate a 1GB/min climb is invisible until it is already
            # most of the way to the cap.
            if (
                rate_limit > 0
                and anon_rate >= rate_limit
                and cur.anon >= rate_floor * 1024 * 1024
            ):
                out.append(
                    f"anon climbing {anon_rate:.0f}MB/min >= {rate_limit:.0f}"
                    f"MB/min at {cur.anon // (1024 * 1024)}MB"
                )
        psi_limit = _num_env("PAPRIKA_MEMGUARD_PSI_PCT", self._MEMGUARD_PSI_PCT)
        if psi_limit > 0 and cur.psi_some_avg60 >= psi_limit:
            out.append(
                f"PSI some avg60 {cur.psi_some_avg60:.1f} >= {psi_limit:.1f}"
            )
        return out

    def _memory_guard_loop(self, loop: "asyncio.AbstractEventLoop") -> None:
        """Daemon thread: trip into drain-and-recycle on sustained memory
        distress, and force-exit if the drain can't complete.

        Two-stage on purpose. The graceful stage sets ``_draining``, which the
        heartbeat loop already turns into "report full, then exit(0) once
        in-flight hits 0" -- the same recycle path the fd-budget gate and the
        drain-after-N counter use, so in-flight jobs are never killed. But a
        worker deep in a refault storm may never finish those jobs (that is the
        whole failure mode), so a deadline force-exits afterwards. Losing the
        in-flight jobs of a thrashing worker is strictly better than the hub
        waiting on a box that has stopped making progress -- the jobs are
        requeued by the redrive path either way.

        "Sustained" is measured as *most of* a fixed window rather than *all
        of* an unbroken run -- see the window_n/need_n note below for the
        measurement that forced that change.
        """
        from server.worker import cgroup_mem, memtrace

        interval = _num_env(
            "PAPRIKA_MEMGUARD_INTERVAL_S", self._MEMGUARD_INTERVAL_S
        )
        sustain_s = _num_env(
            "PAPRIKA_MEMGUARD_SUSTAIN_S", self._MEMGUARD_SUSTAIN_S
        )
        deadline_s = _num_env(
            "PAPRIKA_MEMGUARD_DRAIN_DEADLINE_S", self._MEMGUARD_DRAIN_DEADLINE_S
        )
        # Stagger: a node-wide event (a neighbour VM ballooning, a host OOM)
        # can breach every CT on that node within the same minute. Without a
        # random offset all of them would drain together and take the node's
        # whole share of the fleet offline at once.
        jitter_s = random.uniform(
            0.0, max(0.0, _num_env("PAPRIKA_MEMGUARD_JITTER_S", self._MEMGUARD_JITTER_S))
        )

        # Sliding-window hysteresis. The rule used to be an UNBROKEN run of
        # breaching samples for sustain_s+jitter_s, with the clock reset by any
        # single clear sample. Measured on garage (10.10.50.46) 2026-08-13,
        # across that node's 39 worker CTs: 109 breaches logged (anon median
        # 6194MB, max 9938MB), 109 stand-downs, only 10 trips -- while the
        # kernel OOM-killed those same CTs 86 times in the same window. anon
        # oscillates around the threshold as Chrome lanes recycle and
        # yt-dlp/ffmpeg children come and go, so one dip anywhere in the 10-14
        # sample run threw the whole window away; several stand-downs landed
        # 30-60s after the warning that started them. Counting breaches over a
        # FIXED-LENGTH window survives those dips while still ignoring a lone
        # spike -- the same shape as the fix for the worker self-check flap.
        window_n = max(2, math.ceil((sustain_s + jitter_s) / max(interval, 1.0)))
        need_n = max(
            1,
            min(
                window_n,
                math.ceil(
                    window_n
                    * _num_env(
                        "PAPRIKA_MEMGUARD_WINDOW_FRAC", self._MEMGUARD_WINDOW_FRAC
                    )
                ),
            ),
        )
        window: deque[bool] = deque(maxlen=window_n)

        prev = None
        prev_m = 0.0
        warned = False
        last_reasons: list[str] = []
        while True:
            time.sleep(interval)
            try:
                cur = cgroup_mem.sample()
                if not cur.ok:
                    continue
                now_m = time.monotonic()
                dt = now_m - prev_m if prev is not None else 0.0
                reasons = self._memguard_breaches(prev, cur, dt)
                prev, prev_m = cur, now_m
                window.append(bool(reasons))
                if reasons:
                    last_reasons = reasons
                    # Stamp the START of this run of breaching samples and hold
                    # it until the window goes fully clean. The self-check loop
                    # reads it to stand down, so it must stay set through the
                    # dips inside a run -- clearing it per-sample would hand the
                    # recycle back to self-check exactly on the dips the
                    # sliding window exists to survive.
                    if not self._memguard_breach_since:
                        self._memguard_breach_since = now_m
                hits = sum(window)

                # Force-exit deadline, evaluated on EVERY iteration once we have
                # tripped. The old code reached this check only while the breach
                # was still live, so a single clear sample after the trip
                # disarmed the deadline and a drain that never completes could
                # hang indefinitely -- the exact case the deadline exists for.
                if (
                    deadline_s > 0
                    and self._memguard_drain_m
                    and now_m - self._memguard_drain_m >= deadline_s
                ):
                    _logger.critical(
                        f"[worker {self.worker_id}] memory guard: drain did not "
                        f"complete within {deadline_s:.0f}s ({self._in_flight} "
                        f"in-flight) -- force-exiting now (docker will restart)"
                    )
                    os._exit(0)

                # Leak attribution. The first breach is the cheapest moment to
                # start tracing that is still early enough to be useful: the
                # leak grows continuously (35-80MB/min measured), so a bounded
                # window from here attributes far more than the noise floor,
                # and a worker that never breaches never pays the cost. Armed
                # only when the operator has asked for it -- see memtrace.
                if reasons:
                    memtrace.arm(self.worker_id)
                if memtrace.due():
                    memtrace.report(self.worker_id)

                if reasons and not warned:
                    warned = True
                    _logger.warning(
                        f"[worker {self.worker_id}] memory guard: "
                        f"{'; '.join(reasons)} -- breaching {hits} of the last "
                        f"{window_n} samples, drains at {need_n}/{window_n} "
                        f"({cgroup_mem.status_line(cur)})"
                    )
                elif warned and hits == 0:
                    warned = False
                    _logger.info(
                        f"[worker {self.worker_id}] memory guard: pressure "
                        f"cleared (no breach in the last {window_n} samples) "
                        f"-- standing down"
                    )
                if hits == 0:
                    self._memguard_breach_since = 0.0

                if hits < need_n:
                    continue

                # Sustained. Trip -- unless something else already drained us
                # (a rolling self-update): stealing that drain would let our
                # deadline force-exit a worker mid-update.
                if not self._draining:
                    # last_reasons, not reasons: the window can reach need_n on
                    # an iteration whose own sample happens to be a dip, and an
                    # empty reason string would strip the WHY out of both the
                    # log line and the heartbeat the operator reads.
                    self._memguard_reason = "; ".join(last_reasons)
                    self._memguard_at = time.time()
                    self._memguard_drain_m = now_m
                    self._draining = True
                    _logger.critical(
                        f"[worker {self.worker_id}] memory guard TRIPPED "
                        f"({self._memguard_reason}) -- draining for recycle; "
                        f"force-exit in {deadline_s:.0f}s if in-flight work "
                        f"does not finish. {cgroup_mem.status_line(cur)}"
                    )
                    # Tell the hub NOW rather than up to a heartbeat later, so
                    # it stops dispatching to a worker we've already given up on.
                    try:
                        loop.call_soon_threadsafe(self._heartbeat_kick.set)
                    except Exception:
                        pass
                    # Flush the leak report before the drain exits the process:
                    # a fast leaker (380MB/min was measured) trips well inside
                    # the trace window, and an unflushed trace is a wasted run.
                    memtrace.report(self.worker_id)
            except Exception:
                # A guard that crashes is worse than one that misses a cycle.
                _logger.debug("memory guard iteration failed", exc_info=True)

    def _watchdog_loop(self, loop: "asyncio.AbstractEventLoop") -> None:
        """Daemon thread: detect a wedged event loop and force-exit so the
        supervisor (docker ``restart: unless-stopped``) relaunches us clean.
        Runs OFF the loop, so it works even when the loop is fully blocked --
        the failure mode the old in-loop ``_reconnect_giveup_s`` check could
        never catch."""
        self._wd_last_pong = time.monotonic()
        while True:
            time.sleep(self._wd_check_s)
            try:
                loop.call_soon_threadsafe(self._wd_pong)
            except RuntimeError:
                return  # loop closed -> the process is shutting down
            stale = time.monotonic() - self._wd_last_pong
            if stale > self._wd_threshold_s:
                try:
                    _logger.critical(
                        f"[worker {self.worker_id}] event loop WEDGED: no callback "
                        f"ran for {stale:.0f}s (> {self._wd_threshold_s:.0f}s threshold) "
                        f"-> exit({WORKER_EXIT_CODE_VERSION_MISMATCH}) for supervisor restart"
                    )
                except Exception:
                    pass
                os._exit(WORKER_EXIT_CODE_VERSION_MISMATCH)
            # v2: loop still ticks (pong fresh above) but no successful hub
            # heartbeat for a long time => coroutines wedged (async hang -- the
            # dominant heavy-site / monsnode failure). _last_link_ok is seeded
            # at run() start + refreshed on every heartbeat; the >0 guard skips
            # the pre-loop window. Threshold ~5x the old 120s that false-fired,
            # so normal reconnects / load-induced heartbeat misses don't trip.
            if (
                self._wd_link_threshold_s > 0
                and self._last_link_ok > 0
                and (time.monotonic() - self._last_link_ok) > self._wd_link_threshold_s
            ):
                link_stale = time.monotonic() - self._last_link_ok
                try:
                    _logger.critical(
                        f"[worker {self.worker_id}] hub link STUCK: no successful "
                        f"heartbeat for {link_stale:.0f}s (> "
                        f"{self._wd_link_threshold_s:.0f}s) while the loop still "
                        f"ticks -- coroutines wedged -> "
                        f"exit({WORKER_EXIT_CODE_VERSION_MISMATCH})"
                    )
                except Exception:
                    pass
                os._exit(WORKER_EXIT_CODE_VERSION_MISMATCH)
            # v3: INBOUND-silence arm. The link arm above trusts our SEND
            # succeeding; on a stale proxied WS the send keeps "succeeding" into
            # nginx while no hub consumes us (the ghost). _last_inbound_ok is
            # stamped only on a frame RECEIVED from the hub. If we BELIEVE we are
            # connected (self._ws set) yet have heard nothing back past the
            # threshold, no hub is serving this link -> exit + reconnect re-homes
            # us via the consistent hash. The >0 guard + reset-on-disconnect keep
            # idle-on-old-hub and reconnect windows from false-firing.
            if (
                self._wd_inbound_threshold_s > 0
                and self._ws is not None
                and self._last_inbound_ok > 0
                and (time.monotonic() - self._last_inbound_ok) > self._wd_inbound_threshold_s
            ):
                inb_stale = time.monotonic() - self._last_inbound_ok
                try:
                    _logger.critical(
                        f"[worker {self.worker_id}] hub link GHOST: no inbound "
                        f"frame for {inb_stale:.0f}s (> "
                        f"{self._wd_inbound_threshold_s:.0f}s) while connected -- "
                        f"no hub consuming us -> "
                        f"exit({WORKER_EXIT_CODE_VERSION_MISMATCH})"
                    )
                except Exception:
                    pass
                os._exit(WORKER_EXIT_CODE_VERSION_MISMATCH)

    def _wd_pong(self) -> None:
        """Runs ON the event loop (scheduled via call_soon_threadsafe by the
        watchdog thread): proof the loop is executing callbacks. Cheap +
        high-priority, so a merely busy / starved loop still runs it -- only a
        genuinely BLOCKED loop misses it."""
        self._wd_last_pong = time.monotonic()

    async def _handle_hub_message(self, msg) -> None:
        if isinstance(msg, HubAssignJob):
            t = asyncio.create_task(self._run_assigned_job(msg))
            t.add_done_callback(self._on_job_task_done)
            return
        if isinstance(msg, HubAssignVideoDownload):
            # Downloader tier (docs/ramdisk-video-tier.md): no lane, no Chrome
            # -- download to the shared ramdisk and upload to the parent job.
            # Counted through the same in-flight bookkeeping as jobs so the
            # heartbeat/capacity view stays honest.
            t = asyncio.create_task(self._handle_assign_video_download(msg))
            t.add_done_callback(self._on_job_task_done)
            return
        if isinstance(msg, HubForceCompleteJob):
            # Hub asked us to wrap up a deferred video download for this
            # job_id. Mark the flag so the deferred task's finally block
            # ffmpeg-remuxes any partial .part into a playable .mp4 and
            # uploads it. Then SIGTERM the in-flight yt-dlp / ffmpeg
            # subprocess(es) for this job so ``run_ytdlp`` returns and the
            # finally block runs. Best-effort: if there's no in-flight DL
            # the SIGTERM scan is a no-op and the flag is cleared by the
            # task's done callback (or by next force-complete arrival).
            asyncio.create_task(self._force_complete_video_job(
                msg.job_id, msg.reason or "",
            ))
            return
        if isinstance(msg, HubExpectedVersion):
            # Hub re-advertised its expected worker version mid-connection
            # (heartbeat). Run the SAME rolling self-update check as at handshake
            # so a worker-code deploy rolls out without a hub restart.
            await self._maybe_begin_self_update(
                msg.expected_worker_version, source="hub heartbeat",
            )
            return
        if isinstance(msg, HubScreenshotRequest):
            # Don't block the recv loop on ffmpeg; fan out to a task.
            asyncio.create_task(self._handle_screenshot(msg))
            return
        if isinstance(msg, HubPreviewSubscribe):
            # Push-based previews: an admin is watching us -> (re)arm the
            # self-capture loop. Cheap synchronous state update.
            self._on_preview_subscribe(msg)
            return
        if isinstance(msg, HubSessionStart):
            asyncio.create_task(self._handle_session_start(msg))
            return
        if isinstance(msg, HubSessionAction):
            # One task per action; the per-session Lock serialises them
            # so concurrent ops on the same session can't interleave.
            asyncio.create_task(self._handle_session_action(msg))
            return
        if isinstance(msg, HubSessionEnd):
            asyncio.create_task(self._handle_session_end(msg))
            return
        if isinstance(msg, HubSessionAgent):
            asyncio.create_task(self._handle_session_agent(msg))
            return
        if isinstance(msg, HubProfileSync):
            # Prefetch into the local cache without blocking the WS
            # loop. Same async pattern as HubAssignJob; failures are
            # logged but never propagate (the on-demand fetch path
            # is the fallback).
            asyncio.create_task(self._handle_profile_sync(msg))
            return
        if isinstance(msg, HubProfileDelete):
            asyncio.create_task(self._handle_profile_delete(msg))
            return
        if isinstance(msg, HubSessionInteraction):
            # Record that the operator is actively driving this session
            # via noVNC. The yt-dlp stall-detection gates consult
            # is_session_protected(session_id) before killing -- as long
            # as pings keep arriving (= human is moving the mouse /
            # typing), kills are deferred. Cheap dict write; no async
            # work to schedule.
            try:
                _session_interaction_at[msg.session_id] = float(msg.ts) or time.time()
            except Exception:
                pass
            return
        if isinstance(msg, HubUpdateGate):
            # Hub's response to our WorkerDraining: either green-light
            # the fetch + exit (a slot in the rolling-update budget
            # opened up) or "stay in drain mode, we're full". The
            # _drain_and_self_update task awaits self._update_gate and
            # reads self._update_jitter_s; we set them here.
            if msg.allow_now:
                self._update_jitter_s = max(0.0, float(msg.jitter_s or 0.0))
                _logger.info(
                    f"[worker {self.worker_id}] update gate: "
                    f"allow_now=True (jitter={self._update_jitter_s:.1f}s); "
                    f"{msg.why}"
                )
                self._update_gate.set()
            else:
                # Hub is full; keep draining and wait for the next
                # HubUpdateGate(allow_now=True). The hub auto-pushes one
                # whenever a slot frees up (another worker disconnected).
                _logger.info(
                    f"[worker {self.worker_id}] update gate: "
                    f"queued -- {msg.why}"
                )
            return

    def _on_job_task_done(self, task) -> None:
        """Fires once per finished assignment (success, failure, or early
        return). Counts it and trips the recycle drain at the threshold."""
        try:
            exc = task.exception()
        except BaseException:
            # cancelled (CancelledError is BaseException) or not-done; either
            # way we still count the assignment as finished below.
            exc = None
        if exc is not None:
            _logger.info(
                f"[worker {self.worker_id}] job task ended with "
                f"{type(exc).__name__}: {exc}",
            )
        self._jobs_done += 1
        if (
            self._recycle_after > 0
            and not self._draining
            and self._jobs_done >= self._recycle_after
        ):
            self._draining = True
            _logger.info(
                f"[worker {self.worker_id}] recycle threshold reached "
                f"({self._jobs_done} >= {self._recycle_after}); draining "
                f"(no new jobs) then exiting for a fresh restart",
            )

