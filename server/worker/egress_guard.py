"""Phase 3 E (Approach B): self-maintaining, self-contained worker egress firewall.

At worker startup — BEFORE any Chrome lane spawns — install an iptables OUTPUT
firewall that DROPs RFC1918 / cloud-metadata / loopback / CGNAT (+ IPv6 ULA/
link-local) so a redirect / fetch() / window.location to a private IP is dropped
at the kernel, catching what the hub + nav-time app-layer SSRF checks can't. The
worker's own hub plus every other hub (fetched live from /fleet/egress-allow) and
DNS stay allowed, so legitimate traffic is unaffected.

SELF-CONTAINED: the rules are applied directly via the ``iptables`` binary from
this module — NOT by shelling out to a baked image script. The prod worker images
are heterogeneous (many predate docker/worker/egress-firewall.sh), so depending on
that script left most workers unprotected (canary, 2026-06-08: "script missing
from image"). Applying the rules here ships the whole firewall via the normal
zero-downtime worker self-update (server/worker/*) — NO image rebuild, works on
every worker regardless of image age. The worker runs as root in-container with
CAP_NET_ADMIN + iptables (confirmed in prod).

Enable: ``PAPRIKA_EGRESS_GUARD=1`` (separate from the legacy
``PAPRIKA_WORKER_EGRESS_FIREWALL`` so this is the single authoritative applier).
Default off = behavioural no-op.

Ordering (fixes the canary startup race): apply the bootstrap firewall allowing
our own hub FIRST — protects immediately AND guarantees the hub is reachable for
the allowlist fetch — THEN fetch /fleet/egress-allow and ADD the other hub IPs on
top via insert (no flush, no window). If the fetch fails the bootstrap firewall
stands: functionally sufficient since all worker infra traffic goes via the nginx
front anyway.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Private / non-routable ranges the worker must NOT reach (SSRF). Mirrors
# docker/worker/egress-firewall.sh + core/ssrf_guard.classify_ip.
_DROP_CIDRS = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16",  # link-local incl. cloud metadata 169.254.169.254
    "127.0.0.0/8", "100.64.0.0/10",
)
_DROP_CIDRS6 = ("fc00::/7", "fe80::/10", "::1/128")


def _enabled() -> bool:
    return (os.environ.get("PAPRIKA_EGRESS_GUARD", "0").strip().lower()
            in ("1", "true", "yes", "on"))


def _hub_http_and_host(hub_ws_url: str) -> tuple[str, str]:
    """``ws://10.10.50.34:8000`` -> (``http://10.10.50.34:8000``, ``10.10.50.34``)."""
    u = urlparse(hub_ws_url or "")
    host = u.hostname or ""
    scheme = "https" if u.scheme in ("wss", "https") else "http"
    port = f":{u.port}" if u.port else ""
    return (f"{scheme}://{host}{port}" if host else ""), host


def _ipt(*args: str, v6: bool = False) -> int:
    """Run one iptables/ip6tables command. Returns rc; logs (not raises) on error."""
    cmd = "ip6tables" if v6 else "iptables"
    try:
        p = subprocess.run([cmd, *args], capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            log.warning("egress-guard: %s %s -> rc=%s %s",
                        cmd, " ".join(args), p.returncode, (p.stderr or "").strip()[:160])
        return p.returncode
    except Exception as e:
        log.warning("egress-guard: %s %s failed: %s", cmd, " ".join(args), e)
        return 1


def _have_netadmin() -> bool:
    """Probe: can we manage iptables? (CAP_NET_ADMIN + binary present)."""
    try:
        p = subprocess.run(["iptables", "-L", "OUTPUT", "-n"],
                           capture_output=True, text=True, timeout=10)
        return p.returncode == 0
    except Exception:
        return False


def _apply_rules(allow_ips: "set[str]") -> None:
    """Flush + rebuild the OUTPUT firewall: ACCEPT lo / ESTABLISHED / docker DNS /
    DNS(53) / allow_ips, DROP the private CIDRs, policy ACCEPT (public allowed).

    DNS is allowed broadly (udp/tcp 53 to any) because the container resolves via
    docker's 127.0.0.11 which FORWARDS to the LXC upstream resolver (a private IP
    in 10/8); that forward traverses OUTPUT and would otherwise hit the DROP 10/8
    rule, breaking all name resolution (canary, 2026-06-08). HTTP-to-private stays
    blocked, so the SSRF protection is intact (DNS can't fetch internal services)."""
    # ---- IPv4 ----
    _ipt("-F", "OUTPUT")
    _ipt("-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    _ipt("-A", "OUTPUT", "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT")
    _ipt("-A", "OUTPUT", "-d", "127.0.0.11", "-j", "ACCEPT")  # docker embedded DNS
    for proto in ("udp", "tcp"):
        _ipt("-A", "OUTPUT", "-p", proto, "--dport", "53", "-j", "ACCEPT")
    for ip in sorted(allow_ips):
        if ip and ":" not in ip:
            _ipt("-A", "OUTPUT", "-d", ip, "-j", "ACCEPT")
    for cidr in _DROP_CIDRS:
        _ipt("-A", "OUTPUT", "-d", cidr, "-j", "DROP")
    _ipt("-P", "OUTPUT", "ACCEPT")
    # ---- IPv6 (best-effort; skip if unusable, e.g. IPv6 disabled) ----
    try:
        probe = subprocess.run(["ip6tables", "-L", "OUTPUT", "-n"],
                               capture_output=True, text=True, timeout=10)
        if probe.returncode == 0:
            _ipt("-F", "OUTPUT", v6=True)
            _ipt("-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT", v6=True)
            _ipt("-A", "OUTPUT", "-m", "state", "--state", "ESTABLISHED,RELATED",
                 "-j", "ACCEPT", v6=True)
            for ip in sorted(allow_ips):
                if ip and ":" in ip:
                    _ipt("-A", "OUTPUT", "-d", ip, "-j", "ACCEPT", v6=True)
            for cidr in _DROP_CIDRS6:
                _ipt("-A", "OUTPUT", "-d", cidr, "-j", "DROP", v6=True)
            _ipt("-P", "OUTPUT", "ACCEPT", v6=True)
    except Exception:
        pass


def _fetch_allow(hub_http: str) -> set[str]:
    """Fetch the hub's egress allowlist (one IP/CIDR per line). Empty on failure.

    Retries with a short delay. By the time this runs the bootstrap firewall
    already allows our hub, so attempt 1 normally succeeds."""
    out: set[str] = set()
    if not hub_http:
        return out
    try:
        import httpx
    except Exception:
        return out
    for attempt in range(1, 5):
        try:
            r = httpx.get(f"{hub_http}/fleet/egress-allow", timeout=6.0)
            if r.status_code == 200:
                for ln in r.text.splitlines():
                    ln = ln.strip()
                    if ln:
                        out.add(ln)
                return out
            log.warning("egress-guard: /fleet/egress-allow -> HTTP %s (attempt %d)",
                        r.status_code, attempt)
        except Exception as e:
            log.warning("egress-guard: allowlist fetch attempt %d failed: %s", attempt, e)
        if attempt < 4:
            try:
                time.sleep(2)
            except Exception:
                pass
    return out


def _insert_accept(ip: str) -> None:
    """Insert an ACCEPT for ``ip`` at the TOP of OUTPUT (above the DROP block),
    WITHOUT a flush — adds a fetched hub IP on top of the active firewall with no
    open window. IPv6 literals go to ip6tables."""
    if not ip:
        return
    if ":" in ip:
        _ipt("-I", "OUTPUT", "1", "-d", ip, "-j", "ACCEPT", v6=True)
    else:
        _ipt("-I", "OUTPUT", "1", "-d", ip, "-j", "ACCEPT")


def apply(hub_ws_url: str) -> None:
    """Apply the worker egress firewall. No-op unless ``PAPRIKA_EGRESS_GUARD=1``."""
    if not _enabled():
        return
    if not _have_netadmin():
        log.warning("egress-guard: iptables/OUTPUT not manageable (missing "
                    "CAP_NET_ADMIN or iptables binary); firewall NOT applied")
        return
    hub_http, hub_host = _hub_http_and_host(hub_ws_url)
    # 1) Bootstrap: protect now + guarantee the hub is reachable for the fetch.
    bootstrap = {hub_host} if hub_host else set()
    _apply_rules(bootstrap)
    # 2) Fetch the full allowlist THROUGH the bootstrap firewall (hub allowed).
    fetched = _fetch_allow(hub_http)
    # 3) Add extra infra IPs on top (insert, no flush). Bootstrap stands if none.
    extra = sorted(ip for ip in fetched if ip and ip not in bootstrap)
    for ip in extra:
        _insert_accept(ip)
    if extra:
        log.info("egress-guard: firewall applied (bootstrap=%s + fetched=%s)",
                 sorted(bootstrap), extra)
    else:
        log.warning("egress-guard: firewall applied BOOTSTRAP-ONLY (allow=%s); "
                    "allowlist fetch returned nothing extra", sorted(bootstrap))
