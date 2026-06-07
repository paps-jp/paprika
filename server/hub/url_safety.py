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


__all__ = ["validate_public_url", "assert_public_url"]
