"""paprika-proxy-fetch adapter — proxied HTTP GET for IP-banned hosts.

Fourth strategy in paprika's Cloudflare bake-off:

    1. cloudscraper / curl-cffi      pure-Python, fast      -- defeated by modern CF
    2. paprika-flare                  Worker Chrome           -- clears JS challenges
    3. paprika-flare + vision agent   Worker Chrome + click   -- clears Turnstile
    4. paprika-proxy-fetch (THIS)     httpx via proxy         -- bypasses IP bans

The pure-Worker strategies (#2, #3) all share the same egress IP pool
(the LAN NAT). Some hosts (aoxx69.net, asianscreens.com, maitun.net at
the time of this writing) blanket-block that range with Cloudflare 1020
"Access denied" -- there is no challenge to solve, the request never
reaches the JS layer. The only fix is to swap the egress IP.

This adapter does the minimum that's actually useful for that case:
issues a single browser-shaped GET through a configurable proxy and
returns whatever the response set as cookies, plus a short body excerpt
so the operator can confirm whether the proxy got through.

The cookies returned here are IP-bound to the *proxy's* exit IP, not
the Worker's -- so they are NOT safe to inject into a subsequent Worker
job. The pre-flight dispatcher in routes/jobs.py uses this plugin's
``cookies`` only when the host's HostKnowledge declares subtype=
ip_banned, and even then it primarily reads the response to decide
"yes this host is reachable via proxy" / "no, we're truly blocked".

For a host that needs BOTH proxy egress AND a real-Chrome JS-challenge
clear, the right design is to give the Worker Chrome a proxy of its
own (browser-level proxy, not just an httpx-level proxy). That's a
separate piece of infrastructure outside this plugin's scope.
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import httpx


# Browser-shape headers. Kept close to a recent Chrome on Linux so we
# don't trip the trivial UA-based blocklists.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _redact(proxy: str) -> str:
    """Strip user:pass from a proxy URL before we put it in audit logs / responses."""
    if not proxy:
        return ""
    try:
        u = urlparse(proxy)
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        return f"{u.scheme}://{host}{port}"
    except Exception:
        return "<unparseable>"


def get_cookies(
    *,
    url: str,
    proxy: str | None = None,
    headers: dict | None = None,
    timeout_s: int = 30,
    verify: bool = True,
) -> dict:
    """Issue a browser-shaped GET via the configured proxy."""
    proxy_url = proxy or os.environ.get("PAPRIKA_PROXY_URL", "")
    if not proxy_url:
        raise RuntimeError(
            "paprika-proxy-fetch: no proxy configured. "
            "Pass 'proxy' in params or set PAPRIKA_PROXY_URL."
        )

    merged_headers = dict(_DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)

    t0 = time.time()
    # httpx accepts a single proxy URL via the `proxy` kwarg in 0.27+
    # and the `proxies` kwarg in older versions. Try the newer API first
    # then fall back so the plugin works against whatever httpx the
    # plugin's lib dir / system interpreter happens to have.
    try:
        client = httpx.Client(
            proxy=proxy_url,
            timeout=float(timeout_s),
            verify=verify,
            follow_redirects=True,
            headers=merged_headers,
        )
    except TypeError:
        client = httpx.Client(
            proxies=proxy_url,
            timeout=float(timeout_s),
            verify=verify,
            follow_redirects=True,
            headers=merged_headers,
        )

    try:
        r = client.get(url)
    finally:
        client.close()

    elapsed_ms = int((time.time() - t0) * 1000)

    cookies_flat: dict[str, str] = {}
    # httpx.Cookies is iterable of names; values via .get(name, domain=...)
    for name in r.cookies.keys():
        try:
            cookies_flat[name] = r.cookies.get(name) or ""
        except Exception:
            continue

    # Body excerpt -- enough for the dispatcher / operator to recognize a
    # CF 1020 page vs. a real HTML response, but never the whole document.
    body_excerpt = ""
    try:
        text = r.text or ""
        body_excerpt = text[:1200]
    except Exception:
        pass

    return {
        "cookies":      cookies_flat,
        "status_code":  int(r.status_code),
        "elapsed_ms":   elapsed_ms,
        "final_url":    str(r.url),
        "via_proxy":    _redact(proxy_url),
        "body_excerpt": body_excerpt,
    }
