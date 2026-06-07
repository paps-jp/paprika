"""Local job-store cache eviction for MinIO-as-source-of-truth mode.

In the MinIO-SoT deployment ``get_storage_dir()`` is a BOUNDED local disk (the
old multi-TB SMB/CIFS mount is gone). MinIO holds the durable copy; local disk
is a write-through cache. Artifacts are written locally + mirrored to MinIO,
and reads fall back to MinIO via :func:`server.hub.objstore.ensure_local`.

Nothing else bounds the local disk -- ``ensure_local`` only ever ADDS files and
the crawler writes continuously -- so without this loop the cache disk fills and
never drains. This loop keeps usage bounded by deleting the OLDEST job dirs, but
ONLY after confirming every file is already durable in MinIO. Durability is
established at asset-SAVE time -- the asset POST handlers ``mirror_file`` both the
asset AND its ``.meta`` sidecar as they're written -- so here we only verify
``prefix_exists`` BEFORE ``rmtree`` (one cheap list call, NOT a re-mirror of every
multi-GB file, so a pass stays fast enough to keep up). A dir that can't be
confirmed durable is KEPT for the host-cron second stage to reclaim later. So
eviction can never lose data; evicted artifacts are transparently re-fetched
from MinIO on the next read.

Gated behind ``PAPRIKA_CACHE_EVICT_ENABLED`` (default OFF) so it is completely
inert until the storage_dir->local cutover. Skipped in admin role (read-only).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time

from server.hub import objstore
from server.hub._state import get_storage_dir

log = logging.getLogger(__name__)


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# Evict when the storage_dir filesystem is >= HIGH% full; stop at <= LOW%.
_HIGH_PCT = _num("PAPRIKA_CACHE_EVICT_HIGH_PCT", 75.0)
_LOW_PCT = _num("PAPRIKA_CACHE_EVICT_LOW_PCT", 60.0)
_INTERVAL_S = _num("PAPRIKA_CACHE_EVICT_INTERVAL_S", 60.0)
# Auto-gate: only ever run on a BOUNDED cache disk, never on the old multi-TB
# NAS. While storage_dir is the 22TB SMB mount the loop is a no-op; the instant
# it's switched to the small local disk, eviction activates -- so the cutover is
# a pure settings change (no env/compose edit, no restart; get_storage_dir reads
# the setting live).
_MAX_DISK_GB = _num("PAPRIKA_CACHE_EVICT_MAX_DISK_GB", 1024.0)
# Steady-state "keep local clean": once a job dir is older than this AND durable
# in MinIO, evict it regardless of disk pressure -- so the local cache holds only
# the last few minutes of jobs (reads transparently re-fetch from MinIO via
# objstore.ensure_local). Mirroring uploads the .meta sidecars too, so metadata
# reaches MinIO before the local copy is dropped.
_MIN_AGE_S = _num("PAPRIKA_CACHE_EVICT_MIN_AGE_S", 180.0)
# Hard floor: never evict a dir younger than this even under disk pressure --
# protects an actively-writing in-flight job whose dir just crossed the LOW mark.
_HARD_MIN_AGE_S = _num("PAPRIKA_CACHE_EVICT_HARD_MIN_AGE_S", 60.0)
# Only ever delete dirs whose name looks like a job id (hex). Protects sibling
# trees under the storage root -- oprec/, and (if data_dir==storage_dir) the
# hub-internal hosts/skills/conventions/engines metadata -- from eviction.
_JOB_ID_RE = re.compile(r"^[0-9a-f]{8,}$")


def _disk_pct(path) -> tuple[float, int, int]:
    u = shutil.disk_usage(str(path))
    pct = (u.used / u.total * 100.0) if u.total else 0.0
    return pct, u.used, u.total


def _scan_jobs_by_mtime(root) -> list[tuple[float, str, str]]:
    """(mtime, name, path) for each job-id-shaped dir under root, oldest first."""
    out: list[tuple[float, str, str]] = []
    try:
        with os.scandir(str(root)) as it:
            for e in it:
                try:
                    if not e.is_dir() or not _JOB_ID_RE.match(e.name):
                        continue
                    out.append((e.stat().st_mtime, e.name, e.path))
                except OSError:
                    continue
    except OSError:
        return []
    out.sort(key=lambda t: t[0])
    return out


async def _evict_once() -> tuple[int, float]:
    """One eviction pass. Deletes each evictable job dir's local copy ONLY after
    confirming it is durable in MinIO. Assets + their ``.meta`` sidecars are
    mirrored at asset-save time, so this is a cheap ``prefix_exists`` check (not a
    re-mirror) -- the local cache stays clean without losing data. Returns
    (dirs_deleted, disk_pct_after).

    Two triggers, both verify-before-delete (never lose data):
      * AGE -- any dir older than ``_MIN_AGE_S`` is evicted regardless of disk
        usage. This is the steady-state "keep local clean" behaviour: local
        holds only the last few minutes of jobs; reads re-fetch from MinIO via
        :func:`objstore.ensure_local`.
      * PRESSURE -- while disk is above ``_LOW_PCT`` we also evict younger dirs
        (down to ``_HARD_MIN_AGE_S``) to relieve a fill faster.

    A dir that can't be confirmed durable (MinIO down / upload failed) is KEPT;
    the periodic host cron is the second-stage fallback that reclaims it later.
    """
    root = get_storage_dir()
    try:
        if not root.is_dir():
            return 0, 0.0
    except OSError:
        return 0, 0.0
    pct, _used, total = await asyncio.to_thread(_disk_pct, root)
    if total >= _MAX_DISK_GB * 1e9:
        # storage_dir is a big NAS/disk (the pre-cutover SMB mount), not a
        # bounded local cache -> nothing to evict here. Auto-inert until cutover.
        return 0, pct
    if not objstore.enabled():
        # No durable copy to fall back to -> NEVER evict (would be data loss).
        if pct >= _HIGH_PCT:
            log.warning(
                "cache-evict: disk at %.0f%% but objstore disabled -- cannot "
                "evict (no durable MinIO copy). storage_dir=%s",
                pct, root,
            )
        return 0, pct
    candidates = await asyncio.to_thread(_scan_jobs_by_mtime, root)  # oldest first
    now = time.time()
    deleted = 0
    for mtime, name, path in candidates:
        age = now - mtime
        if age < _HARD_MIN_AGE_S:
            break  # too recent (likely in-flight); oldest-first -> rest younger
        pct, _u, _t = await asyncio.to_thread(_disk_pct, root)
        if age < _MIN_AGE_S and pct <= _LOW_PCT:
            break  # young-ish and no disk pressure -> done (remaining younger)
        # Durability is established at asset-save time -- the asset POST handlers
        # mirror_file BOTH the asset AND its .meta sidecar as they're written --
        # so here we only CONFIRM the dir is in MinIO before dropping the local
        # copy. A single cheap list call, not a re-mirror of every (multi-GB)
        # file, so a full pass stays fast enough to keep up with the fleet.
        try:
            safe = await objstore.prefix_exists(name)
        except Exception:
            safe = False
        if not safe:
            log.warning("cache-evict: keep %s (not confirmed durable; cron will retry)", name)
            continue
        try:
            await asyncio.to_thread(shutil.rmtree, path, True)
            deleted += 1
        except Exception:
            log.warning("cache-evict: rmtree(%s) failed", path, exc_info=True)
    if deleted:
        pct = (await asyncio.to_thread(_disk_pct, root))[0]
        log.info(
            "cache-evict: mirrored + removed %d cached job dir(s) (durable in "
            "MinIO, incl .meta metadata); disk now %.0f%%",
            deleted, pct,
        )
    return deleted, pct


async def _cache_evict_loop() -> None:
    """Periodically bound local job-store disk usage (MinIO-SoT mode).
    Inert unless ``PAPRIKA_CACHE_EVICT_ENABLED`` is set."""
    if _flag("PAPRIKA_CACHE_EVICT_DISABLE", False):
        log.info("cache-evict: kill-switch PAPRIKA_CACHE_EVICT_DISABLE set -- not started")
        return
    log.info(
        "cache-evict: loop started (auto-activates when storage_dir is a "
        "<%.0fGB cache disk AND MinIO enabled; high=%.0f%% low=%.0f%% every %.0fs)",
        _MAX_DISK_GB, _HIGH_PCT, _LOW_PCT, _INTERVAL_S,
    )
    while True:
        try:
            await asyncio.sleep(_INTERVAL_S)
        except asyncio.CancelledError:
            return
        try:
            await _evict_once()
        except Exception:
            log.warning("cache-evict: pass failed", exc_info=True)
