"""Asset bytes + gallery JSON/HTML surfaces (/jobs/{id}/assets*, /ui/assets/...).

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


@router.get("/jobs/{job_id}/assets.json")
async def job_assets_json(job_id: str) -> dict:
    """JSON view of captured assets -- powers the inline live panel's
    thumbnail strip. Lighter than the full HTML gallery; just enough for
    the admin UI to render tiles.

    Each item also carries ``source_url`` and ``mime`` when the upload
    came with that metadata (session captures emit it via the passive
    CDP listener). The admin UI's click-through popup shows them.

    Note: the legacy ``/jobs/{id}/gallery.json`` path is kept as an alias
    below for older integrations -- prefer ``assets.json`` going forward.
    """
    await _soft_resolve_job(job_id, require_subdir="assets")
    meta_dir = get_storage_dir() / job_id / "assets" / ".meta"
    items: list[dict] = []
    for a in await _gather_assets(job_id):
        name = a["name"]
        sz = a["size"]
        ext = Path(name).suffix.lower().lstrip(".")
        kind = "other"
        if ext in _IMG_EXTS:
            kind = "image"
        elif ext in _VIDEO_EXTS:
            kind = "video"
        elif ext in _AUDIO_EXTS:
            kind = "audio"
        # Pull sidecar metadata if it exists. The fetch path saves source
        # URLs straight onto the asset row; session captures use the
        # .meta/ sidecar minted by upload_asset. Read from the local copy
        # only (best effort) -- absent for S3-only jobs, so source_url is
        # simply null there; the asset itself still lists.
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
    # Recovery pass: fill in source_url for items whose sidecar was
    # missing (older jobs, regressed upload paths) by parsing the
    # fetcher's network-event markers out of log.txt. No-op for items
    # that already have a source_url.
    await _backfill_source_urls_from_log(job_id, items)
    return {"job_id": job_id, "count": len(items), "items": items}


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


@router.get("/ui/assets/{job_id}", response_class=HTMLResponse)
async def job_assets(job_id: str) -> str:
    """HTML assets browser: every asset saved for this job, grouped
    by type (images / videos / audio / others). Linked from the admin
    UI Live panel and the Recent Jobs row.

    URL moved through ``/gallery`` -> ``/screenshots`` -> ``/ui/attempts``
    -> ``/ui/assets`` over the project's lifetime. Each old path stays
    accepted as an
    alias so bookmarks / external links don't break, but the admin
    UI links + Swagger schema only show the latest one."""
    info = await _soft_resolve_job(job_id, require_subdir="assets")
    # ``info`` is None for session-routed parent_job_ids whose dir was
    # pre-created by create_session but never registered as a JobInfo.
    # The gallery HTML still renders for those (the file listing comes
    # from disk, not info), so fall back to safe display values.
    _info_url = (info.url if info is not None else "") or ""
    try:
        _info_status = info.status.value if (info is not None and info.status is not None) else "?"
    except Exception:
        _info_status = "?"
    # File list comes from the object store unioned with any local copy
    # (see _gather_assets) -- so the gallery renders even when the local copy
    # is gone. screenshot-* and the .meta/ dir are already filtered there.
    buckets = {"images": [], "videos": [], "audios": [], "others": []}
    for a in await _gather_assets(job_id):
        name = a["name"]
        ext = Path(name).suffix.lower().lstrip(".")
        info_d = {
            "name": name,
            "href": _asset_href(job_id, name),
            "size": a["size"],
            "size_h": _human_size(a["size"]),
            "ext": ext,
        }
        if ext in _IMG_EXTS:
            buckets["images"].append(info_d)
        elif ext in _VIDEO_EXTS:
            buckets["videos"].append(info_d)
        elif ext in _AUDIO_EXTS:
            buckets["audios"].append(info_d)
        else:
            buckets["others"].append(info_d)

    import html as _html

    def img_tile(a):
        return (
            f'<a class="tile img" href="{_html.escape(a["href"])}" target="_blank" '
            f'title="{_html.escape(a["name"])} — {a["size_h"]}">'
            f'<img loading="lazy" src="{_html.escape(a["href"])}" alt="">'
            f'<span class="cap">{_html.escape(a["name"])}<br>{a["size_h"]}</span>'
            f"</a>"
        )

    def vid_tile(a):
        return (
            f'<div class="tile vid">'
            f'<video controls preload="metadata" src="{_html.escape(a["href"])}"></video>'
            f'<span class="cap">'
            f'<a href="{_html.escape(a["href"])}" download>{_html.escape(a["name"])}</a>'
            f" — {a['size_h']}</span></div>"
        )

    def aud_tile(a):
        return (
            f'<div class="tile aud">'
            f'<audio controls preload="metadata" src="{_html.escape(a["href"])}"></audio>'
            f'<span class="cap">'
            f'<a href="{_html.escape(a["href"])}" download>{_html.escape(a["name"])}</a>'
            f" — {a['size_h']}</span></div>"
        )

    def other_tile(a):
        return (
            f'<a class="tile other" href="{_html.escape(a["href"])}" download '
            f'title="{_html.escape(a["name"])}">'
            f'<span class="ext">.{_html.escape(a["ext"] or "bin")}</span>'
            f'<span class="cap">{_html.escape(a["name"])}<br>{a["size_h"]}</span></a>'
        )

    def section(title, items, renderer):
        if not items:
            return ""
        return (
            f"<section><h2>{title} ({len(items)})</h2>"
            f'<div class="grid {title.lower()}">'
            + "".join(renderer(a) for a in items)
            + "</div></section>"
        )

    body = (
        section("Images", buckets["images"], img_tile)
        + section("Videos", buckets["videos"], vid_tile)
        + section("Audios", buckets["audios"], aud_tile)
        + section("Others", buckets["others"], other_tile)
    )
    if not body:
        # Distinguish "still running" from "done, captured nothing" so the
        # user doesn't think the gallery is broken on minimal pages like
        # example.com (which legitimately has no images/scripts to capture).
        status = _info_status
        if status in ("completed", "succeeded", "failed"):
            body = (
                '<section><p class="empty">'
                f"Job <code>{_html.escape(status)}</code> — no assets were captured.<br>"
                "This page may have no images / videos / external resources "
                "(e.g. <code>example.com</code> is intentionally minimal).<br>"
                "Try a richer URL to see the gallery populate."
                "</p></section>"
            )
        else:
            body = (
                '<section><p class="empty">'
                f"No assets yet. Job is currently <code>{_html.escape(status)}</code>; "
                "refresh in a moment."
                "</p></section>"
            )

    total = sum(len(v) for v in buckets.values())
    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<title>gallery — {_html.escape(job_id)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font: 14px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif; margin: 0; background: #f5f3ef; color: #222; }}
  header {{ background: #c0392b; color: #fff; padding: .8rem 1.5rem; }}
  header a {{ color: #fff; text-decoration: none; }}
  header a:hover {{ text-decoration: underline; }}
  header h1 {{ margin: 0; font-size: 1.2rem; display: flex; align-items: center; gap: 0.4rem; }}
  header h1 .logo {{ width: 1.4em; height: 1.4em; vertical-align: middle; }}
  header .sub {{ font-size: .85rem; opacity: .9; margin-top: .25rem; }}
  header .sub a {{ font-family: ui-monospace, Consolas, monospace; }}
  main {{ max-width: 1400px; margin: 0 auto; padding: 1rem; }}
  section {{ background: #fff; margin-bottom: 1rem; padding: .8rem 1rem; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.07); }}
  h2 {{ margin: 0 0 .6rem; font-size: 1rem; color: #444; border-bottom: 1px solid #eee; padding-bottom: .3rem; }}
  .grid {{ display: grid; gap: .6rem; }}
  .grid.images {{ grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }}
  .grid.videos {{ grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); }}
  .grid.audios {{ grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }}
  .grid.others {{ grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }}
  .tile {{ display: block; background: #fafafa; border: 1px solid #eee; border-radius: 6px; padding: .4rem; text-decoration: none; color: inherit; overflow: hidden; }}
  .tile.img img {{ width: 100%; height: 140px; object-fit: contain; background: #f0eee9; border-radius: 4px; }}
  .tile.vid video {{ width: 100%; max-height: 220px; background: #000; border-radius: 4px; }}
  .tile.aud audio {{ width: 100%; }}
  .tile.other {{ display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 110px; text-align: center; }}
  .tile.other .ext {{ font-size: 1.6rem; font-weight: 700; color: #c0392b; font-family: ui-monospace, Consolas, monospace; }}
  .cap {{ display: block; margin-top: .35rem; font-size: .78rem; color: #555; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .cap a {{ color: #c0392b; text-decoration: none; }}
  .cap a:hover {{ text-decoration: underline; }}
  .empty {{ color: #999; font-style: italic; }}
</style>
</head>
<body>
<header>
  <h1><img src="/icon.svg" alt="paprika" class="logo"> Gallery: <a href="/jobs/{_html.escape(job_id)}/result"><code>{_html.escape(job_id)}</code></a></h1>
  <div class="sub">
    URL: <a href="{_html.escape(_info_url)}" target="_blank">{_html.escape(_info_url)}</a>
    &nbsp;|&nbsp; status: {_html.escape(_info_status)}
    &nbsp;|&nbsp; total: <strong>{total}</strong> files
    &nbsp;|&nbsp;
    <a href="/">← admin</a>
    &nbsp;
    <a href="/jobs/{_html.escape(job_id)}/page.html" target="_blank">page.html</a>
    &nbsp;
    <a href="/jobs/{_html.escape(job_id)}/log.txt" target="_blank">log.txt</a>
  </div>
</header>
<main>
{body}
</main>
</body></html>"""


@router.get(
    "/ui/attempts/{job_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def job_assets_attempts_legacy(job_id: str) -> str:
    return await job_assets(job_id)


@router.get(
    "/jobs/{job_id}/assets.html",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def job_assets_html(job_id: str) -> str:
    return await job_assets(job_id)

