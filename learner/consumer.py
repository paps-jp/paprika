"""Stream consumer: outcome events -> verify -> ledger.

Reads the hub's Redis Stream through a consumer group, so the learner can
restart (or run a second replica) without losing its place, and a crashed
replica's unacked entries are picked up again.

Delivery is at-least-once into an ADDITIVE daily counter, so the one thing
this loop must not do is ack before the counts are durable: entries are
acked only after the flush that contains them has committed. A crash
between commit and ack re-counts at most one flush interval of jobs --
bounded and rare, and preferable to silently losing rows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from learner.ledger import DailyAggregator
from learner.verify import VerifyConfig, verify

log = logging.getLogger("learner.consumer")

STREAM_KEY = os.environ.get("PAPRIKA_OUTCOME_STREAM_KEY") or "paprika:learn:outcomes"
GROUP = os.environ.get("LEARNER_GROUP") or "learner"
BATCH = int(os.environ.get("LEARNER_BATCH") or 500)
BLOCK_MS = int(os.environ.get("LEARNER_BLOCK_MS") or 2000)
FLUSH_INTERVAL_S = float(os.environ.get("LEARNER_FLUSH_INTERVAL_S") or 15)
#: Delete entries from the stream once they are durable in the ledger. Turn
#: off (LEARNER_DELETE_AFTER_ACK=0) only if a second consumer GROUP is added.
DELETE_AFTER_ACK = (os.environ.get("LEARNER_DELETE_AFTER_ACK") or "1").strip().lower() not in (
    "0", "false", "no", "off",
)
#: Entries another (dead) consumer read but never acked are reclaimed after this.
CLAIM_IDLE_MS = int(os.environ.get("LEARNER_CLAIM_IDLE_MS") or 120_000)


def decode(fields: dict) -> dict | None:
    """One stream entry -> event dict, or None if it is not ours."""
    raw = fields.get("d") if "d" in fields else fields.get(b"d")
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        ev = json.loads(raw)
    except Exception:
        return None
    return ev if isinstance(ev, dict) else None


class Consumer:
    def __init__(self, redis, ledger, *, name: str, cfg: VerifyConfig | None = None) -> None:
        self._r = redis
        self._ledger = ledger
        self._name = name
        self._cfg = cfg or VerifyConfig()
        self._agg = DailyAggregator()
        self._unacked: list = []
        self._last_flush = time.monotonic()
        self.stats = {"read": 0, "bad": 0, "flushed": 0, "flush_errors": 0}

    async def ensure_group(self) -> None:
        try:
            # "$": a brand-new group starts at now; history before the
            # learner existed is not replayed.
            await self._r.xgroup_create(STREAM_KEY, GROUP, id="$", mkstream=True)
            log.info("created consumer group %s on %s", GROUP, STREAM_KEY)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    def handle(self, entry_id, fields: dict) -> None:
        ev = decode(fields)
        self._unacked.append(entry_id)
        if ev is None:
            self.stats["bad"] += 1
            return
        self.stats["read"] += 1
        self._agg.add(ev, verify(ev, self._cfg))

    async def flush(self) -> None:
        self._last_flush = time.monotonic()
        if not self._unacked:
            return
        daily, detail = self._agg.drain()
        ids = self._unacked
        self._unacked = []
        try:
            await self._ledger.write(daily, detail)
        except Exception:
            # Not durable -> do NOT ack. The entries stay pending in the
            # group and are re-read via the claim path; the in-memory deltas
            # for them are dropped so they are not double counted then.
            self.stats["flush_errors"] += 1
            log.warning("ledger flush failed; %d entries left pending", len(ids), exc_info=True)
            return
        self.stats["flushed"] += len(ids)
        try:
            for i in range(0, len(ids), 1000):
                chunk = ids[i:i + 1000]
                await self._r.xack(STREAM_KEY, GROUP, *chunk)
                if DELETE_AFTER_ACK:
                    # The ledger now holds these; the stream is a buffer for
                    # learner downtime, not storage. Measured live an entry
                    # costs ~1.9 KB of Redis, so leaving them until MAXLEN
                    # trims would park ~95 MB there for nothing.
                    await self._r.xdel(STREAM_KEY, *chunk)
        except Exception:
            log.warning("xack/xdel failed for %d entries", len(ids), exc_info=True)

    async def _read(self, start_id: str) -> list:
        resp = await self._r.xreadgroup(
            GROUP, self._name, {STREAM_KEY: start_id}, count=BATCH,
            block=BLOCK_MS if start_id == ">" else None,
        )
        out = []
        for _stream, entries in resp or []:
            out.extend(entries)
        return out

    async def _reclaim(self) -> int:
        """Take over entries a dead consumer left pending."""
        try:
            resp = await self._r.xautoclaim(
                STREAM_KEY, GROUP, self._name, min_idle_time=CLAIM_IDLE_MS,
                start_id="0-0", count=BATCH,
            )
        except Exception:
            return 0
        entries = resp[1] if isinstance(resp, (list, tuple)) and len(resp) > 1 else []
        for entry_id, fields in entries or []:
            if fields:
                self.handle(entry_id, fields)
        return len(entries or [])

    async def run(self, stop: asyncio.Event) -> None:
        await self.ensure_group()
        # First drain our own pending entries (read but not acked before a
        # restart), then switch to new ones.
        for entry_id, fields in await self._read("0"):
            self.handle(entry_id, fields)
        await self.flush()
        last_reclaim = time.monotonic()
        while not stop.is_set():
            try:
                for entry_id, fields in await self._read(">"):
                    self.handle(entry_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("stream read failed; retrying", exc_info=True)
                await asyncio.sleep(2.0)
            now = time.monotonic()
            if now - last_reclaim > CLAIM_IDLE_MS / 1000.0:
                last_reclaim = now
                n = await self._reclaim()
                if n:
                    log.info("reclaimed %d pending entries", n)
            if now - self._last_flush >= FLUSH_INTERVAL_S or len(self._unacked) >= 5 * BATCH:
                await self.flush()
        await self.flush()
