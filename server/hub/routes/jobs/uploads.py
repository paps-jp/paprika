"""Worker-facing writes: screenshots, asset uploads, special files.

Part of the jobs/ route package (split from the old monolithic
routes/jobs.py). Shared helpers + router live in jobs/_base.py."""

from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from server.hub._state import config, get_storage_dir, state
from server.hub import objstore
from server.hub._helpers import _safe_job_file
from server.hub.routes.novnc import _proxy_session_dict
from server.hub.routes.sessions import (
    _novnc_autoconnect,
    _route_to_page,
    _send_session_action,
)
from server.protocol import JobInfo
import os
import shutil
from datetime import datetime
from server.hub.routes.novnc import _proxy_info
from server.protocol import AssetInfo, JobResult, JobStatus
from server.runner import DONE_SENTINEL
import uuid
from fastapi import WebSocket, WebSocketDisconnect
from server.protocol import Event
import time
from server.hub.hosts import _normalise_host, cookies_for_cdp
from server.hub.iterative_codegen import resolve_rerun_source
from server.hub.sessions import SessionInfo, new_session_id
from server.protocol import (
    HubAssignJob,
    JobProgress,
    JobRequest,
)
from server.hub.app import (  # noqa: E402
    _JOB_DISPATCH_POLL_S,
    JOB_DISPATCH_GRACE_S,
)

log = logging.getLogger(__name__)

from server.hub.routes.jobs._base import *  # noqa: F401,F403 (router + helpers)

@router.post("/jobs/{job_id}/screenshot")
async def take_job_screenshot(
    job_id: str,
    request: Request,
    width: int = 1280,
    quality: int = 85,
) -> dict:
    """**SCREENSHOT** action: take a high-quality screenshot of the
    job's lane and save it as a JPEG asset.

    Returns ``{ok, name, size, mime, href}``. The saved asset shows
    up in ``/jobs/{id}/assets.json`` alongside everything else, so the
    admin Live panel's inline gallery + the per-job result page pick it
    up automatically.

    Used by the Submit-form Live panel's "Screenshot" button so an
    operator can manually snapshot the running browser without
    waiting for the script to call ``page.capture()``.

    Width / quality default to "good for human review" -- 1280px,
    quality=85 on the 0-100 perceptual scale (= ffmpeg q≈6, high
    quality). This is the opposite end of the cost spectrum from
    :func:`worker_lane_screenshot` (PREVIEW endpoint) which targets
    ~30-80 KB thumbnails.

    Note: previously ``quality`` was being passed raw to the worker's
    ffmpeg q:v parameter (2-31 inverted scale), so ``quality=80``
    actually produced the *worst* possible quality. Fixed in this
    revision; ``quality`` now consistently means "higher = better".
    """
    if state.store is None:
        raise HTTPException(503, "store not ready")
    info = await _require_job_info(job_id)
    worker_id = info.worker_id
    lane_idx = info.lane_idx
    if not worker_id or lane_idx is None:
        raise HTTPException(
            409,
            "job has no lane bound yet (worker_id or lane_idx missing)",
        )
    if state.registry is None:
        raise HTTPException(503, "registry not ready")
    # Multi-hub: the screenshot RPC needs the worker's WS, which lives on
    # its owner hub. Forward there when nginx routed this POST to a peer
    # (worker_id comes from the shared JobInfo, so it resolves on any hub).
    from server.hub.routes.workers import _maybe_forward_worker
    fwd = await _maybe_forward_worker(worker_id, request)
    if fwd is not None:
        return fwd
    worker = state.registry.connections.get(worker_id)
    if worker is None:
        raise HTTPException(
            502,
            f"worker '{worker_id}' is no longer connected",
        )
    ffmpeg_q = _ffmpeg_q_from_quality_pct(quality)
    try:
        reply = await worker.request_screenshot(
            int(lane_idx),
            max_width=int(width),
            quality=ffmpeg_q,
        )
    except TimeoutError:
        raise HTTPException(504, "screenshot timed out")
    except Exception as e:
        raise HTTPException(502, f"screenshot send failed: {e}")
    if reply.error:
        raise HTTPException(502, f"worker error: {reply.error}")

    import base64

    try:
        jpeg = base64.b64decode(reply.jpeg_b64)
    except Exception:
        raise HTTPException(502, "worker returned invalid base64")
    if not jpeg:
        raise HTTPException(502, "worker returned empty image")

    # Name the file by capture timestamp so the asset list reads in
    # chronological order. Use ``.jpg`` extension to match the wire
    # MIME (no client-side re-encode needed); ``screenshot-`` prefix
    # so the Screenshot-tab thumbnail strip can filter for them.
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    # Sub-second salt so back-to-back captures don't collide.
    salt = uuid.uuid4().hex[:4]
    name = f"screenshot-{ts}-{salt}.jpg"

    target_dir = get_storage_dir() / job_id / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / name
    target_path.write_bytes(jpeg)

    # Sidecar metadata so the gallery popup shows context.
    try:
        meta_dir = target_dir / ".meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "source": "manual-screenshot",
                    "page_url": info.url,
                    "mime": "image/jpeg",
                    "size": len(jpeg),
                    "captured_at": datetime.utcnow().isoformat() + "Z",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # Mirror the .meta sidecar to MinIO too (metadata durability).
        await objstore.mirror_file(meta_dir / f"{name}.json")
    except Exception:
        pass

    # Mirror to shared object storage (dormant unless PAPRIKA_S3_ENABLED).
    await objstore.mirror_file(target_path)

    return {
        "ok": True,
        "name": name,
        "size": len(jpeg),
        "mime": "image/jpeg",
        "href": _asset_href(job_id, name),
    }


@router.post(
    "/jobs/{job_id}/screenshot/capture",
    include_in_schema=False,
)
async def take_job_screenshot_legacy(
    job_id: str,
    request: Request,
    width: int = 1280,
    quality: int = 85,
) -> dict:
    return await take_job_screenshot(
        job_id=job_id, request=request, width=width, quality=quality,
    )


@router.post("/jobs/{job_id}/assets")
async def upload_asset(
    job_id: str,
    file: UploadFile = File(...),
    asset_name: str | None = Form(None),
    secret: str | None = Form(None),
    source_url: str | None = Form(None),
    mime: str | None = Form(None),
    page_url: str | None = Form(None),
):
    """Receive a file uploaded by a worker. Stored under
    data/jobs/{job_id}/assets/{asset_name}.

    Optional metadata captured by the session-mode passive listener:
      - ``source_url``: the URL the browser fetched this resource from
      - ``mime``:       the response's content-type
      - ``page_url``:   the URL of the page that initiated the request
                        (CDP's Network.RequestWillBeSent.documentURL).
                        Lets the gallery popup answer "which page did
                        this image come from", even if the script
                        subsequently navigated away from that page.
    When provided, persisted to a JSON sidecar
    (``assets/.meta/<name>.json``) the gallery endpoint reads back.
    """
    if config.worker_secret and secret != config.worker_secret:
        raise HTTPException(401, "bad secret")
    # Soft 404 via the shared helper: accept session-routed
    # parent_job_ids whose assets dir create_session pre-made, so raw
    # cli.session(parent_job_id=...) callers (e.g. an external crawler
    # publishing page.screenshot(label=) frames) can populate a gallery
    # the admin UI shows inline (via /jobs/{id}/assets.json) without
    # first POSTing a /jobs entry. The 404 still fires for unknown ids.
    await _soft_resolve_job(job_id, require_subdir="assets")

    name = asset_name or file.filename or "unnamed"
    # sanitize
    import re

    name = re.sub(r'[<>:"/\\|?*]', "_", name)[:180]

    target_dir = get_storage_dir() / job_id / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name

    # Stream the upload to disk in 1 MiB chunks instead of loading
    # the entire body into memory via ``await file.read()``. For a
    # large video (1+ GB iframe-mp4 captured by the passive m3u8 /
    # mp4 listener) the buffered form pushed the hub container past
    # its 2 GB cgroup memory cap and tripped the OOM killer mid-
    # upload -- the file was lost AND every other in-flight session
    # died because the hub process was replaced. Streaming write keeps
    # peak memory bounded at chunk size regardless of body length.
    # Use a .part suffix + atomic rename so readers never see a half-
    # written file under the final name.
    part = target.with_suffix(target.suffix + ".part")
    written = 0
    try:
        with open(part, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)  # 1 MiB
                if not chunk:
                    break
                # Disk write OFF the event loop: a 1 GB video = ~1000 sync
                # writes that otherwise block the loop between the async reads
                # (py-spy 2026-06-08). One thread hop per 1 MiB chunk; the writes
                # stay sequential (we await each before the next read).
                await asyncio.to_thread(out.write, chunk)
                written += len(chunk)
        part.replace(target)
    finally:
        # If the upload was interrupted, .part may still exist.
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass

    # Sidecar metadata. Best-effort -- failure to write the .meta JSON
    # must not fail the asset upload (the file itself is the important
    # artefact; metadata is a UX nicety).
    if source_url or mime or page_url:
        try:
            meta_dir = target_dir / ".meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / f"{name}.json").write_text(
                json.dumps(
                    {
                        "name": name,
                        "source_url": source_url or None,
                        "page_url": page_url or None,
                        "mime": mime or None,
                        "size": written,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            # Mirror the .meta sidecar to MinIO too (metadata durability).
            await objstore.mirror_file(meta_dir / f"{name}.json")
        except Exception:
            pass

    # Multi-hub foundation: mirror to shared object storage (no-op unless
    # PAPRIKA_S3_ENABLED). Local disk stays the source of truth.
    await objstore.mirror_file(target)

    # str(target), not target.resolve(): the latter is a sync realpath (lstat
    # syscalls) on the event loop per asset upload; target is already absolute
    # under the storage root, so the response path is identical without the
    # syscalls (py-spy 2026-06-08).
    return {"saved": str(target), "size": written, "name": name}


@router.post("/jobs/{job_id}/assets/presign")
async def presign_asset_upload(job_id: str, body: dict) -> dict:
    """Issue a presigned PUT URL so the worker uploads the asset BYTES straight
    to MinIO (the hub never sees them; these uploads are ~45% of hub load). The
    worker then calls ``POST /jobs/{id}/assets/complete`` with the metadata.

    ADDITIVE / dormant: the legacy ``POST /jobs/{id}/assets`` (bytes-through-hub)
    is unchanged, so nothing uses this until a worker opts in. Phase 1 of the
    worker->MinIO-direct cutover (see /complete).

    Body: ``{filename, mime?, source_url?, page_url?, expires_in?, secret?}``.
    Auth mirrors ``upload_asset`` (worker_secret only when configured)."""
    if config.worker_secret and body.get("secret") != config.worker_secret:
        raise HTTPException(401, "bad secret")
    if not objstore.enabled():
        raise HTTPException(503, "object store not enabled")
    await _soft_resolve_job(job_id, require_subdir="assets")
    raw_name = (body.get("filename") or body.get("asset_name") or "").strip()
    if not raw_name:
        raise HTTPException(400, "filename required")
    import re

    name = re.sub(r'[<>:"/\\|?*]', "_", raw_name)[:180]
    key = objstore.asset_key(job_id, name)
    try:
        expires = int(body.get("expires_in") or 7200)
    except (TypeError, ValueError):
        expires = 7200
    expires = max(60, min(expires, 6 * 3600))  # clamp 1 min .. 6 h
    url = await objstore.presign_put(key, expires)
    if not url:
        raise HTTPException(503, "could not presign upload")
    return {
        "put_url": url,
        "method": "PUT",
        "key": key,
        "name": name,
        "expires_in": expires,
    }


@router.post("/jobs/{job_id}/assets/complete")
async def complete_asset_upload(job_id: str, body: dict) -> dict:
    """Record metadata for an asset the worker uploaded DIRECTLY to MinIO via a
    presigned PUT (see /presign) -- the second half of the worker->MinIO-direct
    path. The hub writes the ``.meta`` sidecar (source_url / page_url / mime /
    size) the gallery + delian read, after CONFIRMING the object is in the
    bucket. NO bytes flow through the hub. The asset itself already shows in the
    gallery because ``_gather_assets`` unions the MinIO listing.

    Body: ``{filename, size?, mime?, source_url?, page_url?, secret?}``.
    Auth mirrors ``upload_asset``. Returns 409 if the object isn't in the store
    yet (PUT not finished / failed) so the worker can retry just this step."""
    if config.worker_secret and body.get("secret") != config.worker_secret:
        raise HTTPException(401, "bad secret")
    if not objstore.enabled():
        raise HTTPException(503, "object store not enabled")
    await _soft_resolve_job(job_id, require_subdir="assets")
    import re

    raw_name = (body.get("filename") or body.get("asset_name") or "").strip()
    if not raw_name:
        raise HTTPException(400, "filename required")
    name = re.sub(r'[<>:"/\\|?*]', "_", raw_name)[:180]
    key = objstore.asset_key(job_id, name)
    # Confirm the worker's direct PUT actually landed before recording anything.
    if not await objstore.head_object(key):
        raise HTTPException(409, "object not in store yet (PUT not completed?)")
    source_url = (body.get("source_url") or "").strip() or None
    page_url = (body.get("page_url") or "").strip() or None
    mime = (body.get("mime") or "").strip() or None
    try:
        size = int(body.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    # Sidecar metadata -- written + mirrored EXACTLY like upload_asset so the
    # gallery / get_page_meta / delian source_url cascade read it unchanged.
    # Best-effort: a sidecar failure must not fail the (already-stored) asset.
    if source_url or mime or page_url:
        try:
            meta_dir = get_storage_dir() / job_id / "assets" / ".meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / f"{name}.json").write_text(
                json.dumps(
                    {
                        "name": name,
                        "source_url": source_url,
                        "page_url": page_url,
                        "mime": mime,
                        "size": size,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            await objstore.mirror_file(meta_dir / f"{name}.json")
        except Exception:
            pass
    return {"ok": True, "name": name, "key": key, "size": size}


@router.post("/jobs/{job_id}/assets/from_url")
async def save_asset_from_url(job_id: str, body: dict) -> dict:
    """Download a resource by URL and save it as a job asset.

    Used by the Live panel "Network" tab: the operator sees a media
    response in the traffic log and clicks "add to assets". The hub
    fetches the URL (server-side, so cookies/auth don't matter — the
    content is already in the browser's cache; we're using the URL
    as a cache key to re-fetch) and stores it under the job's assets.

    Body::

        {
          "url": "https://cdn.example.com/thumb.jpg",
          "mime": "image/jpeg",          // optional hint
          "page_url": "https://...",     // optional
        }
    """
    info = await _require_job_info(job_id)

    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")

    import re as _re

    # Derive filename from URL.
    from urllib.parse import unquote, urlparse

    import httpx

    parsed = urlparse(url)
    raw_name = unquote(parsed.path.split("/")[-1] or "resource")
    name = _re.sub(r'[<>:"/\\|?*]', "_", raw_name)[:180]
    if not name or name == ".":
        name = "resource"

    target_dir = get_storage_dir() / job_id / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Dedup: if a file with the same name already exists, skip.
    target = target_dir / name
    if target.exists():
        return {
            "status": "already_exists",
            "name": name,
            "size": target.stat().st_size,
        }

    # Download from URL.
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            content = r.content
    except Exception as e:
        raise HTTPException(
            502,
            f"download failed: {type(e).__name__}: {e}",
        )

    target.write_bytes(content)

    # Sidecar metadata.
    mime = body.get("mime") or ""
    page_url = body.get("page_url") or ""
    try:
        meta_dir = target_dir / ".meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "source_url": url,
                    "page_url": page_url or None,
                    "mime": mime or None,
                    "size": len(content),
                    "source": "network-tab",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # Mirror the .meta sidecar to MinIO too (metadata durability).
        await objstore.mirror_file(meta_dir / f"{name}.json")
    except Exception:
        pass

    # Mirror to shared object storage (dormant unless PAPRIKA_S3_ENABLED).
    await objstore.mirror_file(target)

    return {"status": "saved", "name": name, "size": len(content)}


@router.post("/jobs/{job_id}/files/{kind}")
async def upload_special(
    job_id: str,
    kind: str,
    file: UploadFile = File(...),
    secret: str | None = Form(None),
):
    """Upload a special file ('page.html', 'log.txt', or 'meta.json')."""
    if config.worker_secret and secret != config.worker_secret:
        raise HTTPException(401, "bad secret")
    if kind not in ("page.html", "log.txt", "meta.json", "final.jpg"):
        raise HTTPException(400, "kind must be 'page.html', 'log.txt', 'meta.json' or 'final.jpg'")
    await _require_job_info(job_id)
    target = get_storage_dir() / job_id / kind
    target.parent.mkdir(parents=True, exist_ok=True)
    # Stream chunked (1 MiB) instead of buffering the whole body in
    # RAM. Page HTML can run 10+ MB on heavily-scripted sites; not as
    # catastrophic as the /assets video case but still wasteful.
    total = 0
    with open(target, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
    # Mirror to shared object storage (dormant unless PAPRIKA_S3_ENABLED).
    await objstore.mirror_file(target)
    # str(target) not .resolve(): avoid the per-upload realpath on the loop.
    return {"saved": str(target), "size": total}

