"""Hub presence registry — Redis-backed self-heartbeat so each hub in
a multi-hub deployment can be enumerated by the admin UI.

Each hub on startup writes a row at ``paprika:hubs:{hub_id}`` and
refreshes it on a 30 s interval; the key has a 90 s TTL so a
dead hub falls off the list within ~1 minute. An auxiliary ZSET
``paprika:hubs:index`` keeps the union (hub_ids ever seen) so the
listing also surfaces recently-offline hubs (= row TTL-expired but
still in the index), mirroring how the worker registry handles it.

Single-hub deploys still get one entry (the running hub). When
the operator starts ``hub-b`` / ``hub-c`` on a different host
pointing at the same Redis, it shows up automatically -- no manual
registration step. This is the "do hubs auto-connect like workers
do?" answer the operator asked for: yes, via shared Redis, no
per-hub config needed beyond ``REDIS_URL``.

When Redis is unavailable (single-host in-memory deploys), this
registry degrades to a one-element static list: the current hub
only. No background task runs in that mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


_HUB_KEY_PREFIX = "paprika:hubs:"
_HUB_INDEX_KEY = "paprika:hubs:index"
_HUB_TTL_SECONDS = 90      # row TTL; entry "offline" if missed >TTL
_HUB_HEARTBEAT_INTERVAL = 30   # write cadence (1/3 of TTL)


def _k_hub(hub_id: str) -> str:
    return f"{_HUB_KEY_PREFIX}{hub_id}"


class HubRegistry:
    """Per-process hub-presence helper.

    ``self.payload`` is the dict written to Redis on each heartbeat;
    callers (admin route, lifespan) may mutate it before the next tick
    to reflect changing state (active job count, etc.). The required
    fields ``hub_id`` and ``ts`` are written by ``_heartbeat_once``
    so the caller doesn't need to remember them.
    """

    def __init__(
        self,
        redis_client: Any,
        hub_id: str,
        *,
        public_base: str = "",
        version: str = "",
    ) -> None:
        self._r = redis_client
        self.hub_id = hub_id or "hub"
        self.payload: dict[str, Any] = {
            "hub_id": self.hub_id,
            "public_base": public_base,
            "version": version,
        }
        self._task: asyncio.Task | None = None
        # Consecutive heartbeat-write failures. A sustained streak means this
        # hub is silently de-registering (its TTL row expires -> nginx drops it
        # from the upstream). Escalated to WARNING in _heartbeat_once so it can't
        # hide the way it did on 2026-07-09 (a stale Redis client swallowed every
        # SET for 12h behind a log.debug).
        self._hb_fail_streak: int = 0

    # ----- runtime ---------------------------------------------------------

    def update(self, **fields: Any) -> None:
        """Patch the in-memory heartbeat payload. Next ``_heartbeat_once``
        picks it up. Safe to call from any async context (no lock needed
        since dict ops are atomic in CPython)."""
        self.payload.update(fields)

    async def _heartbeat_once(self) -> None:
        if self._r is None:
            return
        row = {
            **self.payload,
            "hub_id": self.hub_id,
            "ts": time.time(),
        }
        try:
            await self._r.set(
                _k_hub(self.hub_id),
                json.dumps(row),
                ex=_HUB_TTL_SECONDS,
            )
            await self._r.zadd(_HUB_INDEX_KEY, {self.hub_id: time.time()})
            if self._hb_fail_streak:
                # Recovered after a run of failures -- surface the recovery too
                # so the operator sees the de-registration window close.
                log.warning(
                    "hub heartbeat RECOVERED for %s after %d consecutive "
                    "misses -- registration restored",
                    self.hub_id, self._hb_fail_streak,
                )
                self._hb_fail_streak = 0
        except Exception as e:
            self._hb_fail_streak += 1
            streak = self._hb_fail_streak
            # A single blip is noise (debug). A SUSTAINED failure means this hub
            # is de-registering: its ~90s TTL row expires and nginx drops it from
            # the upstream (the 2026-07-09 incident -- .35-.39 fell out while a
            # stale Redis client swallowed every SET, hidden 12h at debug level).
            # Escalate to WARNING once the misses exceed the TTL window, then
            # periodically, so it can't hide again.
            if streak * _HUB_HEARTBEAT_INTERVAL >= _HUB_TTL_SECONDS and (
                streak == 3 or streak % 10 == 0
            ):
                log.warning(
                    "hub heartbeat FAILING for %s: %d consecutive misses (~%ds) "
                    "-- this hub is de-registering from the fleet/nginx; Redis "
                    "client likely stale (a hub restart re-registers it). "
                    "last err: %s",
                    self.hub_id, streak, streak * _HUB_HEARTBEAT_INTERVAL, e,
                )
            else:
                log.debug(
                    "hub heartbeat write failed (streak=%d): %s", streak, e)

    async def _loop(self) -> None:
        # Run forever; cancelled on lifespan shutdown.
        while True:
            await self._heartbeat_once()
            try:
                await asyncio.sleep(_HUB_HEARTBEAT_INTERVAL)
            except asyncio.CancelledError:
                break

    def start(self) -> None:
        """Spawn the background heartbeat task. No-op when Redis isn't
        wired (= single-host in-memory deploy)."""
        if self._r is None or self._task is not None:
            return
        self._task = asyncio.create_task(
            self._loop(), name="hub-heartbeat",
        )

    async def stop(self) -> None:
        """Cancel the heartbeat task. Best-effort drop of the registry
        row so the operator doesn't see a "just died" hub for ~90s
        after a clean shutdown."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        if self._r is not None:
            try:
                await self._r.delete(_k_hub(self.hub_id))
                # Keep the index entry so the admin UI can still show
                # "this hub was here recently" with alive=False.
            except Exception:
                pass

    # ----- query -----------------------------------------------------------

    async def list_all(self) -> list[dict]:
        """Return every hub ever seen, alive-first then offline. Each
        dict carries the heartbeat payload (alive=True) or a minimal
        ``{hub_id, alive=False, last_seen}`` for offline rows.

        Falls back to a one-element list of the current hub when
        Redis isn't wired."""
        if self._r is None:
            # Single-host deploy: synthesise the local view.
            return [{
                **self.payload,
                "hub_id": self.hub_id,
                "alive": True,
                "ts": time.time(),
                "local": True,
            }]
        try:
            ids = await self._r.zrange(_HUB_INDEX_KEY, 0, -1)
        except Exception:
            return []
        out: list[dict] = []
        for raw_id in ids:
            hid = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            row = None
            try:
                raw = await self._r.get(_k_hub(hid))
                if raw:
                    row = json.loads(
                        raw.decode() if isinstance(raw, bytes) else raw
                    )
            except Exception:
                row = None
            if row is not None:
                row.setdefault("hub_id", hid)
                row["alive"] = True
                row["local"] = (hid == self.hub_id)
                out.append(row)
            else:
                # In index but no live row -> TTL-expired (offline).
                try:
                    last_ts = await self._r.zscore(_HUB_INDEX_KEY, hid)
                except Exception:
                    last_ts = None
                out.append({
                    "hub_id": hid,
                    "alive": False,
                    "last_seen": float(last_ts) if last_ts else None,
                    "local": (hid == self.hub_id),
                })
        # Stable order: alive first, then by hub_id.
        out.sort(key=lambda d: (not d.get("alive"), d.get("hub_id", "")))
        return out

    async def forget(self, hub_id: str) -> bool:
        """Remove a hub from the registry (both row + index). Refuses
        to forget the running hub since the next heartbeat would just
        re-add it -- operator should stop the hub container first."""
        if self._r is None:
            return False
        if hub_id == self.hub_id:
            return False
        try:
            await self._r.delete(_k_hub(hub_id))
            await self._r.zrem(_HUB_INDEX_KEY, hub_id)
            return True
        except Exception:
            return False
