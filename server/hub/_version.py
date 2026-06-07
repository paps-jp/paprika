"""Hub version resolution.

Two-step:
* :func:`_compute_hub_source_version` -- SHA-256 of every ``.py`` under
  ``/app/server`` + ``/app/core``. Deterministic across hosts; mirrors
  the worker-side hash so a worker that auto-extracted the hub-served
  tarball converges to the same digest.
* :func:`_hub_version` -- the cached resolver. Tries the source hash
  first, falls back to ``/app/VERSION`` (legacy sync-workers.sh stamp),
  then ``$PAPRIKA_VERSION``, then the ``"dev"`` sentinel.

Returned to workers in ``HubRegistered.expected_worker_version`` and
surfaced in ``/health`` so external monitoring can spot fleet drift.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_APP_ROOT = Path("/app")
_HUB_VERSION_FILE = _APP_ROOT / "VERSION"

# Match the worker's hash roots (``server/worker/agent.py``).
# Walking these on the hub side gives the same digest the worker
# will compute after extracting the hub-pushed tarball, so the
# handshake comparison converges.
_HUB_SOURCE_HASH_ROOTS: tuple[Path, ...] = (_APP_ROOT / "server", _APP_ROOT / "core")


def _iter_hashed_files(
    roots: tuple[Path, ...] | None = None, app_root: Path | None = None
):
    """Yield ``(rel_posix, path)`` for every ``.py`` that feeds the source
    hash, applying the SAME exclusion as the worker (skip ``server/hub/**``
    and ``server/scheduler.py``). Order is stable -- roots in declared order,
    ``sorted`` within each -- so the digest is deterministic and matches the
    worker's. Shared by :func:`_compute_hub_source_version` (reads + hashes)
    and :func:`_source_signature` (stat-only) so the two can NEVER disagree on
    the file set. ``roots`` / ``app_root`` default to the module globals and
    are overridable for tests.
    """
    if app_root is None:
        app_root = _APP_ROOT
    if roots is None:
        roots = _HUB_SOURCE_HASH_ROOTS
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.py")):
            if not p.is_file():
                continue
            rel_posix = p.relative_to(app_root).as_posix()
            # Worker self-update tracks ONLY code the worker actually runs.
            # Skip hub-only modules (server/hub/** routes/UI/registry, and
            # server/scheduler.py) so editing them never churns the fleet --
            # workers would otherwise self-update for code they don't import.
            # (The worker reads only HEARTBEAT_INTERVAL from scheduler.py; it
            # changes ~never and rides the next real worker-code update.)
            # MUST stay byte-identical to the skip in
            # server/worker/agent.py:_compute_source_version().
            if rel_posix.startswith("server/hub/") or rel_posix == "server/scheduler.py":
                continue
            yield rel_posix, p


def _compute_hub_source_version(
    roots: tuple[Path, ...] | None = None, app_root: Path | None = None
) -> str:
    """SHA-256 of every hashed ``.py`` (see :func:`_iter_hashed_files`),
    truncated to 12 hex chars. Mirror of the worker-side helper in
    ``server/worker/agent.py``; kept inline rather than importing to avoid
    pulling worker-only deps into the hub image. The byte layout (``rel`` +
    NUL + file contents, in sorted order) MUST stay identical so the hub and
    worker digests converge.
    """
    try:
        import hashlib

        h = hashlib.sha256()
        any_file = False
        for rel_posix, p in _iter_hashed_files(roots, app_root):
            try:
                rel = rel_posix.encode("utf-8")
                h.update(rel)
                h.update(b"\0")
                h.update(p.read_bytes())
                any_file = True
            except Exception:
                continue
        if not any_file:
            return ""
        return h.hexdigest()[:12]
    except Exception:
        return ""


def _source_signature(
    roots: tuple[Path, ...] | None = None, app_root: Path | None = None
) -> tuple | None:
    """Cheap fingerprint of the hashed source tree -- ``(rel_posix,
    st_mtime_ns, st_size)`` per file, with NO content read. Changes iff a
    hashed ``.py`` is added / removed / modified, letting :func:`_hub_version`
    skip the expensive SHA-256 walk when nothing changed (the /health hot
    path: it re-read ~all source every TTL, stalling 3-6s every 30s). Returns
    ``None`` on failure so the caller falls back to always re-hashing.
    """
    try:
        sig: list[tuple] = []
        for rel_posix, p in _iter_hashed_files(roots, app_root):
            try:
                st = p.stat()
                sig.append((rel_posix, st.st_mtime_ns, st.st_size))
            except Exception:
                continue
        return tuple(sig)
    except Exception:
        return None


_CACHED_HUB_VERSION: str | None = None
_CACHED_HUB_VERSION_AT: float = 0.0
# mtime/size fingerprint of the source tree at the last full hash. When the
# TTL elapses we re-stat (cheap) and, if this is unchanged, skip the SHA-256
# walk entirely -- so /health no longer re-reads ~all source every 30s.
_CACHED_SOURCE_SIG: tuple | None = None
# Re-walk the source tree at most once per this interval so a git pull
# on the hub host is reflected in ``HubRegistered.expected_worker_version``
# within one TTL without requiring a Hub restart.  Mirrors the worker-side
# ``_VERSION_CACHE_TTL_S`` fix (see server/worker/agent.py and the
# 2026-05-25 post-mortem in CHANGELOG).  Falls back to the permanent-cache
# path when the source hash returns empty (no /app/server present).
_HUB_VERSION_TTL_S: float = 30.0


def _hub_version() -> str:
    """Hub-side version resolver, mirrors the worker's
    :func:`default_worker_version`.

    Resolution order:
      1. SHA-256 hash of /app/server + /app/core (deterministic across
         hosts, survives the tarball round-trip).  Re-validated every
         ``_HUB_VERSION_TTL_S`` seconds via a cheap mtime/size check; the
         hash is only re-walked when a file actually changed, so a ``git
         pull`` on the bind-mounted source is picked up within one TTL
         without a Hub restart -- and without re-reading all source on
         every call.
      2. ``/app/VERSION`` file (legacy ``sync-workers.sh`` stamp).
      3. ``PAPRIKA_VERSION`` env override.
      4. ``"dev"`` sentinel (= I can't tell my own version).

    Returned to workers in ``HubRegistered.expected_worker_version`` so
    each connecting worker can self-check on registration.
    """
    global _CACHED_HUB_VERSION, _CACHED_HUB_VERSION_AT, _CACHED_SOURCE_SIG
    now = time.monotonic()
    if (
        _CACHED_HUB_VERSION is not None
        and (now - _CACHED_HUB_VERSION_AT) < _HUB_VERSION_TTL_S
    ):
        return _CACHED_HUB_VERSION

    # TTL elapsed. Before paying for the full SHA-256 walk (it re-reads ~all
    # source on every call -- the /health stall of 3-6s every 30s), cheaply
    # check whether any hashed file actually changed (mtime/size only). If
    # nothing changed, reuse the cached digest and just refresh the clock.
    sig = _source_signature()
    if (
        _CACHED_HUB_VERSION is not None
        and sig is not None
        and sig == _CACHED_SOURCE_SIG
    ):
        _CACHED_HUB_VERSION_AT = now
        return _CACHED_HUB_VERSION

    v = _compute_hub_source_version()
    if v:
        _CACHED_HUB_VERSION = v
        _CACHED_HUB_VERSION_AT = now
        _CACHED_SOURCE_SIG = sig
        return v
    # Source hash unavailable (e.g. source tree absent).  Fall back to
    # legacy paths; these don't change at runtime so permanent cache is fine.
    if _CACHED_HUB_VERSION is not None:
        return _CACHED_HUB_VERSION
    try:
        if _HUB_VERSION_FILE.exists():
            disk = _HUB_VERSION_FILE.read_text().strip()
            if disk:
                _CACHED_HUB_VERSION = disk
                _CACHED_HUB_VERSION_AT = now
                return disk
    except Exception:
        pass
    env = os.environ.get("PAPRIKA_VERSION", "").strip()
    _CACHED_HUB_VERSION = env or "dev"
    _CACHED_HUB_VERSION_AT = now
    return _CACHED_HUB_VERSION
