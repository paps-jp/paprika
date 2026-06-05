"""Hub-side session registry (RFC-001 §5.2).

A Session is a reservation of a Lane on some worker. The hub maps
``session_id -> (worker_id, lane_idx, novnc_url, created_at,
last_active_at)`` so HTTP requests like ``POST /sessions/{id}/click``
know which worker to forward the action to.

V1 is in-memory only -- sessions die with the hub process. Persistence
+ recovery is RFC-002.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime

SessionState = str  # Literal["idle", "running", "closing"]


@dataclass
class SessionInfo:
    """One Session as the hub sees it."""

    session_id: str
    worker_id: str
    lane_idx: int | None = None
    novnc_url: str | None = None
    initial_url: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_active_at: datetime = field(default_factory=datetime.utcnow)
    # Idle and absolute TTLs (seconds). Enforced by the reaper task.
    idle_ttl_s: int = 300
    absolute_ttl_s: int = 3600
    # ``auto = True`` for sessions created by ``POST /jobs`` (RFC-001 §5.2).
    # The /jobs path keeps its existing implementation in V1; this flag
    # is reserved for the future cross-over.
    auto: bool = False
    # The /jobs id that owns this session, when applicable.
    # Sessions opened by paprika-runner under a codegen-loop job tag
    # themselves with PAPRIKA_JOB_ID so the admin UI can group them
    # under the right Submit panel for live noVNC display.
    job_id: str | None = None
    # Set of canonicalised URLs the agent / API client has been on
    # during this session. Mirrors AgentResult.visited_urls and powers
    # the visited=true marker in the outline.
    visited_urls: list[str] = field(default_factory=list)
    # ``idle``  -- session is open, no action currently in flight
    # ``running`` -- a session_action is being processed (lock held)
    # ``closing`` -- DELETE has been issued, awaiting worker ack
    state: SessionState = "idle"
    # When ``state == "running"``, the kind of the in-flight action
    # (``click``, ``navigate``, ``outline``, ...). ``None`` otherwise.
    current_action: str | None = None
    # Set by ``POST /sessions/{sid}/keepalive`` (= the SDK's
    # ``Page.detach()`` / ``Session.detach()``). Signals "this
    # session is intentional and managed by the operator from here
    # on -- do NOT reap it as an orphan when the parent script
    # exits". The TTL reaper still applies (idle / absolute), so a
    # forgotten detach can't pin a lane forever.
    detached: bool = False
    # Per-session lock so actions on the same session serialise even
    # when multiple HTTP requests race. Different sessions are
    # independent and run in parallel.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def to_json(self) -> dict:
        return {
            "session_id": self.session_id,
            "worker_id": self.worker_id,
            "lane_idx": self.lane_idx,
            "novnc_url": self.novnc_url,
            "initial_url": self.initial_url,
            "created_at": self.created_at.isoformat() + "Z",
            "last_active_at": self.last_active_at.isoformat() + "Z",
            "idle_ttl_s": self.idle_ttl_s,
            "absolute_ttl_s": self.absolute_ttl_s,
            "auto": self.auto,
            "visited_count": len(self.visited_urls),
            "state": self.state,
            "current_action": self.current_action,
            "job_id": self.job_id,
            "detached": self.detached,
        }


def new_session_id() -> str:
    """ses_<22-char-url-safe-base64> -- ~128 bits entropy."""
    return "ses_" + secrets.token_urlsafe(16)


def session_from_json(d: dict) -> SessionInfo:
    """Reconstruct a SessionInfo from its Redis-persisted to_json()+hub dict
    (P2): lets a restarted / peer hub rebuild a live session created on another
    hub process so sessions survive a hub restart. The asyncio Lock comes back
    fresh; visited_urls is not persisted (only the count) so it returns empty
    -- acceptable for survival (visited markers rebuild as the session is used
    again)."""
    def _dt(s) -> datetime:
        try:
            return datetime.fromisoformat(str(s or "").rstrip("Z"))
        except Exception:
            return datetime.utcnow()
    return SessionInfo(
        session_id=str(d.get("session_id") or ""),
        worker_id=str(d.get("worker_id") or ""),
        lane_idx=d.get("lane_idx"),
        novnc_url=d.get("novnc_url"),
        initial_url=d.get("initial_url"),
        created_at=_dt(d.get("created_at")),
        last_active_at=_dt(d.get("last_active_at")),
        idle_ttl_s=int(d.get("idle_ttl_s") or 300),
        absolute_ttl_s=int(d.get("absolute_ttl_s") or 3600),
        auto=bool(d.get("auto")),
        job_id=d.get("job_id"),
        state=str(d.get("state") or "idle"),
        current_action=d.get("current_action"),
        detached=bool(d.get("detached")),
    )


class SessionRegistry:
    """In-process map of active Sessions on this hub.

    All operations are O(1) dict lookups; the hub holds at most a few
    hundred sessions at once even in the worst case (limited by the
    sum of lane capacities across connected workers).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._lock = asyncio.Lock()
        # Multi-hub foundation: optional Redis-backed Session Map
        # (sid -> {worker_id, hub}). Mirrors the in-memory map so a
        # future Hub→Hub forwarding layer running behind nginx can,
        # on a hub that does NOT hold the session, look up which hub
        # owns the worker and forward the action there. Bound at
        # lifespan via bind_redis(); None (and fully dormant) for
        # single-hub deployments and tests.
        self._r = None  # redis.asyncio.Redis | None
        self._hub_id = ""

    def bind_redis(self, redis_client, hub_id: str) -> None:
        """Attach a redis client + this hub's id so add/remove mirror the
        Session Map to Redis. Safe no-op shape when ``redis_client`` is
        None. Writes only; nothing reads the map back until a Hub→Hub
        forwarding layer is built, so this never changes single-hub
        behaviour."""
        self._r = redis_client
        self._hub_id = hub_id or ""

    @staticmethod
    def _k_session(session_id: str) -> str:
        return f"paprika:session:{session_id}"

    def _schedule(self, coro) -> None:
        """Fire-and-forget a Redis mirror op on the running loop. If
        there's no loop (sync test context) close the coro cleanly so
        Python doesn't warn about a never-awaited coroutine."""
        if self._r is None:
            coro.close()
            return
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()

    async def _redis_put(self, info: SessionInfo, ttl: int) -> None:
        """Persist the FULL session state to Redis (P2): the session map now
        carries to_json() + hub, not just {worker_id, hub}, so a restarted or
        peer hub can RECONSTRUCT the live session (see session_from_json) and
        keep serving it -- sessions survive a hub restart. lookup_owner /
        count_shared still read worker_id + hub from this richer dict."""
        try:
            d = info.to_json()
            d["hub"] = self._hub_id
            await self._r.set(
                self._k_session(info.session_id),
                json.dumps(d),
                ex=max(60, ttl),
            )
        except Exception:
            pass

    async def _redis_del(self, session_id: str) -> None:
        try:
            await self._r.delete(self._k_session(session_id))
        except Exception:
            pass

    async def lookup_owner(self, session_id: str) -> tuple[str, str] | None:
        """Read the Redis Session Map for a session this hub does NOT hold
        locally: returns ``(worker_id, hub)`` or None.

        This is the *read* side of the map (add/remove are the writes).
        The Hub→Hub forwarding layer calls it when a ``/sessions/*``
        request lands on a hub that doesn't own the session, to discover
        which hub does. Fully dormant (returns None) when no redis client
        is bound -- i.e. single-hub deployments and tests -- so callers
        fall through to their existing 404 path unchanged."""
        if self._r is None:
            return None
        try:
            raw = await self._r.get(self._k_session(session_id))
            if not raw:
                return None
            d = json.loads(raw)
            return (d.get("worker_id") or ""), (d.get("hub") or "")
        except Exception:
            return None

    async def count_shared(self) -> int | None:
        """Fleet-wide active-session count from the Redis Session Map.

        Each live session writes a ``paprika:session:{sid}`` key with a TTL
        (see add() / _redis_put), so SCANning them counts sessions across
        ALL hubs -- and a missed remove() self-heals when the key TTLs out.
        The admin header polls this so its ``sessions=N`` badge stops
        flickering between hubs under the nginx round-robin (each hub's
        local ``len(self._sessions)`` only sees its own). Cached ~2s so the
        2s admin poll doesn't SCAN every tick. Returns None when redis-less
        -> the caller falls back to the local count."""
        if self._r is None:
            return None
        import time as _time

        now = _time.time()
        cached = getattr(self, "_count_cache", None)
        if cached is not None and (now - cached[0]) < 2.0:
            return cached[1]
        n = 0
        try:
            cur = 0
            while True:
                cur, keys = await self._r.scan(
                    cur, match="paprika:session:*", count=500,
                )
                n += len(keys)
                if cur == 0:
                    break
        except Exception:
            return None
        self._count_cache = (now, n)
        return n

    async def touch_redis_map(self, ttl: int = 120) -> None:
        """Re-put every live session's owner-map entry so it never expires
        while the session is alive. ``add()`` writes the entry once with a TTL
        tied to the fetch's ``absolute_ttl_s``; on a long-lived / keepalive
        session that TTL lapses while the session is still up, leaving
        cross-hub session-action forwarding unable to resolve the owner hub
        (-> a non-owner hub returns 404 'session not found'). The session
        reaper calls this every few seconds to keep the map fresh -- mirrors
        how the worker-owner lease is refreshed on heartbeat. No-op w/o redis."""
        if self._r is None:
            return
        for info in list(self._sessions.values()):
            await self._redis_put(info, ttl)

    async def reconstruct_owned_sessions(self) -> int:
        """On hub startup, rebuild this hub's OWN live sessions from Redis (P2).

        The in-memory registry is empty after a restart, but the full
        SessionInfo survives in the Redis session map (see _redis_put), so we
        re-hydrate every entry whose ``hub`` == us. A session created before
        the restart then keeps working once its worker reconnects -- the worker
        holds its Chrome tab across the WS drop -- and cross-hub forwarding can
        still resolve us as the owner. Sessions whose worker never returns
        become zombies the reaper clears on idle/absolute TTL. We insert
        straight into ``_sessions`` (NOT add(), which would needlessly re-write
        the entry we just read). No-op without redis (single-hub / tests)."""
        if self._r is None:
            return 0
        n = 0
        try:
            cur = 0
            while True:
                cur, keys = await self._r.scan(
                    cur, match="paprika:session:*", count=500,
                )
                for k in keys:
                    try:
                        raw = await self._r.get(k)
                        if not raw:
                            continue
                        d = json.loads(raw)
                        if not isinstance(d, dict):
                            continue
                        if (d.get("hub") or "") != self._hub_id:
                            continue
                        sid = d.get("session_id") or ""
                        if not sid or sid in self._sessions:
                            continue
                        info = session_from_json(d)
                        # Fresh idle window so the reaper doesn't immediately
                        # reap a session that just survived the restart; the
                        # absolute TTL (created_at-based) still caps lifetime.
                        info.last_active_at = datetime.utcnow()
                        self._sessions[sid] = info
                        n += 1
                    except Exception:
                        continue
                if cur == 0:
                    break
        except Exception:
            return n
        return n

    def add(self, info: SessionInfo) -> None:
        self._sessions[info.session_id] = info
        # Mirror the FULL session to Redis with a TTL a bit beyond the
        # session's absolute TTL so a missed remove() self-heals instead of
        # leaking a key -- and so a restarted hub can reconstruct it (P2).
        self._schedule(
            self._redis_put(info, int(info.absolute_ttl_s) + 60)
        )

    def get(self, session_id: str) -> SessionInfo | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> SessionInfo | None:
        info = self._sessions.pop(session_id, None)
        if info is not None:
            self._schedule(self._redis_del(session_id))
        return info

    def all(self) -> list[SessionInfo]:
        return list(self._sessions.values())

    def touch(self, session_id: str) -> None:
        info = self._sessions.get(session_id)
        if info is not None:
            info.last_active_at = datetime.utcnow()

    def by_worker(self, worker_id: str) -> list[SessionInfo]:
        return [s for s in self._sessions.values() if s.worker_id == worker_id]

    def drop_by_worker(self, worker_id: str) -> list[str]:
        """Remove every session bound to a worker (e.g. on disconnect).
        Returns the list of session_ids that were dropped."""
        dropped: list[str] = []
        for sid, info in list(self._sessions.items()):
            if info.worker_id == worker_id:
                self._sessions.pop(sid, None)
                self._schedule(self._redis_del(sid))
                dropped.append(sid)
        return dropped
