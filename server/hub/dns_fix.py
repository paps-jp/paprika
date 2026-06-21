"""Hub DNS fixup — bypass docker's flaky embedded resolver (127.0.0.11).

Sibling of ``server/worker/dns_fix.py``. The hub container also runs on
docker's user-defined network, so its ``/etc/resolv.conf`` points at the
embedded resolver ``127.0.0.11`` — which intermittently HANGS on COLD
lookups for ~4-16s and returns nothing. That trips ``core/ssrf_guard.py``'s
pre-session ``resolve_all`` retry-and-give-up path with::

    POST /sessions HTTP 400:
      {"detail": "host 'X' does not resolve to any address"}

which surfaces in codegen-loop sandbox stderr as a ``paprika_client._client.
PaprikaError`` traceback (caught case 2026-06-17: job f8a9aa803021 calling
www.wmmcv.cc — the parent fetch on the worker side succeeded because workers
already bypass 127.0.0.11 via ``server/worker/dns_fix.py``, but the hub-side
SSRF guard fell into the docker-DNS stall).

``apply()`` rewrites ``/etc/resolv.conf`` to query the configured nameservers
directly, and ONLY when ``127.0.0.11`` is the current resolver (so a hub
running on a real LAN resolver is left untouched). The hub talks to MariaDB,
Redis, and MinIO by IP (or by LAN name resolved at hub start via a separate
configuration), so it does not need docker-side service-name resolution.
``search`` / ``options`` lines are preserved.

Deployed as hub source (``server/hub/``) so it ships via the .34-SoT graceful
rollout — no direct hub-container modification (CLAUDE.md absolute rule).

Controlled by env ``PAPRIKA_HUB_DNS``:
  * unset / empty -> default ``["1.1.1.1", "8.8.8.8"]``
  * ``"off"`` / ``"0"`` / ``"false"`` -> disabled (no-op)
  * ``"1.1.1.1,8.8.8.8,10.10.50.1"`` -> use exactly those (append the LAN
    gateway last if some deployment DOES need docker-name resolution)
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("paprika.hub.dns_fix")

_RESOLV = "/etc/resolv.conf"
_DEFAULT = ["1.1.1.1", "8.8.8.8"]
_DISABLE = {"off", "0", "false", "no", "disable", "disabled"}


def apply(path: str = _RESOLV) -> bool:
    """Rewrite ``path`` to bypass docker's embedded DNS. Returns True if it
    rewrote the file, False otherwise. Best-effort: never raises (a DNS
    cosmetic must not crash hub startup)."""
    try:
        cfg = (os.environ.get("PAPRIKA_HUB_DNS") or "").strip()
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
            ["# paprika hub dns_fix: bypass docker embedded DNS 127.0.0.11"]
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
