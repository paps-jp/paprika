"""Node-side periodic health scan of every worker container.

Why this exists (the hole it fills)
-----------------------------------
Every other check paprika makes runs INSIDE the worker: the memory guard, the
fd-budget gate, the loop watchdog, the self-check. That is fine while the
worker is alive, and it is exactly why none of them helped on hall CT133
(2026-08-03):

    memcg 6098 MB / 6144 MB   (99% of its limit)
    file  6014 MB             (the whole cgroup is page cache)
    anon     0 MB             <- the python they all run in was already dead
    PSI some avg60 20.57      <- past the guard's OWN threshold

The guard's trip condition was satisfied and the guard was not there to act on
it. The CT kept thrashing, its TCP stack still accepted connections so the box
looked alive, sshd could not get far enough to send a banner, and every stage
of the hub's salvage ladder failed identically. Recovery needed a human.

A Proxmox node reads ``/sys/fs/cgroup/lxc/<id>/`` regardless of how sick the
container is -- that is the one vantage point the failure cannot take away.
This module polls each node's ``scan`` verb, keeps the previous sample so it
can turn cumulative counters into RATES, and acts before a worker reaches the
state where only a human can help.

Escalation, gentlest first
--------------------------
The whole point of scanning from outside is to arrive EARLY, while the worker
process is still running -- so the default action is the one that costs
nothing: ask the worker to recycle itself, exactly as its own guard would have.
Only a container that can no longer do that gets the hypervisor treatment.

    worker still alive   -> POST :9099/self-restart   (drain + exit, jobs kept)
    worker gone / wedged -> pct restart                (needs ct_reboot_enabled)

Detection
---------
``refault`` rate is the primary signal, for the same reason it is in the
worker-side guard: a major fault can be a legitimate first read, a refault
cannot. Measured 0.0/s on every healthy CT across boiler, garage and hall.

The second signal is the CT133 fingerprint, which no rate catches on its own:
a memcg pinned near its limit whose contents are almost entirely page cache
means the worker's own memory is gone. Note the deliberate absence of a plain
"current is high" rule -- a memcg fills with clean cache by design, and garage
CT351 sat at 75% of its limit while perfectly healthy.

Reading the limit here also fixes something the worker cannot do for itself:
``memory.max`` is invisible from inside the container (it lives on the parent
CT cgroup), so the worker-side thresholds have to be absolute. From the node
the limit is right there, so these thresholds are PERCENTAGES and work equally
on hall's 6GB CTs and boiler's 8GB ones.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from server.hub._state import state

log = logging.getLogger("paprika.ctscan")

#: (node, ctid) -> (monotonic, refault, majfault)
_prev: dict[tuple[str, str], tuple[float, int, int]] = {}
#: (node, ctid) -> monotonic when it first looked unhealthy. Sustained-only,
#: same contract as the worker-side guard: a burst is not a storm. Measured
#: 2026-08-03 on w51175, where refault hit 1938/s and cleared by itself in 94s.
_breach_since: dict[tuple[str, str], float] = {}
#: (node, ctid) -> monotonic of the last action, so one sick CT can't be
#: hammered every pass while it restarts.
_last_action: dict[tuple[str, str], float] = {}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _setting(key: str, default):
    if state.settings is None:
        return default
    try:
        v = state.settings.get(key)
        return default if v is None else v
    except Exception:
        return default


def parse_scan_line(line: str) -> dict | None:
    """``ct=133 host=paprika-worker187 cur=... max=... anon=...`` -> dict.

    Unknown keys are kept, so adding a field to the node script does not
    require a hub deploy to stop discarding it.
    """
    out: dict = {}
    for tok in line.split():
        k, _, v = tok.partition("=")
        if not k or not v:
            continue
        if k in ("ct", "host", "max"):
            out[k] = v
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out if out.get("ct") else None


#: Signals whose reason text contains one of these are MONOTONIC -- they do not
#: spike and recover, so waiting out the full sustain window only costs the
#: worker. Anonymous memory is the case: measured on balcony CT337
#: (2026-08-03), anon ran from the worker guard's 5500MB threshold to 7652MB
#: and killed the process before a 300s window could elapse. Contrast refault,
#: which on w51175 the same day hit 1938/s and cleared by itself in 94s -- that
#: one MUST wait, or the scan recycles workers that were about to recover.
_FAST_SIGNALS = ("leak at the wall",)


def is_fast(reasons: list[str]) -> bool:
    return any(f in r for r in reasons for f in _FAST_SIGNALS)


def evaluate(cur: dict, prev: tuple[float, int, int] | None, now_m: float) -> list[str]:
    """Reasons this container looks unhealthy. Empty list == fine."""
    reasons: list[str] = []
    limit = cur.get("max")
    limit_b = 0.0
    if isinstance(limit, str) and limit != "max":
        try:
            limit_b = float(limit)
        except ValueError:
            limit_b = 0.0
    elif isinstance(limit, (int, float)):
        limit_b = float(limit)

    rf_limit = _float_env("PAPRIKA_CTSCAN_REFAULT_PER_S", 500.0)
    if prev is not None and rf_limit > 0:
        dt = now_m - prev[0]
        if dt > 0:
            d = cur.get("refault", 0.0) - prev[1]
            if d >= 0:
                rate = d / dt
                if rate >= rf_limit:
                    reasons.append(f"refault {rate:.0f}/s >= {rf_limit:.0f}/s")

    # Two ways a container reaches the wall, and they look like opposites.
    # Both were seen on 2026-08-03, both left the CT wedged (exec=hang) with
    # every salvage stage failing, and each is invisible to the other's rule.
    full_pct = _float_env("PAPRIKA_CTSCAN_FULL_PCT", 95.0)
    anon_pct = _float_env("PAPRIKA_CTSCAN_MIN_ANON_PCT", 10.0)
    leak_pct = _float_env("PAPRIKA_CTSCAN_ANON_PCT", 75.0)
    if limit_b > 0:
        used_pct = 100.0 * cur.get("cur", 0.0) / limit_b
        # (a) THRASH -- hall CT133: 99% full, anon 0.4MB. The cgroup is
        # entirely page cache because the worker process is already gone.
        # Needs BOTH conditions: "near the limit" alone is the normal state of
        # a busy container (garage CT351 sat at 75% and was perfectly healthy).
        a_of_cur = 100.0 * cur.get("anon", 0.0) / max(1.0, cur.get("cur", 0.0))
        if used_pct >= full_pct and a_of_cur <= anon_pct:
            reasons.append(
                f"memcg {used_pct:.0f}% full but only {a_of_cur:.0f}% anon "
                f"(worker memory gone, cgroup is all cache)"
            )
        # (b) LEAK AT THE WALL -- balcony CT337: 99.98% full with anon at 93%
        # OF THE LIMIT. The mirror image, and the first rule cannot see it.
        # Anonymous memory is not reclaimable (the CTs run swap: 0), so a
        # cgroup this full of anon has no headroom left at all -- which is why
        # that container could no longer spawn a process.
        # Measured against the limit rather than against current on purpose:
        # the healthiest balcony CTs ran 40-53% of their limit in anon, so the
        # margin here is wide. This is the node-side mirror of the worker
        # guard's absolute anon threshold, expressed as a percentage so it
        # works on hall's 6GB CTs as well as boiler's 8GB ones.
        a_of_limit = 100.0 * cur.get("anon", 0.0) / limit_b
        if a_of_limit >= leak_pct:
            reasons.append(
                f"anon {a_of_limit:.0f}% of the cgroup limit "
                f"(leak at the wall, nothing reclaimable left)"
            )

    psi_limit = _float_env("PAPRIKA_CTSCAN_PSI_PCT", 20.0)
    if psi_limit > 0 and cur.get("psi60", 0.0) >= psi_limit:
        reasons.append(f"PSI some avg60 {cur.get('psi60', 0.0):.1f} >= {psi_limit:.0f}")
    return reasons


async def _act(node: str, ctid: str, host: str, reasons: str) -> str:
    """Recover one container, gentlest option first. Returns what was done."""
    from server.hub import _salvage

    # diagnose gives us both the container's IP and whether it can still run a
    # process -- exactly the two things that decide gentle vs hypervisor.
    user, port, key = _salvage._proxmox_ssh()
    diag = (await _salvage._ssh_capture(
        node, user, port, key, f"diagnose {ctid}", timeout=45.0,
    ) or "").strip()
    ip = next((t[3:] for t in diag.split() if t.startswith("ip=")), "")

    if ip and "exec=ok" in diag:
        # Is it still talking to a hub? Only then can it drain gracefully.
        alive: set[str] = set()
        try:
            payload = await state.registry.stats_async()
            alive = {
                str(w.get("address") or "")
                for w in payload.get("workers", []) if w.get("alive")
            }
        except Exception:
            pass
        if ip in alive:
            secret = ""
            try:
                from server.hub._state import config
                secret = config.worker_secret or ""
            except Exception:
                pass
            if await _salvage._http_self_restart(ip, secret, 9099):
                log.warning(
                    "ctscan: %s (CT %s on %s) recycling itself -- %s",
                    host, ctid, node, reasons,
                )
                return "self-restart"

    # Cannot ask nicely: the container is wedged or its worker is gone.
    if not _salvage._proxmox_armed():
        log.warning(
            "ctscan: %s (CT %s on %s) needs a CT restart (%s) but the "
            "hypervisor stage is not armed -- set ct_reboot_enabled to let "
            "this recover automatically. Diagnosis: %s",
            host, ctid, node, reasons, diag or "(none)",
        )
        return "blocked"
    ok = await _salvage._ssh_run(
        node, user, port, key, f"restart {ctid}",
        timeout=180.0, what="ctscan-restart",
    )
    log.warning(
        "ctscan: %s (CT %s on %s) CT restart %s -- %s",
        host, ctid, node, "OK" if ok else "FAILED", reasons,
    )
    return "ct-restart" if ok else "failed"


async def _scan_node(node_name: str, addr: str) -> list[dict]:
    """Poll one node. Returns the parsed rows (empty when unreachable)."""
    from server.hub import _salvage
    user, port, key = _salvage._proxmox_ssh()
    out = await _salvage._ssh_capture(addr, user, port, key, "scan", timeout=60.0)
    if not out:
        return []
    rows = []
    for line in out.splitlines():
        row = parse_scan_line(line.strip())
        if row:
            row["_node"] = addr
            row["_node_name"] = node_name
            rows.append(row)
    return rows


def _redis():
    return getattr(state.registry, "_r", None)


def _hub_id() -> str:
    return getattr(state.registry, "_hub_id", "") or "?"


async def _claim(key: str, ttl: int) -> bool:
    """Cross-hub CAS. True when THIS hub may proceed.

    Without this every hub runs the whole thing independently: measured after
    the first deploy, six hubs were each SSH-ing every Proxmox node every two
    minutes for identical data, and -- far worse -- an unhealthy container
    would have been restarted once per hub. Salvage has had this guard from
    the start; the scan loop shipped without it.

    No Redis (single-hub / dev) -> proceed, since there is nobody to race.
    """
    r = _redis()
    if r is None:
        return True
    try:
        return bool(await r.set(key, _hub_id(), nx=True, ex=ttl))
    except Exception:
        return True


async def scan_pass(*, respect_lease: bool = True) -> dict:
    """One sweep of every configured node. Returns a small summary."""
    from server.hub import _salvage
    nodes = _salvage._proxmox_nodes()
    if not nodes or not _salvage._proxmox_ssh()[2]:
        return {"nodes": 0, "cts": 0, "unhealthy": 0, "acted": 0}

    interval = _int_env("PAPRIKA_CTSCAN_INTERVAL_S", 120)
    if respect_lease and not await _claim(
        "paprika:ctscan:lease", max(30, int(interval * 0.9))
    ):
        return {"nodes": 0, "cts": 0, "unhealthy": 0, "acted": 0, "skipped": "lease"}

    sustain = _float_env("PAPRIKA_CTSCAN_SUSTAIN_S", 300.0)
    cooldown = _float_env("PAPRIKA_CTSCAN_COOLDOWN_S", 900.0)
    max_act = _int_env("PAPRIKA_CTSCAN_MAX_PER_PASS", 2)

    results = await asyncio.gather(
        *(_scan_node(n, a) for n, a in nodes), return_exceptions=True,
    )
    now_m = time.monotonic()
    n_cts = n_bad = n_acted = 0
    unhealthy: list[tuple[str, str, str, str]] = []

    for res in results:
        if isinstance(res, BaseException) or not res:
            continue
        for row in res:
            n_cts += 1
            node = row["_node"]
            ctid = str(row["ct"])
            key = (node, ctid)
            reasons = evaluate(row, _prev.get(key), now_m)
            _prev[key] = (now_m, int(row.get("refault", 0)), int(row.get("majfault", 0)))
            if not reasons:
                _breach_since.pop(key, None)
                continue
            n_bad += 1
            first = _breach_since.setdefault(key, now_m)
            need = _float_env("PAPRIKA_CTSCAN_FAST_SUSTAIN_S", 60.0) \
                if is_fast(reasons) else sustain
            if now_m - first < need:
                continue
            if now_m - _last_action.get(key, 0.0) < cooldown:
                continue
            unhealthy.append((node, ctid, str(row.get("host", "?")), "; ".join(reasons)))

    for node, ctid, host, reasons in unhealthy[:max_act]:
        # Second, narrower claim: even if two hubs somehow both ran a pass,
        # only one may act on a given container. This one also outlives a hub
        # restart, which the in-memory cooldown above does not.
        if not await _claim(f"paprika:ctscan:act:{node}:{ctid}", int(cooldown)):
            continue
        _last_action[(node, ctid)] = now_m
        _breach_since.pop((node, ctid), None)
        try:
            await _act(node, ctid, host, reasons)
            n_acted += 1
        except Exception:
            log.warning("ctscan: action failed for CT %s on %s", ctid, node,
                        exc_info=True)
    if len(unhealthy) > max_act:
        log.warning(
            "ctscan: %d containers need attention, acting on %d this pass "
            "(PAPRIKA_CTSCAN_MAX_PER_PASS) -- deferred: %s",
            len(unhealthy), max_act, [u[2] for u in unhealthy[max_act:]],
        )
    return {"nodes": len(nodes), "cts": n_cts, "unhealthy": n_bad, "acted": n_acted}


async def ct_scan_loop() -> None:
    """Periodic node-side scan. Inert until the Proxmox settings are filled in.

    Detection runs as soon as the credentials exist; ACTING on a container that
    cannot recycle itself additionally needs ``ct_reboot_enabled``, so an
    operator can turn the observation on and watch what it would have done
    before arming it.
    """
    interval = _int_env("PAPRIKA_CTSCAN_INTERVAL_S", 120)
    log.info("ctscan: loop started (interval=%ds)", interval)
    first = True
    while True:
        await asyncio.sleep(20 if first else interval)
        first = False
        try:
            if not bool(_setting("ct_scan_enabled", True)):
                continue
            summary = await scan_pass()
            if summary.get("unhealthy"):
                log.info("ctscan: %s", summary)
        except Exception:
            log.warning("ctscan: pass failed", exc_info=True)
