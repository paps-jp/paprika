"""Generic scheduled-maintenance plugin ticker.

A small, dependency-free driver that invokes ONE operator-chosen plugin on a
cadence, cross-hub gated so only a single hub runs per tick. It knows nothing
about what the plugin does — it just:

  * resolves Paprika's own runtime config (MariaDB + S3 endpoints/creds) from
    the live Settings and injects it as a ``_paprika``-flavoured context so a
    maintenance plugin doesn't have to rediscover it, and
  * carries an opaque ``cursor`` across ticks (persisted in Redis) so a plugin
    can page through a large backlog a bounded chunk at a time.

This exists so pipeline-coupled maintenance (e.g. the video-asset GC, which
depends on the ai-pipeline ``delian`` schema) can live entirely in an
out-of-git plugin under ``data/tools/installed/<name>/`` while the generic
scheduling/locking/Redis-cursor plumbing stays in core. The ticker has no
knowledge of delian, GC, or any specific plugin.

Off by default (``maintenance_plugin_enabled`` = False): inert until an
operator names a plugin and switches it on.
"""

from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger("paprika.maintenance")


def _setting(key, default):
    from server.hub._state import state

    try:
        if state.settings is not None:
            v = state.settings.get(key, None)
            if v is not None and str(v).strip() != "":
                return v
    except Exception:
        pass
    return default


def _enabled() -> bool:
    try:
        return bool(_setting("maintenance_plugin_enabled", False))
    except Exception:
        return False


def _plugin_name() -> str:
    return str(_setting("maintenance_plugin_name", "") or "").strip()


def _interval_s() -> int:
    try:
        return max(30, min(3600, int(_setting("maintenance_plugin_interval_s", 300))))
    except Exception:
        return 300


def _operator_params() -> dict:
    raw = _setting("maintenance_plugin_params", "{}")
    try:
        d = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        return d if isinstance(d, dict) else {}
    except Exception:
        log.warning("maintenance_plugin_params is not valid JSON; ignoring")
        return {}


def _paprika_context() -> dict:
    """Resolve MariaDB + S3 config from the live Settings for the plugin.

    Generic: this is Paprika's own runtime config, not anything GC-specific.
    Secret-bearing keys ride here; the plugin registry's audit log redacts
    ``*password*``/``*secret*`` keys, and these never leave the hub host.

    Each value is resolved Setting -> env -> default, exactly like
    ``objstore._s3cfg`` -- critical because in prod the S3/MariaDB credentials
    live in the ENV (``PAPRIKA_S3_ACCESS_KEY`` etc.), NOT in the settings.json
    registry (where ``s3_access_key`` is blank). Reading only the Setting would
    hand the plugin empty creds.
    """
    import os as _os

    def s(k, env, d=""):
        v = _setting(k, None)
        if v is not None and str(v).strip() != "":
            return v
        return (_os.environ.get(env) or d)

    return {
        "mariadb": {
            "host": s("mariadb_host", "PAPRIKA_MARIADB_HOST", "10.10.50.20"),
            "port": int(s("mariadb_port", "PAPRIKA_MARIADB_PORT", 3306) or 3306),
            "user": s("mariadb_username", "PAPRIKA_MARIADB_USERNAME", ""),
            "password": s("mariadb_password", "PAPRIKA_MARIADB_PASSWORD", ""),
        },
        "s3": {
            "video_endpoint": s("s3_endpoint", "PAPRIKA_S3_ENDPOINT", ""),        # .48
            "spill_endpoint": s("s3_spill_endpoint", "PAPRIKA_S3_SPILL_ENDPOINT", ""),  # .8
            "access": s("s3_access_key", "PAPRIKA_S3_ACCESS_KEY", ""),
            "secret": s("s3_secret_key", "PAPRIKA_S3_SECRET_KEY", ""),
            "region": s("s3_region", "PAPRIKA_S3_REGION", "us-east-1"),
            "bucket": s("s3_bucket", "PAPRIKA_S3_BUCKET", "paprika"),
            "prefix": s("s3_prefix", "PAPRIKA_S3_PREFIX", "jobs"),
        },
    }


def _redis():
    try:
        from server.hub._state import state

        return getattr(state.store, "_r", None)
    except Exception:
        return None


def _installed_here(name: str) -> bool:
    """True when this hub actually has the plugin, so we don't take the
    cross-hub lock only to no-op (a hub that HAS it should win the tick)."""
    try:
        from server.hub import plugins

        return name in plugins.load_registry()
    except Exception:
        return False


async def _maintenance_loop() -> None:
    from server.hub._state import state

    try:
        await asyncio.sleep(60 + (hash(str(getattr(state, "hub_id", ""))) & 0x1F))
    except asyncio.CancelledError:
        return

    while True:
        try:
            await asyncio.sleep(_interval_s())
        except asyncio.CancelledError:
            return

        try:
            await _tick()
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("maintenance tick failed", exc_info=True)


async def _tick() -> None:
    from server.hub._state import state

    if not _enabled():
        return
    name = _plugin_name()
    if not name:
        return
    if not _installed_here(name):
        return  # let a hub that has the plugin take the lock

    redis = _redis()
    hub_id = str(getattr(state, "hub_id", "") or "")
    lock_key = "paprika:maint:%s:lock" % name
    cursor_key = "paprika:maint:%s:cursor" % name

    if redis is not None:
        try:
            got = await redis.set(
                lock_key, hub_id, nx=True, ex=max(30, int(_interval_s() * 0.9))
            )
            if not got:
                return  # another hub owns this tick
        except Exception:
            pass  # a Redis blip must not silence maintenance

    # Restore the opaque cursor from the previous tick.
    cursor = None
    if redis is not None:
        try:
            raw = await redis.get(cursor_key)
            if raw is not None:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode()
                cursor = json.loads(raw)
        except Exception:
            cursor = None

    params = _operator_params()
    params.update(_paprika_context())
    if cursor is not None:
        params["cursor"] = cursor

    from server.hub.plugins import invoke_plugin

    try:
        result = await invoke_plugin(
            name, "run", params, audit_context={"driver": "maintenance"}
        )
    except Exception as exc:
        log.warning("maintenance plugin %s failed: %s", name, exc)
        return

    # Persist the returned cursor for the next tick (None -> clear = restart).
    if redis is not None:
        try:
            nxt = (result or {}).get("next_cursor") if isinstance(result, dict) else None
            if nxt is None:
                await redis.delete(cursor_key)
            else:
                await redis.set(cursor_key, json.dumps(nxt), ex=7 * 24 * 3600)
        except Exception:
            pass

    if isinstance(result, dict):
        log.info(
            "maintenance %s: %s",
            name,
            {k: result.get(k) for k in (
                "dry_run", "scanned", "candidates", "candidate_gb",
                "deleted_from_ram48", "deleted_from_hdd8", "by_status", "wrapped",
            ) if k in result},
        )
