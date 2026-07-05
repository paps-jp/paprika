"""Asset bytes + gallery/screenshot JSON surfaces (/jobs/{id}/assets/*, assets.json, screenshots.json).

Part of the jobs/ route package (split from the old monolithic
routes/jobs.py). Shared helpers + router live in jobs/_base.py."""

from __future__ import annotations
import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse
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

@router.get("/jobs/{job_id}/assets/{filename:path}")
async def get_asset(job_id: str, filename: str, request: Request):
    """Serve an asset file. ``filename`` may include forward slashes for
    nested paths (e.g. ``post_verification/post_verification.png`` from
    ``page.capture(label=...)`` output).

    Path traversal is blocked by rejecting ``.`` / ``..`` / empty segments
    up front, so the file resolves within the job's ``assets/`` dir without
    needing that dir to exist on disk.

    Source order: the local copy first (fast, Range handled by
    FileResponse), then a direct stream from the object store when the
    local copy is missing -- no local write needed, which is the case
    cache eviction leaves behind. Falls back to the legacy
    ensure_local pull last."""
    if not filename or "\\" in filename or filename.startswith("/"):
        raise HTTPException(400, "invalid path")
    # Reject any segment that's empty / "." / ".." -- with these gone the
    # filename cannot escape the assets/ dir (local) or its key prefix (S3).
    parts = filename.split("/")
    for seg in parts:
        if seg in ("", ".", ".."):
            raise HTTPException(400, "invalid path component")
    target = get_storage_dir() / job_id / "assets" / filename
    # 1) Local fast path -- serve straight off disk. Belt-and-braces
    #    traversal guard against the resolved assets root.
    if target.exists() and target.is_file():
        assets_root = (get_storage_dir() / job_id / "assets").resolve()
        try:
            target.resolve().relative_to(assets_root)
        except ValueError:
            raise HTTPException(400, "path escapes assets dir")
        return FileResponse(target)
    # 2) Object store -- stream directly, honouring Range, without needing
    #    the (possibly-absent) local copy.
    if objstore.enabled():
        obj = await objstore.open_object(
            job_id, f"assets/{filename}", request.headers.get("range")
        )
        if obj is not None:
            import mimetypes

            media_type = (
                mimetypes.guess_type(filename)[0] or "application/octet-stream"
            )
            return StreamingResponse(
                obj["iter"](),
                status_code=obj["status"],
                headers=obj["headers"],
                media_type=media_type,
            )
    # 3) Last resort: pull into the local cache then serve (legacy path).
    await objstore.ensure_local(target)
    if target.exists() and target.is_file():
        return FileResponse(target)
    raise HTTPException(404, f"file not found: {filename}")


# Filename-sanitisation pattern mirrors core/fetcher.py:_filename_from.
# Keep them in sync so the recovery lookup below produces the same
# basename shape the on-disk asset names use.
_FNAME_SANITIZE_RE = re.compile(r'[<>:"/\\|?*]')
# Matches the '_<N>' uniqueness suffix _unique_path appends when two
# captured resources collide on their derived filename.
_UNIQ_SUFFIX_RE = re.compile(r"^(.+)_\d+$")


async def _backfill_source_urls_from_log(job_id: str, items: list[dict]) -> None:
    """Recover ``source_url`` for assets whose ``.meta/<name>.json``
    sidecar is missing, by parsing the fetcher's ``[[paprika:netcap]]``
    markers in ``log.txt``.

    The sidecar can go missing for several reasons:
      - the asset was uploaded by an older worker build that pre-dated
        the source_url Form parameter,
      - the worker upload succeeded but the hub silent-failed the
        sidecar write (the streaming-write regression we already fixed
        in upload_asset, but which left a tail of un-meta'd jobs), or
      - the asset came via a yt-dlp / late-stragglers path that doesn't
        carry a per-asset URL.

    The netcap markers (emitted by the fetcher's network-log poll loop)
    record every captured network event with its URL, size, mime, and
    ``saved`` flag. We index entries that landed on disk by their
    URL-derived basename and confirm matches by size, so two images
    saved as e.g. ``cat.jpg`` + ``cat_1.jpg`` resolve back to their
    respective source URLs without crossing wires.

    Best-effort: never raises. Items whose source_url is already
    populated are left untouched. If the log isn't parseable or the
    file isn't available, just returns and the caller sees the
    original null values."""
    # Cheap precondition: skip the I/O entirely if every item already
    # has source_url filled in (the common case for jobs that ran
    # against a current worker + current hub).
    if not items or all(it.get("source_url") for it in items):
        return

    log_path = get_storage_dir() / job_id / "log.txt"
    try:
        await objstore.ensure_local(log_path)
    except Exception:
        pass
    if not log_path.exists():
        return

    marker = "[[paprika:netcap]] "
    # basename (with fetcher's sanitization applied) -> [{url, size, mime}, ...]
    by_basename: dict[str, list[dict]] = {}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                idx = line.find(marker)
                if idx < 0:
                    continue
                try:
                    payload = json.loads(line[idx + len(marker):])
                except Exception:
                    continue
                for ent in payload.get("net", []) or []:
                    if not ent.get("saved"):
                        continue
                    url = ent.get("url")
                    sz = ent.get("size")
                    if not url or sz is None:
                        continue
                    try:
                        bn = Path(urlparse(url).path).name
                    except Exception:
                        continue
                    if not bn:
                        continue
                    # Mirror _filename_from's sanitization + 180-char trim.
                    bn = _FNAME_SANITIZE_RE.sub("_", bn)[:180]
                    by_basename.setdefault(bn, []).append({
                        "url": url,
                        "size": int(sz),
                        "mime": ent.get("mime") or None,
                    })
    except Exception:
        return

    if not by_basename:
        return

    for it in items:
        if it.get("source_url"):
            continue
        name = it.get("name") or ""
        if not name:
            continue
        size = int(it.get("size") or 0)
        # Try the on-disk name first (handles the no-collision case).
        cands = by_basename.get(name)
        if not cands:
            # _unique_path appended '_N' to dedupe: peel it off and try
            # the original URL-derived basename.
            stem = Path(name).stem
            suffix = Path(name).suffix
            m = _UNIQ_SUFFIX_RE.match(stem)
            if m:
                cands = by_basename.get(m.group(1) + suffix)
        if not cands:
            continue
        # Size confirms when multiple URLs share a basename. Fall back
        # to the first candidate when no size matches (e.g. fetcher
        # logged a size of 0 / null for a chunked response).
        sized = [c for c in cands if c["size"] == size]
        pick = sized[0] if sized else cands[0]
        it["source_url"] = pick["url"]
        if not it.get("mime") and pick.get("mime"):
            it["mime"] = pick["mime"]


async def _backfill_source_urls_from_result(job_id: str, items: list[dict]) -> None:
    """Fill ``source_url`` (+ ``page_url`` / ``mime``) for assets whose
    ``.meta`` sidecar is missing, from the DURABLE job result.

    The worker reports every captured asset's source URL in the final
    ``JobResult.assets[].url`` (persisted to ``job_results`` in MariaDB), so this
    recovers ``source_url`` even when the local sidecar was never written or was
    cache-evicted -- the common reason ``assets.json`` showed ``source_url:
    null`` while the result had the URL all along. More reliable than the
    netcap-marker backfill (which only covers fetch-mode ``saved=true`` network
    events), so it runs FIRST. No-op when every item already has ``source_url``
    or the result is unavailable. Never raises."""
    if not items or all(it.get("source_url") for it in items):
        return
    try:
        result = await state.store.get_job_result(job_id)
    except Exception:
        result = None
    if result is None:
        return
    from urllib.parse import unquote

    def _f(a, k):
        return a.get(k) if isinstance(a, dict) else getattr(a, k, None)

    by_name: dict = {}
    for a in (getattr(result, "assets", None) or []):
        nm = _f(a, "name")
        if nm and _f(a, "url"):
            by_name[nm] = a
    if not by_name:
        return
    for it in items:
        if it.get("source_url"):
            continue
        nm = it.get("name") or ""
        # assets.json names can be URL-encoded (e.g. Japanese filenames) while
        # the result stores the decoded name -- try both.
        a = by_name.get(nm) or by_name.get(unquote(nm))
        if a is None:
            continue
        url = _f(a, "url")
        if url:
            it["source_url"] = url
        if not it.get("page_url"):
            pu = _f(a, "page_url")
            if pu:
                it["page_url"] = pu
        if not it.get("mime"):
            mm = _f(a, "mime")
            if mm:
                it["mime"] = mm


async def _backfill_source_urls_from_network(job_id: str, items: list[dict]) -> None:
    """Recover ``source_url`` (+ ``page_url`` / ``mime``) from the session
    network dump (``{job}/network.jsonl``) for items still missing it.

    Session-mode passive capture (``browser_ops.install_session_asset_capture``)
    records every media response's URL there with ``saved=true`` -- including
    assets loaded by embedded widgets / iframes (e.g. a FundraiseUp donation
    widget's emoji icons served from static.fundraiseup.com) that never appear
    in the fetcher's ``JobResult.assets`` NOR its ``[[paprika:netcap]]`` log
    markers. Matches by URL basename (+ ``_N`` dedup-suffix peel) confirmed by
    size, same as the netcap backfill. Never raises."""
    if not items or all(it.get("source_url") for it in items):
        return
    net_path = get_storage_dir() / job_id / "network.jsonl"
    try:
        await objstore.ensure_local(net_path)
    except Exception:
        pass
    if not net_path.exists():
        return
    by_basename: dict[str, list[dict]] = {}
    try:
        with open(net_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ent = json.loads(line)
                except Exception:
                    continue
                if not ent.get("saved"):
                    continue
                url = ent.get("url")
                sz = ent.get("size")
                if not url or sz is None:
                    continue
                try:
                    bn = Path(urlparse(url).path).name
                except Exception:
                    continue
                if not bn:
                    continue
                bn = _FNAME_SANITIZE_RE.sub("_", bn)[:180]
                by_basename.setdefault(bn, []).append({
                    "url": url,
                    "size": int(sz),
                    "mime": ent.get("mime") or None,
                    "page_url": ent.get("document_url") or None,
                })
    except Exception:
        return
    if not by_basename:
        return
    for it in items:
        if it.get("source_url"):
            continue
        name = it.get("name") or ""
        if not name:
            continue
        size = int(it.get("size") or 0)
        cands = by_basename.get(name)
        if not cands:
            stem = Path(name).stem
            suffix = Path(name).suffix
            m = _UNIQ_SUFFIX_RE.match(stem)
            if m:
                cands = by_basename.get(m.group(1) + suffix)
        if not cands:
            continue
        sized = [c for c in cands if c["size"] == size]
        pick = sized[0] if sized else cands[0]
        it["source_url"] = pick["url"]
        if not it.get("page_url") and pick.get("page_url"):
            it["page_url"] = pick["page_url"]
        if not it.get("mime") and pick.get("mime"):
            it["mime"] = pick["mime"]


# ④ Short-TTL per-job cache for assets.json. The live panel polls this every
# few seconds; without it each poll re-issues the MinIO list/GET calls below.
# job_id -> (expiry_monotonic, result). TTL is short so newly-captured assets
# on a RUNNING job still surface within a couple seconds.
_ASSETS_JSON_CACHE: dict[str, tuple[float, dict]] = {}
_ASSETS_JSON_TTL_S = 3.0
_ASSETS_JSON_CACHE_MAX = 2048
# ③ Cap concurrent MinIO sidecar GETs so a big-asset job can't exhaust the
# boto3 thread pool (see [[hub-eventloop-stalls]]).
_ASSETS_SIDECAR_CONC = 16


@router.get("/jobs/{job_id}/assets.json")
async def job_assets_json(job_id: str) -> dict:
    """JSON view of captured assets -- powers the inline live panel's
    thumbnail strip. Lighter than the full HTML gallery; just enough for
    the admin UI to render tiles.

    Each item also carries ``source_url`` and ``mime`` when the upload
    came with that metadata (session captures emit it via the passive
    CDP listener). The admin UI's click-through popup shows them.

    MinIO-call budget (the endpoint was timing out at ~30s for big-asset /
    cold-cache jobs while MinIO was under video-DL write load, 2026-06-28):
      * resolve ∥ asset-list run concurrently (1 MinIO list in the common case);
      * source_url is filled from the DURABLE job result FIRST (DB, no MinIO),
        so fetch-path jobs SKIP the ``.meta`` list + per-asset sidecar GETs
        entirely;
      * the residual sidecar GETs (session / worker->MinIO-direct uploads whose
        URL lives only in the sidecar) run CONCURRENTLY, bounded, not in a
        sequential await-per-asset loop;
      * a 3s per-job cache absorbs the live panel's repeated polls.

    Note: the legacy ``/jobs/{id}/gallery.json`` path is kept as an alias
    below for older integrations -- prefer ``assets.json`` going forward.
    """
    # ④ cache hit
    _now = time.monotonic()
    _hit = _ASSETS_JSON_CACHE.get(job_id)
    if _hit is not None and _hit[0] > _now:
        return _hit[1]

    # ① resolve (DB-fast for store-resident jobs) ∥ enumerate assets (1 MinIO
    # list). Independent -> run concurrently. _soft_resolve_job raises 404 when
    # the job exists nowhere; gather propagates it.
    _, assets = await asyncio.gather(
        _soft_resolve_job(job_id, require_subdir="assets"),
        _gather_assets(job_id),
    )
    meta_dir = get_storage_dir() / job_id / "assets" / ".meta"

    items: list[dict] = []
    for a in assets:
        name = a["name"]
        sz = a["size"]
        ext = Path(name).suffix.lower().lstrip(".")
        kind = "other"
        if ext in _RASTER_IMG_EXTS:
            kind = "image"
        elif ext in _VECTOR_IMG_EXTS:
            kind = "vector"      # SVG logos / UI icons -- not a raster photo
        elif ext in _ICON_IMG_EXTS:
            kind = "icon"        # favicon / .ico -- decoration
        elif ext in _VIDEO_EXTS:
            kind = "video"
        elif ext in _AUDIO_EXTS:
            kind = "audio"
        # Local sidecar is free (FS) -- authoritative when present locally.
        source_url = None
        mime = None
        page_url = None
        meta_path = meta_dir / f"{name}.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                source_url = meta.get("source_url")
                mime = meta.get("mime")
                page_url = meta.get("page_url")
            except Exception:
                pass
        items.append(
            {
                "name": name,
                "href": _asset_href(job_id, name),
                "size": sz,
                "size_h": _human_size(sz),
                "ext": ext,
                "kind": kind,
                "source_url": source_url,
                "page_url": page_url,
                "mime": mime,
            }
        )

    # ② result-first: fill source_url/page_url/mime from the DURABLE job result
    # (DB, no MinIO). For fetch-path jobs this covers everything, so the MinIO
    # ``.meta`` list + per-asset sidecar GETs below are SKIPPED entirely.
    await _backfill_source_urls_from_result(job_id, items)

    # ②+③ Only for items STILL missing source_url (session-capture /
    # worker->MinIO-direct uploads whose URL lives in the sidecar, not the
    # result): one ``.meta`` list, then pull the residual sidecars CONCURRENTLY
    # (bounded) instead of one sequential await per asset.
    missing = [it for it in items if not it.get("source_url")]
    if missing and objstore.enabled():
        try:
            _minio_meta = {
                (o.get("name") or "")
                for o in await objstore.list_dir(job_id, "assets/.meta")
            }
        except Exception:
            _minio_meta = set()
        to_pull = [
            it for it in missing
            if f"{it['name']}.json" in _minio_meta
            and not (meta_dir / f"{it['name']}.json").exists()
        ]
        if to_pull:
            _sem = asyncio.Semaphore(_ASSETS_SIDECAR_CONC)

            async def _pull(it: dict) -> None:
                async with _sem:
                    try:
                        await objstore.ensure_local(meta_dir / f"{it['name']}.json")
                    except Exception:
                        pass

            await asyncio.gather(*[_pull(it) for it in to_pull])
        # Re-read the now-local sidecars for the still-missing items.
        for it in missing:
            mp = meta_dir / f"{it['name']}.json"
            if not mp.exists():
                continue
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not it.get("source_url"):
                it["source_url"] = meta.get("source_url")
            if not it.get("mime"):
                it["mime"] = meta.get("mime")
            if not it.get("page_url"):
                it["page_url"] = meta.get("page_url")

    # Recovery passes (gated: no-op + no MinIO once every item has source_url).
    # Session network dump first (widget/iframe resources, e.g. FundraiseUp
    # emoji), then log.txt netcap markers.
    await _backfill_source_urls_from_network(job_id, items)
    await _backfill_source_urls_from_log(job_id, items)

    result = {"job_id": job_id, "count": len(items), "items": items}

    # ④ cache store (+ cheap bound: prune expired when the map grows).
    if len(_ASSETS_JSON_CACHE) > _ASSETS_JSON_CACHE_MAX:
        for _k in [k for k, v in _ASSETS_JSON_CACHE.items() if v[0] <= _now]:
            _ASSETS_JSON_CACHE.pop(_k, None)
    _ASSETS_JSON_CACHE[job_id] = (_now + _ASSETS_JSON_TTL_S, result)
    return result


@router.get("/jobs/{job_id}/gallery.json", include_in_schema=False)
async def job_gallery_json(job_id: str) -> dict:
    return await job_assets_json(job_id)


@router.get("/jobs/{job_id}/screenshots.json")
async def job_screenshots_json(job_id: str) -> dict:
    """List every screenshot-like asset under this job, regardless of
    depth. Powers the Live panel's Screenshot tab viewer (operator-driven
    captures, ``page.screenshot()`` SDK calls, ``page.capture(label=...)``
    PNG dumps from codegen-loop / vision-agent attempts, and Fetch
    mode's passive PNG/JPG capture).

    The plain ``assets.json`` endpoint only enumerates the TOP-LEVEL
    assets/ directory, which misses ``page.capture()`` output that
    lands at ``assets/<label>/<label>.png`` and per-attempt screenshots
    at ``assets/.../final_screenshot.jpg``. This endpoint walks
    recursively and filters to image extensions, so the operator sees
    a single chronological stream of "what the browser looked like"
    regardless of which code path saved each PNG.

    Each item carries:
      * ``name``      -- filename only (no path)
      * ``path``      -- relative path under assets/ (e.g. "screenshot-...",
                          "post_verification/post_verification.png")
      * ``href``      -- absolute URL to fetch the PNG
      * ``size``      -- bytes
      * ``mtime``     -- file mtime as POSIX seconds (float)
      * ``label``     -- subdirectory the file lives in, or "" for top
                          level. Useful to group AI capture() output.

    Sorted by mtime ASCENDING so the array index lines up with
    chronology (UI defaults to showing the latest at the end).
    """
    await _soft_resolve_job(job_id, require_subdir="assets")
    assets_dir = get_storage_dir() / job_id / "assets"
    # rel-path -> {size, mtime}; local wins on dup. Sourced from the local
    # tree UNIONed with the S3 mirror (recursive list_tree), so the
    # screenshot stream survives a deleted job row / evicted local copy.
    by_rel: dict[str, dict] = {}
    if assets_dir.exists():
        for p in assets_dir.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(assets_dir).as_posix()
            except Exception:
                rel = p.name
            try:
                st = p.stat()
            except Exception:
                continue
            by_rel[rel] = {"size": st.st_size, "mtime": st.st_mtime}
    if objstore.enabled():
        for o in await objstore.list_tree(job_id, "assets"):
            by_rel.setdefault(o["rel"], {"size": o["size"], "mtime": o["mtime"]})
    # Classify which image files are "screenshots" (taken intentionally
    # by API / client / AI / operator) vs "page assets" (downloaded by
    # the browser as part of the crawled page itself, e.g. logo.png,
    # banner.gif). The latter belong in the asset gallery only.
    #
    # Heuristic:
    #   * Top-level image whose name starts with "screenshot-" -- the
    #     manual /screenshot endpoint's output. INCLUDE.
    #   * Image in a SUBDIRECTORY of assets/ -- output of
    #     ``page.capture(label="...")`` (saves <label>/<label>.png +
    #     .html + .axtree.json) and per-attempt final_screenshot.jpg
    #     under attempts/N/. INCLUDE.
    #   * Other top-level images (no "screenshot-" prefix) -- assumed
    #     page-downloaded asset. EXCLUDE.
    items: list[dict] = []
    for rel, meta in by_rel.items():
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        if ext not in _IMG_EXTS:
            continue
        in_subdir = "/" in rel
        is_named_screenshot = rel.lower().startswith("screenshot-")
        if not (in_subdir or is_named_screenshot):
            continue  # page-downloaded asset, not a screenshot
        label = rel.rsplit("/", 1)[0] if "/" in rel else ""
        items.append({
            "name": rel.rsplit("/", 1)[-1],
            "path": rel,
            "href": _asset_href(job_id, rel),
            "size": meta["size"],
            "size_h": _human_size(meta["size"]),
            "ext": ext,
            "mtime": meta["mtime"],
            "label": label,
        })
    items.sort(key=lambda d: (d["mtime"], d["path"]))
    return {"job_id": job_id, "count": len(items), "items": items}
