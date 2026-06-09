"""Worker DNS fixup — bypass docker's flaky embedded resolver (127.0.0.11).

The worker fleet runs on a docker user-defined (compose) network, so the
container's ``/etc/resolv.conf`` points at docker's embedded DNS
``127.0.0.11``. Forwarding through it to the LAN gateway intermittently
HANGS on COLD lookups: measured ~10-20% of fresh hostnames stalling 4-16s
before ``getaddrinfo`` gives up and returns nothing. That:

  * tripped the SSRF guard's pre-navigate resolve (``core/ssrf_guard.py``)
    -> ``host 'X' does not resolve to any address`` (HTTP 400) — the
    failures ``crawl_movie.py`` hit against monsnode.com; and
  * slowed Chrome's OWN navigation (Chrome resolves via the same path).

Diagnosis (2026-06-09): the spikes are NOT load and NOT the upstream —
querying ``127.0.0.11`` while pointing it at 1.1.1.1/8.8.8.8 STILL spiked
~20%, but querying public resolvers DIRECTLY (no 127.0.0.11) measured ZERO
cold spikes. So the embedded resolver itself is the bottleneck; the fix is
to not go through it.

``apply()`` rewrites ``/etc/resolv.conf`` to query the configured
nameservers directly, and ONLY when ``127.0.0.11`` is the current resolver
(so a worker on a real resolver is left untouched). The worker reaches the
hub / agent by IP (``HUB_URL=ws://<ip>:8000``), so it needs no LAN-name
resolution; public DNS covers target-site lookups. ``search`` / ``options``
lines are preserved.

Deployed as worker source (``server/worker/``) so it ships via the .34-SoT
graceful self-update — no direct worker-container modification (CLAUDE.md).

Controlled by env ``PAPRIKA_WORKER_DNS``:
  * unset / empty -> default ``["1.1.1.1", "8.8.8.8"]``
  * ``"off"`` / ``"0"`` / ``"false"`` -> disabled (no-op)
  * ``"1.1.1.1,8.8.8.8,10.10.50.1"`` -> use exactly those (e.g. append the
    gateway last if some deployment DOES need LAN-name resolution)
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("paprika.worker.dns_fix")

_RESOLV = "/etc/resolv.conf"
_DEFAULT = ["1.1.1.1", "8.8.8.8"]
_DISABLE = {"off", "0", "false", "no", "disable", "disabled"}


def apply(path: str = _RESOLV) -> bool:
    """Rewrite ``path`` to bypass docker's embedded DNS. Returns True if it
    rewrote the file, False otherwise. Best-effort: never raises (a DNS
    cosmetic must not crash worker startup)."""
    try:
        cfg = (os.environ.get("PAPRIKA_WORKER_DNS") or "").strip()
        if cfg.lower() in _DISABLE:
            return False
        servers = [s.strip() for s in cfg.split(",") if s.strip()] or _DEFAULT

        try:
            with open(path) as f:
                cur = f.read()
        except OSError:
            return False  # no resolv.conf (not a container) -> leave alone

        if "127.0.0.11" not in cur:
            return False  # not on docker's embedded resolver -> nothing to bypass

        # Preserve search / options lines; replace only the nameserver lines.
        keep = [
            ln for ln in cur.splitlines()
            if ln.strip()
            and not ln.lstrip().startswith("nameserver")
            and not ln.lstrip().startswith("#")
        ]
        new = "\n".join(
            ["# paprika worker dns_fix: bypass docker embedded DNS 127.0.0.11"]
            + [f"nameserver {s}" for s in servers]
            + keep
        ) + "\n"

        with open(path, "w") as f:
            f.write(new)
        log.info("dns_fix: resolv.conf nameservers -> %s (bypassed 127.0.0.11)", servers)
        return True
    except Exception as e:  # pragma: no cover - defensive
        log.warning("dns_fix: skipped (%s: %s)", type(e).__name__, e)
        return False
