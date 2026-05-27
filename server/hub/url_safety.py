"""Public-URL safety validator for SSRF protection.

The hub accepts URLs from operators / public submitters (POST /jobs,
POST /sessions, page.goto inside scripts via /sessions/{id}/...) and
hands them to a worker Chrome. Without protection, a submitter can
make the worker dial:

  * Private RFC1918 ranges -> attacker probes internal services
    (other paprika hubs, internal admin panels, etc.)
  * Cloud-metadata endpoints (169.254.169.254, fd00:ec2:...) -> read
    AWS instance role credentials
  * Loopback (127.0.0.0/8, ::1) -> reach paprika's own admin UI from
    inside the network where it usually believes itself safe
  * Link-local / multicast / broadcast -> niche but bad
  * file:// / javascript: / data: -> trivial sandbox escape

This module resolves the host once and validates every resolved IP.
DNS rebinding (server returns a public IP first, a private one on the
second resolution) is partially mitigated by the worker-side egress
firewall (iptables DROP for private CIDRs); together they form
defense in depth.

Set ``PAPRIKA_ALLOW_PRIVATE_URLS=1`` in the hub env to disable the
check entirely. Useful when operating a private fleet on a LAN where
you genuinely want to fetch ``http://10.0.0.5/`` -- but the default
is OFF so a public hub is safe out of the box.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Iterable, Tuple
from urllib.parse import urlparse


# Schemes accepted. Anything else is rejected outright (file://,
# javascript:, data:, ftp:// etc.). The blocklist would be too long;
# whitelist is simpler.
_ALLOWED_SCHEMES = frozenset(("http", "https"))


# Per-network reasons mapped via subnet check. Loopback / private /
# link-local / multicast all get a short, operator-friendly label
# instead of raw CIDR so the rejection message is readable.
def _classify(ip: ipaddress._BaseAddress) -> str:
    if ip.is_loopback:
        return "loopback (127.0.0.0/8, ::1)"
    if ip.is_link_local:
        # 169.254.0.0/16 (covers AWS/Azure/GCP IMDS), fe80::/10
        return "link-local (incl. cloud metadata 169.254.169.254)"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified (0.0.0.0, ::)"
    if ip.is_reserved:
        return "reserved"
    if ip.is_private:
        # 10/8, 172.16/12, 192.168/16, fc00::/7
        return "private (RFC1918 / ULA)"
    return ""


def _all_resolutions(host: str) -> Iterable[str]:
    """Yield every IPv4 + IPv6 address ``host`` resolves to.

    Uses ``socket.getaddrinfo`` so /etc/hosts overrides and OS-level
    resolution policies are respected the same way the worker's
    Chrome would see them. AAAA records are included because IPv6
    private ranges (fc00::/7 etc.) are equally attack-worthy.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for _, _, _, _, sockaddr in infos:
        ip = sockaddr[0]
        # Strip IPv6 scope (fe80::1%eth0 -> fe80::1)
        if "%" in ip:
            ip = ip.split("%", 1)[0]
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def _allow_private_enabled() -> bool:
    """Whether the operator opted into private-URL access globally."""
    val = os.environ.get("PAPRIKA_ALLOW_PRIVATE_URLS", "0").strip().lower()
    return val in ("1", "true", "yes", "on")


def validate_public_url(url: str) -> Tuple[bool, str]:
    """Return ``(ok, reason)`` for whether ``url`` is safe to dispatch.

    * ``ok=True``  → URL is OK or the operator disabled the check.
    * ``ok=False`` → call the failure ``reason`` back to the submitter
      via the HTTP status text.

    Rules applied (in order):
      1. Scheme must be ``http`` or ``https``.
      2. Host must be present.
      3. Host must NOT be a numeric literal in a private range -- this
         catches the dumb case where the submitter writes
         ``http://10.0.0.5/`` directly.
      4. The host's DNS resolution must NOT include any private IP --
         every resolved IPv4 + IPv6 record is checked. If ANY record
         is private, the URL is rejected (defends against split-horizon
         DNS where a public CNAME points at an internal A record).

    Operators who legitimately need to crawl LAN services can set
    ``PAPRIKA_ALLOW_PRIVATE_URLS=1`` on the hub.
    """
    if _allow_private_enabled():
        return True, ""

    if not url:
        return False, "empty URL"
    # ``about:blank`` is a built-in browser scheme that cannot reach
    # the network, so it has zero SSRF surface. Whitelist it so
    # callers (e.g. session(initial_url="about:blank") to open a tab
    # without navigating anywhere) work without setting
    # PAPRIKA_ALLOW_PRIVATE_URLS=1. Only the literal ``about:blank``
    # is allowed -- ``about:config`` and friends stay rejected.
    if url.strip().lower() == "about:blank":
        return True, ""
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"unparseable URL: {e}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return False, (
            f"scheme {scheme!r} not allowed -- only http and https "
            f"may be submitted (got {url[:120]!r})"
        )

    host = parsed.hostname
    if not host:
        return False, f"no host component in URL {url[:120]!r}"

    # Strip brackets from IPv6 literals so ipaddress can parse.
    h = host.strip("[]")

    # Case 1: host is a numeric literal. Check it directly.
    try:
        ip = ipaddress.ip_address(h)
        cls = _classify(ip)
        if cls:
            return False, (
                f"refusing to fetch a {cls} address ({ip}); set "
                f"PAPRIKA_ALLOW_PRIVATE_URLS=1 if this is intentional"
            )
        return True, ""
    except ValueError:
        # Not a literal -- fall through to DNS resolution.
        pass

    # Case 2: hostname. Resolve all addresses + check each.
    addrs = list(_all_resolutions(h))
    if not addrs:
        return False, f"host {h!r} does not resolve to any address"
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        cls = _classify(ip)
        if cls:
            return False, (
                f"host {h!r} resolves to a {cls} address ({a}); "
                f"refusing to fetch. Set PAPRIKA_ALLOW_PRIVATE_URLS=1 "
                f"on the hub if this is intentional."
            )
    return True, ""


def assert_public_url(url: str) -> None:
    """Convenience wrapper for route handlers: raises HTTPException(400)
    instead of returning a tuple. Imports HTTPException lazily so this
    module stays usable from non-FastAPI callers (e.g. CLI / tests)."""
    ok, reason = validate_public_url(url)
    if not ok:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=reason)
