"""Chrome extension registry routes: /extensions/* (list, GET, upload,
download, enable toggle, delete).

Operator uploads Chrome extensions (uBlock Origin Lite, AdGuard, custom
test extensions, ...) and they auto-load on every worker lane via
``--load-extension``. Distinct from profiles because extensions are
app-shaped (universal) whereas profiles are operator-identity-shaped
(cookies / login state, opt-in per job). See server/hub/extensions.py
for the on-disk format.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from server.hub._state import state
from server.hub.extensions import (
    ExtensionRegistry,
)
from server.hub.extensions import (
    is_valid_slug as _extension_slug_valid,
)
from server.hub.extensions import (
    normalise_slug as _extension_normalise_slug,
)

log = logging.getLogger(__name__)
router = APIRouter(tags=["Extensions"])


def _require_extensions() -> ExtensionRegistry:
    if state.extensions is None:
        raise HTTPException(503, "extension registry not initialised")
    return state.extensions


def _extension_meta_to_dict(m) -> dict:
    """Project ExtensionMeta + on-disk tag into the operator-facing JSON
    shape the admin UI consumes."""
    d = m.to_json()
    reg = _require_extensions()
    tag = reg.etag(m.slug)
    if tag:
        d["etag"] = tag
    return d


@router.get("/extensions")
async def list_extensions() -> dict:
    """List every uploaded Chrome extension (metadata only).

    Tarballs are at ``GET /extensions/{slug}/download``; workers fetch
    them on connect and extract into ``/tmp/paprika-extensions/<slug>/``
    for ``--load-extension``.

    Response shape::

        {
          "count": N,
          "extensions": [{slug, name, version, size_bytes, enabled,
                          uploaded_at, updated_at, note, etag}, ...]
        }
    """
    reg = _require_extensions()
    rows = [_extension_meta_to_dict(m) for m in reg.list()]
    return {"count": len(rows), "extensions": rows}


@router.get("/extensions/{slug}")
async def get_extension_info(slug: str) -> dict:
    """Metadata for one extension without downloading the tarball."""
    if not _extension_slug_valid(slug):
        raise HTTPException(400, "invalid extension slug")
    reg = _require_extensions()
    m = reg.get_meta(slug)
    if m is None:
        raise HTTPException(404, f"extension '{slug}' not found")
    return _extension_meta_to_dict(m)


@router.get("/extensions/{slug}/download")
async def download_extension(slug: str):
    """Stream the unpacked-dir tarball for ``{slug}``. Workers hit this
    on connect (and after any HubExtensionSync broadcast) to refresh
    their local cache.

    Returns ``application/gzip`` so the worker can stream-tar-extract.
    """
    if not _extension_slug_valid(slug):
        raise HTTPException(400, "invalid extension slug")
    reg = _require_extensions()
    p = reg.get_tarball_path(slug)
    if p is None:
        raise HTTPException(404, f"extension '{slug}' not found")
    return FileResponse(
        path=str(p),
        media_type="application/gzip",
        filename=f"{slug}.tar.gz",
    )


@router.post("/extensions/{slug}")
async def upload_extension(slug: str, request: Request) -> dict:
    """Upload a Chrome extension.

    Accepts the raw bytes of a ``.zip``, ``.crx``, or ``.tar.gz`` body.
    The server detects the format (by filename suffix in the
    ``X-Filename`` header, falling back to magic-byte sniff) and
    normalises into the unpacked-dir tarball used on disk.

    Validates the upload contains a ``manifest.json`` at its top level
    (single-wrapper-dir layouts get flattened automatically). Re-upload
    of the same slug replaces the previous content.

    Optional headers:
      * ``X-Filename``      original filename (used to pick the unpacker)
      * ``X-Paprika-Note``  free-text operator note
    """
    raw_slug = slug
    slug = _extension_normalise_slug(raw_slug)
    if not _extension_slug_valid(slug):
        raise HTTPException(
            400,
            f"invalid extension slug: {raw_slug!r} (use A-Za-z0-9._- only, 1-64 chars)",
        )
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty upload body")
    if len(body) > 200 * 1024 * 1024:
        raise HTTPException(413, "extension upload too large (max 200 MB)")
    filename = request.headers.get("x-filename") or ""
    note = request.headers.get("x-paprika-note") or None
    reg = _require_extensions()
    lock = state.extensions_lock
    assert lock is not None
    try:
        async with lock:
            meta = reg.save(
                slug,
                upload_bytes=body,
                filename=filename,
                note=note,
            )
    except ValueError as e:
        raise HTTPException(400, str(e))
    log.info(
        "extension %r uploaded (%d bytes, v%s)",
        slug,
        meta.size_bytes,
        meta.version or "?",
    )
    return _extension_meta_to_dict(meta)


@router.post("/extensions/{slug}/enabled")
async def set_extension_enabled(slug: str, body: dict) -> dict:
    """Toggle the ``enabled`` flag for an extension.

    Body: ``{"enabled": true|false}``.

    Disabled extensions stay on disk but workers skip them when they
    sync, so disabled extensions don't load into any lane. Re-enable
    without re-uploading.
    """
    if not _extension_slug_valid(slug):
        raise HTTPException(400, "invalid extension slug")
    body = body or {}
    if "enabled" not in body:
        raise HTTPException(400, "missing 'enabled' in body")
    enabled = bool(body.get("enabled"))
    reg = _require_extensions()
    meta = reg.set_enabled(slug, enabled)
    if meta is None:
        raise HTTPException(404, f"extension '{slug}' not found")
    return _extension_meta_to_dict(meta)


@router.delete("/extensions/{slug}")
async def delete_extension(slug: str) -> dict:
    """Remove the tarball + metadata for one extension."""
    if not _extension_slug_valid(slug):
        raise HTTPException(400, "invalid extension slug")
    reg = _require_extensions()
    lock = state.extensions_lock
    assert lock is not None
    async with lock:
        ok = reg.delete(slug)
    return {"slug": slug, "deleted": ok}
