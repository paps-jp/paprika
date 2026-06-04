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

# Heartbeat-related constants (seconds)
WORKER_TTL = 120  # if no heartbeat for this long, worker is considered dead
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
                    }
                ),
            )
            await self._r.set(_k_online(worker_id), "1", ex=WORKER_TTL)
            # Claim WS ownership for this hub (multi-hub foundation).
            await self._r.set(_k_owner(worker_id), self._hub_id, ex=WORKER_TTL)
        return worker

    async def unregister(self, worker_id: str) -> None:
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

    async def heartbeat(self, worker_id: str, in_flight: int) -> None:
        worker = self.connections.get(worker_id)
        if worker is None:
            return
        worker.in_flight = in_flight
        worker.last_heartbeat = time.time()
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
                if addr:
                    raw = await self._r.get(_k_worker(worker_id))
                    if raw:
                        try:
                            d = json.loads(
                                raw.decode() if isinstance(raw, bytes) else raw
                            )
                        except Exception:
                            d = None
                        if isinstance(d, dict) and d.get("address") != addr:
                            d["address"] = addr
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

    def pick_worker(self) -> ConnectedWorker | None:
        """Pick an alive worker with spare capacity, load-balanced.

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
        candidates = [
            w
            for w in self.alive_workers()
            if w.status == "active"
            and w.in_flight < w.capabilities.max_concurrent
            and (
                len(w.capabilities.lane_novnc_urls or []) > 0
                or bool(w.capabilities.novnc_url)
            )
        ]
        if not candidates:
            return None

        # Rank key: smaller in_flight first, then larger capacity.
        def _key(w: ConnectedWorker) -> tuple:
            return (w.in_flight, -w.capabilities.max_concurrent)

        best = min(_key(w) for w in candidates)
        tied = [w for w in candidates if _key(w) == best]
        # Random pick among equally-good workers -> even spread.
        return random.choice(tied)

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
        out: list[dict] = []
        for raw_id in ids:
            wid = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            if wid in exclude_ids:
                continue
            try:
                row = await self._r.get(_k_worker(wid))
                last_ts = await self._r.zscore(_k_index(), wid)
                owner = await self._r.get(_k_owner(wid))
            except Exception:
                continue
            if not row:
                continue
            try:
                data = json.loads(
                    row.decode() if isinstance(row, bytes) else row
                )
            except Exception:
                continue
            caps = data.get("capabilities") or {}
            # Cross-hub presence: a worker NOT connected to THIS process may
            # still be live on a peer hub. Judge "alive" by redis heartbeat
            # freshness (same WORKER_TTL window stats() uses for local
            # connections) instead of hardcoding offline -- else a read-only
            # admin / a peer hub shows the whole fleet as offline. Dispatch
            # (pick_worker -> alive_workers -> self.connections) is unaffected;
            # this only changes the DISPLAY of redis-known rows.
            _alive = bool(last_ts) and (time.time() - float(last_ts)) < WORKER_TTL
            out.append({
                "worker_id": wid,
                "in_flight": 0,
                "capacity": int(caps.get("max_concurrent") or 1),
                "labels": dict(caps.get("labels") or {}),
                "alive": _alive,
                "age_seconds": (
                    int(time.time() - float(last_ts)) if last_ts else None
                ),
                "last_heartbeat": float(last_ts) if last_ts else None,
                "lane_novnc_urls": [],
                "slot_novnc_urls": [],
                "profiles_cached": [],
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
                # worker is connected to in a multi-hub deploy.
                "hub_id": (
                    owner.decode() if isinstance(owner, bytes) else str(owner)
                ) if owner else "",
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
        snap["workers"].extend(await self._fetch_known_workers(seen))
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
        """Push an `assign_job` message and bump in_flight. Returns True
        on success, False if the send raises."""
        try:
            await worker.send(msg)
        except Exception:
            return False
        worker.in_flight += 1
        self.assignments.setdefault(worker.worker_id, set()).add(msg.job_id)
        return True

    def release(self, worker_id: str, job_id: str) -> None:
        """Decrement in_flight when a job finishes."""
        worker = self.connections.get(worker_id)
        if worker and worker.in_flight > 0:
            worker.in_flight -= 1
        self.assignments.get(worker_id, set()).discard(job_id)
