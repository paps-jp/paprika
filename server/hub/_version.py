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

_HUB_VERSION_FILE = Path("/app/VERSION")

# Match the worker's hash roots (``server/worker/agent.py``).
# Walking these on the hub side gives the same digest the worker
# will compute after extracting the hub-pushed tarball, so the
# handshake comparison converges.
_HUB_SOURCE_HASH_ROOTS: tuple[Path, ...] = (Path("/app/server"), Path("/app/core"))


def _compute_hub_source_version() -> str:
    """SHA-256 of every ``.py`` under /app/server and /app/core,
    truncated to 12 hex chars. Mirror of the worker-side helper in
    ``server/worker/agent.py``; kept inline rather than importing to
    avoid pulling worker-only deps into the hub image.
    """
    try:
        import hashlib

        h = hashlib.sha256()
        any_file = False
        for root in _HUB_SOURCE_HASH_ROOTS:
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*.py")):
                if not p.is_file():
                    continue
                try:
                    rel = p.relative_to("/app").as_posix().encode("utf-8")
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


_CACHED_HUB_VERSION: str | None = None
_CACHED_HUB_VERSION_AT: float = 0.0
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
         hosts, survives the tarball round-trip).  Re-computed every
         ``_HUB_VERSION_TTL_S`` seconds so a ``git pull`` on the bind-
         mounted source is picked up without a Hub restart.
      2. ``/app/VERSION`` file (legacy ``sync-workers.sh`` stamp).
      3. ``PAPRIKA_VERSION`` env override.
      4. ``"dev"`` sentinel (= I can't tell my own version).

    Returned to workers in ``HubRegistered.expected_worker_version`` so
    each connecting worker can self-check on registration.
    """
    global _CACHED_HUB_VERSION, _CACHED_HUB_VERSION_AT
    now = time.monotonic()
    if (
        _CACHED_HUB_VERSION is not None
        and (now - _CACHED_HUB_VERSION_AT) < _HUB_VERSION_TTL_S
    ):
        return _CACHED_HUB_VERSION

    v = _compute_hub_source_version()
    if v:
        _CACHED_HUB_VERSION = v
        _CACHED_HUB_VERSION_AT = now
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
