"""Cross-hub config / knowledge propagation (Phase B).

When an operator edits a shared registry (skills, conventions, presets,
hosts, ...) on one hub, that hub:

  1. persists to its local file registry (unchanged), then
  2. writes the record through to MariaDB (the durable source of truth), then
  3. broadcasts the single changed record on a Redis pub/sub channel.

Every hub runs :func:`run_invalidation_subscriber`, which replays a peer's
change *surgically* onto its own file registry -- one record at a time, via the
registry's own ``_to_json`` / ``_from_json`` round-trip. We deliberately do NOT
re-run the full ``restore_*`` reconcile on a change: that path DELETES local
records absent from MariaDB, which would wipe each hub's locally auto-distilled
(not-yet-written-through) skills / conventions. Surgical apply touches only the
record that actually changed.

Publish is wired at the *route* layer (operator edits), never inside the
registry ``_write`` -- so a peer applying an event via ``_write`` does not echo
it back (no broadcast loop). Self-originated events are skipped by ``origin``.
"""
from __future__ import annotations

import asyncio
import json
import logging

from server.hub._state import config, state

log = logging.getLogger(__name__)

_CHAN = "paprika:registry:invalidate"

# kind -> server.hub.mariadb function name (per-record write-through)
_MDB_UPSERT = {
    "skills": "upsert_skill_row",
    "conventions": "upsert_convention_row",
    "presets": "upsert_preset_row",
    "hosts": "upsert_host_row",
}
_MDB_DELETE = {
    "skills": "delete_skill_row",
    "conventions": "delete_convention_row",
    "presets": "delete_preset_row",
    "hosts": "delete_host_row",
}
# kind -> the ``state`` attribute holding the file-backed registry
_STATE_ATTR = {
    "skills": "skills",
    "conventions": "conventions",
    "presets": "presets",
    "hosts": "hosts",
}

# Settings keys that must NOT be shared cross-hub: the MariaDB DSN itself is
# bootstrap config (each hub needs its own to connect at all), so propagating
# it would be circular and could lock a hub out. Everything else (S3, SMB,
# toggles, fetch defaults) is shared.
_BOOTSTRAP_KEYS = frozenset({
    "mariadb_host", "mariadb_port", "mariadb_database",
    "mariadb_username", "mariadb_password",
})


def _registry(kind: str):
    attr = _STATE_ATTR.get(kind)
    return getattr(state, attr, None) if attr else None


def _redis_pub():
    """The store's existing Redis client (publish side). ``None`` for the
    in-memory / single-hub store -> propagation is simply a no-op."""
    return getattr(state.store, "_r", None)


async def _publish(evt: dict) -> None:
    r = _redis_pub()
    if r is None:
        return
    try:
        await r.publish(_CHAN, json.dumps(evt, default=str))
    except Exception:
        log.debug(
            "registry-share: publish failed (%s/%s)",
            evt.get("kind"), evt.get("action"), exc_info=True,
        )


async def share_upsert(kind: str, reg, rec) -> None:
    """Write ``rec`` through to MariaDB and broadcast it to peer hubs.

    Best-effort: the caller has ALREADY written the local file, so a MariaDB or
    Redis hiccup here must never surface as a failed edit -- everything is
    swallowed (logged). Call right AFTER the local ``reg.upsert(...)``.
    """
    pool = state.mariadb_pool
    if pool is not None and kind in _MDB_UPSERT:
        try:
            import server.hub.mariadb as _m
            await getattr(_m, _MDB_UPSERT[kind])(pool, rec)
        except Exception:
            log.warning("registry-share: mariadb upsert %s failed", kind, exc_info=True)
    try:
        key = reg._key_of(rec)
        await _publish({
            "origin": config.hub_id,
            "kind": kind,
            "action": "upsert",
            "key": key,
            "record": reg._to_json(rec),
        })
    except Exception:
        log.debug("registry-share: build/publish upsert %s failed", kind, exc_info=True)


async def share_delete(kind: str, key: str) -> None:
    """Delete ``key`` from MariaDB and broadcast the deletion. Best-effort.
    ``key`` must be the registry's canonical key (the caller normalises)."""
    pool = state.mariadb_pool
    if pool is not None and kind in _MDB_DELETE:
        try:
            import server.hub.mariadb as _m
            await getattr(_m, _MDB_DELETE[kind])(pool, key)
        except Exception:
            log.warning("registry-share: mariadb delete %s failed", kind, exc_info=True)
    await _publish({
        "origin": config.hub_id,
        "kind": kind,
        "action": "delete",
        "key": key,
    })


async def share_settings(changed: dict) -> None:
    """Write changed hub settings through to MariaDB and broadcast them to peer
    hubs. The mariadb_* DSN keys are dropped (bootstrap, per-hub). Best-effort;
    call AFTER the local ``reg.update(...)``."""
    shareable = {
        k: v for k, v in (changed or {}).items() if k not in _BOOTSTRAP_KEYS
    }
    if not shareable:
        return
    pool = state.mariadb_pool
    if pool is not None:
        try:
            import server.hub.mariadb as _m
            await _m.upsert_settings(pool, shareable)
        except Exception:
            log.warning("registry-share: mariadb settings upsert failed", exc_info=True)
    await _publish({
        "origin": config.hub_id,
        "kind": "settings",
        "action": "update",
        "values": shareable,
    })


def _apply_event(evt: dict) -> None:
    """Replay one peer event onto the local file registry. Runs inside the
    subscriber loop (sync registry file ops; small + quick)."""
    kind = evt.get("kind")
    action = evt.get("action")
    if kind == "settings":
        reg = getattr(state, "settings", None)
        if (
            reg is not None
            and action == "update"
            and isinstance(evt.get("values"), dict)
        ):
            reg.update(evt["values"])
        return
    reg = _registry(kind)
    if reg is None:
        return
    key = evt.get("key") or ""
    if action == "delete":
        if key:
            reg.delete(key)
        return
    if action == "upsert" and evt.get("record") is not None:
        rec = reg._from_json(evt["record"])
        # Tiered registries (skills / conventions): a promote/demote moves the
        # record between tier dirs. Clear every tier first so a peer never ends
        # up with the same slug duplicated across tiers.
        if getattr(reg, "tiers", None) and key:
            reg.delete(key)
        reg._write(rec)


async def run_invalidation_subscriber() -> None:
    """Subscribe to the invalidation channel and surgically apply peer changes
    forever. Reconnects on Redis errors; never raises out of the loop."""
    from server.store import make_redis_client

    while True:
        client = None
        try:
            client = make_redis_client(config.redis_url)
            pubsub = client.pubsub()
            await pubsub.subscribe(_CHAN)
            log.info(
                "registry-invalidate: subscribed to %s as %s", _CHAN, config.hub_id
            )
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    evt = json.loads(message["data"])
                except Exception:
                    continue
                if not isinstance(evt, dict) or evt.get("origin") == config.hub_id:
                    continue  # our own change -- already applied locally
                try:
                    _apply_event(evt)
                except Exception:
                    log.warning(
                        "registry-invalidate: apply failed (%s/%s)",
                        evt.get("kind"), evt.get("action"), exc_info=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "registry-invalidate: subscriber error: %s; retry in 5s", e
            )
            await asyncio.sleep(5)
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    try:
                        await client.close()
                    except Exception:
                        pass
