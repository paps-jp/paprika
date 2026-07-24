#!/usr/bin/env python3
"""nginx upstream auto-reconciler for the paprika hub fleet.

Runs on the router host (.34) beside nginx. Watches the Redis hub-presence
registry (``paprika:hubs:*``, written + TTL-refreshed by each hub's
``server/hub/_hubs.py:HubRegistry``) and keeps the ``hubs`` / ``hubs_sticky``
nginx upstreams in sync -- so a cloned / newly-booted hub VM auto-joins the load
balancer with no manual ``nginx.conf`` edit (mirrors how workers auto-join a
hub via Redis).

Safety:
  * Only rewrites when the rendered config actually CHANGES (no churny reloads).
  * Writes the file IN-PLACE (same inode) so the nginx bind-mount sees it
    without a container restart (a fresh-inode write would be invisible).
  * Validates with ``nginx -t`` and ROLLS BACK + skips reload on failure.
  * Never wipes the upstreams to empty (Redis blip / 0 live hubs => leave the
    running config untouched).
  * 90 s presence TTL (matches _hubs.py) is the grace window: a hub that
    briefly stops heartbeating is not dropped until it's really gone.

Hub backend IP resolution: prefer an explicit ``ip`` in the hub's presence
payload; else derive from the IP-encoded hub_id (``hub-36`` -> ``<subnet>.36``)
produced by the host-IP auto-derivation in app.py. Subnet via env.

Membership is decided by an ACTIVE ``/health`` probe, not by Redis presence
alone (RECONCILER_HEALTH_PROBE=0 disables, restoring presence-only behaviour).
Redis presence -- plus the hubs currently in nginx.conf and the
``paprika:hubs:index`` ZSET -- only supplies the CANDIDATE set; whether a
candidate goes into the upstream is settled by talking to it. This exists
because presence and reachability failed in OPPOSITE directions on 2026-07-24:
``.40/.41`` heartbeated happily while fd-exhausted (EMFILE -> accept() dead)
and ``.35-.39`` served ``/health`` 200 for 28 h while de-registered (they booted
during a Redis ``LOADING`` window, mis-derived their hub_id and never
registered). Trusting presence alone, this reconciler kept the two dead hubs
and dropped the five live ones -- "no live upstreams", a full 502 outage.
Streaks damp the flapping: a REGISTERED hub is only dropped after
``RECONCILER_DROP_STREAK`` consecutive probe failures, and an UNREGISTERED one
is only rescued after ``RECONCILER_RESCUE_STREAK`` consecutive successes.

Reuses the paprika-hub image (python + redis-py + docker CLI already inside);
needs the Docker socket (to ``docker exec <nginx> nginx -t / -s reload``) and
the deploy dir (to read/write nginx.conf) mounted.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

try:
    import redis  # redis-py, bundled in the paprika-hub image
except Exception as exc:  # pragma: no cover
    print(f"reconciler: cannot import redis: {exc}", flush=True)
    sys.exit(1)

REDIS_URL = os.environ.get("RECONCILER_REDIS_URL", "redis://10.10.50.34:6379")
NGINX_CONF = os.environ.get("RECONCILER_NGINX_CONF", "/deploy/nginx.conf")
NGINX_CONTAINER = os.environ.get("RECONCILER_NGINX_CONTAINER", "paprika-nginx-1")
HUB_PORT = os.environ.get("RECONCILER_HUB_PORT", "8100")
HUB_SUBNET = os.environ.get("RECONCILER_HUB_SUBNET", "10.10.50")
INTERVAL_S = int(os.environ.get("RECONCILER_INTERVAL_S", "20"))
HUB_TTL_S = int(os.environ.get("RECONCILER_HUB_TTL_S", "90"))
HEALTH_PROBE = os.environ.get("RECONCILER_HEALTH_PROBE", "1") not in ("0", "false", "")
HEALTH_TIMEOUT_S = float(os.environ.get("RECONCILER_HEALTH_TIMEOUT_S", "4"))
DROP_STREAK = int(os.environ.get("RECONCILER_DROP_STREAK", "3"))
RESCUE_STREAK = int(os.environ.get("RECONCILER_RESCUE_STREAK", "2"))

_HUB_INDEX_KEY = "paprika:hubs:index"  # ZSET of every hub_id ever seen (_hubs.py)
_HUB_ID_OCTET = re.compile(r"^hub-(\d{1,3})$")
_BLK_HUBS = re.compile(r"upstream hubs \{.*?\n    \}", re.DOTALL)
_BLK_STICKY = re.compile(r"upstream hubs_sticky \{.*?\n    \}", re.DOTALL)
_CONF_SERVER = re.compile(r"^\s*server\s+(\d+\.\d+\.\d+\.\d+):\d+", re.MULTILINE)


def _log(msg: str) -> None:
    print(f"reconciler: {msg}", flush=True)


def registered_ips(r) -> "set[str] | None":
    """De-duped backend IPs for hubs alive in the Redis presence registry.

    Returns None on a Redis error (caller then leaves the config untouched);
    an empty set when Redis is reachable but reports no live hubs.
    """
    now = time.time()
    ips: set[str] = set()
    try:
        keys = list(r.scan_iter(match="paprika:hubs:*", count=200))
    except Exception as exc:
        _log(f"redis scan failed: {exc}")
        return None
    for key in keys:
        k = key.decode() if isinstance(key, bytes) else str(key)
        if k.endswith(":index"):
            continue
        try:
            raw = r.get(k)
        except Exception:
            continue
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if now - float(row.get("ts") or 0) > HUB_TTL_S:
            continue  # stale presence -> treat as gone
        ip = row.get("ip")
        if not ip:
            m = _HUB_ID_OCTET.match(str(row.get("hub_id") or ""))
            if m:
                ip = f"{HUB_SUBNET}.{m.group(1)}"
        if ip:
            ips.add(str(ip))
    return ips


def indexed_ips(r) -> "set[str]":
    """Backend IPs for every hub EVER seen (``paprika:hubs:index`` ZSET).

    Candidate source only -- an entry here is ancient history until a live
    ``/health`` probe says otherwise. This is what lets a hub that lost its
    registration (2026-07-24: mis-derived hub_id after booting into a Redis
    ``LOADING`` window) be found again without a subnet scan. Random-hostname
    hub_ids don't resolve to an IP and are simply skipped."""
    out: set[str] = set()
    try:
        for m in r.zrange(_HUB_INDEX_KEY, 0, -1):
            hub_id = m.decode() if isinstance(m, bytes) else str(m)
            mo = _HUB_ID_OCTET.match(hub_id)
            if mo:
                out.add(f"{HUB_SUBNET}.{mo.group(1)}")
    except Exception:
        pass
    return out


def conf_ips(conf: str) -> "set[str]":
    """Backend IPs currently written into nginx.conf. A hub already carrying
    traffic is a candidate even if its presence row just expired -- dropping a
    hub that is visibly serving requests is exactly the 2026-07-24 mistake."""
    return set(_CONF_SERVER.findall(conf))


def probe_health(ip: str) -> bool:
    """True iff the hub at ``ip`` answers ``/health`` with a paprika-shaped
    200. Body-shape check (``status`` == ok) so an unrelated service that
    happens to listen on the hub port can't be adopted into the upstream."""
    import urllib.request

    url = f"http://{ip}:{HUB_PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
            body = resp.read(4096).decode("utf-8", "replace")
    except Exception:
        return False
    try:
        return (json.loads(body).get("status") or "") == "ok"
    except Exception:
        return False


# ip -> [consecutive_ok, consecutive_fail]; damps probe flapping across ticks.
_STREAKS: dict[str, list[int]] = {}


def _probe_all(ips: "set[str]") -> dict[str, bool]:
    """Probe every candidate CONCURRENTLY so one hung hub can't stretch the
    tick past INTERVAL_S (7 hubs x 4 s serial would)."""
    from concurrent.futures import ThreadPoolExecutor

    ordered = sorted(ips)
    if not ordered:
        return {}
    with ThreadPoolExecutor(max_workers=min(16, len(ordered))) as pool:
        return dict(zip(ordered, pool.map(probe_health, ordered)))


def decide_members(registered: "set[str]", candidates: "set[str]") -> "set[str]":
    """Settle upstream membership from presence + live probes.

    Registered + healthy  -> in.
    Registered + failing   -> out only after DROP_STREAK consecutive failures
                              (a single blip must not evict a busy hub).
    Unregistered + healthy -> in after RESCUE_STREAK consecutive successes
                              (rescues a hub whose registration broke).
    Unregistered + failing -> out.
    """
    if not HEALTH_PROBE:
        return set(registered)
    probes = _probe_all(candidates)
    for ip in list(_STREAKS):
        if ip not in probes:
            del _STREAKS[ip]  # no longer a candidate -> forget its history
    members: set[str] = set()
    for ip, ok in probes.items():
        st = _STREAKS.setdefault(ip, [0, 0])
        st[0] = st[0] + 1 if ok else 0
        st[1] = 0 if ok else st[1] + 1
        if ip in registered:
            if st[1] < DROP_STREAK:
                members.add(ip)
        elif st[0] >= RESCUE_STREAK:
            members.add(ip)
    return members


_DRAIN_KEY = "paprika:hubs:draining"


def _drained_ips(r) -> "set[str]":
    """IPs being drained right now. deploy-from-34.sh marks a hub draining
    just BEFORE it restarts it (案B), so nginx pulls that hub OUT of the
    consistent-hash ring and its pinned workers re-home to a LIVE hub instead
    of ghosting on the restarting hub (root cause of the 60->37 fleet drop,
    see worker-ghost-proxied-ws). Redis SET; deploy sets a short TTL on the key
    so a crashed/aborted deploy can't leave a hub down forever."""
    try:
        return {
            (x.decode() if isinstance(x, bytes) else str(x))
            for x in r.smembers(_DRAIN_KEY)
        }
    except Exception:
        return set()


def render(conf: str, backends: list[str], drained: "set[str] | None" = None) -> str:
    drained = drained or set()

    def _srv(b: str) -> str:
        # ``down`` removes a hub from the upstream (and from the consistent-hash
        # ring) without deleting the line, so a drained hub's workers re-home to
        # a live hub during its restart and don't ghost.
        suffix = " down" if b.split(":")[0] in drained else ""
        return f"        server {b} max_fails=3 fail_timeout=10s{suffix};"

    servers = "\n".join(_srv(b) for b in backends)
    hubs = "upstream hubs {\n" + servers + "\n        keepalive 64;\n    }"
    sticky = (
        "upstream hubs_sticky {\n        hash $worker_id consistent;\n"
        + servers
        + "\n    }"
    )
    new, n1 = _BLK_HUBS.subn(hubs, conf, count=1)
    new, n2 = _BLK_STICKY.subn(sticky, new, count=1)
    if not (n1 and n2):
        raise RuntimeError(
            f"could not locate upstream blocks (hubs={n1} hubs_sticky={n2})"
        )
    return new


def _write_inplace(path: str, content: str) -> None:
    # Preserve the inode so the nginx bind-mount sees the change without a
    # container restart (a new-inode write would be invisible to nginx).
    with open(path, "r+", encoding="utf-8") as f:
        f.seek(0)
        f.write(content)
        f.truncate()


def _nginx(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", NGINX_CONTAINER, "nginx", *args],
        capture_output=True,
        text=True,
    )


def reconcile_once(r) -> None:
    registered = registered_ips(r)
    if registered is None:
        return  # Redis error -> leave config untouched
    try:
        with open(NGINX_CONF, "r", encoding="utf-8") as f:
            cur = f.read()
    except Exception as exc:
        _log(f"read {NGINX_CONF} failed: {exc}")
        return
    # Presence is a candidate source, not the verdict: also consider the hubs
    # already in nginx.conf and every hub the index has ever seen, then let the
    # /health probe settle who actually serves traffic.
    members = decide_members(registered, registered | conf_ips(cur) | indexed_ips(r))
    backends = sorted(f"{ip}:{HUB_PORT}" for ip in members)
    if not backends:
        _log("0 live hubs reported; leaving nginx upstreams unchanged")
        return
    rescued = sorted(members - registered)
    dropped = sorted(registered - members)
    if rescued:
        _log(f"rescued unregistered but /health-OK hub(s): {rescued}")
    if dropped:
        _log(f"dropping registered but /health-DEAD hub(s): {dropped}")
    try:
        new = render(cur, backends, _drained_ips(r))
    except Exception as exc:
        _log(f"render failed: {exc}")
        return
    if new == cur:
        return  # nothing changed
    _write_inplace(NGINX_CONF, new)
    test = _nginx("-t")
    if test.returncode != 0:
        _log(f"nginx -t FAILED -> rolling back: {test.stderr.strip()[:300]}")
        _write_inplace(NGINX_CONF, cur)
        return
    reload = _nginx("-s", "reload")
    if reload.returncode == 0:
        _log(f"upstreams synced -> {backends}; nginx reloaded")
    else:
        _log(f"nginx reload FAILED: {reload.stderr.strip()[:300]}")


def main() -> int:
    _log(
        f"start redis={REDIS_URL} conf={NGINX_CONF} nginx={NGINX_CONTAINER} "
        f"port={HUB_PORT} subnet={HUB_SUBNET} interval={INTERVAL_S}s ttl={HUB_TTL_S}s "
        f"health_probe={'on' if HEALTH_PROBE else 'off'} "
        f"(timeout={HEALTH_TIMEOUT_S}s drop_streak={DROP_STREAK} "
        f"rescue_streak={RESCUE_STREAK})"
    )
    r = redis.from_url(REDIS_URL, socket_timeout=5, socket_connect_timeout=5)
    while True:
        try:
            reconcile_once(r)
        except Exception as exc:  # never die on a transient error
            _log(f"reconcile loop error: {exc}")
        time.sleep(INTERVAL_S)


def _drain_cli(action: str, ip: str) -> int:
    """``nginx_reconciler.py drain|undrain <ip>`` -- toggle a hub's drain flag
    and reconcile IMMEDIATELY (don't wait for the ~20s loop). deploy-from-34.sh
    calls this around each hub restart (案B) so the restarting hub's workers
    re-home to a live hub instead of ghosting."""
    r = redis.from_url(REDIS_URL, socket_timeout=5, socket_connect_timeout=5)
    if action == "drain":
        r.sadd(_DRAIN_KEY, ip)
        r.expire(_DRAIN_KEY, 600)  # safety: auto-clear if a deploy aborts mid-roll
        _log(f"drain {ip}: marked draining + reconciling now")
    else:
        r.srem(_DRAIN_KEY, ip)
        _log(f"undrain {ip}: cleared draining + reconciling now")
    reconcile_once(r)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] in ("drain", "undrain"):
        raise SystemExit(_drain_cli(sys.argv[1], sys.argv[2]))
    raise SystemExit(main())
