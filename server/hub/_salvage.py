"""Ghost-worker salvage loop — auto-recover workers that have ghosted.

A "ghost" worker keeps a live proxied WS (worker->nginx ESTABLISHED) but no hub
consumes it, so the box is up yet it's absent from ``/workers`` (see
[[worker-ghost-proxied-ws]]). The worker's own watchdog can't always self-recover
it, so this loop does it from the hub side, two-stage:

  1. **HTTP self-restart** -- POST the worker's ``:9099/self-restart`` endpoint
     (``_start_selfrestart_server`` in worker ``_mix_run.py``). Works while the
     worker's asyncio loop is idle/ghosted (the endpoint runs in its own thread).
  2. **SSH fallback** -- ``docker restart paprika-worker-1`` over SSH, for a box
     so wedged even its HTTP thread won't answer. Needs an ssh client + key on
     the hub (operator infra); skipped (no-op) when no key is configured.

On success it bumps ``workers.recovery_count`` (the MariaDB ledger, cross-hub) so
the admin Workers tab shows how often each box has been salvaged.

Ghost detection = in the MariaDB ``workers`` ledger + recently seen, but NOT in
the live fleet (``registry.stats_async`` alive set). A genuinely-dead VM answers
neither HTTP nor SSH, so it's left alone (no infinite retry); the [min,max]-age
window also skips long-dead rows.

SAFETY: OFF by default -- arm with ``PAPRIKA_SALVAGE_ENABLE=1`` only once the
infra is ready (worker ``:9099`` exposed and/or hub ssh client + key). Guards:
cross-hub CAS (one hub salvages a given worker at a time), per-worker cooldown,
per-pass rate limit. Env knobs:
  PAPRIKA_SALVAGE_ENABLE (0), PAPRIKA_SALVAGE_INTERVAL_S (60),
  PAPRIKA_SALVAGE_MAX_PER_PASS (3), PAPRIKA_SALVAGE_COOLDOWN_S (600),
  PAPRIKA_SALVAGE_GHOST_MIN_AGE_S (300), PAPRIKA_SALVAGE_GHOST_MAX_AGE_S (3600),
  PAPRIKA_WORKER_SELFRESTART_PORT (9099),
  worker SSH via settings (worker_ssh_user/port/key_path) or
  PAPRIKA_WORKER_SSH_USER / _PORT / _KEY.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from server.hub._state import state, config

log = logging.getLogger("paprika.salvage")


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _salvage_armed() -> bool:
    """Salvage ON if the Settings toggle (salvage_enabled, shared cross-hub via
    settings) OR the env flag is set. Checked EVERY pass so the operator can
    arm/disarm from the admin UI with no hub restart."""
    if state.settings is not None:
        try:
            if bool(state.settings.get("salvage_enabled")):
                return True
        except Exception:
            pass
    return _flag("PAPRIKA_SALVAGE_ENABLE", False)


_KEY_MATERIAL_PATH = "/tmp/paprika-worker-ssh-key"


def _materialize_key(pem: str) -> str:
    """Write an uploaded SSH key PEM (settings worker_ssh_key_pem, shared to
    every hub) to a local 0600 file so ssh can use it. Idempotent: only
    rewrites when the content changed. Returns the path, or '' on failure."""
    # OpenSSH private keys REQUIRE a trailing newline. settings._coerce runs
    # str(v).strip() on every value, which eats that newline -> ssh fails with
    # "error in libcrypto" and salvage can never authenticate (ghosts pile up).
    # Re-add it before writing so the materialised key is valid.
    if pem and not pem.endswith("\n"):
        pem = pem + "\n"
    try:
        try:
            with open(_KEY_MATERIAL_PATH, "r", encoding="utf-8") as f:
                if f.read() == pem:
                    return _KEY_MATERIAL_PATH
        except FileNotFoundError:
            pass
        with open(_KEY_MATERIAL_PATH, "w", encoding="utf-8") as f:
            f.write(pem)
        os.chmod(_KEY_MATERIAL_PATH, 0o600)
        return _KEY_MATERIAL_PATH
    except Exception:
        log.warning("salvage: failed to materialize uploaded SSH key", exc_info=True)
        return ""


_ssh_client_ready: "bool | None" = None


async def _ensure_ssh_client() -> bool:
    """Ensure an `ssh` binary exists in the hub container. The hub image ships
    WITHOUT one (debian base, but apt-get IS present), so install openssh-client
    on first SSH-salvage need -- no Dockerfile rebuild required, and it re-runs
    automatically after a hub restart (image has no ssh until first arm+SSH).
    Cached: attempts the apt install at most once per process. Returns True iff
    ssh is available afterwards."""
    global _ssh_client_ready
    import shutil
    if shutil.which("ssh"):
        _ssh_client_ready = True
        return True
    if _ssh_client_ready is False:
        return False  # already tried + failed this process; don't re-spam apt
    try:
        log.info("salvage: ssh client missing -- installing openssh-client (one-time)")
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c",
            "apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
            "apt-get install -y -qq openssh-client",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=180)
    except Exception:
        log.warning("salvage: openssh-client auto-install failed", exc_info=True)
    ok = shutil.which("ssh") is not None
    _ssh_client_ready = ok
    log.info("salvage: ssh client %s", "ready" if ok else "UNAVAILABLE (SSH salvage disabled)")
    return ok


def _ssh_conf() -> tuple[str, str, str]:
    """(user, port, key_path): settings (設定タブ) first, then .env, then default."""
    def g(skey: str, env: str, dflt: str) -> str:
        v = None
        if state.settings is not None:
            try:
                v = state.settings.get(skey)
            except Exception:
                v = None
        return str(v or os.environ.get(env) or dflt)
    user = g("worker_ssh_user", "PAPRIKA_WORKER_SSH_USER", "root")
    port = g("worker_ssh_port", "PAPRIKA_WORKER_SSH_PORT", "22")
    key_path = g("worker_ssh_key_path", "PAPRIKA_WORKER_SSH_KEY", "")
    # No explicit path? Fall back to a UI-uploaded key PEM (settings, shared to
    # every hub), materialised to a local 0600 file on this hub.
    if not key_path and state.settings is not None:
        try:
            pem = state.settings.get("worker_ssh_key_pem") or ""
        except Exception:
            pem = ""
        if pem:
            key_path = _materialize_key(pem)
    return (user, port, key_path)


async def _http_self_restart(ip: str, secret: str, port: int) -> bool:
    """POST the worker self-restart endpoint. True iff HTTP 200."""
    from core.httpclient import make_async_client
    url = f"http://{ip}:{port}/self-restart"
    headers = {"X-Worker-Secret": secret} if secret else {}
    try:
        async with make_async_client(timeout=8.0) as http:
            r = await http.post(url, headers=headers)
            return getattr(r, "status_code", 0) == 200
    except Exception:
        return False


async def _ssh_restart(ip: str, user: str, port: str, key: str) -> bool:
    """SSH ``docker restart paprika-worker-1``. Needs an ssh client + key on the
    hub (operator infra). Returns True iff rc 0; no-op (False) without a key."""
    if not key:
        return False
    # Hub image has no ssh client by default -- auto-install on first use so SSH
    # salvage works without a Dockerfile rebuild.
    if not await _ensure_ssh_client():
        return False
    cmd = [
        "ssh", "-i", key, "-p", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        f"{user}@{ip}", "docker restart -t 8 paprika-worker-1",
    ]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=30.0)
        return rc == 0
    except Exception:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        return False


async def _salvage_one(wid: str, ip: str) -> str:
    """Salvage one ghost. Returns 'http' | 'ssh' | 'failed' | 'skip'."""
    r = getattr(state.registry, "_r", None)
    hub_id = getattr(state.registry, "_hub_id", "") or ""
    # Cross-hub mutex: only one hub salvages a given worker at a time.
    if r is not None:
        try:
            ok = await r.set(f"paprika:salvage:{wid}", hub_id, nx=True, ex=120)
            if not ok:
                return "skip"
        except Exception:
            pass
    secret = config.worker_secret or ""
    port = _int("PAPRIKA_WORKER_SELFRESTART_PORT", 9099)
    if await _http_self_restart(ip, secret, port):
        try:
            await state.store.bump_worker_recovery(wid, "http self-restart")
            await state.store.record_recovery_event(
                wid, hub_id=hub_id, ip=ip, method="http",
                result="ok", detail="http self-restart")
        except Exception:
            pass
        return "http"
    user, sshport, key = _ssh_conf()
    if await _ssh_restart(ip, user, sshport, key):
        try:
            await state.store.bump_worker_recovery(wid, "ssh restart")
            await state.store.record_recovery_event(
                wid, hub_id=hub_id, ip=ip, method="ssh",
                result="ok", detail="ssh docker restart")
        except Exception:
            pass
        return "ssh"
    # Both methods failed -> record the failed attempt too (durable history).
    try:
        await state.store.record_recovery_event(
            wid, hub_id=hub_id, ip=ip, method="http+ssh",
            result="failed", detail="all salvage methods failed")
    except Exception:
        pass
    return "failed"


async def _salvage_pass() -> int:
    if state.store is None or state.registry is None:
        return 0
    # Live fleet (cross-hub) -- authoritative "alive" set.
    try:
        payload = await state.registry.stats_async()
        alive = {
            w.get("worker_id")
            for w in payload.get("workers", [])
            if w.get("alive")
        }
    except Exception:
        log.warning("salvage: stats_async failed -- pass aborted", exc_info=True)
        return 0
    # MariaDB ledger -- recently-seen workers (cross-hub, durable).
    try:
        meta = await state.store.get_workers_meta()
    except Exception:
        log.warning("salvage: get_workers_meta failed -- pass aborted", exc_info=True)
        return 0
    now = time.time()
    min_age = _int("PAPRIKA_SALVAGE_GHOST_MIN_AGE_S", 300)
    # 24h default (was 1h): a ghost whose VM is still alive (answers HTTP/SSH)
    # is worth salvaging regardless of how long it's been ghosted. The old 1h
    # cap silently skipped any ghost older than an hour -- which, combined with
    # last_seen not being refreshed on heartbeat, meant the window caught zero
    # ghosts. A genuinely dead VM just fails HTTP+SSH and is left alone anyway,
    # so a wide cap is safe; it only widens "which ghosts we TRY".
    max_age = _int("PAPRIKA_SALVAGE_GHOST_MAX_AGE_S", 86400)
    cooldown = _int("PAPRIKA_SALVAGE_COOLDOWN_S", 600)
    ghosts: list[tuple[str, str]] = []
    for wid, m in meta.items():
        if wid in alive:
            continue
        ip = m.get("ledger_ip")
        if not ip:
            continue
        seen = m.get("last_seen_epoch")
        if seen is not None:  # only [min,max]-age gone (skip long-dead VMs)
            gone = now - seen
            if gone < min_age or gone > max_age:
                continue
        rec = m.get("last_recovery_epoch")
        if rec is not None and (now - rec) < cooldown:
            continue  # cooldown: avoid restart storms
        ghosts.append((wid, ip))
    if ghosts:
        log.info(
            "salvage: detected %d ghost(s) (alive=%d ledger=%d): %s",
            len(ghosts), len(alive), len(meta), [g[0] for g in ghosts[:8]],
        )
    n = 0
    for wid, ip in ghosts[: _int("PAPRIKA_SALVAGE_MAX_PER_PASS", 3)]:
        res = await _salvage_one(wid, ip)
        if res in ("http", "ssh"):
            log.info("salvage: recovered ghost %s (%s) via %s", wid, ip, res)
            n += 1
        elif res == "failed":
            log.info("salvage: %s (%s) unreachable (HTTP+SSH) -- left alone", wid, ip)
        elif res == "skip":
            log.info("salvage: %s held by another hub this pass -- skip", wid)
    return n


async def _salvage_loop() -> None:
    """Periodic ghost-salvage. OFF by default; arm with PAPRIKA_SALVAGE_ENABLE=1
    once the infra (worker :9099 exposed and/or hub ssh client+key) is ready."""
    interval = _int("PAPRIKA_SALVAGE_INTERVAL_S", 60)
    log.info(
        "salvage: loop started (interval=%ds, armed=%s) -- arm/disarm live via "
        "Settings salvage_enabled or PAPRIKA_SALVAGE_ENABLE (no restart needed)",
        interval, _salvage_armed(),
    )
    first = True
    while True:
        await asyncio.sleep(5 if first else interval)
        first = False
        # Re-evaluate EVERY pass so the Settings toggle takes effect without a
        # hub restart (salvage_enabled is shared cross-hub via settings).
        if not _salvage_armed():
            continue
        try:
            await _salvage_pass()
        except Exception:
            log.warning("salvage: pass failed", exc_info=True)
