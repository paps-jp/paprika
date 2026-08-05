"""Worker registry + simple job scheduler.

State model (Redis-backed when available, in-memory dict as fallback):

  paprika:workers                       Sorted Set: worker_id -> last_heartbeat_ts
  paprika:worker:{worker_id}              Hash: capabilities JSON, in_flight, capacity
  paprika:worker:{worker_id}:online       String: "1" with EXPIRE (TTL ~30s)

The hub also keeps a local `connections: dict[worker_id, WebSocket]` because
WebSocket handles aren't serializable. Cross-hub job dispatch (multi-hub
deployments) is Phase 4+.

Scheduling algorithm (Phase 3 MVP): pick the alive worker with the smallest
`in_flight` value. No label-based filtering yet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import WebSocket

from server.protocol import (
    HubAssignJob,
    HubScreenshotRequest,
    HubSessionAction,
    HubSessionAgent,
    HubSessionEnd,
    HubSessionStart,
    WorkerCapabilities,
    encode_msg,
)

log = logging.getLogger(__name__)

# Heartbeat-related constants (seconds)
WORKER_TTL = 120  # if no heartbeat for this long, worker is considered dead
# A "no free lane in pool" auto-drained worker (Case C, see workers.py) is
# flipped back to "active" by heartbeat() the instant it reports a free lane
# again -- UNLESS it has rejected this many assigns in a row while still
# claiming a free lane (no successful WorkerJobAccepted in between). That
# pattern means a logically-broken lane pool (reports idle but can't actually
# spawn/serve a lane), which must STAY drained instead of thrashing the
# dispatcher. Reset to 0 on any accept. Env override for ops tuning.
try:
    _NO_FREE_LANE_MAX_STRIKES = max(
        1, int(os.environ.get("PAPRIKA_NO_FREE_LANE_MAX_STRIKES") or 3)
    )
except (TypeError, ValueError):
    _NO_FREE_LANE_MAX_STRIKES = 3
# A worker whose Chrome lanes are free (a video download released them, running
# off-lane via yt-dlp) can still be resource-strained by many concurrent
# downloads. pick_worker no longer counts downloading jobs against the Chrome-
# lane budget (they moved to ``downloading_jobs``), but it DOES skip a worker
# holding at least this many of them, so an off-lane download pile-up still
# throttles new dispatch onto that box. Env override for ops tuning.
try:
    _DOWNLOAD_CAP = max(1, int(os.environ.get("PAPRIKA_WORKER_DOWNLOAD_CAP") or 6))
except (TypeError, ValueError):
    _DOWNLOAD_CAP = 6


def _download_cap() -> int:
    """Live per-worker download cap: the cross-hub Setting ``worker_download_cap``
    (admin Settings tab -- raise it to lift the dispatch ceiling when the fleet
    is download-bound, no restart) first, else the env/static default. Lazy
    ``state`` import avoids a scheduler<->_state import cycle; the module is
    fully loaded by the time pick_worker calls this per dispatch."""
    try:
        from server.hub._state import state
        if state.settings is not None:
            v = state.settings.get("worker_download_cap", None)
            if isinstance(v, (int, float)) and v:
                return max(1, int(v))
    except Exception:
        pass
    return _DOWNLOAD_CAP
# I/O-aware dispatch (Phase 1, 2026-07-09). ``load1`` is a PHYSICAL-HOST signal:
# an LXC CT shares its Proxmox node's getloadavg, and a bare-metal worker reports
# its own box -- so ``io_sat = load1 / nproc`` (host load per core) lets pick_worker
# steer off an I/O-saturated physical host UNIFORMLY across both topologies, with
# no physical-topology model needed. Sibling of the existing ``disk_pct > 90`` gate,
# but for host load instead of CT disk fullness.
#   _IO_SAT_SOFT       = start applying the soft rank penalty above this load-per-core
#   _IO_SAT_CEIL       = two-tier hard gate: prefer hosts under this; fall back to
#                        saturated ones only when no under-ceil worker has a free lane
#   _IO_PENALTY_WEIGHT = how hard the soft penalty biases vs the integer lane load
# Set PAPRIKA_IO_AWARE_DISPATCH_DISABLE=1 to restore the pure lane-count pick.
def _io_env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
_IO_SAT_SOFT = _io_env_float("PAPRIKA_IO_SAT_SOFT", 1.5)
_IO_SAT_CEIL = _io_env_float("PAPRIKA_IO_SAT_CEIL", 4.0)
_IO_PENALTY_WEIGHT = _io_env_float("PAPRIKA_IO_PENALTY_WEIGHT", 1.0)
_IO_DISPATCH_DISABLED = (
    (os.environ.get("PAPRIKA_IO_AWARE_DISPATCH_DISABLE") or "").strip().lower()
    in ("1", "true", "yes", "on")
)
# How long stats_async() may reuse the last GOOD cross-hub (redis) worker
# aggregation when a fresh fetch times out, instead of collapsing the list to
# this hub's local connections only. Bridges transient redis stalls so the
# Workers tab row count doesn't flap (37 -> 30/7/0) as nginx round-robins the
# poll across hubs with different local fleets. Kept <= WORKER_TTL so a genuinely
# dead worker can't linger in the list longer than its heartbeat window.
_KNOWN_WORKERS_CACHE_TTL_S = 60.0
HEARTBEAT_INTERVAL = 10  # worker sends this often
# 120s TTL = 12 heartbeats of slack. Higher value than the old 30s so a
# brief event-loop stall (yt-dlp subprocess block, gc pause, big
# Python compute) doesn't false-positive "worker dead". Worker-side
# yt-dlp is offloaded to asyncio.to_thread (see core/fetcher.py /
# server/worker/agent.py), making this purely a defensive margin.


# Redis key helpers
def _k_index() -> str:
    return "paprika:workers"


def _k_worker(worker_id: str) -> str:
    return f"paprika:worker:{worker_id}"


def _k_online(worker_id: str) -> str:
    return f"paprika:worker:{worker_id}:online"


def _k_owner(worker_id: str) -> str:
    """Which hub replica currently holds this worker's control WS.

    Multi-hub foundation: when several hubs sit behind nginx, a session
    request can land on a hub that does NOT own the target worker's WS.
    The owning hub records its ``hub_id`` here (TTL-refreshed on
    register + heartbeat) so a future Hub→Hub forwarding layer can look
    up where to forward. Dormant for single-hub: written but never read.
    """
    return f"paprika:worker:{worker_id}:owner"


# Push-based preview cache (Redis-backed so ANY hub can serve a frame that was
# captured on the worker's OWNER hub). Workers push frames on a ~10s timer, but
# only while an admin is watching (see WorkerRegistry.preview_subscribe_loop),
# which decouples capture rate from admin poll rate.
PREVIEW_FRAME_TTL = 25   # secs a worker-pushed frame stays servable
PREVIEW_WATCH_TTL = 30   # secs an admin's "watching" interest persists in redis


def _k_preview_frame(worker_id: str, lane: int) -> str:
    return f"paprika:preview:frame:{worker_id}:{lane}"


def _k_preview_watch(worker_id: str) -> str:
    return f"paprika:preview:watch:{worker_id}"


# Atomic compare-and-delete: only drop the owner key if it still points
# at US. Guards the race where a worker's WS flaps from hub A to hub B
# (B's register sets owner=B) and A's delayed unregister would otherwise
# wipe B's valid ownership.
_OWNER_CAD_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


WorkerStatus = str  # "active" | "drain" | "standby"
ALLOWED_STATUSES = {"active", "drain", "standby"}


def _rewrite_lane_urls_if_stale(
    lane_urls: list[str],
    client_address: str | None,
) -> list[str]:
    """Auto-correct lane noVNC URLs that point to the wrong host.

    The worker constructs each lane's URL using ``NOVNC_PUBLIC_HOST``
    from its own ``.env``. When a host is cloned (LXC / Proxmox / VMware
    / dd), the ``.env`` -- including a stale ``NOVNC_PUBLIC_HOST`` --
    comes along. The clone then advertises ``http://<template-ip>:6080``
    even though the actual host has a different LAN IP. The admin UI
    surfaces this as "<worker-host> (via <worker-host>)" and the
    click-through noVNC link 404s for the operator.

    The hub already knows the worker's true source IP from the WS
    connection (``ws.client.host``, stored as ``client_address``). When
    that differs from the URL host AND looks like a regular LAN IP
    (not a Docker bridge / loopback), we substitute it in so the
    admin UI shows reachable URLs without operator intervention. The
    raw stale URLs remain on disk on the worker host -- this only
    affects what the hub serves back.

    Rules:
      * ``client_address`` is ``None`` / empty: nothing to rewrite.
      * ``client_address`` starts with ``127.``, ``172.16.``-``172.31.``,
        or is a docker-bridge prefix: this is the in-compose worker
        seen via the docker network. The worker's ``NOVNC_PUBLIC_HOST``
        is the operator-supplied LAN-visible IP, which is the correct
        value to expose -- skip rewriting.
      * URL host already matches ``client_address``: no-op.
      * Otherwise: replace the host portion. Port + path stay intact.
    """
    if not client_address:
        return lane_urls
    if client_address.startswith("127.") or client_address.startswith("::1"):
        return lane_urls
    # RFC 1918 docker-bridge ranges. We only filter the 172.16/12 block
    # because that's where docker default bridges live (172.17.0.0/16,
    # 172.18.0.0/16, ...). 10.0.0.0/8 and 192.168/16 are typical real
    # LANs and must not be skipped.
    if client_address.startswith("172."):
        try:
            second = int(client_address.split(".", 2)[1])
            if 16 <= second <= 31:
                return lane_urls
        except (ValueError, IndexError):
            pass

    out: list[str] = []
    for url in lane_urls:
        out.append(_swap_url_host(url, client_address))
    return out


def _swap_url_host(url: str, new_host: str) -> str:
    """Replace the host portion of ``url`` with ``new_host``, preserving
    scheme / port / path / query. Returns the url unchanged if it can't
    be parsed (defence in depth -- never blow up the registry response
    on a malformed worker-supplied URL)."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        if not parts.hostname:
            return url
        if parts.hostname == new_host:
            return url
        # Preserve port. Strip any embedded userinfo as it's not used here.
        netloc = new_host
        if parts.port is not None:
            netloc = f"{new_host}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return url


@dataclass
class ConnectedWorker:
    """In-memory record of a worker currently linked to this hub process."""

    worker_id: str
    ws: WebSocket
    capabilities: WorkerCapabilities
    in_flight: int = 0
    last_heartbeat: float = field(default_factory=time.time)
    # Snapshot of the worker's profile cache, taken from the most
    # recent WorkerHeartbeat. Empty list = no profiles prefetched
    # (or this worker hasn't sent a heartbeat yet -- prefetch may
    # be in flight). Each entry: {name, etag, size_bytes}.
    profiles_cached: list[dict] = field(default_factory=list)
    # CT / LXC-host resource snapshot from the most recent heartbeat.
    # All zero until the first heartbeat lands, or if the worker is on
    # a pre-2026-06-06 build that doesn't send them. cpu_pct + load1
    # reflect the Proxmox node; mem_pct + disk_* reflect the CT itself.
    # Used for: (a) the admin Workers list CPU/Mem/Disk columns, (b) the
    # pick_worker() disk-pressure skip (disk_pct > 90 -> no new jobs).
    cpu_pct: float = 0.0
    mem_pct: float = 0.0
    disk_pct: float = 0.0
    disk_free_gb: float = 0.0
    load1: float = 0.0
    # Cgroup-scoped memory from the heartbeat (2026-08-03). mem_pct above is
    # only trustworthy when mem_scope == "cgroup"; on the docker-in-LXC fleet
    # it is usually "host" (= the Proxmox node's memory, not this CT's), so
    # the ABSOLUTE figures here are the ones to reason about. mem_psi_* is the
    # refault-storm signal a percentage gauge structurally cannot show.
    # All zero/"" for pre-2026-08-03 workers.
    mem_scope: str = ""
    mem_current_mb: float = 0.0
    mem_anon_mb: float = 0.0
    mem_psi_some_avg60: float = 0.0
    mem_psi_full_avg60: float = 0.0
    # Fault RATES (not cumulative counters -- see protocol.WorkerHeartbeat).
    # mem_refault_per_s is the direct cache-thrash measure and reads 0.0 on a
    # healthy worker, so it is the one to watch for a building storm.
    mem_majfault_per_s: float = 0.0
    mem_refault_per_s: float = 0.0
    # Non-empty while the worker's own memory guard is draining it for a
    # recycle; carries the reason. Read by the hub's memory-choke salvage
    # escalation to tell "recovering itself" from "stuck and needs a push".
    memguard: str = ""
    #: Monotonic instant this hub first SAW memguard non-empty, so the
    #: escalation can require it to persist rather than firing on one beat.
    memguard_since: float = 0.0
    # Host CPU core count (os.cpu_count), heartbeat-reported. Lets the hub
    # normalise the host-level load1 into "load per core" (io_sat) for I/O-aware
    # dispatch -- essential for a fleet mixing Proxmox nodes and bare-metal boxes
    # of differing core counts. 0 = pre-2026-07-09 build (treated as healthy).
    nproc: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Operator-controlled status set from the admin UI:
    #   active   = normal -- receives new jobs (default)
    #   drain    = no new jobs; in-flight ones finish; will be brought
    #              back online with another status change
    #   standby  = paused intentionally; same effect as drain for the
    #              scheduler but semantically "don't auto-resume"
    # Lives in memory; resets to "active" on hub restart.
    status: WorkerStatus = "active"
    # Per-worker view of the hub's HTTP base URL. Resolved at WS handshake
    # time from the `Host` header on the worker -> hub upgrade request:
    # whatever address the worker dialed to reach us is, by definition,
    # an address it can reach again for HTTP uploads. Avoids the operator
    # having to set PUBLIC_BASE_URL when mixing networked + in-compose
    # workers.
    public_base_url: str | None = None
    # IP address the worker connected from (TCP src address as observed
    # by the hub). For in-compose workers this is the docker bridge IP
    # like 172.18.0.x; for LAN workers it's the actual host LAN IP.
    # Shown in the admin UI Workers table so operators can tell similar
    # `worker_id`s apart at a glance.
    client_address: str | None = None
    # Rolling-update state: when the worker has sent a WorkerDraining
    # because it detected the hub advertising a newer expected_worker_
    # version, this holds that version string until the worker disconnects
    # (presumably to restart on new code). status is also flipped to
    # ``drain`` so pick_worker() skips it. ``None`` == not currently
    # updating (either matches the hub version or is operator-controlled
    # drain rather than auto-update drain).
    pending_update_to: str | None = None
    # In-flight screenshot RPCs: req_id -> Future[WorkerScreenshotReply].
    # WorkerScreenshotReply messages from the worker resolve the matching
    # future so the requesting HTTP handler can return the JPEG.
    pending_screenshots: dict[str, asyncio.Future] = field(default_factory=dict)
    # Hub-side scheduling counters, independent of the worker-reported
    # ``in_flight`` (which heartbeat overwrites every HEARTBEAT_INTERVAL
    # seconds and is therefore racy as a scheduling input).
    #
    # ``pending_assigns`` = jobs THIS hub has dispatched (or reserved at
    # pick_worker time) that the worker has NOT YET ACKED via
    # WorkerJobAccepted. Bookkept by pick_worker(reserve_for_job=...) and
    # assign(). Moved into ``committed_jobs`` on WorkerJobAccepted (a single
    # atomic transition keeps the effective load stable across the accept).
    #
    # ``committed_jobs`` = jobs the worker has accepted and is running --
    # the hub's ground truth, independent of heartbeat resets. Populated by
    # WorkerJobAccepted, drained by WorkerJobComplete / WorkerJobFailed
    # (via :meth:`release`), and seeded from session snapshots at worker
    # reconnect (so a hub restart doesn't undercount the fleet's load).
    #
    # pick_worker uses ``len(pending_assigns) + len(committed_jobs)`` as the
    # effective load -- never ``in_flight``. This closes the over-dispatch
    # race that survived earlier fixes (incident 2026-06-15): with the
    # heartbeat-driven in_flight in the mix, the gap between assign-send
    # and WorkerJobAccepted always allowed a second picker to underestimate
    # the load, drive WorkerJobFailed "no free lane in pool" and burn 60%+
    # of failures. With both counters tracked here, no heartbeat reset can
    # undo a reservation.
    pending_assigns: set[str] = field(default_factory=set)
    committed_jobs: set[str] = field(default_factory=set)
    # Jobs this worker ACCEPTED that have entered the "downloading" phase: the
    # video is being fetched in a DETACHED task (yt-dlp) and the Chrome LANE HAS
    # BEEN RELEASED (see _spawn_deferred_video_download). Moved here OUT of
    # ``committed_jobs`` on the worker's WorkerJobProgress(phase="downloading")
    # -- its lane-release signal -- so pick_worker's ``_load`` stops counting
    # them against the Chrome-lane budget. Counting downloads as lane-occupancy
    # stranded free lanes on download-busy workers, so ``pick_worker`` returned
    # None while lanes sat idle and the >180s redrive reaper killed the queued
    # jobs (incident 2026-07-09: recurring 40-50% failure spikes). Still bounded
    # per-worker by ``_DOWNLOAD_CAP`` (off-lane downloads eat CPU/net/disk).
    # Discarded by :meth:`release` on complete/failed.
    downloading_jobs: set[str] = field(default_factory=set)

    # ``active_downloads`` = download_ids this hub has dispatched to a
    # DOWNLOADER-role worker (docs/ramdisk-video-tier.md) and not yet seen a
    # WorkerVideoDownloadComplete for. Distinct from ``downloading_jobs``,
    # which tracks yt-dlp running INSIDE a browse job on a browser-role
    # worker. Used two ways:
    #   * per-worker: keep one downloader from being handed everything
    #   * per-POOL: several downloader workers can bind-mount the SAME host
    #     tmpfs, so the real constraint is the sum across the pool -- the
    #     shared ramdisk is what actually runs out, not any one worker.
    active_downloads: set[str] = field(default_factory=set)
    # Monotonic deadline (``time.monotonic()`` epoch) for an auto-clearing
    # ``status="drain"``. The "no free lane in pool" recovery (Case C,
    # 2026-06-15) sets status="drain" + drain_until = now + 900s when a
    # worker rejects an assign for that reason -- a stuck lane needs time
    # to recover (or for operator intervention) but draining FOREVER would
    # leak fleet capacity. ``pick_worker`` checks the deadline and flips
    # status back to "active" the moment it passes (lazy clear, no reaper
    # task needed). 0.0 = no auto-clear (operator-set drain stays).
    drain_until: float = 0.0
    # Consecutive "no free lane in pool" assign rejections (Case C) with NO
    # successful WorkerJobAccepted in between. heartbeat() returns a drained
    # worker to "active" the moment it reports a free lane -- but only while
    # this stays below ``_NO_FREE_LANE_MAX_STRIKES``. A worker that keeps
    # claiming a free lane yet rejecting every assign has a broken lane pool
    # and is left drained (until its deadline / operator). Reset to 0 by the
    # WorkerJobAccepted handler (proof the lane pool actually works).
    no_free_lane_strikes: int = 0
    # True between WS handshake and the worker's first WorkerSessionAnnounce.
    # The announce is what populates ``committed_jobs`` from the worker's
    # actually-running sessions; until it arrives, ``committed_jobs`` is
    # empty even for a worker that's been busy for hours (it just reconnected
    # to a different hub after a deploy / consistent-hash re-route). Picking
    # such a worker would over-dispatch onto its already-busy lanes -- the
    # surviving "no free lane in pool" cascade after the pending_assigns +
    # committed_jobs fix went live. pick_worker skips workers with this
    # flag set; the flag is flipped to False at the end of the
    # WorkerSessionAnnounce reconcile.
    awaiting_announce: bool = True
    # Session RPCs:
    #   pending_session_starts[session_id] -> Future[WorkerSessionStartAck]
    #   pending_session_actions[request_id] -> Future[WorkerSessionActionResult]
    #   pending_session_ends[session_id] -> Future[WorkerSessionEndAck]
    pending_session_starts: dict[str, asyncio.Future] = field(default_factory=dict)
    pending_session_actions: dict[str, asyncio.Future] = field(default_factory=dict)
    pending_session_ends: dict[str, asyncio.Future] = field(default_factory=dict)
    pending_session_agents: dict[str, asyncio.Future] = field(default_factory=dict)

    async def send(self, msg) -> None:
        """Serialize and send a Pydantic message over the WS."""
        async with self.send_lock:
            await self.ws.send_text(encode_msg(msg))

    async def request_screenshot(
        self,
        lane_idx: int,
        *,
        max_width: int | None = 480,
        quality: int = 5,
        timeout: float = 8.0,
    ):
        """RPC: ask the worker for a JPEG of one lane. Returns the
        WorkerScreenshotReply on success, raises on timeout / send error.

        Caller is responsible for interpreting `reply.error`.
        """
        import uuid

        req_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_screenshots[req_id] = fut
        req = HubScreenshotRequest(
            req_id=req_id,
            lane_idx=lane_idx,
            max_width=max_width,
            quality=quality,
        )
        try:
            await self.send(req)
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.CancelledError:
                # Mid-capture cancellation (e.g. a codegen-loop attempt
                # finishing -> the screenshot task is cancelled) yields
                # "no screenshot" instead of a noisy CancelledError
                # traceback in the caller. TimeoutError is deliberately
                # NOT caught here: it must propagate so callers hit their
                # own `except TimeoutError` -> 504, instead of receiving
                # None and crashing on `reply.error` (the live-preview grid
                # calls this ~20x/poll and times out often on busy workers).
                return None
        finally:
            self.pending_screenshots.pop(req_id, None)

    def deliver_screenshot_reply(self, reply) -> None:
        """Resolve the pending future matching `reply.req_id`. No-op if the
        request already timed out or was cancelled."""
        fut = self.pending_screenshots.get(reply.req_id)
        if fut is not None and not fut.done():
            fut.set_result(reply)

    # ----- session RPC ------------------------------------------------------

    async def start_session(
        self,
        session_id: str,
        *,
        initial_url: str | None = None,
        lane_hint: int | None = None,
        asset_upload_base: str | None = None,
        cookies: list[dict] | None = None,
        min_asset_size_bytes: int = 0,
        asset_url_blacklist: list[str] | None = None,
        popup_policy: str = "kill",
        profile_url: str | None = None,
        profile_name: str | None = None,
        profile_etag: str | None = None,
        download_video: bool = False,
        timeout: float = 60.0,
    ):
        """Send HubSessionStart and await the worker's ack. Raises on
        timeout. Returns WorkerSessionStartAck (caller checks .error).

        ``asset_upload_base`` (optional, e.g.
        ``http://hub:8000/jobs/{parent_job_id}/assets``) is forwarded
        to the worker so ``page.capture()`` calls inside the session can
        upload their HTML/PNG/AX-tree to that endpoint -- otherwise
        captures stay on the worker's tempdir and the parent job's
        gallery looks empty.

        ``cookies`` (optional) is a list of CDP CookieParam dicts that
        the worker installs via Network.setCookies BEFORE navigating to
        ``initial_url``. The hub fills this in by looking the host of
        ``initial_url`` up in the per-host registry.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_session_starts[session_id] = fut
        try:
            await self.send(
                HubSessionStart(
                    session_id=session_id,
                    lane_hint=lane_hint,
                    initial_url=initial_url,
                    asset_upload_base=asset_upload_base,
                    cookies=cookies,
                    min_asset_size_bytes=int(min_asset_size_bytes or 0),
                    asset_url_blacklist=list(asset_url_blacklist or []),
                    popup_policy=(popup_policy or "kill"),
                    profile_url=profile_url,
                    profile_name=profile_name,
                    profile_etag=profile_etag,
                    download_video=bool(download_video),
                )
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending_session_starts.pop(session_id, None)

    async def session_action(
        self,
        session_id: str,
        action: dict,
        *,
        timeout: float = 30.0,
    ):
        """Send HubSessionAction and await the result Future."""
        import uuid

        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_session_actions[request_id] = fut
        try:
            await self.send(
                HubSessionAction(
                    session_id=session_id,
                    request_id=request_id,
                    action=action,
                )
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending_session_actions.pop(request_id, None)

    async def end_session(
        self,
        session_id: str,
        *,
        timeout: float = 20.0,
    ):
        """Send HubSessionEnd and await ack."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_session_ends[session_id] = fut
        try:
            await self.send(HubSessionEnd(session_id=session_id))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending_session_ends.pop(session_id, None)

    def deliver_session_start_ack(self, ack) -> None:
        fut = self.pending_session_starts.get(ack.session_id)
        if fut is not None and not fut.done():
            fut.set_result(ack)

    def deliver_session_action_result(self, result) -> None:
        fut = self.pending_session_actions.get(result.request_id)
        if fut is not None and not fut.done():
            fut.set_result(result)

    def deliver_session_end_ack(self, ack) -> None:
        fut = self.pending_session_ends.get(ack.session_id)
        if fut is not None and not fut.done():
            fut.set_result(ack)

    async def session_agent(
        self,
        session_id: str,
        goal: str,
        max_steps: int,
        *,
        engine: str = "auto",
        timeout: float = 300.0,
    ):
        """Send HubSessionAgent (page.agent()) and await the result.

        ``engine`` is one of ``"auto"`` / ``"qwen"`` / ``"cogagent"``;
        the worker dispatches to the matching driver. See HubSessionAgent
        docstring for the cascade semantics of auto.
        """
        import uuid

        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_session_agents[request_id] = fut
        try:
            await self.send(
                HubSessionAgent(
                    session_id=session_id,
                    request_id=request_id,
                    goal=goal,
                    max_steps=max_steps,
                    engine=engine,
                )
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending_session_agents.pop(request_id, None)

    def deliver_session_agent_result(self, result) -> None:
        fut = self.pending_session_agents.get(result.request_id)
        if fut is not None and not fut.done():
            fut.set_result(result)


class WorkerRegistry:
    """Tracks connected workers. Persistent details go to Redis; live WS
    handles stay in process memory."""

    def __init__(self, redis_client=None, hub_id: str = ""):
        # redis_client: redis.asyncio.Redis | None
        self._r = redis_client
        # Stable id of THIS hub replica (see _k_owner). Empty string in
        # single-hub / tests; ownership writes still happen but are
        # never consulted, so the value is immaterial there.
        self._hub_id = hub_id or ""
        # worker_id -> ConnectedWorker
        self.connections: dict[str, ConnectedWorker] = {}
        # worker_id -> set of job_ids currently assigned by this hub
        self.assignments: dict[str, set[str]] = {}
        # Per-worker hub-side ring buffer of recent activity. The admin
        # UI's Workers tab "..." menu reads this via GET /workers/{id}/logs
        # so the operator can debug a worker without SSHing to its host.
        # Populated from:
        #   * connect / disconnect lifecycle events
        #   * status changes (active / drain / standby)
        #   * heartbeat in_flight transitions
        #   * forwarded WorkerJobLog lines (with job_id prefix)
        #   * errors raised in _handle_worker_message
        # In-memory only; resets on hub restart. Survives the worker's
        # own disconnect (we keep historical workers' rows in Redis;
        # logs are the in-process companion to that).
        self._worker_logs: dict[str, deque] = {}
        self._worker_log_cap = 400
        # Last GOOD cross-hub aggregation from _fetch_known_workers (redis),
        # reused by stats_async() as a fallback when a fresh fetch times out so
        # the Workers tab doesn't flap to local-hub-only. See
        # _KNOWN_WORKERS_CACHE_TTL_S.
        self._last_known_extra: list[dict] | None = None
        self._last_known_extra_at: float = 0.0

    # ----- registration ----------------------------------------------------

    async def register(
        self, worker_id: str, ws: WebSocket, caps: WorkerCapabilities
    ) -> ConnectedWorker:
        worker = ConnectedWorker(worker_id=worker_id, ws=ws, capabilities=caps)
        self.connections[worker_id] = worker
        self.assignments.setdefault(worker_id, set())
        if self._r is not None:
            await self._r.zadd(_k_index(), {worker_id: time.time()})
            await self._r.set(
                _k_worker(worker_id),
                json.dumps(
                    {
                        "worker_id": worker_id,
                        "capabilities": caps.model_dump(),
                        "in_flight": 0,
                        # Owning hub from the start, so the admin "which hub"
                        # badge is populated before the first heartbeat too.
                        "hub_id": self._hub_id,
                    }
                ),
            )
            await self._r.set(_k_online(worker_id), "1", ex=WORKER_TTL)
            # Claim WS ownership for this hub (multi-hub foundation).
            await self._r.set(_k_owner(worker_id), self._hub_id, ex=WORKER_TTL)
        return worker

    async def rebind_redis(self, redis_client, hub_id: str = "") -> int:
        """Attach a Redis client to a registry that started WITHOUT one, and
        re-publish every locally-connected worker. Returns the count.

        A hub that boots while Redis is still ``LOADING`` wires ``_r=None`` for
        its entire lifetime (the client is read once, in lifespan): its workers
        are never mirrored, so every peer hub's /workers shows only its own
        locals -- which nginx round-robin turns into the flapping worker count
        (2026-08-03 / 2026-08-04). ``server/hub/app.py:_redis_reattach_loop``
        calls this the moment Redis answers again.

        The re-publish pass is the point: ``register()`` is the ONLY place the
        full worker row is written -- the heartbeat path merely PATCHES a row
        that already exists. Without it, the workers connected during the
        Redis-less window would stay invisible to peers until each happened to
        reconnect."""
        self._r = redis_client
        if hub_id:
            self._hub_id = hub_id
        if redis_client is None:
            return 0
        n = 0
        now = time.time()
        for worker_id, w in list(self.connections.items()):
            row = {
                "worker_id": worker_id,
                "capabilities": w.capabilities.model_dump(),
                "in_flight": w.in_flight,
                "hub_id": self._hub_id,
                "status": w.status,
            }
            addr = (w.client_address or "").strip()
            if addr:
                row["address"] = addr
            try:
                await redis_client.zadd(_k_index(), {worker_id: now})
                await redis_client.set(_k_worker(worker_id), json.dumps(row))
                await redis_client.set(_k_online(worker_id), "1", ex=WORKER_TTL)
                await redis_client.set(
                    _k_owner(worker_id), self._hub_id, ex=WORKER_TTL,
                )
                n += 1
            except Exception as e:
                log.warning(
                    "rebind_redis: re-mirror of %s failed: %s", worker_id, e)
                continue
        return n

    async def persist_client_address(self, worker_id: str, address: str | None) -> None:
        """Write a worker's client_address into its Redis row right away, so a
        NON-OWNER hub serving /workers shows the IP immediately instead of
        waiting for the worker's first heartbeat to stamp it. Without this the
        IP column flickered to '-' for freshly-(re)connected workers whenever
        nginx routed /workers to a hub that does not own that worker's WS.
        No-op for single-hub / no-redis / empty address."""
        if self._r is None:
            return
        addr = (address or "").strip()
        if not addr:
            return
        try:
            raw = await self._r.get(_k_worker(worker_id))
            if not raw:
                return
            d = json.loads(raw)
            if isinstance(d, dict) and d.get("address") != addr:
                d["address"] = addr
                await self._r.set(_k_worker(worker_id), json.dumps(d))
        except Exception:
            pass

    async def drain_local_workers(self) -> int:
        """Graceful pre-restart (in-flight protection, layer 1): set every
        LOCALLY-connected worker to 'drain' so THIS hub's pick_worker stops
        handing them NEW jobs, and mirror the status into each worker's Redis
        row immediately so peer hubs' cross-hub dispatch (P1) also stops
        routing jobs here while we drain + restart. In-flight jobs keep
        running. Workers reset to 'active' on their next register after the
        restart, so there's nothing to undo. Returns the count drained."""
        n = 0
        for wid, w in list(self.connections.items()):
            try:
                w.status = "drain"
                n += 1
            except Exception:
                continue
            if self._r is not None:
                try:
                    raw = await self._r.get(_k_worker(wid))
                    if raw:
                        d = json.loads(raw)
                        if isinstance(d, dict):
                            d["status"] = "drain"
                            await self._r.set(_k_worker(wid), json.dumps(d))
                except Exception:
                    pass
        return n

    def local_in_flight(self) -> int:
        """Sum of in-flight jobs across this hub's LOCALLY-connected workers.
        Used by the prepare-restart drain loop to know when it's safe to
        restart without failing in-flight work."""
        return sum(
            int(getattr(w, "in_flight", 0) or 0) for w in self.connections.values()
        )

    # ----- push-based preview cache + interest signalling ------------------
    async def preview_put_frame(
        self, worker_id: str, lane: int, jpeg_b64: str, ts, width,
    ) -> None:
        """Store a worker-PUSHED preview frame in Redis so any hub serving the
        admin grid reads it without a live cross-hub capture."""
        if self._r is None:
            return
        try:
            await self._r.set(
                _k_preview_frame(worker_id, lane),
                json.dumps(
                    {"b": jpeg_b64, "t": float(ts or 0.0), "w": int(width or 0)}
                ),
                ex=PREVIEW_FRAME_TTL,
            )
        except Exception:
            pass

    async def preview_get_frame(self, worker_id: str, lane: int):
        """Most-recent pushed frame as {'b':jpeg_b64,'t':ts,'w':width}, or None."""
        if self._r is None:
            return None
        try:
            raw = await self._r.get(_k_preview_frame(worker_id, lane))
        except Exception:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            return None

    async def preview_mark_watch(self, worker_id: str) -> None:
        """Record (cross-hub, via Redis) that an admin is watching this worker
        NOW. The worker's OWNER hub reads this in preview_subscribe_loop to ask
        the worker to self-capture; the key self-expires so interest fades once
        the operator closes #screens."""
        if self._r is None:
            return
        try:
            await self._r.set(_k_preview_watch(worker_id), "1", ex=PREVIEW_WATCH_TTL)
        except Exception:
            pass

    async def preview_is_watched(self, worker_id: str) -> bool:
        if self._r is None:
            return False
        try:
            return bool(await self._r.get(_k_preview_watch(worker_id)))
        except Exception:
            return False

    async def preview_subscribe_loop(self, interval_s: float = 3.0) -> None:
        """Background loop: while an admin is watching one of THIS hub's
        connected workers, tell that worker to self-capture + push its lanes
        (HubPreviewSubscribe). Only workers advertising supports_preview_push
        are messaged (older ones can't parse the type and keep the legacy pull
        path). Sends nothing when nobody is watching, so an idle fleet / an
        unwatched grid costs zero capture."""
        from server.protocol import HubPreviewSubscribe
        while True:
            try:
                for worker_id, worker in list(self.connections.items()):
                    caps = getattr(worker, "capabilities", None)
                    if not getattr(caps, "supports_preview_push", False):
                        continue
                    try:
                        if not await self.preview_is_watched(worker_id):
                            continue
                        # Cadence tuning (2026-06-06, #screens "もっさり" fix):
                        # this loop now ticks every 3s (was 8s) so a freshly
                        # watched worker starts capturing within ~3s instead of
                        # ~9s (warm-up), and the worker self-captures every 5s
                        # (was 10s) so the grid refreshes twice as smoothly. Cost
                        # is bounded: only WATCHED + push-capable workers (≤ the
                        # 20-tile page) capture, at 320px/low-q -- far below the
                        # old per-poll live-capture storm this push model replaced.
                        await worker.send(
                            HubPreviewSubscribe(
                                lanes=None, interval_s=5.0, ttl_s=30.0,
                                max_width=320, quality=5,
                            )
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            await asyncio.sleep(interval_s)

    async def unregister(self, worker_id: str, ws: "WebSocket | None" = None) -> None:
        # Compare-and-delete: a STALE worker_link's finally (the worker has
        # already reconnected on a NEW ws that re-registered under the same
        # worker_id) must NOT evict the live registration. Without this guard
        # the old connection's cleanup pops the NEW connection -> the worker
        # keeps a live, echoing WS yet is absent from `connections` -> it shows
        # up as a reaped "/workers ghost" that the worker's own inbound-liveness
        # watchdog cannot detect (it still receives this hub's heartbeat echoes,
        # so _last_inbound_ok keeps refreshing). ws=None keeps the legacy
        # unconditional behaviour for any other caller.
        cur = self.connections.get(worker_id)
        if ws is not None and cur is not None and cur.ws is not ws:
            return  # a newer connection owns this worker_id now -- leave it
        self.connections.pop(worker_id, None)
        self.assignments.pop(worker_id, None)
        if self._r is not None:
            try:
                await self._r.delete(_k_online(worker_id))
                # Release WS ownership, but only if it still points at us
                # (a reconnect to another hub may already own it).
                await self._r.eval(
                    _OWNER_CAD_LUA, 1, _k_owner(worker_id), self._hub_id,
                )
                # Keep the worker row + index entry so operators can see history.
                # If you want to fully remove, use:
                # await self._r.delete(_k_worker(worker_id))
                # await self._r.zrem(_k_index(), worker_id)
            except Exception:
                pass

    async def heartbeat(
        self,
        worker_id: str,
        in_flight: int,
        cpu_pct: float = 0.0,
        mem_pct: float = 0.0,
        disk_pct: float = 0.0,
        disk_free_gb: float = 0.0,
        load1: float = 0.0,
        nproc: int = 0,
        mem_scope: str = "",
        mem_current_mb: float = 0.0,
        mem_anon_mb: float = 0.0,
        mem_psi_some_avg60: float = 0.0,
        mem_psi_full_avg60: float = 0.0,
        mem_majfault_per_s: float = 0.0,
        mem_refault_per_s: float = 0.0,
        memguard: str = "",
    ) -> None:
        worker = self.connections.get(worker_id)
        if worker is None:
            return
        worker.in_flight = in_flight
        worker.last_heartbeat = time.time()
        worker.cpu_pct = cpu_pct
        worker.mem_pct = mem_pct
        worker.disk_pct = disk_pct
        worker.disk_free_gb = disk_free_gb
        worker.load1 = load1
        worker.mem_scope = mem_scope
        worker.mem_current_mb = mem_current_mb
        worker.mem_anon_mb = mem_anon_mb
        worker.mem_psi_some_avg60 = mem_psi_some_avg60
        worker.mem_psi_full_avg60 = mem_psi_full_avg60
        worker.mem_majfault_per_s = mem_majfault_per_s
        worker.mem_refault_per_s = mem_refault_per_s
        # Stamp the FIRST beat that carried a memguard reason and hold it until
        # the reason clears. The escalation needs "has been trying to recycle
        # itself for N minutes", which a per-beat flag can't express -- and the
        # worker's own drain deadline must be allowed to win before the hub
        # reaches for a heavier hammer.
        if memguard:
            if not worker.memguard:
                worker.memguard_since = time.monotonic()
        else:
            worker.memguard_since = 0.0
        worker.memguard = memguard
        # nproc is static; a heartbeat may omit it (default 0) -- keep the last
        # known non-zero value so io_sat stays computable across such beats.
        if nproc:
            worker.nproc = nproc
        # Lane-freed auto-recovery (operator ask 2026-07-08): a worker that was
        # auto-drained for "no free lane in pool" (Case C sets drain_until > 0)
        # returns to "active" the instant its own heartbeat proves a lane is
        # free again (in_flight < capacity) -- no need to sit out the full
        # PAPRIKA_NO_FREE_LANE_DRAIN_S (900s) deadline while healthy. Operator-
        # and prepare-restart-set drains keep drain_until == 0.0 and are never
        # touched here. The strike gate leaves a logically-broken worker (claims
        # a free lane but keeps rejecting assigns) drained. The status change is
        # mirrored into Redis by the block below (it stamps worker.status).
        if (
            worker.status == "drain"
            and worker.drain_until
            and in_flight < worker.capabilities.max_concurrent
            and worker.no_free_lane_strikes < _NO_FREE_LANE_MAX_STRIKES
        ):
            worker.status = "active"
            worker.drain_until = 0.0
        if self._r is not None:
            try:
                await self._r.zadd(_k_index(), {worker_id: time.time()})
                await self._r.set(_k_online(worker_id), "1", ex=WORKER_TTL)
                # Refresh WS-ownership lease (multi-hub foundation).
                await self._r.set(
                    _k_owner(worker_id), self._hub_id, ex=WORKER_TTL,
                )
                await self._r.hset(
                    _k_worker(worker_id) + ":counts",
                    mapping={
                        "in_flight": in_flight,
                        "capacity": worker.capabilities.max_concurrent,
                    },
                )
                # Persist client_address into the worker row so the admin
                # UI shows the IP for offline workers too. _fetch_known_workers
                # picks it up when reconstructing the disconnected list.
                # We do this on heartbeat (not just register) because
                # client_address is assigned by the route handler AFTER
                # register() returns -- doing it here catches the value
                # on the first heartbeat at the latest.
                addr = (worker.client_address or "").strip()
                raw = await self._r.get(_k_worker(worker_id))
                if raw:
                    try:
                        d = json.loads(
                            raw.decode() if isinstance(raw, bytes) else raw
                        )
                    except Exception:
                        d = None
                    if isinstance(d, dict):
                        changed = False
                        if addr and d.get("address") != addr:
                            d["address"] = addr
                            changed = True
                        # Stamp the owning hub_id into the row too, so the admin
                        # "which hub" badge stays STABLE for a worker that's
                        # mid-reconnect: a non-owner hub reads it from here
                        # (refreshed every heartbeat) instead of the _k_owner
                        # lease, which a disconnect can briefly clear -> the
                        # badge flickered between hubs as nginx round-robined.
                        if d.get("hub_id") != self._hub_id:
                            d["hub_id"] = self._hub_id
                            changed = True
                        # Stamp the worker's prefetched-profile list into the
                        # row so a NON-OWNER hub serving /workers shows the
                        # プロファイル column instead of '-' (same cross-hub
                        # flicker the address / hub_id stamps above fix;
                        # _fetch_known_workers reads this back). profiles_cached
                        # changes rarely (only when a profile is prefetched /
                        # evicted), so a heartbeat-interval delay to first
                        # appearance is fine.
                        _pc = list(worker.profiles_cached or [])
                        if d.get("profiles_cached") != _pc:
                            d["profiles_cached"] = _pc
                            changed = True
                        # Operator-set status (active/drain/standby) and the
                        # rolling-update target are hub-side in-memory only;
                        # stamp them too so a NON-OWNER hub serving /workers
                        # shows the real ステータス + "draining→vX" badge
                        # instead of falling back to "active" / no badge.
                        if d.get("status") != worker.status:
                            d["status"] = worker.status
                            changed = True
                        if d.get("pending_update_to") != worker.pending_update_to:
                            d["pending_update_to"] = worker.pending_update_to
                            changed = True
                        # Mirror the resource snapshot into the row so a
                        # NON-OWNER hub serving /workers can render the
                        # CPU/Mem/Disk columns instead of "—". These move
                        # every heartbeat (true rates / disk-on-write), so we
                        # accept the write cost for every refresh -- without
                        # it the columns flicker depending on which hub the
                        # nginx round-robin lands on. Skip-write threshold
                        # not worth the complexity at heartbeat cadence.
                        if d.get("cpu_pct") != worker.cpu_pct:
                            d["cpu_pct"] = worker.cpu_pct
                            changed = True
                        if d.get("mem_pct") != worker.mem_pct:
                            d["mem_pct"] = worker.mem_pct
                            changed = True
                        if d.get("disk_pct") != worker.disk_pct:
                            d["disk_pct"] = worker.disk_pct
                            changed = True
                        if d.get("disk_free_gb") != worker.disk_free_gb:
                            d["disk_free_gb"] = worker.disk_free_gb
                            changed = True
                        if d.get("load1") != worker.load1:
                            d["load1"] = worker.load1
                            changed = True
                        # Same reasoning for the cgroup-scoped memory fields.
                        # Without them the Workers tab shows the memory column
                        # for only the ~1/7 of the fleet that happens to be
                        # owned by whichever hub nginx round-robined to -- the
                        # recurring per-hub-view trap.
                        #
                        # Only the DISPLAY fields are mirrored. The
                        # memguard-escalation timer stays owner-local on
                        # purpose: the owning hub is the one holding the live
                        # WS, so it is the right hub to decide a worker has
                        # stopped recycling itself, and a mirrored monotonic
                        # clock would be meaningless on a peer anyway.
                        for _k, _v in (
                            ("mem_scope", worker.mem_scope),
                            ("mem_current_mb", round(worker.mem_current_mb, 1)),
                            ("mem_anon_mb", round(worker.mem_anon_mb, 1)),
                            ("mem_psi_some_avg60",
                             round(worker.mem_psi_some_avg60, 2)),
                            ("mem_psi_full_avg60",
                             round(worker.mem_psi_full_avg60, 2)),
                            ("mem_majfault_per_s",
                             round(worker.mem_majfault_per_s, 1)),
                            ("mem_refault_per_s",
                             round(worker.mem_refault_per_s, 1)),
                            ("memguard", worker.memguard),
                        ):
                            if d.get(_k) != _v:
                                d[_k] = _v
                                changed = True
                        if changed:
                            await self._r.set(
                                _k_worker(worker_id), json.dumps(d),
                            )
            except Exception:
                pass

    async def owner_of(self, worker_id: str) -> str | None:
        """Return the hub_id currently owning ``worker_id``'s control WS,
        or None if unknown / Redis-less. Foundation for Hub→Hub routing;
        unused while running a single hub."""
        if self._r is None:
            return None
        try:
            raw = await self._r.get(_k_owner(worker_id))
        except Exception:
            return None
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    # ----- queries ---------------------------------------------------------

    def alive_workers(self) -> list[ConnectedWorker]:
        cutoff = time.time() - WORKER_TTL
        return [w for w in self.connections.values() if w.last_heartbeat >= cutoff]

    def downloader_workers(self) -> list[ConnectedWorker]:
        """All alive, active downloader-role workers (video ramdisk tier)."""
        return [
            w
            for w in self.alive_workers()
            if w.status == "active"
            and getattr(w.capabilities, "role", "browser") == "downloader"
        ]

    def _pool_key_of(self, w: ConnectedWorker) -> str:
        """Shared-ramdisk pool identity. Falls back to the worker_id so a
        downloader that forgot PAPRIKA_DOWNLOAD_POOL_KEY is treated as its own
        private pool rather than silently sharing (and over-committing) another
        node's tmpfs budget."""
        return getattr(w.capabilities, "download_pool_key", None) or w.worker_id

    def _pool_slots(self, pool_key: str) -> int:
        """Concurrency budget for one shared tmpfs pool.

        Workers on a node all bind-mount the same tmpfs and each advertise the
        POOL's budget (not their own share), so the pool size is the MAX
        advertised among them -- not the sum, which would multiply the budget
        by the number of co-located workers and let them fill the ramdisk."""
        slots = [
            int(getattr(w.capabilities, "download_slots", 0) or 0)
            for w in self.downloader_workers()
            if self._pool_key_of(w) == pool_key
        ]
        return max(slots) if slots else 0

    def _pool_in_flight(self, pool_key: str) -> int:
        """Downloads currently in flight across every worker in the pool."""
        return sum(
            len(w.active_downloads)
            for w in self.downloader_workers()
            if self._pool_key_of(w) == pool_key
        )

    def pick_downloader(
        self, reserve_download_id: str | None = None,
    ) -> ConnectedWorker | None:
        """Pick a downloader-role worker with a free slot in its ramdisk pool.

        Returns None when no downloader is connected or every pool is at its
        concurrency budget -- the caller MUST then fall back to the legacy
        in-browser download path (the tier is additive, never a hard
        dependency; see docs/ramdisk-video-tier.md §6).

        Reserves the slot synchronously (same contract as ``pick_worker``'s
        ``reserve_for_job``) so two concurrent handoffs can't both claim the
        last slot across an await boundary. Callers that bail before sending
        MUST call ``release_download``.
        """
        candidates: list[ConnectedWorker] = []
        for w in self.downloader_workers():
            pool = self._pool_key_of(w)
            slots = self._pool_slots(pool)
            if slots <= 0:
                continue  # pool budget unset -> tier disabled for it
            if self._pool_in_flight(pool) >= slots:
                continue  # shared ramdisk is at capacity
            if len(w.active_downloads) >= max(1, w.capabilities.max_concurrent):
                continue  # this particular worker is saturated
            candidates.append(w)
        if not candidates:
            return None
        # Least-loaded first, random among ties (same rationale as pick_worker:
        # avoids hammering the first-in-dict worker when the tier is idle).
        best = min(len(w.active_downloads) for w in candidates)
        tied = [w for w in candidates if len(w.active_downloads) == best]
        winner = random.choice(tied)
        if reserve_download_id:
            winner.active_downloads.add(reserve_download_id)
        return winner

    def release_download(self, worker_id: str, download_id: str) -> None:
        """Free a reserved/in-flight download slot. Idempotent -- safe to call
        from both the completion handler and a bail-out path."""
        w = self.connections.get(worker_id)
        if w is not None:
            w.active_downloads.discard(download_id)

    def pick_worker(
        self, reserve_for_job: str | None = None,
    ) -> ConnectedWorker | None:
        """Pick an alive worker with spare capacity, load-balanced.

        When ``reserve_for_job`` is given the returned worker's
        ``pending_assigns`` set is atomically (sync, no await) updated to
        include that job_id, so a concurrent ``pick_worker`` call running on
        an await suspension boundary (e.g. inside the caller's
        ``await store.claim_queued_job``) sees the worker as full. Callers
        that pick but then bail BEFORE ``assign()`` MUST call
        ``release_pending_assign(worker_id, job_id)`` -- the redrive's
        CAS-claim-then-assign flow is the canonical pattern.

        Only considers workers whose operator-controlled status is
        "active"; drained / standby workers are skipped silently.

        Also skips workers that advertise capacity but have no actual
        Chrome lanes (``lane_novnc_urls == []``). Such a worker accepts
        the assignment, then immediately rejects with "no free lane".
        That was the failure mode for job 71ec64da63c5 where the hub
        kept routing session_start to a misconfigured lane-less worker
        (<worker-host>, version "phase3") and all 3 codegen-loop attempts
        died with HTTP 502 "no free lane" within a few seconds each.

        Selection: least-loaded first (smallest in_flight, ties broken
        toward bigger total capacity), then **random among the tied
        best**. The randomisation matters because interactive use
        (Fetch / LLM / Macro / Code run one at a time) lets in_flight
        fall back to 0 between jobs, so without it every job lands on
        the same first-in-dict worker -- the "specific worker keeps
        getting hammered" complaint. Randomising the tie spreads the
        load across all idle workers while still preferring genuinely
        less-loaded ones when the fleet is busy.
        """
        # A worker is eligible when it has spare in_flight capacity AND
        # advertises at least one usable Chrome attach surface. There
        # are two valid shapes:
        #   * fleet workers run a lane pool -> ``lane_novnc_urls`` has
        #     one entry per pre-spawned lane
        #   * Windows portable workers run a SINGLE bundled Chrome
        #     attached via chrome_host/chrome_port, no lane pool -> they
        #     advertise the bundled-noVNC URL on ``novnc_url`` instead
        #     (lane_novnc_urls stays empty)
        # The old check (``lane_novnc_urls > 0`` only) rejected
        # Windows workers as "misconfigured / lane-less" and caused
        # "fleet at capacity" on a single-machine install.
        #
        # Effective load = ``len(pending_assigns) + max(in_flight,
        #                                              len(committed_jobs))``.
        # pending_assigns are FRESH reservations that haven't been absorbed
        # into either the worker's ``in_flight`` count or the hub's
        # ``committed_jobs`` set yet -- they ADD to the steady-state load,
        # not max with it. After WorkerJobAccepted lands the reservation
        # moves into ``committed_jobs`` (a single atomic transition), and
        # heartbeat eventually catches in_flight up; from then on the MAX
        # of the two steady-state counters is what represents the work
        # actually running on the worker. The MAX absorbs the
        # heartbeat lag (in_flight stale after accept) AND a drifted
        # committed_jobs (post-restart, sessions reconciled from snapshots
        # may be incomplete). The earlier formulations -- pure SUM
        # (over-counted after the accept-discard) and pure MAX (under-
        # counted during the pick reservation window) -- each fell
        # through one of these races; the
        # ``pending + max(in_flight, committed)`` shape is the smallest
        # one that closes both.
        def _load(w: ConnectedWorker) -> int:
            return len(w.pending_assigns) + max(w.in_flight, len(w.committed_jobs))

        # I/O-aware dispatch (Phase 1): io_sat = load1 / nproc = the worker's
        # PHYSICAL-HOST load per core. For an LXC CT that's the Proxmox node's
        # load (shared getloadavg); for bare-metal it's the box itself -- so the
        # same number steers dispatch off an I/O-saturated host uniformly, no
        # topology model. 0 (old worker / no signal) reads as healthy so a rolling
        # deploy is non-disruptive; the whole thing is off if the env kill-switch
        # is set. The soft penalty (in _key) biases ranking; the hard ceiling (a
        # two-tier gate below) keeps jobs off severely-slammed hosts unless the
        # fleet is otherwise full.
        def _io_sat(w: ConnectedWorker) -> float:
            return (w.load1 / w.nproc) if (w.nproc and w.load1) else 0.0

        def _io_penalty(w: ConnectedWorker) -> float:
            if _IO_DISPATCH_DISABLED:
                return 0.0
            return _IO_PENALTY_WEIGHT * max(0.0, _io_sat(w) - _IO_SAT_SOFT)

        # Lazy auto-clear of the "no-free-lane" auto-drain: any worker whose
        # ``drain_until`` deadline has passed flips back to "active" the
        # instant pick_worker scans it. Operator-set drains keep
        # drain_until=0.0 so they're never auto-cleared.
        _now_mono = time.monotonic()
        for _w in self.connections.values():
            if (
                _w.status == "drain"
                and _w.drain_until
                and _now_mono >= _w.drain_until
            ):
                _w.status = "active"
                _w.drain_until = 0.0
        # Live per-worker download cap (Setting worker_download_cap, else env).
        _dl_cap = _download_cap()
        candidates = [
            w
            for w in self.alive_workers()
            if w.status == "active"
            # Skip workers we haven't reconciled yet: their committed_jobs
            # set is empty even when they're actually busy, so picking them
            # would over-dispatch onto their already-occupied lanes. Cleared
            # the moment WorkerSessionAnnounce lands (typically <1s after
            # WS handshake).
            and not w.awaiting_announce
            and _load(w) < w.capabilities.max_concurrent
            # Off-lane download backpressure: skip a worker already holding
            # worker_download_cap downloading jobs (their Chrome lanes are free,
            # but yt-dlp is eating its CPU/net/disk) so we don't pile more on.
            and len(w.downloading_jobs) < _dl_cap
            # Downloader-tier workers run NO Chrome (docs/ramdisk-video-tier.md):
            # they must NEVER receive a browse job. The Chrome-surface check
            # below already excludes them in practice (no lanes, no novnc_url),
            # but an explicit role gate is the guarantee -- a downloader CT that
            # happens to inherit chrome_host/chrome_port from its args would
            # otherwise slip through and fail every fetch with "no free lane".
            and getattr(w.capabilities, "role", "browser") != "downloader"
            and (
                len(w.capabilities.lane_novnc_urls or []) > 0
                or bool(w.capabilities.novnc_url)
            )
            # Disk-pressure dispatch gate: skip workers whose CT root is
            # >90% full. A heartbeat lag means the worker-side preflight in
            # _mix_jobexec is the final defence, but doing it here too saves
            # the dispatch round-trip + the WorkerJobFailed handshake when
            # the operator can see it's full. disk_pct==0.0 == pre-2026-06-06
            # build (didn't report) -- treat as healthy to stay compatible.
            and w.disk_pct < 90.0
        ]
        if not candidates:
            return None

        # Two-tier host-I/O gate: prefer workers whose PHYSICAL host is under the
        # saturation ceiling; fall back to the saturated ones ONLY if no healthy
        # worker has a free lane (never drop a job for I/O). This is what keeps
        # dispatch off the "free Chrome lane but host load ~300" boxes while they
        # thrash, without stranding work when the whole fleet is hot.
        if not _IO_DISPATCH_DISABLED:
            _healthy = [w for w in candidates if _io_sat(w) < _IO_SAT_CEIL]
            if _healthy:
                candidates = _healthy

        # Rank key: smaller EFFECTIVE load first, then larger capacity. Effective
        # load = lane load + host-I/O soft penalty, so a free lane on an I/O-hot
        # host ranks below a free lane on a cool one (below the soft threshold the
        # penalty is 0, so a healthy fleet ranks exactly as it did pre-change).
        def _key(w: ConnectedWorker) -> tuple:
            return (_load(w) + _io_penalty(w), -w.capabilities.max_concurrent)

        best = min(_key(w) for w in candidates)
        tied = [w for w in candidates if _key(w) == best]
        # Random pick among equally-good workers -> even spread.
        winner = random.choice(tied)
        # Reserve the slot atomically WITH the pick. Otherwise the caller's
        # subsequent ``await`` (e.g. store.claim_queued_job's DB round-trip,
        # or building the assign msg) would let a second picker pick the same
        # worker before its slot is booked -- the over-dispatch race that
        # drove "no free lane in pool" failures even after pending_assigns
        # was added inside assign() (incident 2026-06-15, pre-pick-reserve).
        # Idempotent: assign() adds the same job_id to pending_assigns again,
        # set semantics make that a no-op. The caller MUST call
        # ``release_pending_assign`` on EVERY non-assign exit path (failed
        # CAS claim, build error) or the reservation persists and the worker
        # capacity drifts down by one until the next reconcile.
        if reserve_for_job is not None:
            winner.pending_assigns.add(reserve_for_job)
        return winner

    def release_pending_assign(self, worker_id: str, job_id: str) -> None:
        """Roll back a ``pick_worker(reserve_for_job=...)`` reservation when
        the caller decided NOT to call ``assign`` -- e.g. the CAS claim
        failed, or the assign-message build threw. Safe to call multiple
        times (set.discard is no-op on missing); safe to call after a
        successful WorkerJobAccepted handler discarded the same entry."""
        worker = self.connections.get(worker_id)
        if worker is not None:
            worker.pending_assigns.discard(job_id)

    def stats(self) -> dict:
        # Pass 1: every currently-connected worker (rich data: in_flight
        # counters, send_lock, ws handle ...). These are guaranteed
        # ``alive=True`` plus whatever the worker reported on its last
        # heartbeat.
        workers = []
        for w in self.connections.values():
            lane_urls = list(w.capabilities.lane_novnc_urls or [])
            lane_urls = _rewrite_lane_urls_if_stale(
                lane_urls,
                w.client_address,
            )
            workers.append(
                {
                    "worker_id": w.worker_id,
                    "in_flight": w.in_flight,
                    "capacity": w.capabilities.max_concurrent,
                    "labels": w.capabilities.labels,
                    "alive": (time.time() - w.last_heartbeat) < WORKER_TTL,
                    "age_seconds": int(time.time() - w.last_heartbeat),
                    "last_heartbeat": w.last_heartbeat,
                    # Per-lane noVNC URLs (index = lane_idx). Admin UI uses
                    # these for the click-through on each screenshot tile.
                    "lane_novnc_urls": lane_urls,
                    # Backwards-compat alias for one release cycle.
                    "slot_novnc_urls": list(lane_urls),
                    # Profile-cache view: which operator-uploaded Chrome
                    # profiles this worker has prefetched + the etag the
                    # hub last advertised. Lets the admin UI show "ready"
                    # vs "downloading" status per worker without polling
                    # the worker directly. Empty list = nothing cached.
                    "profiles_cached": list(w.profiles_cached or []),
                    "version": w.capabilities.version or "",
                    "status": w.status,
                    "address": w.client_address or "",
                    # CT/host resource snapshot from the last heartbeat.
                    # All zero for pre-2026-06-06 workers; admin UI renders
                    # those as "—" so the operator can tell unreported from
                    # genuinely-idle.
                    "cpu_pct": w.cpu_pct,
                    "mem_pct": w.mem_pct,
                    "disk_pct": w.disk_pct,
                    "disk_free_gb": w.disk_free_gb,
                    "load1": w.load1,
                    "nproc": w.nproc,
                    # Cgroup-scoped memory. mem_scope tells the UI whether
                    # mem_pct above is a real percentage ("cgroup") or the
                    # Proxmox node's ("host") -- render the latter as such
                    # instead of implying it's this worker's.
                    "mem_scope": w.mem_scope,
                    "mem_current_mb": round(w.mem_current_mb, 1),
                    "mem_anon_mb": round(w.mem_anon_mb, 1),
                    "mem_psi_some_avg60": round(w.mem_psi_some_avg60, 2),
                    "mem_psi_full_avg60": round(w.mem_psi_full_avg60, 2),
                    "mem_majfault_per_s": round(w.mem_majfault_per_s, 1),
                    "mem_refault_per_s": round(w.mem_refault_per_s, 1),
                    "memguard": w.memguard,
                    # Seconds this hub has seen the guard tripped without the
                    # worker managing to recycle itself. The hub-side memory
                    # escalation waits on THIS, not on the flag, so a worker
                    # that is simply mid-drain is left to finish on its own.
                    "memguard_s": (
                        round(time.monotonic() - w.memguard_since, 1)
                        if w.memguard_since
                        else 0.0
                    ),
                    # Host load per core (io_sat) -- the I/O-aware dispatch signal.
                    # None when nproc unknown (old worker) so the UI shows "—".
                    "io_sat": round(w.load1 / w.nproc, 2) if w.nproc else None,
                    # Which hub owns this worker's control WS. A live
                    # connection in THIS process is owned by THIS hub, so the
                    # admin can show which hub each worker is connected to.
                    "hub_id": self._hub_id or "",
                    # Rolling-update state. None unless the worker has
                    # signalled WorkerDraining; the admin UI shows this
                    # as a "draining (→ vX)" badge so operators can see
                    # which workers are mid-update vs operator-drained.
                    "pending_update_to": w.pending_update_to,
                }
            )
        # Pass 2 (historical workers from Redis) lives in ``stats_async``
        # below, since pulling Redis rows needs to await. Sync ``stats()``
        # only returns the live in-memory connections. The /workers route
        # uses ``stats_async`` so the UI sees the full set including
        # disconnected-but-remembered workers.
        return {"count": len(workers), "workers": workers}

    async def _fetch_known_workers(
        self, exclude_ids: set[str],
    ) -> list[dict]:
        """Pull worker rows from Redis for ids NOT in ``exclude_ids``
        (= the alive set, already added by stats()). Returns dicts in
        the same shape as ``stats()['workers']`` with ``alive=False``."""
        if self._r is None:
            return []
        try:
            ids = await self._r.zrange(_k_index(), 0, -1)
        except Exception:
            return []
        # Build the candidate id list (skip the alive set already in snap),
        # then batch ALL per-worker reads into ONE Redis pipeline round-trip.
        # The old path issued 4 SEQUENTIAL awaits per worker inside this loop;
        # with ~80 known rows (alive + offline history, incl. not-yet-reaped
        # stale ids) that was ~320 sequential cross-host RTTs (.35/.36/.37 hub
        # -> .34 redis) on EVERY /overview admin poll (~2s) + tab switch. A
        # brief RTT inflation turned that into a 10-20s poll hang ("もっさり").
        # Pipelining collapses it to ~1 RTT regardless of fleet/history size.
        out: list[dict] = []
        wids: list[str] = []
        for raw_id in ids:
            wid = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            if wid in exclude_ids:
                continue
            wids.append(wid)
        if not wids:
            return out
        try:
            pipe = self._r.pipeline(transaction=False)
            for wid in wids:
                pipe.get(_k_worker(wid))
                pipe.zscore(_k_index(), wid)
                pipe.get(_k_owner(wid))
                pipe.hgetall(_k_worker(wid) + ":counts")
            results = await pipe.execute()
        except Exception:
            return out
        # Decode the per-worker JSON blobs in ONE worker thread, off the event
        # loop. With ~80 known rows this json.loads batch was ~20/2600 of the
        # hub's on-CPU loop samples on EVERY ~2s /overview admin poll (py-spy
        # 2026-06-08) -- a contributor to the intermittent multi-second poll
        # stalls. The redis round-trip above stays async; only the CPU-bound
        # decode moves off the loop. One hop for the whole batch (not N).
        _raw_rows = [results[_i * 4] for _i in range(len(wids))]

        def _decode_rows(rows: list) -> list:
            decoded: list = []
            for _r in rows:
                if not _r:
                    decoded.append(None)
                    continue
                try:
                    decoded.append(
                        json.loads(_r.decode() if isinstance(_r, bytes) else _r)
                    )
                except Exception:
                    decoded.append(None)
            return decoded

        # Decode INLINE, not via asyncio.to_thread. These ~57 rows are <1ms of
        # json.loads. to_thread hands them to the default ThreadPoolExecutor,
        # which under load is saturated by heavier offloaded work (asset
        # mirror_file, /jobs row deser, HTML parse -- see hub-eventloop-stalls).
        # When saturated, this tiny decode queues behind them and stats_async's
        # 1.5s wait_for FIRES EVERY TIME -> _fetch_known_workers returns [] ->
        # the Workers tab collapses to local-hub-only and nginx round-robin
        # flaps the count (8 <-> 57). A <1ms inline decode beats that failure.
        try:
            _decoded_rows = _decode_rows(_raw_rows)
        except Exception:
            _decoded_rows = [None] * len(wids)
        for _i, wid in enumerate(wids):
            last_ts = results[_i * 4 + 1]
            owner = results[_i * 4 + 2]
            counts = results[_i * 4 + 3]
            data = _decoded_rows[_i]
            if data is None:
                continue
            caps = data.get("capabilities") or {}
            # Live load from the heartbeat-maintained ``:counts`` hash so a
            # non-owner hub shows the real 負荷 (in_flight was hardcoded 0
            # below, which flickered the load to 0/N whenever nginx routed
            # /workers to a peer hub). Redis returns bytes unless the client
            # decodes responses; normalise both.
            _in_flight = 0
            try:
                if counts:
                    _cd = {
                        (k.decode() if isinstance(k, bytes) else k):
                        (v.decode() if isinstance(v, bytes) else v)
                        for k, v in counts.items()
                    }
                    _in_flight = int(_cd.get("in_flight") or 0)
            except Exception:
                _in_flight = 0
            # Per-lane noVNC URLs live in the worker's (static) capabilities
            # row; rewrite stale clone hosts with the recorded client IP --
            # the same correction stats() applies for local connections -- so
            # a non-owner hub keeps the #screens tile noVNC click-through.
            _lane_urls = _rewrite_lane_urls_if_stale(
                list(caps.get("lane_novnc_urls") or []),
                str(data.get("address") or "") or None,
            )
            # Cross-hub presence: a worker NOT connected to THIS process may
            # still be live on a peer hub. Judge "alive" by redis heartbeat
            # freshness (same WORKER_TTL window stats() uses for local
            # connections) instead of hardcoding offline -- else a read-only
            # admin / a peer hub shows the whole fleet as offline. Dispatch
            # (pick_worker -> alive_workers -> self.connections) is unaffected;
            # this only changes the DISPLAY of redis-known rows.
            # A redis-known row is "alive" only when BOTH (a) its heartbeat is
            # fresh AND (b) some hub still holds its control-WS -- i.e. the
            # _k_owner lease is present. Gating on (a) alone lets a disconnected
            # worker linger as alive=True until its heartbeat index ages out
            # (WORKER_TTL=120s); but with no owner the preview/forward path can
            # only answer "worker not connected", so #screens renders a ghost
            # tile that ALWAYS fails to capture ("worker not connected" /
            # "peer HTTP 404"). unregister() clears the lease on a clean
            # disconnect, so requiring it drops those ghost tiles at once; a
            # crashed worker's lease still TTLs out inside the same window. The
            # owner lease is (re)written on every heartbeat alongside the index,
            # so a genuinely-connected worker (incl. one held by a PEER hub) is
            # never wrongly hidden. (owner is fetched above for hub_id.)
            _fresh = bool(last_ts) and (time.time() - float(last_ts)) < WORKER_TTL
            _alive = _fresh and bool(owner)
            out.append({
                "worker_id": wid,
                "in_flight": _in_flight,
                "capacity": int(caps.get("max_concurrent") or 1),
                "labels": dict(caps.get("labels") or {}),
                "alive": _alive,
                "age_seconds": (
                    int(time.time() - float(last_ts)) if last_ts else None
                ),
                "last_heartbeat": float(last_ts) if last_ts else None,
                # Per-lane noVNC URLs from the worker's capabilities row
                # (rewritten above) so a non-owner hub keeps the #screens tile
                # noVNC click-through instead of dropping it to an empty list.
                "lane_novnc_urls": list(_lane_urls),
                "slot_novnc_urls": list(_lane_urls),
                # Owning hub stamps the worker's prefetched-profile list into
                # the row on heartbeat (see heartbeat()), so a non-owner hub
                # serving /workers shows the same プロファイル column instead of
                # a flickering '-'. Empty for legacy rows / not-yet-stamped.
                "profiles_cached": list(data.get("profiles_cached") or []),
                "version": caps.get("version") or "",
                "status": (
                    str(data["status"]) if data.get("status")
                    else ("active" if _alive else "offline")
                ),
                # Surface the last-known IP for offline workers (persisted
                # to the worker row on heartbeat, see heartbeat() above).
                # Empty string when this worker has never heartbeated
                # against the current redis schema (= pre-fix legacy row).
                "address": str(data.get("address") or ""),
                # Owning hub (control-WS) so the admin shows which hub each
                # worker is connected to. Prefer the heartbeat-stamped row value
                # (stable across reconnects); fall back to the _k_owner lease.
                "hub_id": str(data.get("hub_id") or "") or (
                    (owner.decode() if isinstance(owner, bytes) else str(owner))
                    if owner else ""
                ),
                # Rolling-update target (heartbeat-stamped) so the
                # "draining→vX" badge survives a non-owner-hub /workers poll.
                "pending_update_to": data.get("pending_update_to"),
                # CT/host resource snapshot (heartbeat-stamped). Each is
                # ``float(... or 0)`` so a legacy row without these keys
                # renders as 0.0 (= "—" in the admin UI), matching what
                # stats() returns for a worker that hasn't heartbeated yet.
                "cpu_pct": float(data.get("cpu_pct") or 0.0),
                "mem_pct": float(data.get("mem_pct") or 0.0),
                "disk_pct": float(data.get("disk_pct") or 0.0),
                "disk_free_gb": float(data.get("disk_free_gb") or 0.0),
                "load1": float(data.get("load1") or 0.0),
                "nproc": int(data.get("nproc") or 0),
                # Cgroup-scoped memory. This dict is an explicit WHITELIST, so
                # mirroring a field into the Redis row (see heartbeat()) is only
                # half the job -- without a line here the value is written every
                # heartbeat and silently dropped on read, and the Workers tab
                # shows the memory column for just the ~1/7 of the fleet owned
                # by whichever hub nginx happened to route to.
                # mem_scope defaults to "" (not "host") so a legacy row is
                # rendered as "unknown scope", not as a claim we can't back.
                "mem_scope": str(data.get("mem_scope") or ""),
                "mem_current_mb": float(data.get("mem_current_mb") or 0.0),
                "mem_anon_mb": float(data.get("mem_anon_mb") or 0.0),
                "mem_psi_some_avg60": float(data.get("mem_psi_some_avg60") or 0.0),
                "mem_psi_full_avg60": float(data.get("mem_psi_full_avg60") or 0.0),
                "mem_majfault_per_s": float(data.get("mem_majfault_per_s") or 0.0),
                "mem_refault_per_s": float(data.get("mem_refault_per_s") or 0.0),
                "memguard": str(data.get("memguard") or ""),
            })
        return out

    async def stats_async(self) -> dict:
        """Async-aware variant of :func:`stats` that pulls historical
        worker rows from Redis. Use this from FastAPI handlers (which
        are themselves async); ``stats()`` is kept as a sync stub for
        any legacy caller that can't await."""
        snap = self.stats()
        if self._r is None:
            return snap
        seen = {w["worker_id"] for w in snap["workers"]}
        # Defensive ceiling: _fetch_known_workers is now pipelined (~1 RTT),
        # but if redis itself stalls we must NOT hang the /overview admin poll
        # (called every ~2s). Cap the wait at 1.5s.
        def _fresh_cache() -> list:
            # Reuse the most recent GOOD (non-empty) aggregation if still fresh,
            # dropping any id that is now a live local connection (already in
            # ``snap`` with fresher data) to avoid duplicate rows.
            if (
                self._last_known_extra is not None
                and (time.time() - self._last_known_extra_at)
                <= _KNOWN_WORKERS_CACHE_TTL_S
            ):
                return [
                    w for w in self._last_known_extra
                    if w.get("worker_id") not in seen
                ]
            return []
        try:
            extra = await asyncio.wait_for(
                self._fetch_known_workers(seen), timeout=1.5
            )
            # Only a NON-EMPTY aggregation is trusted as the GOOD fallback.
            # _fetch_known_workers swallows redis pipeline/zrange hiccups and
            # returns [] (NOT an exception), so an empty result here is
            # AMBIGUOUS: either the fleet genuinely has no peer workers, or
            # redis blipped this once. Overwriting the cache with [] made ONE
            # HUB AT A TIME collapse to local-only and flap the Workers count
            # (2 <-> 20) as nginx round-robined the poll. Treat empty as "no
            # fresh data": keep + reuse the last GOOD cache instead of poisoning
            # it. (A genuine empty fleet just shows the stale cache for <=TTL.)
            if extra:
                self._last_known_extra = extra
                self._last_known_extra_at = time.time()
            else:
                extra = _fresh_cache()
        except Exception:
            # Redis stalled / timed out -- reuse the most recent GOOD cache.
            extra = _fresh_cache()
        snap["workers"].extend(extra)
        snap["count"] = len(snap["workers"])
        return snap

    async def forget(self, worker_id: str) -> bool:
        """Delete a worker's Redis history (= row + index entry).
        Returns True iff anything was deleted. Refuses if the worker
        is still in ``self.connections`` (= alive) -- caller should
        unregister first via DELETE /workers/{id} which checks this.
        """
        ok = False
        if self._r is not None:
            try:
                removed = await self._r.zrem(_k_index(), worker_id)
                await self._r.delete(_k_worker(worker_id))
                await self._r.delete(_k_online(worker_id))
                await self._r.delete(_k_worker(worker_id) + ":counts")
                ok = bool(removed) or ok
            except Exception:
                pass
        # Drop the in-process log buffer too -- the operator asked to
        # delete this worker; the logs hanging around would just clutter
        # future "..." menus.
        if worker_id in self._worker_logs:
            self._worker_logs.pop(worker_id, None)
            ok = True
        return ok

    # ----- worker activity log buffer --------------------------------------

    def log_event(self, worker_id: str, line: str, *, kind: str = "info") -> None:
        """Append a one-line event to this worker's in-memory ring buffer.

        Safe to call from any hub-side handler (worker connect, heartbeat,
        job log forwarding, status toggle, errors). Lossy by design -- the
        oldest entry is dropped once the buffer hits ``_worker_log_cap``.
        ``kind`` is one of ``"info"`` / ``"job"`` / ``"warn"`` / ``"error"``
        / ``"lifecycle"``; the admin UI uses it to colour rows.
        """
        if not worker_id:
            return
        buf = self._worker_logs.get(worker_id)
        if buf is None:
            buf = deque(maxlen=self._worker_log_cap)
            self._worker_logs[worker_id] = buf
        buf.append({"ts": time.time(), "kind": kind, "line": line})

    def get_logs(self, worker_id: str, *, limit: int = 200) -> list[dict]:
        """Return up to ``limit`` most-recent log events for a worker.

        Empty list if we have no buffer for that worker (= it never
        connected to this hub process). Each entry is the shape produced
        by ``log_event``: ``{"ts": float, "kind": str, "line": str}``.
        """
        buf = self._worker_logs.get(worker_id)
        if buf is None:
            return []
        if limit <= 0 or limit >= len(buf):
            return list(buf)
        # Return tail of the deque.
        return list(buf)[-limit:]

    # ----- dispatch --------------------------------------------------------

    async def assign(self, worker: ConnectedWorker, msg: HubAssignJob) -> bool:
        """Reserve a lane on this worker AND push the ``HubAssignJob``. Returns
        True on success, False if the send raises (reservation is rolled back).

        Race-safety: the slot is reserved by **adding to ``pending_assigns``**
        the instant assign() begins -- BEFORE the awaited ``worker.send`` --
        so a concurrent ``pick_worker`` that runs during the send (or during
        a parent caller's ``await store.claim_queued_job``) sees the
        reservation and skips the worker. Bumping ``in_flight`` was NOT
        enough on its own (incident 2026-06-15 fix #1) because the next
        WorkerHeartbeat overwrites ``in_flight`` with the worker's own count
        -- so the increment survived only until the worker's next heartbeat,
        and the race fired again the moment heartbeat reset us. Concretely:
        redrive_A pick (in_flight=1) -> claim_queued_job await -> redrive_B
        pick (in_flight=1 *or* heartbeat reset to 1) -> both assign ->
        worker capacity 2, lane #1 already busy -> WorkerJobFailed
        "no free lane in pool". The ``pending_assigns`` set persists until
        WorkerJobAccepted / WorkerJobComplete / WorkerJobFailed lands,
        independent of heartbeat resets."""
        worker.pending_assigns.add(msg.job_id)
        worker.in_flight += 1
        self.assignments.setdefault(worker.worker_id, set()).add(msg.job_id)
        try:
            await worker.send(msg)
        except Exception:
            # Roll back the reservation -- the worker never got the assign.
            worker.pending_assigns.discard(msg.job_id)
            worker.in_flight = max(0, worker.in_flight - 1)
            self.assignments.get(worker.worker_id, set()).discard(msg.job_id)
            return False
        return True

    def mark_downloading(self, worker_id: str, job_id: str) -> None:
        """The job's Chrome lane has been released -- its video is now
        downloading OFF-LANE (yt-dlp in a detached task). Move it out of the
        Chrome-lane budget (``committed_jobs``) into ``downloading_jobs`` so
        pick_worker stops treating the worker as lane-full while the download
        runs, letting the freed lane take new fetches. Idempotent; keyed on the
        worker's WorkerJobProgress(phase="downloading") lane-release signal.
        release() clears both sets on complete/failed."""
        worker = self.connections.get(worker_id)
        if worker is None:
            return
        worker.committed_jobs.discard(job_id)
        worker.downloading_jobs.add(job_id)

    def release(self, worker_id: str, job_id: str) -> None:
        """A job is no longer running on the worker (WorkerJobComplete /
        WorkerJobFailed / post-assign guard reclaiming a stranded assign).
        Discards from ``pending_assigns`` (the never-acked case),
        ``committed_jobs`` (the normal acked-and-ran case) AND
        ``downloading_jobs`` (the off-lane video-download case) so the
        scheduler sees the slot free. Also legacy-decrements the heartbeat-
        driven ``in_flight`` for display continuity; the next heartbeat
        reconciles."""
        worker = self.connections.get(worker_id)
        if worker:
            if worker.in_flight > 0:
                worker.in_flight -= 1
            worker.pending_assigns.discard(job_id)
            worker.committed_jobs.discard(job_id)
            worker.downloading_jobs.discard(job_id)
        self.assignments.get(worker_id, set()).discard(job_id)
