"""Worker-facing writes: screenshots, asset uploads, special files.

Part of the jobs/ route package (split from the old monolithic
routes/jobs.py). Shared helpers + router live in jobs/_base.py."""

from __future__ import annotations
import asyncio
import json
import logging
import re
from pathlib import Path

# multipart をハンドラ内でストリーム解析するために、 Starlette が内部で使う
# のと同じ parser を直接使う。 配布によって python_multipart / multipart の
# 二通りの名前があるので、 Starlette 自身と同じ順で探す。
try:
    from python_multipart.multipart import MultipartParser, parse_options_header
except ImportError:  # pragma: no cover - 古い配布名
    try:
        from multipart.multipart import MultipartParser, parse_options_header
    except ImportError:  # pragma: no cover - 未インストール
        MultipartParser = None  # type: ignore[assignment]
        parse_options_header = None  # type: ignore[assignment]
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
        await objstore.mirror_file(meta_dir / f"{name}.json", job_id=job_id)
    except Exception:
        pass

    # Mirror to shared object storage (dormant unless PAPRIKA_S3_ENABLED).
    await objstore.mirror_file(target_path, job_id=job_id)

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


# --------------------------------------------------------------------------
# ストリーミング multipart 受信 (2026-08-31)
# --------------------------------------------------------------------------
# FastAPI の ``file: UploadFile = File(...)`` は Starlette の
# ``SpooledTemporaryFile(max_size=1MB)`` に載る。 1 MB を超えるボディは
# **ハンドラが呼ばれる前に全量が /tmp へ書き出される**。 コンテナの /tmp は
# overlayfs = CT のディスクなので、 worker が上げてくる動画 (実測で 1 本
# 660 MB / 126 MB / 116 MB) がまるごと SSD に落ち、 そのあとハンドラが
# assets/*.part へもう一度書く —— **同じバイト列を 2 回書いて、 1 回目は
# 読み返して捨てるだけ**だった。
#
# 2026-08-31 実測 (hub-903, 75 秒, 書き込み fd のみサンプリング):
#     /tmp スプール           1,798 MB  76.8%
#     assets/*.part (保存)      528 MB  22.6%
#     その他                     14 MB   0.6%
# base の書き込みは 400 MB/s に達し、 コンシューマ SSD (pve/data =
# Predator GM7, DRAM-less) が util 91-100% / await 38.9 ms で飽和していた。
# その結果ハブのイベントループが fsync 待ちで数秒単位に固まり、 POST /jobs の
# 内訳が capacity=11,372 ms、 stats_async の 1.5 秒 wait_for が発火して fleet
# 集約が local-only へ縮退する (Workers が 8 <-> 270 で振れる) —— 一連の症状の
# 出どころがこの二重書き込みだった。
#
# ここでは python_multipart のコールバック API でボディを 1 パス舐め、
# **file パートだけを直接 assets/*.part へ流す**。 非ファイルフィールド
# (asset_name / secret / source_url / mime / page_url) は小さいのでメモリに
# 溜める (合計 _MAX_FIELD_BYTES で頭打ち)。 スプールが消えるので SSD への
# 書き込みは保存分だけになる。
#
# 注意: multipart はフィールド順を保証しない。 httpx は data -> files の順に
# 送るので実運用では file パート開始時点で asset_name が既に読めているが、
# **順序に依存しない**ように最終名は解析完了後に決め、 .part からの rename で
# 確定させる (.part 中の暫定名が見えても実害はない)。

_MAX_FIELD_BYTES = 1 << 20        # 非ファイルフィールドの合計上限


class _MultipartSink:
    """multipart を舐めて file パートだけディスクへ流すためのシンク。

    python_multipart のコールバックは同期関数なので、 ここではバイト列を
    溜めるだけにして、 実際の write は呼び出し側が 1 ネットワークチャンク
    につき 1 回 ``asyncio.to_thread`` でまとめて行う。 旧実装の「ディスク
    書き込みはイベントループの外」という性質を保ちつつ、 溜まる量は
    1 チャンク分で頭打ちになる。
    """

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}
        self.filename: str | None = None
        self.file_seen = False
        self.pending: list[bytes] = []
        self.pending_n = 0
        self._field_bytes = 0
        self._hdr_name = b""
        self._hdr_value = b""
        self._disposition = b""
        self._part_name: str | None = None
        self._part_is_file = False
        self._buf = bytearray()

    # ---- python_multipart コールバック (すべて同期) ----
    def on_part_begin(self) -> None:
        self._hdr_name = b""
        self._hdr_value = b""
        self._disposition = b""
        self._part_name = None
        self._part_is_file = False
        self._buf = bytearray()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._hdr_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._hdr_value += data[start:end]

    def on_header_end(self) -> None:
        if self._hdr_name.lower() == b"content-disposition":
            self._disposition = self._hdr_value
        self._hdr_name = b""
        self._hdr_value = b""

    def on_headers_finished(self) -> None:
        _, opts = parse_options_header(self._disposition)
        raw_name = opts.get(b"name")
        self._part_name = (
            raw_name.decode("utf-8", "replace") if raw_name else None
        )
        # Starlette と同じ判定: filename が「有る」ならファイルパート
        # (filename="" の空アップロードも含む)。
        raw_fn = opts.get(b"filename")
        self._part_is_file = raw_fn is not None
        if self._part_is_file:
            self.file_seen = True
            self.filename = raw_fn.decode("utf-8", "replace")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._part_is_file:
            blob = bytes(data[start:end])
            self.pending.append(blob)
            self.pending_n += len(blob)
            return
        self._field_bytes += end - start
        if self._field_bytes > _MAX_FIELD_BYTES:
            # 巨大な非ファイルフィールドでメモリを食わせない。
            raise ValueError("multipart form fields too large")
        self._buf.extend(data[start:end])

    def on_part_end(self) -> None:
        if not self._part_is_file and self._part_name:
            self.fields[self._part_name] = self._buf.decode("utf-8", "replace")
        self._buf = bytearray()

    def on_end(self) -> None:
        return None

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }

    def take(self) -> bytes:
        """溜まったファイルバイトを取り出して空にする。"""
        if not self.pending:
            return b""
        blob = b"".join(self.pending)
        self.pending.clear()
        self.pending_n = 0
        return blob


def _sanitise_asset_name(raw: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", raw)[:180]


@router.post("/jobs/{job_id}/assets")
async def upload_asset(job_id: str, request: Request) -> dict:
    """Receive a file uploaded by a worker. Stored under
    data/jobs/{job_id}/assets/{asset_name}.

    The body is parsed as a STREAM (see the comment above
    ``_MultipartSink``): the file part goes straight to
    ``assets/<name>.part`` and is renamed into place, so the bytes hit the
    disk exactly once. The previous ``UploadFile = File(...)`` signature
    made Starlette spool the whole body to /tmp first, which doubled every
    asset write onto the hub's SSD.

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
    if parse_options_header is None:
        raise HTTPException(500, "python-multipart is not installed")
    _mt, _opts = parse_options_header(request.headers.get("content-type") or "")
    if _mt.lower() != b"multipart/form-data":
        raise HTTPException(400, "expected multipart/form-data")
    boundary = _opts.get(b"boundary")
    if not boundary:
        raise HTTPException(400, "multipart boundary missing")

    # Soft 404 via the shared helper: accept session-routed
    # parent_job_ids whose assets dir create_session pre-made, so raw
    # cli.session(parent_job_id=...) callers (e.g. an external crawler
    # publishing page.screenshot(label=) frames) can populate a gallery
    # the admin UI shows inline (via /jobs/{id}/assets.json) without
    # first POSTing a /jobs entry. The 404 still fires for unknown ids.
    await _soft_resolve_job(job_id, require_subdir="assets")

    target_dir = get_storage_dir() / job_id / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)

    sink = _MultipartSink()
    parser = MultipartParser(boundary, sink.callbacks())

    def _secret_rejected() -> bool:
        """secret が既に届いていて、 かつ違うときだけ True。

        まだ届いていない (= file パートが先に来た) 場合は判定を保留し、
        解析完了後にもう一度見る。"""
        if not config.worker_secret:
            return False
        got = sink.fields.get("secret")
        return got is not None and got != config.worker_secret

    out = None
    part: Path | None = None
    written = 0
    target: Path | None = None
    try:
        try:
            async for chunk in request.stream():
                if chunk:
                    parser.write(chunk)
                if sink.pending_n:
                    if out is None:
                        if _secret_rejected():
                            raise HTTPException(401, "bad secret")
                        # 一時ファイルは **リクエストごとに一意な名前**にする。
                        # 同じ job へ同名アセットが並行で上がってくることが
                        # あり (2026-08-31 実測 5,000 件に 1-7 件)、 固定の
                        # "<name>.part" を共有すると
                        #   * 先に終わった方が rename した瞬間、 後続の
                        #     part.replace() が FileNotFoundError で 500
                        #   * それ以前に、 2 本が同じファイルへ書くので
                        #     バイト列が混ざって壊れる
                        # という 2 つの事故になる。 旧実装も同じ固定名だったが、
                        # Starlette が先に全量スプールしていたぶん実際に
                        # ディスクへ書いている区間が短く、 表に出にくかった。
                        # 最終名は解析完了後に確定させ rename でそこへ移すので、
                        # フィールド順にも依存しない。
                        part = target_dir / f".upload-{uuid.uuid4().hex}.part"
                        out = open(part, "wb")
                    blob = sink.take()
                    # Disk write OFF the event loop: a 1 GB video = ~1000 sync
                    # writes that otherwise block the loop between the async
                    # reads (py-spy 2026-06-08). One thread hop per network
                    # chunk; the writes stay sequential (we await each before
                    # reading the next chunk).
                    await asyncio.to_thread(out.write, blob)
                    written += len(blob)
            parser.finalize()
            if sink.pending_n and out is not None:
                blob = sink.take()
                await asyncio.to_thread(out.write, blob)
                written += len(blob)
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(413, str(e)) from e
        except Exception as e:  # noqa: BLE001 -- 壊れた multipart / 途中切断
            raise HTTPException(400, f"malformed multipart body: {e}") from e

        if config.worker_secret and sink.fields.get("secret") != config.worker_secret:
            raise HTTPException(401, "bad secret")
        if not sink.file_seen:
            raise HTTPException(422, "missing 'file' part")

        name = _sanitise_asset_name(
            sink.fields.get("asset_name") or sink.filename or "unnamed"
        )
        target = target_dir / name
        if out is not None:
            out.close()
            out = None
            # .part -> 最終名は atomic rename。 readers が半端なファイルを
            # 最終名で見ることは無い (旧実装と同じ保証)。
            part.replace(target)
            part = None
        else:
            # filename は有るが中身 0 バイトだったケース。
            await asyncio.to_thread(target.write_bytes, b"")
    finally:
        if out is not None:
            try:
                out.close()
            except OSError:
                pass
        # 中断された場合、 .part が残っていれば消す。
        if part is not None and part.exists():
            try:
                part.unlink()
            except OSError:
                pass

    source_url = sink.fields.get("source_url") or None
    mime = sink.fields.get("mime") or None
    page_url = sink.fields.get("page_url") or None

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
                        "source_url": source_url,
                        "page_url": page_url,
                        "mime": mime,
                        "size": written,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            # Mirror the .meta sidecar to MinIO too (metadata durability).
            await objstore.mirror_file(meta_dir / f"{name}.json", job_id=job_id)
        except Exception:
            pass

    # Multi-hub foundation: mirror to shared object storage (no-op unless
    # PAPRIKA_S3_ENABLED). Local disk stays the source of truth.
    await objstore.mirror_file(target, job_id=job_id)

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
    size) the gallery + the downstream takedown service read, after CONFIRMING the object is in the
    bucket. NO bytes flow through the hub. The asset itself already shows in the
    gallery because ``_gather_assets`` unions the MinIO listing.

    Body: ``{filename, size?, mime?, source_url?, page_url?, secret?}``.
    Auth mirrors ``upload_asset``. Returns 409 if the object isn't in the store
    yet (PUT not finished / failed) so the worker can retry just this step."""
    if config.worker_secret and body.get("secret") != config.worker_secret:
        raise HTTPException(401, "bad secret")
    if not objstore.enabled():
        raise HTTPException(503, "object store not enabled")
    # Existence gate only -- we ignore the JobInfo. need_info=False uses a cheap
    # SELECT-1 instead of the row->pydantic build (this runs once per asset,
    # thousands/min; py-spy 2026-07-08 flagged get_job_info as the top MainThread
    # on-CPU cost after the sidecar-FS fix).
    await _soft_resolve_job(job_id, require_subdir="assets", need_info=False)
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
    # gallery / get_page_meta / downstream source_url cascade read it unchanged.
    # Best-effort: a sidecar failure must not fail the (already-stored) asset.
    if source_url or mime or page_url:
        # The sidecar write is synchronous filesystem I/O (mkdir + write_text).
        # complete_asset_upload runs once per asset -> thousands/min under load,
        # and doing this ON the event loop made it the #1 intermittent hub-stall
        # source (py-spy 2026-07-08: uploads.py mkdir/open dominated MainThread
        # on-CPU time; a slow-disk moment blocked the loop for 100s of ms). Do
        # the local FS work in a thread so it can never block the loop; the
        # MinIO mirror below is already off-loop.
        def _write_sidecar() -> Path:
            meta_dir = get_storage_dir() / job_id / "assets" / ".meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            p = meta_dir / f"{name}.json"
            p.write_text(
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
            return p

        try:
            _sidecar = await asyncio.to_thread(_write_sidecar)
            await objstore.mirror_file(_sidecar, job_id=job_id)
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
        await objstore.mirror_file(meta_dir / f"{name}.json", job_id=job_id)
    except Exception:
        pass

    # Mirror to shared object storage (dormant unless PAPRIKA_S3_ENABLED).
    await objstore.mirror_file(target, job_id=job_id)

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
    await objstore.mirror_file(target, job_id=job_id)
    # str(target) not .resolve(): avoid the per-upload realpath on the loop.
    return {"saved": str(target), "size": total}

