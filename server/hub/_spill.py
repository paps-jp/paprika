"""Pre-emptive RAM-disk spill-over for asset writes.

Why this exists (2026-07-20 incident)
-------------------------------------
The image RAM-disk MinIO (a tmpfs-backed store) filled up and the whole
*host* userspace went unresponsive -- kernel alive, ping 0% loss, TCP
connect to :9000 instant, but ``/minio/health/live`` took 8-15s and even
sshd stopped completing its banner exchange. The consuming image pipeline
stalled for ~40 minutes.

The critical lesson: **writes did not fail cleanly.** There was no ENOSPC to
catch -- the host stopped answering *before* returning any error, so a write
blocked indefinitely. An error-driven "on failure, try the other store"
fallback never fires, because the failure never arrives. Hence this module
switches on a **free-space threshold, pre-emptively**, well before the store
is full.

How it works
------------
* A background loop samples each RAM tier's capacity via MinIO's Prometheus
  ``/minio/v2/metrics/cluster`` endpoint (``_storage_metrics``, which mints
  its own HS512 bearer token -- no external Prometheus, no admin-API signing,
  no SSH; only credentials the hub already holds).
* Hysteresis: switch to the spill store at ``asset_spill_high_pct`` (default
  80%), and only come back once usage drops below ``asset_spill_low_pct``
  (default 60%). Flapping at a single threshold would scatter one job's
  assets over two stores and cost the consumer a lookup miss per asset.
* The resulting decision is published to Redis so all hubs agree, and each
  hub mirrors it into a process-local snapshot. The write path
  (``objstore._write_cb``) runs inside a thread-pool executor and therefore
  **cannot await Redis** -- it only ever reads that plain-dict snapshot.
* **Fail-safe:** if a tier cannot be sampled (timeout / error / stale shared
  state), it is treated as OVER threshold and writes spill. This is the case
  that actually matters -- during the incident the health endpoint hung, so
  "unmeasurable" is precisely the signature of the failure we are avoiding.
  Note this is not error-*driven* fallback: it keys off the *sampler* timing
  out, never off a blocked asset write.
* **Per-job pinning:** the decision is fixed per (job, tier) on first touch
  via Redis SETNX, so every asset of one job lands in the same store even if
  the threshold is crossed mid-job or the uploads hit different hubs.

Disabled by default (``asset_spill_enabled`` = False): with the flag off
every code path here is inert and routing is byte-for-byte what it was.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

log = logging.getLogger("paprika.spill")

# Tier identifiers. "nonvideo" = the images/HTML/json hot tier
# (s3_nonvideo_endpoint); "primary" = the s3_endpoint tier (video).
TIER_NONVIDEO = "nonvideo"
TIER_PRIMARY = "primary"
TIERS = (TIER_NONVIDEO, TIER_PRIMARY)

_STATE_KEY = "paprika:spill:state"
_SAMPLER_LOCK_KEY = "paprika:spill:sample_lock"
_PIN_KEY_FMT = "paprika:spill:job:%s"

# Pins outlive any realistic job; cheap to keep (one small string per job).
_PIN_TTL_S = 7 * 24 * 3600

# ---------------------------------------------------------------------------
# Process-local snapshot of the shared decision.
#
# Shape: {tier: {"spill": bool, "pct": float, "ts": float, "note": str}}
# Read from the executor thread by _is_spilling_now() -- plain dict access,
# never any I/O, so the write path can't block on Redis.
_LOCAL_STATE: dict[str, dict[str, Any]] = {}

# job_id -> ({tier: bool}, expires_at)
_PIN_CACHE: dict[str, tuple[dict[str, bool], float]] = {}
_PIN_CACHE_MAX = 20000
_PIN_CACHE_TTL_S = 900.0


# ---------------------------------------------------------------------------
# Settings


def _setting(key: str, default: Any) -> Any:
    try:
        from server.hub._state import state

        if state.settings is not None:
            v = state.settings.get(key, None)
            if v is not None and str(v).strip() != "":
                return v
    except Exception:
        pass
    return default


def spill_enabled() -> bool:
    """Master switch. Off (default) = this module is entirely inert."""
    try:
        return bool(_setting("asset_spill_enabled", False))
    except Exception:
        return False


def high_pct() -> float:
    try:
        return max(1.0, min(99.0, float(_setting("asset_spill_high_pct", 80.0))))
    except Exception:
        return 80.0


def low_pct() -> float:
    """Return-to-RAM threshold, forced strictly below the high mark so a
    mis-set pair can never collapse the hysteresis band into a flap."""
    try:
        v = float(_setting("asset_spill_low_pct", 60.0))
    except Exception:
        v = 60.0
    return max(0.0, min(high_pct() - 1.0, v))


def sample_interval_s() -> int:
    try:
        return max(5, min(600, int(_setting("asset_spill_sample_interval_s", 30))))
    except Exception:
        return 30


def probe_timeout_s() -> float:
    """Kept well under the 8-15s hang seen during the incident so a wedged
    host is classified as unmeasurable quickly rather than stalling the loop."""
    try:
        return max(1.0, min(30.0, float(_setting("asset_spill_probe_timeout_s", 5.0))))
    except Exception:
        return 5.0


def stale_after_s() -> int:
    """Shared state older than this counts as unmeasurable -> spill."""
    try:
        return max(30, min(3600, int(_setting("asset_spill_stale_after_s", 180))))
    except Exception:
        return 180


# ---------------------------------------------------------------------------
# Decision read path (SYNCHRONOUS -- safe to call from the executor thread)


def _is_spilling_now(tier: str) -> bool:
    """Current unpinned decision for ``tier`` from the local snapshot.

    Missing or stale snapshot -> True (spill). See the fail-safe note in the
    module docstring: unmeasurable is exactly what a wedged host looks like.
    """
    rec = _LOCAL_STATE.get(tier)
    if not rec:
        return True
    try:
        if (time.time() - float(rec.get("ts") or 0)) > stale_after_s():
            return True
        return bool(rec.get("spill"))
    except Exception:
        return True


def is_spilling(tier: str, job_id: str | None = None) -> bool:
    """Should ``tier`` write to the spill store right now?

    Honours the per-job pin when one has been resolved (see ``ensure_pin``),
    so a job that started on RAM keeps writing to RAM even after the
    threshold trips. Falls back to the live decision when unpinned.
    """
    if job_id:
        ent = _PIN_CACHE.get(job_id)
        if ent is not None:
            pin, exp = ent
            if exp > time.time() and tier in pin:
                return bool(pin[tier])
    return _is_spilling_now(tier)


def snapshot() -> dict[str, Any]:
    """Current decision + measurement, for /health and the admin UI."""
    on = spill_enabled()
    out: dict[str, Any] = {
        "enabled": on,
        "high_pct": high_pct(),
        "low_pct": low_pct(),
        "tiers": {},
    }
    now = time.time()
    for tier in TIERS:
        rec = dict(_LOCAL_STATE.get(tier) or {})
        age = now - float(rec.get("ts") or 0) if rec else None
        out["tiers"][tier] = {
            # Report the EFFECTIVE routing. With the feature off nothing ever
            # diverts, so reporting the raw fail-safe "spill" here would claim
            # a diversion that cannot happen.
            "spill": bool(on and is_spilling(tier)),
            "pct": rec.get("pct"),
            "note": rec.get("note", "no sample yet") if on else "disabled",
            "age_s": round(age, 1) if age is not None else None,
            "stale": (age is None or age > stale_after_s()),
        }
    return out


# ---------------------------------------------------------------------------
# Per-job pin


def _cache_pin(job_id: str, pin: dict[str, bool]) -> None:
    if len(_PIN_CACHE) >= _PIN_CACHE_MAX:
        # Cheap bounded eviction: drop the oldest ~10% by expiry.
        try:
            victims = sorted(_PIN_CACHE.items(), key=lambda kv: kv[1][1])
            for k, _ in victims[: max(1, _PIN_CACHE_MAX // 10)]:
                _PIN_CACHE.pop(k, None)
        except Exception:
            _PIN_CACHE.clear()
    _PIN_CACHE[job_id] = (dict(pin), time.time() + _PIN_CACHE_TTL_S)


def _encode_pin(pin: dict[str, bool]) -> str:
    return ",".join("%s=%d" % (t, 1 if pin.get(t) else 0) for t in TIERS)


def _decode_pin(raw: Any) -> dict[str, bool] | None:
    try:
        s = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception:
        return None
    pin: dict[str, bool] = {}
    for part in s.split(","):
        k, _, v = part.partition("=")
        k = k.strip()
        if k in TIERS:
            pin[k] = v.strip() == "1"
    return pin or None


async def ensure_pin(job_id: str | None) -> dict[str, bool] | None:
    """Resolve and locally cache the per-job store decision.

    Call this from the ASYNC side before dispatching a write into the
    executor -- it warms ``_PIN_CACHE`` so the synchronous ``_write_cb`` can
    read the pin without touching Redis.

    First writer for a job wins via SETNX, so all hubs agree on one store for
    that job's whole lifetime.
    """
    if not job_id or not spill_enabled():
        return None

    ent = _PIN_CACHE.get(job_id)
    if ent is not None and ent[1] > time.time():
        return ent[0]

    redis = _redis()
    if redis is None:
        # No shared store (single-hub dev): fall back to the live decision.
        pin = {t: _is_spilling_now(t) for t in TIERS}
        _cache_pin(job_id, pin)
        return pin

    key = _PIN_KEY_FMT % job_id
    try:
        raw = await redis.get(key)
        pin = _decode_pin(raw) if raw is not None else None
        if pin is None:
            want = {t: _is_spilling_now(t) for t in TIERS}
            # NX: whichever hub gets here first fixes the decision.
            await redis.set(key, _encode_pin(want), nx=True, ex=_PIN_TTL_S)
            raw = await redis.get(key)
            pin = _decode_pin(raw) or want
        _cache_pin(job_id, pin)
        return pin
    except Exception:
        log.debug("spill pin resolve failed for %s", job_id, exc_info=True)
        pin = {t: _is_spilling_now(t) for t in TIERS}
        _cache_pin(job_id, pin)
        return pin


# ---------------------------------------------------------------------------
# Sampler


def _redis():
    try:
        from server.hub._state import state

        return getattr(state.store, "_r", None)
    except Exception:
        return None


def _tier_probe_cfg(tier: str) -> tuple[str, str, str, str]:
    """(endpoint, access, secret, bucket) of the RAM store backing ``tier``.

    This deliberately measures the *RAM* endpoint, not the spill target --
    the spill target is the large durable store and never drives the switch.
    """
    from server.hub import objstore

    if tier == TIER_NONVIDEO:
        return (
            objstore._nonvideo_endpoint(),
            objstore._s3cfg("s3_nonvideo_access_key", "PAPRIKA_S3_NONVIDEO_ACCESS_KEY")
            or objstore._s3cfg("s3_access_key", "PAPRIKA_S3_ACCESS_KEY"),
            objstore._s3cfg("s3_nonvideo_secret_key", "PAPRIKA_S3_NONVIDEO_SECRET_KEY")
            or objstore._s3cfg("s3_secret_key", "PAPRIKA_S3_SECRET_KEY"),
            objstore._nonvideo_bucket(),
        )
    return (
        objstore._s3cfg("s3_endpoint", "PAPRIKA_S3_ENDPOINT"),
        objstore._s3cfg("s3_access_key", "PAPRIKA_S3_ACCESS_KEY"),
        objstore._s3cfg("s3_secret_key", "PAPRIKA_S3_SECRET_KEY"),
        objstore._bucket(),
    )


def _tier_has_spill_target(tier: str) -> bool:
    from server.hub import objstore

    return (
        objstore._nv_spill_enabled()
        if tier == TIER_NONVIDEO
        else objstore._spill_enabled()
    )


async def _sample_tier(tier: str, prev: dict[str, Any] | None) -> dict[str, Any]:
    """One capacity sample -> new decision record, applying hysteresis."""
    from server.hub._storage_metrics import fetch_minio_capacity

    was_spilling = bool(prev.get("spill")) if prev else False
    endpoint, access, secret, bucket = _tier_probe_cfg(tier)

    if not endpoint:
        # Tier not configured -> nothing to protect, never spill.
        return {"spill": False, "pct": None, "ts": time.time(), "note": "tier not configured"}

    # Persistence: the RAM store wipes its bucket on reboot. (Re)create it if
    # missing. If it's missing AND unrecoverable (endpoint wedged), the tier
    # is not writable -> spill to the durable store, same as 'unmeasurable'.
    try:
        from server.hub import objstore

        writable = await objstore.ensure_ram_bucket(tier)
    except Exception:
        writable = False
    if not writable:
        return {
            "spill": True,
            "pct": None,
            "ts": time.time(),
            "note": "RAM bucket missing/unwritable -> spill",
        }

    cap = await fetch_minio_capacity(
        endpoint, access, secret, bucket, timeout_s=probe_timeout_s()
    )

    if not cap.healthy or not cap.total_bytes:
        # THE important branch: a wedged host answers slowly or not at all.
        # Treat as over-threshold and get writes off it.
        return {
            "spill": True,
            "pct": None,
            "ts": time.time(),
            "note": "unmeasurable (%s) -> spill" % (cap.note or "no data"),
        }

    pct = 100.0 * float(cap.used_bytes) / float(cap.total_bytes)
    if was_spilling:
        spill = pct >= low_pct()          # stay spilled until we drop below low
    else:
        spill = pct >= high_pct()         # switch out at high
    return {"spill": spill, "pct": round(pct, 2), "ts": time.time(), "note": ""}


async def _publish(redis, state_map: dict[str, dict[str, Any]]) -> None:
    if redis is None:
        return
    try:
        await redis.set(
            _STATE_KEY, json.dumps(state_map), ex=max(120, stale_after_s() * 3)
        )
    except Exception:
        log.debug("spill state publish failed", exc_info=True)


async def _load_shared(redis) -> dict[str, dict[str, Any]] | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(_STATE_KEY)
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()
        got = json.loads(raw)
        return got if isinstance(got, dict) else None
    except Exception:
        return None


async def _spill_loop() -> None:
    """Per-hub loop. One hub samples per interval (Redis SET NX EX); every
    hub refreshes its local snapshot from the shared state each tick so the
    synchronous write path always has a recent decision to read."""
    from server.hub._state import state

    try:
        await asyncio.sleep(3 + (hash(str(getattr(state, "hub_id", ""))) & 0x07))
    except asyncio.CancelledError:
        return

    while True:
        # Sample FIRST, sleep after. Until a tier has a sample the fail-safe
        # reports "spill", so sleeping first would divert writes for a whole
        # interval after every hub restart for no reason.
        try:
            await _tick()
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug("spill tick failed", exc_info=True)
        try:
            await asyncio.sleep(sample_interval_s())
        except asyncio.CancelledError:
            return


async def _tick() -> None:
    """One sampler pass: claim the lock and measure, or mirror the shared state."""
    from server.hub._state import state

    if not spill_enabled():
        # Keep the snapshot clean so a re-enable starts from a fresh
        # sample rather than a stale (and therefore spill-forcing) one.
        _LOCAL_STATE.clear()
        return

    redis = _redis()
    hub_id = str(getattr(state, "hub_id", "") or "")

    claimed = True
    if redis is not None:
        try:
            claimed = bool(
                await redis.set(
                    _SAMPLER_LOCK_KEY,
                    hub_id,
                    nx=True,
                    ex=max(5, sample_interval_s() - 2),
                )
            )
        except Exception:
            claimed = True  # a Redis blip must not silence the sampler

    if claimed:
        shared = await _load_shared(redis) or {}
        fresh: dict[str, dict[str, Any]] = {}
        for tier in TIERS:
            if not _tier_has_spill_target(tier):
                # No spill store configured for this tier: never spill,
                # and don't waste a probe on it.
                fresh[tier] = {
                    "spill": False,
                    "pct": None,
                    "ts": time.time(),
                    "note": "no spill target configured",
                }
                continue
            try:
                fresh[tier] = await _sample_tier(tier, shared.get(tier))
            except Exception:
                log.debug("spill sample failed for %s", tier, exc_info=True)
                fresh[tier] = {
                    "spill": True,
                    "pct": None,
                    "ts": time.time(),
                    "note": "sampler error -> spill",
                }
            # Log every transition -- this is the line an operator greps for
            # when asking "when did assets start going to the spill store?".
            prev = (shared.get(tier) or {}).get("spill")
            now_s = fresh[tier]["spill"]
            if prev is not None and bool(prev) != bool(now_s):
                log.warning(
                    "asset spill %s: %s -> %s (pct=%s, %s)",
                    tier,
                    "SPILL" if prev else "ram",
                    "SPILL" if now_s else "ram",
                    fresh[tier].get("pct"),
                    fresh[tier].get("note") or "threshold",
                )
        # Defensive: keep the spill-target buckets alive too (durable stores,
        # but a missing bucket there would break the fallback).
        try:
            from server.hub import objstore

            await objstore.ensure_spill_buckets()
        except Exception:
            pass
        await _publish(redis, fresh)
        _LOCAL_STATE.clear()
        _LOCAL_STATE.update(fresh)
    else:
        got = await _load_shared(redis)
        if got:
            _LOCAL_STATE.clear()
            _LOCAL_STATE.update(got)
