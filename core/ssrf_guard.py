"""Shared SSRF guard — usable by BOTH the hub (POST /jobs, /sessions) and
the worker (pre-navigate / pre-fetch, the authoritative check).

Phase 3 of moving paprika off its "trusted LAN" premise. The hub validates
submitted URLs up front (``server/hub/url_safety.py`` delegates here), but that
check is TOCTOU-vulnerable: DNS can rebind between the hub's resolve and the
worker's actual connect, and redirects can hop to an internal address the hub
never saw. So the worker re-validates at navigate time using the SAME logic,
forming defense-in-depth with the (opt-in) iptables egress firewall.

What a submitter could otherwise make a worker Chrome dial:
  * Private RFC1918 / ULA (fc00::/7) -> probe internal services / other hubs
  * Cloud metadata (169.254.169.254, link-local) -> steal instance creds
  * Loopback (127/8, ::1) -> reach paprika's own admin UI from "inside"
  * link-local / multicast / reserved / unspecified -> niche but bad
  * file:// / javascript: / data: / ftp: -> sandbox escape (scheme whitelist)

stdlib only (ipaddress / socket / os / urllib) so the worker — which imports
``core.*`` but not ``server.hub.*`` — can use it without pulling FastAPI.

Bypass: ``PAPRIKA_ALLOW_PRIVATE_URLS=1`` (default OFF → a public hub is safe
out of the box; set it only when intentionally crawling a trusted LAN).
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Iterable
from urllib.parse import urlparse

# Whitelist of schemes that may reach the network. Everything else
# (file://, javascript:, data:, ftp:, about:* besides about:blank) is refused.
_ALLOWED_SCHEMES = frozenset(("http", "https"))


def allow_private_enabled() -> bool:
    """Whether the operator opted into private-URL access globally
    (``PAPRIKA_ALLOW_PRIVATE_URLS=1``). Read live so a worker restart isn't
    needed to flip it."""
    val = os.environ.get("PAPRIKA_ALLOW_PRIVATE_URLS", "0").strip().lower()
    return val in ("1", "true", "yes", "on")


def classify_ip(ip: "ipaddress._BaseAddress | str") -> str:
    """Return a short, operator-friendly label if ``ip`` is in a
    non-publicly-routable range that must NOT be fetched, else ``""`` (safe).

    Accepts an ``ipaddress`` object or a string; an unparseable string is
    treated as unsafe ("unparseable") rather than silently allowed."""
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip.strip().strip("[]"))
        except ValueError:
            return "unparseable address"
    if ip.is_loopback:
        return "loopback (127.0.0.0/8, ::1)"
    if ip.is_link_local:
        # 169.254.0.0/16 (covers AWS/Azure/GCP IMDS 169.254.169.254), fe80::/10
        return "link-local (incl. cloud metadata 169.254.169.254)"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified (0.0.0.0, ::)"
    if ip.is_reserved:
        return "reserved"
    if ip.is_private:
        # 10/8, 172.16/12, 192.168/16, fc00::/7 (ULA)
        return "private (RFC1918 / ULA)"
    return ""


def resolve_all(host: str) -> list[str]:
    """Every IPv4 + IPv6 address ``host`` resolves to (deduped), via
    ``socket.getaddrinfo`` so /etc/hosts + OS policy match what Chrome sees.
    AAAA records are included (IPv6 private ranges are equally attackable)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for *_rest, sockaddr in infos:
        ip = sockaddr[0]
        if "%" in ip:  # strip IPv6 scope (fe80::1%eth0 -> fe80::1)
            ip = ip.split("%", 1)[0]
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def validate_public_url(url: str) -> tuple[bool, str]:
    """``(ok, reason)`` for whether ``url`` is safe to dispatch / navigate.

    Order: bypass env → about:blank allow → parseable → scheme whitelist →
    host present → numeric-literal range check → resolve ALL records + check
    each (any private record rejects: defends split-horizon DNS). ``reason``
    is the operator-facing rejection string when ``ok`` is False."""
    if allow_private_enabled():
        return True, ""
    if not url:
        return False, "empty URL"
    # about:blank cannot reach the network (zero SSRF surface). Only the
    # exact literal — about:config and friends stay rejected by the scheme
    # whitelist below.
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
            f"may be fetched (got {url[:120]!r})"
        )

    host = parsed.hostname
    if not host:
        return False, f"no host component in URL {url[:120]!r}"
    h = host.strip("[]")  # IPv6 literals arrive bracketed

    # Numeric literal: check directly (catches http://10.0.0.5/ etc.).
    try:
        ipaddress.ip_address(h)
        cls = classify_ip(h)
        if cls:
            return False, (
                f"refusing to fetch a {cls} address ({h}); set "
                f"PAPRIKA_ALLOW_PRIVATE_URLS=1 if this is intentional"
            )
        return True, ""
    except ValueError:
        pass  # hostname -> resolve

    addrs = resolve_all(h)
    if not addrs:
        return False, f"host {h!r} does not resolve to any address"
    for a in addrs:
        cls = classify_ip(a)
        if cls:
            return False, (
                f"host {h!r} resolves to a {cls} address ({a}); refusing to "
                f"fetch. Set PAPRIKA_ALLOW_PRIVATE_URLS=1 on the hub if this "
                f"is intentional."
            )
    return True, ""


def url_block_reason(url: str) -> str | None:
    """Worker-friendly form of :func:`validate_public_url`: the rejection
    reason string, or ``None`` when the URL is safe. Lets the worker's
    pre-navigate hook do ``if (r := url_block_reason(url)): return f"ERR: {r}"``
    without unpacking a tuple."""
    ok, reason = validate_public_url(url)
    return None if ok else reason


__all__ = [
    "allow_private_enabled",
    "classify_ip",
    "resolve_all",
    "validate_public_url",
    "url_block_reason",
]
