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


class SessionRegistry:
    """In-process map of active Sessions on this hub.

    All operations are O(1) dict lookups; the hub holds at most a few
    hundred sessions at once even in the worst case (limited by the
    sum of lane capacities across connected workers).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._lock = asyncio.Lock()

    def add(self, info: SessionInfo) -> None:
        self._sessions[info.session_id] = info

    def get(self, session_id: str) -> SessionInfo | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> SessionInfo | None:
        return self._sessions.pop(session_id, None)

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
                dropped.append(sid)
        return dropped
