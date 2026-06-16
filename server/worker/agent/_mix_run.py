"""WorkerAgent mixin: run loop + WS handshake/heartbeat/watchdog + hub-message dispatch.

Part of the agent/ package; methods reach siblings via self (MRO).
Shared helpers + Phase-1 functions come from the imports below."""

from __future__ import annotations
import asyncio
import functools
import json
import os
import random
import shutil
import socket
import logging
import string
import sys
import tempfile
import time
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
    HubExpectedVersion,
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


def _sample_resources() -> tuple[float, float, float, float, float]:
    """Return (cpu_pct, mem_pct, disk_pct, disk_free_gb, load1) for this CT.

    Best-effort. A missing or unparseable /proc entry returns 0.0 for that
    field instead of raising, so the heartbeat loop stays tight and a
    funky kernel doesn't take the worker down. Designed to be called from
    the heartbeat thread (~10s cadence) so the CPU% delta window matches.

    cpu_pct + load1 are LXC-host (Proxmox node) signals because the CT
    shares /proc/stat + getloadavg with its host. mem_pct + disk_* are
    CT-local (cgroup memory + overlayfs root). The split matches what an
    operator needs to triage "this CT is full" vs "this whole node is
    overloaded across all CTs sharing it".
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

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
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
                except Exception:
                    return
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

