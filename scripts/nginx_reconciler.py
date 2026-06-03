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

_HUB_ID_OCTET = re.compile(r"^hub-(\d{1,3})$")
_BLK_HUBS = re.compile(r"upstream hubs \{.*?\n    \}", re.DOTALL)
_BLK_STICKY = re.compile(r"upstream hubs_sticky \{.*?\n    \}", re.DOTALL)


def _log(msg: str) -> None:
    print(f"reconciler: {msg}", flush=True)


def live_backends(r) -> list[str] | None:
    """Sorted, de-duped ``<ip>:<port>`` for hubs alive in Redis.

    Returns None on a Redis error (caller then leaves the config untouched);
    an empty list when Redis is reachable but reports no live hubs.
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
    return sorted(f"{ip}:{HUB_PORT}" for ip in ips)


def render(conf: str, backends: list[str]) -> str:
    servers = "\n".join(
        f"        server {b} max_fails=3 fail_timeout=10s;" for b in backends
    )
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
    backends = live_backends(r)
    if backends is None:
        return  # Redis error -> leave config untouched
    if not backends:
        _log("0 live hubs reported; leaving nginx upstreams unchanged")
        return
    try:
        with open(NGINX_CONF, "r", encoding="utf-8") as f:
            cur = f.read()
    except Exception as exc:
        _log(f"read {NGINX_CONF} failed: {exc}")
        return
    try:
        new = render(cur, backends)
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
        f"port={HUB_PORT} subnet={HUB_SUBNET} interval={INTERVAL_S}s ttl={HUB_TTL_S}s"
    )
    r = redis.from_url(REDIS_URL, socket_timeout=5, socket_connect_timeout=5)
    while True:
        try:
            reconcile_once(r)
        except Exception as exc:  # never die on a transient error
            _log(f"reconcile loop error: {exc}")
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    raise SystemExit(main())
