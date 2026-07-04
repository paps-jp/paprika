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
import secrets
import socket
import struct
import time as _time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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


# ----------------------------------------------------------------------------
# Parallel DNS resolver (race-pattern, no dependencies)
# ----------------------------------------------------------------------------
# OS resolver via ``socket.getaddrinfo`` tries ``/etc/resolv.conf`` nameservers
# SEQUENTIALLY -- if the first nameserver is slow or returns an empty answer,
# the OS waits a full timeout (default 5s) before falling to the next. With
# upstream public DNS flapping (observed 40% failures on javmix.tv at
# 1.1.1.1 even with no docker-DNS hop), that puts the SSRF guard's
# ``resolve_all`` into 5-40s stalls and the codegen-loop sandbox sees a
# "host does not resolve" 400 anyway.
#
# Fix: query several upstreams (1.1.1.1 / 8.8.8.8 / 9.9.9.9) IN PARALLEL via
# raw UDP and return the first non-empty answer. A single slow upstream no
# longer blocks anyone — the race finishes in whatever the fastest healthy
# resolver took (~50-300ms typical).
#
# No third-party deps (would need a container rebuild); the DNS message
# format is implemented inline (A + AAAA only, ~80 lines). Falls back to the
# OS resolver on any failure so this is purely additive — kill via
# ``PAPRIKA_DNS_PARALLEL=0`` if it ever misbehaves.
_DNS_PARALLEL_ENABLED = (
    (os.environ.get("PAPRIKA_DNS_PARALLEL") or "1").strip().lower()
    not in ("0", "false", "off", "no", "disabled")
)
_DNS_UPSTREAMS = [
    s.strip()
    for s in (
        os.environ.get("PAPRIKA_DNS_UPSTREAMS") or "1.1.1.1,8.8.8.8,9.9.9.9"
    ).split(",")
    if s.strip()
]
try:
    _DNS_PER_QUERY_TIMEOUT_S = float(
        os.environ.get("PAPRIKA_DNS_PER_QUERY_TIMEOUT_S") or 1.5
    )
except (TypeError, ValueError):
    _DNS_PER_QUERY_TIMEOUT_S = 1.5

# Small in-process TTL cache: positive hits cached 60s (most hosts repeat),
# negative cached 5s (so a 1-off flake doesn't keep rejecting good hosts but
# we don't hammer DNS for every retry of a truly-bad name). Bounded LRU-ish.
_RESOLVE_CACHE_HIT_TTL_S = 60.0
_RESOLVE_CACHE_MISS_TTL_S = 5.0
_RESOLVE_CACHE_MAX = 1024
_RESOLVE_CACHE: dict[str, tuple[float, list[str]]] = {}


def _cache_lookup(host: str) -> list[str] | None:
    ent = _RESOLVE_CACHE.get(host)
    if ent is None:
        return None
    exp, ips = ent
    if _time.monotonic() > exp:
        _RESOLVE_CACHE.pop(host, None)
        return None
    return ips


def _cache_store(host: str, ips: list[str]) -> None:
    ttl = _RESOLVE_CACHE_HIT_TTL_S if ips else _RESOLVE_CACHE_MISS_TTL_S
    _RESOLVE_CACHE[host] = (_time.monotonic() + ttl, ips)
    if len(_RESOLVE_CACHE) > _RESOLVE_CACHE_MAX:
        # Cheap "drop the oldest" eviction. Good enough for a 1024-entry cap.
        try:
            oldest = min(_RESOLVE_CACHE, key=lambda k: _RESOLVE_CACHE[k][0])
            _RESOLVE_CACHE.pop(oldest, None)
        except ValueError:
            _RESOLVE_CACHE.clear()


def _skip_dns_name(data: bytes, pos: int) -> int:
    """Advance past a DNS name (labels, optionally terminated by a 2-byte
    compression pointer). Returns the position immediately after."""
    n = len(data)
    while pos < n:
        b = data[pos]
        if b == 0:
            return pos + 1
        if (b & 0xC0) == 0xC0:  # compression pointer (2 bytes total)
            return pos + 2
        pos += 1 + b
    return pos


def _build_dns_query(host: str, qtype: int) -> tuple[bytes, int]:
    """Build a DNS query packet for A (1) or AAAA (28). Returns (packet,
    message_id). IDNA-encodes non-ASCII labels."""
    msg_id = secrets.randbelow(65536)
    flags = 0x0100  # standard query, recursion desired
    header = struct.pack(">HHHHHH", msg_id, flags, 1, 0, 0, 0)
    qname = b""
    try:
        labels = host.encode("idna")
    except (UnicodeError, UnicodeDecodeError):
        labels = host.encode("ascii", errors="replace")
    for label in labels.split(b"."):
        if not label:
            continue
        if len(label) > 63:
            label = label[:63]
        qname += bytes([len(label)]) + label
    qname += b"\x00"
    question = qname + struct.pack(">HH", qtype, 1)  # qtype, qclass=IN
    return header + question, msg_id


def _parse_dns_response(data: bytes, expected_id: int, qtype: int) -> list[str]:
    """Pull A / AAAA records out of a DNS response. Returns [] on any
    structural problem (treated as 'no answer for this resolver')."""
    if len(data) < 12:
        return []
    try:
        msg_id, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    except struct.error:
        return []
    if msg_id != expected_id:
        return []
    if (flags >> 15) != 1:  # not a response
        return []
    if (flags & 0xF) != 0:  # rcode != NOERROR -> NXDOMAIN/SERVFAIL/etc.
        return []

    pos = 12
    for _ in range(qd):
        pos = _skip_dns_name(data, pos)
        pos += 4  # qtype + qclass
    out: list[str] = []
    for _ in range(an):
        pos = _skip_dns_name(data, pos)
        if pos + 10 > len(data):
            break
        try:
            rtype, _rclass, _ttl, rdlength = struct.unpack(
                ">HHIH", data[pos:pos + 10]
            )
        except struct.error:
            break
        pos += 10
        if pos + rdlength > len(data):
            break
        rdata = data[pos:pos + rdlength]
        pos += rdlength
        if rtype == qtype:
            if qtype == 1 and len(rdata) == 4:
                out.append(".".join(str(b) for b in rdata))
            elif qtype == 28 and len(rdata) == 16:
                try:
                    out.append(str(ipaddress.IPv6Address(rdata)))
                except Exception:
                    pass
    return out


def _query_one_qtype(host: str, nameserver: str, qtype: int, timeout: float) -> list[str]:
    """Send ONE A or AAAA query to ``nameserver`` over UDP. Returns the IP
    list (may be empty); never raises. Used as the unit of work for the
    race -- N resolvers × 2 qtypes = N×2 parallel threads, so a slow AAAA
    on one resolver does not delay the A answer from another."""
    try:
        query, msg_id = _build_dns_query(host, qtype)
        af = socket.AF_INET6 if ":" in nameserver else socket.AF_INET
        with socket.socket(af, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(query, (nameserver, 53))
            deadline = _time.monotonic() + timeout
            while _time.monotonic() < deadline:
                try:
                    data, _addr = s.recvfrom(4096)
                except socket.timeout:
                    return []
                except OSError:
                    return []
                ips = _parse_dns_response(data, msg_id, qtype)
                if ips:
                    return ips
                # ID mismatch / structural fail: maybe a stale UDP packet;
                # loop until our actual reply arrives or the deadline.
        return []
    except Exception:
        return []


# After we get the FIRST non-empty answer we still wait this long for the
# rest to arrive, so the result includes A+AAAA records when they exist.
# Short enough to keep p50 latency low; long enough to absorb a single-RTT
# gap between A and AAAA from the same resolver.
_DNS_GATHER_WINDOW_S = 0.3


def _resolve_parallel(host: str, timeout: float = _DNS_PER_QUERY_TIMEOUT_S) -> list[str]:
    """Race N upstreams × {A, AAAA} (= 6 threads with the default 3 upstreams).
    Return everything that lands within the first 300ms after the fastest
    success, so the result reflects both v4 and v6 when both exist. Empty
    when every (resolver, qtype) returned empty within the deadline."""
    if not _DNS_UPSTREAMS:
        return []
    tasks = [(ns, qt) for ns in _DNS_UPSTREAMS for qt in (1, 28)]
    ex = ThreadPoolExecutor(max_workers=len(tasks))
    try:
        futs = [
            ex.submit(_query_one_qtype, host, ns, qt, timeout)
            for (ns, qt) in tasks
        ]
        pending = set(futs)
        deadline_total = _time.monotonic() + timeout + 0.5
        first_hit_at: float | None = None
        out: list[str] = []
        while pending:
            now = _time.monotonic()
            if now >= deadline_total:
                break
            if first_hit_at is not None:
                # Already have something; keep collecting only until the
                # gather window closes.
                gather_deadline = first_hit_at + _DNS_GATHER_WINDOW_S
                if now >= gather_deadline:
                    break
                slice_s = max(0.02, gather_deadline - now)
            else:
                slice_s = max(0.05, deadline_total - now)
            try:
                done, pending = wait(
                    pending, return_when=FIRST_COMPLETED, timeout=slice_s,
                )
            except Exception:
                break
            if not done:
                break
            for f in done:
                try:
                    ips = f.result() or []
                except Exception:
                    ips = []
                if ips:
                    out.extend(ips)
                    if first_hit_at is None:
                        first_hit_at = _time.monotonic()
        return _dedup_ips(out)
    finally:
        # wait=False so a slow resolver doesn't keep the caller blocked; its
        # thread will tidy itself up when the UDP timeout fires.
        ex.shutdown(wait=False)


def _dedup_ips(ips: list[str]) -> list[str]:
    """Dedupe an IP list while preserving order; strips IPv6 scope (fe80::1%eth0)."""
    seen: set[str] = set()
    out: list[str] = []
    for ip in ips:
        if "%" in ip:
            ip = ip.split("%", 1)[0]
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def _resolve_via_os(host: str, attempts: int = 2) -> list[str]:
    """Original ``socket.getaddrinfo`` path with the EAI_AGAIN retry. Used
    when parallel resolution is disabled or returned empty."""
    _eai_again = getattr(socket, "EAI_AGAIN", -3)
    infos = None
    for _i in range(max(1, attempts)):
        try:
            infos = socket.getaddrinfo(host, None)
            break
        except socket.gaierror as e:
            if getattr(e, "errno", None) != _eai_again:
                return []
            infos = None
        except Exception:
            infos = None
        if _i + 1 < attempts:
            _time.sleep(0.25)
    if not infos:
        return []
    return _dedup_ips([sockaddr[0] for *_rest, sockaddr in infos])


def resolve_all(host: str, _attempts: int = 2) -> list[str]:
    """Every IPv4 + IPv6 address ``host`` resolves to (deduped).

    Tries a *parallel* DNS race against the configured upstreams first
    (``PAPRIKA_DNS_UPSTREAMS``, default 1.1.1.1/8.8.8.8/9.9.9.9) — the first
    non-empty answer wins, which keeps a single slow / silent resolver from
    blocking the whole SSRF guard. Falls back to ``socket.getaddrinfo`` on
    failure so /etc/hosts entries + the EAI_AGAIN retry still cover edge
    cases.

    Hot-path lookups are cached in process for 60s (5s on a miss) so we
    don't fire 3 UDP queries on every job submit / navigate. Disable the
    parallel path entirely with ``PAPRIKA_DNS_PARALLEL=0`` if it ever
    misbehaves; the original OS-resolver behaviour returns.
    """
    if not host:
        return []
    cached = _cache_lookup(host)
    if cached is not None:
        return cached
    out: list[str] = []
    if _DNS_PARALLEL_ENABLED:
        try:
            out = _resolve_parallel(host)
        except Exception:
            out = []
    if not out:
        # Either parallel was disabled OR every upstream returned empty in
        # the timeout window -- fall back to the OS resolver (covers
        # /etc/hosts entries + glibc's nscd cache + any non-standard
        # nsswitch order the operator set up).
        out = _resolve_via_os(host, _attempts)
    _cache_store(host, out)
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


# Schemes that reach the LOCAL filesystem / non-web transports — always blocked
# for an in-session navigation (local file read is the worker-side SSRF/exfil
# risk; Chrome supports file://, the rest are belt-and-suspenders).
_NAV_BLOCKED_SCHEMES = frozenset(("file", "filesystem", "ftp"))


def navigation_block_reason(url: str) -> str | None:
    """SSRF gate for a WORKER in-session navigation (page.goto / agent / macro
    nav), which the hub never validated — only the INITIAL job/session URL is
    checked hub-side, so an in-session ``page.goto("http://169.254.169.254/")``
    would otherwise slip straight through to Chrome. Returns a reason to BLOCK,
    or ``None`` to allow.

    Deliberately MORE permissive than :func:`validate_public_url` (which is for
    operator-submitted URLs and whitelists http/https only): here we only block
    the genuinely dangerous cases —

      * ``file://`` / ``filesystem:`` / ``ftp:``  → local/non-web access
      * ``http(s)://`` whose host resolves to a private / loopback / link-local
        / cloud-metadata address (reuses :func:`validate_public_url`)

    and we ALLOW no-network schemes (``about:`` / ``data:`` / ``blob:`` /
    ``chrome:`` / ``chrome-extension:`` / unknown) so legitimate in-session
    navigations (e.g. ``about:blank``, inline ``data:`` renders) keep working.
    Bypassed entirely by ``PAPRIKA_ALLOW_PRIVATE_URLS=1`` (LAN fleets)."""
    if allow_private_enabled():
        return None
    if not url:
        return None
    u = url.strip()
    try:
        scheme = (urlparse(u).scheme or "").lower()
    except Exception:
        return None  # unparseable -> let the navigate call surface its own error
    if scheme in _NAV_BLOCKED_SCHEMES:
        return f"scheme {scheme!r} blocked in-session (local/file access not allowed)"
    if scheme in ("http", "https"):
        return url_block_reason(u)
    # about: / data: / blob: / chrome: / unknown -> no SSRF surface, allow.
    return None


__all__ = [
    "allow_private_enabled",
    "classify_ip",
    "resolve_all",
    "validate_public_url",
    "url_block_reason",
    "navigation_block_reason",
]
