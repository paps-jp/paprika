"""Public-URL safety validator for SSRF protection (hub side).

Phase 3: the actual validation logic now lives in :mod:`core.ssrf_guard` so the
WORKER (which imports ``core.*`` but not ``server.hub.*``) can run the SAME
checks at navigate time — the authoritative point, since DNS can rebind between
the hub's resolve and the worker's connect. This module is a thin hub-facing
wrapper that adds the FastAPI ``HTTPException`` convenience; everything else
(scheme whitelist, private/loopback/link-local/metadata/ULA classification,
all-records DNS resolution, the ``PAPRIKA_ALLOW_PRIVATE_URLS`` bypass) is shared.

Callers (unchanged): ``assert_public_url(url)`` in create_job / create_session,
and ``validate_public_url(url) -> (ok, reason)`` for tuple-style callers.
"""
from __future__ import annotations

# Re-export the shared validator so existing imports keep working unchanged.
from core.ssrf_guard import validate_public_url  # noqa: F401


def assert_public_url(url: str) -> None:
    """Route-handler convenience: raise ``HTTPException(400)`` instead of
    returning a tuple. Imports HTTPException lazily so this module stays
    usable from non-FastAPI callers (CLI / tests)."""
    ok, reason = validate_public_url(url)
    if not ok:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=reason)


import os
from concurrent.futures import ThreadPoolExecutor

# Dedicated pool for the per-request SSRF DNS check -- NOT asyncio's default
# executor. 2026-08-29 investigation: a single POST /jobs took 0.6s in
# isolation, but paprika-job-submit's submit_parallel=48 concurrent POSTs
# measured ~6s/request in practice (Little's Law: 48 concurrent / 7.8 req/s
# throughput). Root cause traced to this call: asyncio.to_thread() submits to
# the LOOP'S DEFAULT executor, sized ``min(32, cpu_count()+4)`` -- 12 threads
# on an 8-core hub host -- and that pool is ALSO shared by every other
# blocking call in the process (mariadb_store, objstore/MinIO, _cache_evict,
# _reaper, scheduler). 48 concurrent job-submit requests alone already
# oversubscribe 12 threads 4x; a single slow/dead-DNS host among them (already
# a known occurrence -- see client_error_dns handling on the submitter side)
# occupies one of those 12 slots for seconds, and every unrelated blocking
# call in the hub queues up behind it too.
#
# Sized generously: this is pure I/O wait (blocked in socket recv), not CPU
# work, so many more threads than cores is safe and cheap. 128 comfortably
# covers today's submit_parallel(48) with headroom for future increases and
# concurrent codegen-loop/session traffic, without being unbounded.
_DNS_CHECK_POOL_SIZE = int(os.environ.get("PAPRIKA_DNS_CHECK_POOL_SIZE") or 128)
_dns_check_executor = ThreadPoolExecutor(
    max_workers=_DNS_CHECK_POOL_SIZE, thread_name_prefix="ssrf-dns-check",
)


async def assert_public_url_async(url: str) -> None:
    """Async variant: run the SSRF check in a worker thread so its BLOCKING
    ``socket.getaddrinfo`` (validate_public_url -> ssrf_guard.resolve_all's OS
    fallback) can never stall the asyncio event loop. A single job submit with
    a dead/slow-DNS host (e.g. a crawl target that stopped resolving) otherwise
    froze getaddrinfo for ~40s ON the loop, hanging EVERY other request on that
    hub -- /workers/capacity, /health, worker-WS keepalives (2026-07-12
    incident, root-caused via py-spy: create_job -> getaddrinfo on MainThread).
    Async route handlers must ``await assert_public_url_async(url)`` instead of
    calling the sync form directly.

    Dispatched to ``_dns_check_executor`` (a dedicated pool), NOT
    ``asyncio.to_thread`` -- see the pool's own docstring for why the shared
    default executor turned concurrent POST /jobs into a queueing bottleneck."""
    import asyncio

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_dns_check_executor, assert_public_url, url)


__all__ = ["validate_public_url", "assert_public_url", "assert_public_url_async"]
