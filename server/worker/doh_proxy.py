"""Local DNS -> DoH forwarder for the worker (2026-07-08).

Why: the worker's :53 resolver (LAN gateway + public resolvers over UDP/53)
cannot resolve a large fraction of external video-CDN hosts -- port-53 egress
to public resolvers is blocked and the LAN resolver doesn't know them. Chrome
resolves them via its own DoH (so the page plays and the ``.m3u8`` is
captured), but ``yt-dlp`` / ``httpx`` use the system resolver and fail with
``[Errno -3] Temporary failure in name resolution`` -> the video download
dies. Measured 27-170 name-resolution failures per worker per 90 min.

Fix: run a tiny forwarder on ``127.0.0.1:53`` that shuttles raw DNS wire
queries (RFC 8484 ``application/dns-message``) to a DoH endpoint over HTTPS/443
(which IS reachable). ``dns_fix.apply()`` then points ``resolv.conf`` at it
FIRST, keeping the existing resolvers as fallback -- so a forwarder hiccup only
ever degrades to the pre-fix behaviour. Raw-passthrough: the forwarder never
parses/rewrites the DNS payload (only the 2-byte transaction id on a cache
hit), so it cannot mis-resolve -- it returns exactly what the DoH server said.

Runs in a DAEMON THREAD with its own event loop, isolated from the worker's
main asyncio loop (so a main-loop stall never stalls DNS, and vice-versa).

Env:
  * ``PAPRIKA_DOH_URL``       DoH endpoint (default ``https://1.1.1.1/dns-query``)
  * ``PAPRIKA_DOH_CACHE_TTL`` cache seconds (default 60)
  * disable via ``PAPRIKA_DOH_FORWARDER=off`` (handled in dns_fix.apply()).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading

log = logging.getLogger("paprika.worker.doh_proxy")

_DOH_URL = os.environ.get("PAPRIKA_DOH_URL") or "https://1.1.1.1/dns-query"
_LISTEN_HOST = "127.0.0.1"
_LISTEN_PORT = 53
try:
    _CACHE_TTL = float(os.environ.get("PAPRIKA_DOH_CACHE_TTL") or 60.0)
except (TypeError, ValueError):
    _CACHE_TTL = 60.0

_started = False


def _doh_reachable() -> bool:
    """Sync pre-flight: is the DoH endpoint actually answering? Used so we only
    point resolv.conf at the forwarder when DoH works (else it would just add
    a resolve-timeout before the fallback resolvers kick in)."""
    try:
        import httpx
        with httpx.Client(timeout=4.0, verify=False) as c:
            r = c.get(
                _DOH_URL,
                params={"name": "cloudflare.com", "type": "A"},
                headers={"accept": "application/dns-json"},
            )
        return r.status_code == 200 and bool((r.json() or {}).get("Answer"))
    except Exception:
        return False


class _Forwarder(asyncio.DatagramProtocol):
    """UDP :53 -> DoH POST. Caches the raw response keyed on the query's
    question section; on a cache hit only the 2-byte transaction id is
    rewritten to match the new query."""

    def __init__(self, client):
        self._client = client
        self._cache: dict[bytes, tuple[bytes, float]] = {}
        self._transport = None

    def connection_made(self, transport):
        self._transport = transport

    def datagram_received(self, data, addr):
        if len(data) < 12 or self._transport is None:
            return
        asyncio.ensure_future(self._resolve(bytes(data), addr))

    async def _resolve(self, data: bytes, addr) -> None:
        loop = asyncio.get_running_loop()
        key = data[12:]  # question (+ any EDNS OPT); id/flags are bytes 0-12
        now = loop.time()
        hit = self._cache.get(key)
        if hit is not None and hit[1] > now:
            body = hit[0]
        else:
            try:
                r = await self._client.post(
                    _DOH_URL,
                    content=data,
                    headers={
                        "content-type": "application/dns-message",
                        "accept": "application/dns-message",
                    },
                )
            except Exception:
                # Silent: getaddrinfo falls through to the fallback resolvers
                # still in resolv.conf, so a DoH blip never breaks resolution.
                return
            if r.status_code != 200 or not r.content:
                return
            body = r.content
            self._cache[key] = (body, now + _CACHE_TTL)
            if len(self._cache) > 4096:
                for k, v in list(self._cache.items()):
                    if v[1] <= now:
                        self._cache.pop(k, None)
        # Always stamp the caller's transaction id onto the reply: DoH echoes
        # it on a miss, but a cache hit reuses another query's response, so
        # re-stamping uniformly is both correct and defensive.
        resp = bytearray(body)
        resp[0:2] = data[0:2]
        try:
            self._transport.sendto(bytes(resp), addr)
        except Exception:
            pass


def _thread_main(sock: socket.socket) -> None:
    try:
        import httpx
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = httpx.AsyncClient(timeout=5.0, verify=False, http2=False)

        async def _run():
            await loop.create_datagram_endpoint(
                lambda: _Forwarder(client), sock=sock,
            )
            while True:
                await asyncio.sleep(3600)

        loop.run_until_complete(_run())
    except Exception as e:  # pragma: no cover - defensive
        log.warning("doh_proxy: thread exited (%s: %s)", type(e).__name__, e)


def start_doh_forwarder() -> bool:
    """Bind ``127.0.0.1:53`` and run the DoH forwarder in a daemon thread.

    Returns True ONLY when the forwarder is up AND DoH is reachable (so the
    caller can safely point resolv.conf at it). Idempotent; never raises."""
    global _started
    if _started:
        return True
    try:
        if not _doh_reachable():
            log.warning("doh_proxy: DoH %s not reachable — not starting", _DOH_URL)
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((_LISTEN_HOST, _LISTEN_PORT))
        sock.setblocking(False)
    except Exception as e:
        log.warning(
            "doh_proxy: bind %s:%d failed (%s: %s)",
            _LISTEN_HOST, _LISTEN_PORT, type(e).__name__, e,
        )
        return False
    _started = True
    threading.Thread(
        target=_thread_main, args=(sock,), name="doh-forwarder", daemon=True,
    ).start()
    log.info("doh_proxy: forwarding %s:%d -> %s", _LISTEN_HOST, _LISTEN_PORT, _DOH_URL)
    return True
