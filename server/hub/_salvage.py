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


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
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
        async with make_async_client(timeout=15.0) as http:
            r = await http.post(url, headers=headers)
            code = getattr(r, "status_code", 0)
            if code != 200:
                log.info("salvage[http] %s: HTTP %s from :%s", ip, code, port)
            return code == 200
    except Exception as e:
        # Expected when the worker process is gone (port closed) -- info, not
        # a warning. Logged anyway so the ledger's "all methods failed" can be
        # traced to a stage instead of being a dead end.
        log.info("salvage[http] %s:%s unreachable (%s)", ip, port, type(e).__name__)
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
    return await _ssh_run(
        ip, user, port, key, "docker restart -t 8 paprika-worker-1",
        what="docker-restart",
    )


#: SSH timeouts for the salvage path. Deliberately far more generous than a
#: normal SSH call: the population salvage acts on is BY DEFINITION the boxes
#: that are struggling, so the moment a node is loaded enough to need salvage
#: is exactly the moment a tight timeout starts failing. Measured on garage
#: 2026-08-03: the node ran at load 104 / IO PSI 52 right after a crash-reboot,
#: which is when a salvage attempt was recorded as "all methods failed" while
#: the key, the route and the wrapper were all provably fine minutes later.
_SSH_CONNECT_TIMEOUT_S = _int("PAPRIKA_SALVAGE_SSH_CONNECT_TIMEOUT_S", 20)
_SSH_CMD_TIMEOUT_S = _int("PAPRIKA_SALVAGE_SSH_CMD_TIMEOUT_S", 60)


async def _ssh_run(
    host: str, user: str, port: str, key: str, remote_cmd: str, *,
    timeout: float | None = None, what: str = "ssh",
) -> bool:
    """Run one command over SSH. True iff rc 0. No key -> no-op False.

    Logs WHY it failed. The previous version collapsed every failure mode --
    no key, no ssh binary, auth rejection, connect timeout, non-zero rc --
    into a bare False, so the ledger's "all salvage methods failed" was the
    only artefact and diagnosing one required going to the worker and reading
    its sshd/wrapper logs. A recovery system that cannot say why it failed
    cannot be tuned.
    """
    if not key:
        log.info("salvage[%s] %s: no SSH key configured -- stage skipped", what, host)
        return False
    if not await _ensure_ssh_client():
        log.warning("salvage[%s] %s: no ssh client available", what, host)
        return False
    timeout = timeout if timeout is not None else float(_SSH_CMD_TIMEOUT_S)
    cmd = [
        "ssh", "-i", key, "-p", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={_SSH_CONNECT_TIMEOUT_S}",
        f"{user}@{host}", remote_cmd,
    ]
    proc = None
    started = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode
        if rc == 0:
            return True
        detail = (err or b"").decode(errors="replace").strip().splitlines()
        log.info(
            "salvage[%s] %s: rc=%s after %.1fs -- %s", what, host, rc,
            time.time() - started, detail[-1] if detail else "(no stderr)",
        )
        return False
    except asyncio.TimeoutError:
        log.warning(
            "salvage[%s] %s: TIMED OUT after %.0fs (node too loaded to answer?)",
            what, host, timeout,
        )
        return False
    except Exception as e:
        log.warning("salvage[%s] %s: %s: %s", what, host, type(e).__name__, e)
        return False
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass


async def _ssh_capture(
    host: str, user: str, port: str, key: str, remote_cmd: str, *,
    timeout: float = 30.0,
) -> str:
    """Run one command over SSH and return its stdout ('' on any failure)."""
    if not key or not await _ensure_ssh_client():
        return ""
    cmd = [
        "ssh", "-i", key, "-p", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={_SSH_CONNECT_TIMEOUT_S}",
        f"{user}@{host}", remote_cmd,
    ]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (out or b"").decode(errors="replace").strip()
    except Exception:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        return ""


async def _ssh_restart_dockerd(ip: str, user: str, port: str, key: str) -> bool:
    """Restart the CT's docker DAEMON, then bring the worker container back.

    The stage between "restart the container" and "reboot the CT". It exists
    because ``docker restart`` has a failure mode of its own: when a CT is
    rebooted or thrashed with containers running, dockerd can come back with
    the container wedged ``Dead`` / "marked for removal", holding the
    ``paprika-worker-1`` name so nothing can recreate it -- observed on 100%
    of the CTs in the 2026-08-02 scratch-pool rollout that skipped a clean
    ``compose down`` (docs/ramdisk-scratch-pool.md).  ``docker restart`` cannot
    fix that; restarting dockerd and force-removing the corpse can.

    The recipe mirrors the manual repair from that post-mortem: bounce dockerd,
    try a plain start, and only if that fails force-remove the stale name and
    recreate from compose. ``|| true`` on the tail so a box where /opt/paprika
    has no compose file still reports the dockerd bounce as done.
    """
    script = (
        "systemctl restart docker && sleep 5 && "
        "(docker start paprika-worker-1 || "
        " (docker rm -f paprika-worker-1; "
        "  cd /opt/paprika && "
        "  docker compose -f docker-compose-worker.yml up -d worker)) || true"
    )
    return await _ssh_run(
        ip, user, port, key, script, timeout=180.0, what="dockerd-restart",
    )


# --- Proxmox stage (CT reboot) --------------------------------------------
# The heaviest stage, and the only one that reaches OUTSIDE the worker box: it
# SSHes a Proxmox node and reboots the container. That is a genuinely new trust
# boundary for the hub -- a hub compromise now reaches the hypervisor -- so it
# is OFF by default, needs its own credentials (never reuses the worker key),
# and is rate-limited per worker AND per node so a bad signal can't walk a node
# down one CT at a time.

_PROXMOX_KEY_PATH = "/tmp/paprika-proxmox-ssh-key"

#: ip -> (node_address, ctid, resolved_at). /etc/pve is the cluster filesystem,
#: so ANY reachable node answers for every CT in the cluster; the mapping only
#: changes when a CT is migrated or re-addressed, hence the long TTL.
_ct_cache: dict[str, tuple[str, str, float]] = {}
_CT_CACHE_TTL_S = 3600.0


def _proxmox_nodes() -> list[tuple[str, str]]:
    """[(node_name, ssh_address)] from the ``proxmox_nodes`` setting.

    Accepts ``boiler=10.10.50.15,hall=10.10.50.11`` or bare addresses. The
    names matter because ``/etc/pve/nodes/<name>/lxc/<id>.conf`` identifies
    which node actually RUNS a container -- resolution can happen anywhere,
    but ``pct reboot`` only works on the owning node.
    """
    raw = ""
    if state.settings is not None:
        try:
            raw = str(state.settings.get("proxmox_nodes") or "")
        except Exception:
            raw = ""
    raw = raw or os.environ.get("PAPRIKA_PROXMOX_NODES", "")
    out: list[tuple[str, str]] = []
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        name, _, addr = tok.partition("=")
        name = name.strip()
        addr = addr.strip() or name
        if name:
            out.append((name, addr))
    return out


def _proxmox_ssh() -> tuple[str, str, str]:
    """(user, port, key_path) for the Proxmox nodes. Separate from the worker
    SSH config on purpose: these credentials reach the hypervisor."""
    def g(skey: str, env: str, dflt: str) -> str:
        v = None
        if state.settings is not None:
            try:
                v = state.settings.get(skey)
            except Exception:
                v = None
        return str(v or os.environ.get(env) or dflt)
    user = g("proxmox_ssh_user", "PAPRIKA_PROXMOX_SSH_USER", "root")
    port = g("proxmox_ssh_port", "PAPRIKA_PROXMOX_SSH_PORT", "22")
    key_path = g("proxmox_ssh_key_path", "PAPRIKA_PROXMOX_SSH_KEY", "")
    if not key_path and state.settings is not None:
        try:
            pem = state.settings.get("proxmox_ssh_key_pem") or ""
        except Exception:
            pem = ""
        if pem:
            if not pem.endswith("\n"):
                pem = pem + "\n"
            try:
                try:
                    with open(_PROXMOX_KEY_PATH, "r", encoding="utf-8") as f:
                        if f.read() == pem:
                            return (user, port, _PROXMOX_KEY_PATH)
                except FileNotFoundError:
                    pass
                with open(_PROXMOX_KEY_PATH, "w", encoding="utf-8") as f:
                    f.write(pem)
                os.chmod(_PROXMOX_KEY_PATH, 0o600)
                key_path = _PROXMOX_KEY_PATH
            except Exception:
                log.warning("salvage: failed to materialize proxmox key", exc_info=True)
    return (user, port, key_path)


def _parse_ct_conf_path(path: str) -> tuple[str, str] | None:
    """``/etc/pve/nodes/boiler/lxc/382.conf`` -> ``("boiler", "382")``.

    Requires the ``lxc`` segment. Proxmox numbers VMs and containers from the
    same id space, and the sibling ``qemu-server/`` directory holds VM configs
    -- accepting one of those would hand ``pct reboot`` a VM's id, which either
    fails or, if a container happens to share the number, reboots the wrong
    guest entirely.
    """
    parts = [p for p in path.strip().split("/") if p]
    try:
        i = parts.index("nodes")
        node = parts[i + 1]
        kind = parts[i + 2]
        ctid = parts[-1]
    except (ValueError, IndexError):
        return None
    if kind != "lxc" or not ctid.endswith(".conf"):
        return None
    ctid = ctid[: -len(".conf")]
    if not (node and ctid.isdigit()):
        return None
    return (node, ctid)


async def _resolve_ct(ip: str) -> tuple[str, str] | None:
    """Map a worker IP to ``(owning_node_ssh_address, ctid)``, or None.

    Matches on the container's declared address in its Proxmox config rather
    than on any naming convention, so a renamed or re-numbered CT still
    resolves. ``grep -F`` with the trailing ``/`` of the CIDR keeps
    ``10.10.51.16`` from matching ``10.10.51.161``.
    """
    now = time.time()
    hit = _ct_cache.get(ip)
    if hit and now - hit[2] < _CT_CACHE_TTL_S:
        return (hit[0], hit[1])
    nodes = _proxmox_nodes()
    if not nodes:
        return None
    user, port, key = _proxmox_ssh()
    if not key:
        return None
    by_name = {n: a for n, a in nodes}
    # A VERB, not a shell command. The node-side key is pinned to
    # scripts/paprika-ct-reboot.sh, which does the /etc/pve scan itself and
    # only ever accepts "resolve <ip>" / "reboot <ctid>" / "ping". Sending a
    # pipeline here instead would force that wrapper to parse an IP back out
    # of a shell string -- fragile, and it would have to keep working as this
    # string changed. See docs/worker-memory-guard.md §5.
    remote = f"resolve {ip}"
    for _name, addr in nodes:
        out = await _ssh_capture(addr, user, port, key, remote, timeout=20.0)
        if not out:
            continue  # node unreachable, or the CT genuinely isn't here
        parsed = _parse_ct_conf_path(out.splitlines()[0])
        if parsed is None:
            continue
        owner_name, ctid = parsed
        # The OWNING node is what pct must run on; fall back to using the node
        # name as a hostname when the operator didn't list it explicitly.
        owner_addr = by_name.get(owner_name, owner_name)
        _ct_cache[ip] = (owner_addr, ctid, now)
        log.info(
            "salvage: resolved %s -> CT %s on node %s (%s)",
            ip, ctid, owner_name, owner_addr,
        )
        return (owner_addr, ctid)
    return None


async def _ct_reboot_allowed(r, wid: str, node: str) -> bool:
    """Rate limits for the hypervisor stage: at most one reboot per worker per
    hour, and at most N per node per hour. Without the per-node cap a wrong
    signal (a stale fleet view, a bad threshold) could reboot every CT on a
    node in sequence -- exactly the 2026-07-09 failure mode that already forced
    the alive-collapse guard, but with reboots instead of restarts."""
    if r is None:
        return True
    per_node = _int("PAPRIKA_SALVAGE_CT_REBOOT_MAX_PER_NODE_H", 2)
    try:
        ok = await r.set(f"paprika:salvage:ctreboot:{wid}", "1", nx=True, ex=3600)
        if not ok:
            log.info("salvage: %s CT reboot suppressed (per-worker 1/h)", wid)
            return False
        nkey = f"paprika:salvage:ctreboot:node:{node}"
        n = await r.incr(nkey)
        await r.expire(nkey, 3600)
        if n > per_node:
            log.warning(
                "salvage: node %s hit the CT-reboot budget (%d/h) -- refusing "
                "to reboot %s. If this is a real node-wide event it needs an "
                "operator, not more reboots.", node, per_node, wid,
            )
            return False
    except Exception:
        return True
    return True


def _proxmox_armed() -> bool:
    """True when the hypervisor stage is armed AND has credentials."""
    armed = False
    if state.settings is not None:
        try:
            armed = bool(state.settings.get("ct_reboot_enabled"))
        except Exception:
            armed = False
    if not armed and not _flag("PAPRIKA_SALVAGE_CT_REBOOT", False):
        return False
    return bool(_proxmox_ssh()[2]) and bool(_proxmox_nodes())


async def _node_verb(ip: str, verb: str, *, timeout: float = 60.0) -> str | None:
    """Run one wrapper verb against the node that owns *ip*'s container.

    Returns the wrapper's stdout, or None when the CT can't be resolved / the
    node can't be reached. Shared by diagnose and netfix so both go through the
    same resolution + credential path as the reboot stage.
    """
    resolved = await _resolve_ct(ip)
    if resolved is None:
        return None
    node_addr, ctid = resolved
    user, port, key = _proxmox_ssh()
    out = await _ssh_capture(
        node_addr, user, port, key, f"{verb} {ctid}", timeout=timeout,
    )
    return out or None


async def _diagnose_ct(ip: str) -> str:
    """Ask the owning node WHY this worker is unreachable. Read-only.

    The whole point: from the hub, "host is dead", "CT is wedged" and "CT's
    networking is dead" are indistinguishable -- every stage fails identically
    and the ledger records the same useless line for all three. The evidence
    only exists host-side, so go get it and put it in the ledger.
    """
    try:
        out = await _node_verb(ip, "diagnose", timeout=45.0)
    except Exception as e:
        return f"diagnose failed: {type(e).__name__}"
    if not out:
        # Nothing answered on the NODE either -- that itself is the finding.
        return "node unreachable (host down?)"
    return out.strip().splitlines()[0][:300]


async def _netfix_ct(wid: str, ip: str) -> bool:
    """Repair the CT's networking without rebooting it.

    Sits between the SSH stages and ``pct reboot`` because it is the only
    repair that keeps in-flight jobs: a CT whose networking died is otherwise
    healthy, and rebooting it to fix an interface throws away up to two hours
    of video download. The node-side wrapper does the actual work (interface
    bounce -> host veth bounce -> NIC re-apply) and stops at whatever succeeds.
    """
    if not _proxmox_armed():
        return False
    try:
        out = await _node_verb(ip, "netfix", timeout=120.0)
    except Exception as e:
        log.info("salvage[netfix] %s: %s", ip, type(e).__name__)
        return False
    if not out:
        return False
    log.warning("salvage[netfix] %s (%s): %s", wid, ip, out.strip())
    return "netfix:" in out and "failed" not in out


async def _pct_reboot(wid: str, ip: str, r) -> bool:
    """Last resort: reboot the worker's CT from its Proxmox node.

    Host-side ``pct`` is the only lever that still works when the CT is so IO-
    starved that spawning a process inside it blocks (the operator measured
    ``pct exec`` returning nothing for 60s during the 2026-08-02 storm), which
    is precisely when every SSH-into-the-CT stage above has already failed.

    ``pct reboot --timeout`` asks for a clean shutdown first; a CT too wedged
    to honour that falls through to stop+start, which the hypervisor can always
    force.
    """
    if not _proxmox_armed():
        return False
    resolved = await _resolve_ct(ip)
    if resolved is None:
        log.info("salvage: %s (%s) -- could not resolve a CT to reboot", wid, ip)
        return False
    node_addr, ctid = resolved
    if not await _ct_reboot_allowed(r, wid, node_addr):
        return False
    user, port, key = _proxmox_ssh()
    timeout_s = _int("PAPRIKA_SALVAGE_CT_REBOOT_TIMEOUT_S", 60)
    # Again a verb. The wrapper on the node owns the actual pct invocation
    # (reboot --timeout, falling back to stop+start) AND the safety checks the
    # hub cannot make from here: that the id is a container on that node, that
    # its hostname is a paprika worker, and that it is not on the node's
    # operator-maintained deny list. A hub that asked for "pct <anything>"
    # would be trusting itself with the hypervisor; this way the node decides.
    script = f"reboot {ctid}"
    log.warning(
        "salvage: ESCALATING to CT reboot -- worker %s (%s) = CT %s on %s. "
        "Every container-level stage failed.", wid, ip, ctid, node_addr,
    )
    return await _ssh_run(
        node_addr, user, port, key, script, timeout=float(timeout_s + 120),
        what="ct-reboot",
    )


async def _salvage_one(wid: str, ip: str) -> str:
    """Issue a restart for one ghost. Returns 'http' | 'ssh' | 'failed' | 'skip'.

    A restart being ISSUED is NOT recovery (案D). The worker only counts as
    recovered once it RE-REGISTERS (shows up alive again) -- _salvage_pass
    confirms that next pass via the pending key. The OLD code recorded
    result="ok" the moment the worker accepted the restart (HTTP 202 / ssh rc0),
    which was a FALSE signal: a worker that re-ghosts (same nginx consistent-hash
    -> same hub) was counted as "recovered" so recovery_events filled with the
    same wid forever (the observed infinite loop). Now ok is only recorded on
    confirmed re-register; here we just stage the pending check."""
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
    # Escalation ladder, cheapest and least disruptive first. Each stage can
    # recover a failure the one before it cannot:
    #   http     in-process exit -- needs NO disk IO, so it still works on a
    #            box whose SSD is saturated (where `docker restart` blocks).
    #   ssh      the container is stuck but dockerd is fine.
    #   dockerd  dockerd itself is wedged / the container is Dead.
    #   ct       the CT can't even run a process (host-side pct is the only
    #            lever left). Gated + rate-limited; see _pct_reboot.
    method = None
    diagnosis = ""
    if await _http_self_restart(ip, secret, port):
        method = "http"
    else:
        user, sshport, key = _ssh_conf()
        if await _ssh_restart(ip, user, sshport, key):
            method = "ssh"
        elif await _ssh_restart_dockerd(ip, user, sshport, key):
            method = "dockerd"
    if method is None:
        # Only reach for the hypervisor once the box has already proved it
        # can't recover itself -- both on this pass (every stage above failed)
        # and across passes (it has re-ghosted after previous restarts).
        fails = 0
        if r is not None:
            try:
                f = await r.get(f"paprika:salvage:fails:{wid}")
                fails = int(f) if f else 0
            except Exception:
                fails = 0
        # Ask the node what is actually wrong before reaching for anything
        # heavier. Cheap, read-only, and it is what turns a "failed" ledger row
        # from a dead end into a finding.
        # Gated on CREDENTIALS, not on ct_reboot_enabled. Diagnosing is
        # read-only, and the operator who has not armed automatic reboots is
        # exactly the one who needs to know WHY a box failed -- observed
        # 2026-08-03, when three balcony workers logged "all salvage methods
        # failed" with no reason attached while the node could have said
        # "exec=hang" for every one of them.
        if _proxmox_nodes() and _proxmox_ssh()[2]:
            diagnosis = await _diagnose_ct(ip)
            log.warning(
                "salvage %s (%s): every IP-based stage failed -- node says: %s",
                wid, ip, diagnosis,
            )
            # A CT that is running and whose init still answers is NOT a dead
            # box: it is reachable over the hypervisor channel, so repair its
            # networking instead of rebooting it. Keeps in-flight jobs.
            if "status=running" in diagnosis and "exec=ok" in diagnosis:
                if await _netfix_ct(wid, ip):
                    method = "netfix"
        if method is None and fails >= _int("PAPRIKA_SALVAGE_CT_REBOOT_AFTER", 2):
            if await _pct_reboot(wid, ip, r):
                method = "ct-reboot"
    if method:
        # Stage a pending re-register check (resolved next pass). Carry the
        # restart timestamp so _resolve_pending can declare failure if the
        # worker never comes back within the window (= re-ghosted).
        if r is not None:
            try:
                await r.set(
                    f"paprika:salvage:pending:{wid}",
                    f"{hub_id}|{ip}|{method}|{int(time.time())}",
                    ex=900,
                )
            except Exception:
                pass
        return method
    # Every stage failed -> box likely truly dead; record + leave alone.
    # Throttle the RECORD, not just the attempt: a permanently-dead box was
    # otherwise re-tried by all 7 hubs every ~80s forever, and recovery_events
    # filled with identical "failed" rows (observed 2026-08-03: w51161/163/167
    # were the only content of the ledger). Attempts stay cheap and frequent --
    # the box may come back -- but the ledger only gets one row per interval.
    quiet = _int("PAPRIKA_SALVAGE_FAILED_LOG_INTERVAL_S", 3600)
    should_record = True
    if r is not None and quiet > 0:
        try:
            should_record = bool(
                await r.set(f"paprika:salvage:failedlog:{wid}", "1", nx=True, ex=quiet)
            )
        except Exception:
            should_record = True
    if should_record:
        try:
            await state.store.record_recovery_event(
                wid, hub_id=hub_id, ip=ip, method="http+ssh+dockerd",
                result="failed",
                detail=(f"all salvage methods failed -- {diagnosis}"
                        if diagnosis else "all salvage methods failed"))
        except Exception:
            pass
    return "failed"


# 案D: give up restarting a worker after this many confirmed re-ghosts, so we
# don't restart-loop a worker that keeps landing back on a stale-upstream hub.
_SALVAGE_FAIL_GIVEUP = _int("PAPRIKA_SALVAGE_FAIL_GIVEUP", 3)

# SAFETY (2026-07-09): skip a salvage pass when the cross-hub "alive" set has
# collapsed to below this fraction of the ledger -- an almost-certain sign of a
# stale/blipped Redis view rather than a real mass-ghost event. 0 disables the
# ratio check (the empty-set guard in _salvage_pass always applies).
_SALVAGE_MIN_ALIVE_RATIO = _float("PAPRIKA_SALVAGE_MIN_ALIVE_RATIO", 0.2)


def _alive_collapsed(alive_count: int, ledger_count: int, min_ratio: float) -> bool:
    """True when the live cross-hub "alive" worker set has collapsed relative to
    the durable ledger -- the 2026-07-09 signature of a stale/blipped Redis view
    rather than a real mass-ghost. Salvage skips its pass on this so it never
    SSH-restarts the whole fleet on a degraded view.

    - Empty ledger  -> not collapsed (nothing to compare against; guard off).
    - Empty alive with a non-empty ledger -> collapsed (the incident signature).
    - Otherwise collapsed iff the alive fraction is below ``min_ratio``.

    Pure so the guard is regression-testable without a live Redis/registry
    (see tests/test_salvage_guard.py).
    """
    if ledger_count <= 0:
        return False
    if alive_count <= 0:
        return True
    return (alive_count / ledger_count) < min_ratio


async def _resolve_pending(r, alive: set) -> None:
    """案D: confirm or fail previously-issued restarts. A restarted worker is
    only RECOVERED once it re-registers (back in the alive set) -- only THEN do
    we record result=ok. If a restart's re-register window elapses and the
    worker is still not alive, it re-ghosted: bump a fail counter and, past the
    give-up threshold, stop restarting it + record give_up, so the operator sees
    an unrecoverable worker instead of an infinite silent restart loop (the
    observed recovery_events 'ok' spam)."""
    hub_id = getattr(state.registry, "_hub_id", "") or ""
    try:
        keys = [k async for k in r.scan_iter(match="paprika:salvage:pending:*", count=200)]
    except Exception:
        return
    now = time.time()
    for k in keys:
        ks = k.decode() if isinstance(k, bytes) else str(k)
        wid = ks.rsplit(":", 1)[-1]
        try:
            raw = await r.get(ks)
            raw = raw.decode() if isinstance(raw, bytes) else (raw or "")
        except Exception:
            raw = ""
        parts = raw.split("|")
        ipv = parts[1] if len(parts) > 1 else ""
        method = parts[2] if len(parts) > 2 else "restart"
        try:
            ts = float(parts[3]) if len(parts) > 3 else 0.0
        except ValueError:
            ts = 0.0
        if wid in alive:
            # genuine recovery: the worker re-registered after the restart.
            try:
                await r.delete(ks)
                await r.delete(f"paprika:salvage:fails:{wid}")
                await state.store.bump_worker_recovery(wid, f"{method} re-register")
                await state.store.record_recovery_event(
                    wid, hub_id=hub_id, ip=ipv, method=method,
                    result="ok", detail="re-registered after restart")
            except Exception:
                pass
            continue
        if now - ts < 150:
            continue  # still within the re-register grace window; recheck later
        # window elapsed, still not alive -> re-ghosted. Count a failure.
        try:
            await r.delete(ks)
            fkey = f"paprika:salvage:fails:{wid}"
            fails = await r.incr(fkey)
            await r.expire(fkey, 21600)  # 6h: forget the streak if it settles
            if fails >= _SALVAGE_FAIL_GIVEUP:
                await state.store.record_recovery_event(
                    wid, hub_id=hub_id, ip=ipv, method="restart",
                    result="give_up",
                    detail=f"re-ghosted {fails}x after restart -- operator intervention")
                log.warning(
                    "salvage: GIVE UP on %s after %d re-ghosts -- restart keeps "
                    "landing on a stale-upstream hub; needs deploy-time hub drain "
                    "(案B) or manual nginx reload.", wid, fails,
                )
        except Exception:
            pass


async def _memguard_pass(payload: dict, meta: dict) -> int:
    """Escalate workers whose OWN memory guard has failed to recycle them.

    The worker-side guard (server/worker/cgroup_mem.py + agent/_mix_run.py)
    already drains and exits on sustained memory distress, and that handles the
    ordinary case without the hub involved. This pass exists only for the case
    the guard cannot handle: it set ``memguard``, we kept seeing that flag beat
    after beat, and the worker is STILL here well past its own force-exit
    deadline. That means the exit didn't happen or docker didn't bring it back
    -- i.e. the container/daemon layer is itself stuck, which is exactly what
    the dockerd and CT stages are for.

    Waiting on the elapsed time rather than the flag is what keeps this from
    fighting the worker: a healthy guard trip clears in minutes.
    """
    grace = _int("PAPRIKA_SALVAGE_MEMGUARD_GRACE_S", 1200)
    if grace <= 0:
        return 0
    stuck: list[tuple[str, str, float, str]] = []
    for w in payload.get("workers", []):
        if not w.get("alive"):
            continue  # already a ghost -> the normal ghost pass owns it
        reason = w.get("memguard") or ""
        if not reason:
            continue
        held = float(w.get("memguard_s") or 0.0)
        if held < grace:
            continue
        wid = w.get("worker_id") or ""
        # Prefer the MariaDB ledger's IP, same as the ghost path: behind nginx
        # the live connection's client_address can be the proxy's, and SSHing
        # the proxy would be both useless and alarming.
        ip = (meta.get(wid, {}) or {}).get("ledger_ip") or w.get("address") or ""
        if wid and ip:
            stuck.append((wid, ip, held, reason))
    n = 0
    for wid, ip, held, reason in stuck[: _int("PAPRIKA_SALVAGE_MAX_PER_PASS", 3)]:
        log.warning(
            "salvage: %s (%s) has been memory-guard draining for %.0fs "
            "(%s) without recycling -- escalating", wid, ip, held, reason,
        )
        res = await _salvage_one(wid, ip)
        if res in ("http", "ssh", "dockerd", "netfix", "ct-reboot"):
            log.info("salvage: memory escalation for %s issued via %s", wid, res)
            n += 1
    return n


async def _salvage_pass() -> int:
    if state.store is None or state.registry is None:
        return 0
    r = getattr(state.registry, "_r", None)
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
    # MariaDB ledger -- recently-seen workers (cross-hub, durable). Fetched
    # BEFORE issuing/resolving anything so the collapse-guard below can compare
    # the live alive-set against the ledger size.
    try:
        meta = await state.store.get_workers_meta()
    except Exception:
        log.warning("salvage: get_workers_meta failed -- pass aborted", exc_info=True)
        return 0
    # SAFETY (2026-07-09 incident): a COLLAPSED alive set is almost always a
    # stale/blipped cross-hub Redis view, NOT a real mass-ghost. When .35-.39's
    # hubs held a stale Redis client, stats_async read alive=0 while the ledger
    # still held 167 workers -- so salvage SSH-restarted the ENTIRE fleet in a
    # churn loop. The reconciler already skips its orphan pass on this exact
    # signal; salvage lacked the guard. Skip when alive has collapsed to empty,
    # or to a tiny fraction of the ledger (both = degraded view, not real mass
    # death). Tunable via PAPRIKA_SALVAGE_MIN_ALIVE_RATIO (0 = empty-guard only).
    if _alive_collapsed(len(alive), len(meta), _SALVAGE_MIN_ALIVE_RATIO):
        log.warning(
            "salvage: alive set collapsed (alive=%d ledger=%d ratio=%.2f) -- "
            "likely a stale/blipped Redis view, NOT a real mass-ghost; "
            "skipping pass (safety). If this hub's fleet view is genuinely "
            "this small, restart it for a fresh Redis client.",
            len(alive), len(meta), len(alive) / max(1, len(meta)),
        )
        return 0
    # 案D: resolve restarts issued in earlier passes (confirm re-register = ok,
    # or count a re-ghost failure -> eventually give up) BEFORE issuing new ones.
    if r is not None:
        try:
            await _resolve_pending(r, alive)
        except Exception:
            log.warning("salvage: resolve_pending failed", exc_info=True)
    # Memory-choke escalation. Runs under the same collapse guard as the ghost
    # pass above, and against workers that are ALIVE -- so it is a strictly
    # separate population from the ghosts collected below.
    try:
        await _memguard_pass(payload, meta)
    except Exception:
        log.warning("salvage: memguard pass failed", exc_info=True)
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
    # 案D: workers with a restart already issued + awaiting re-register -- don't
    # double-issue (one scan, not a get per wid).
    pending: set = set()
    if r is not None:
        try:
            pending = {
                (k.decode() if isinstance(k, bytes) else str(k)).rsplit(":", 1)[-1]
                async for k in r.scan_iter(match="paprika:salvage:pending:*", count=200)
            }
        except Exception:
            pending = set()
    ghosts: list[tuple[str, str]] = []
    for wid, m in meta.items():
        if wid in alive:
            continue
        if wid in pending:
            continue  # restart already issued, awaiting re-register (案D)
        ip = m.get("ledger_ip")
        if not ip:
            continue
        # 案D: skip persistent re-ghosters we've given up on -- another restart
        # won't help (only a deploy hub-drain / nginx reload fixes those).
        if r is not None:
            try:
                f = await r.get(f"paprika:salvage:fails:{wid}")
                if f and int(f) >= _SALVAGE_FAIL_GIVEUP:
                    continue
            except Exception:
                pass
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
            log.info(
                "salvage: restart issued for %s (%s) via %s -- awaiting "
                "re-register (案D)", wid, ip, res)
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
