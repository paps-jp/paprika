"""WorkerAgent mixin: page.html/log/asset uploads.

Part of the agent/ package; methods reach siblings via self (MRO).
Shared helpers + Phase-1 functions come from the imports below."""

from __future__ import annotations
import asyncio
import functools
import json
import os
import random
import shutil
import socket
import logging
import string
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
import httpx
from core.httpclient import make_async_client
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from core.fetcher import (
    FetchOptions,
    clone_chrome_profile,
    fetch,
)
from server.protocol import (
    AssetInfo,
    HubAssignJob,
    HubExpectedVersion,
    HubProfileDelete,
    HubProfileSync,
    HubRegistered,
    HubPreviewSubscribe,
    HubScreenshotRequest,
    HubSessionAction,
    HubSessionAgent,
    HubSessionEnd,
    HubSessionInteraction,
    HubSessionStart,
    HubUpdateGate,
    JobOptions,
    JobResult,
    JobStatus,
    ProfileCacheEntry,
    SessionStateSnapshot,
    WorkerCapabilities,
    WorkerDraining,
    WorkerHeartbeat,
    WorkerJobAccepted,
    WorkerJobComplete,
    WorkerJobFailed,
    ASSET_CAPTURE_MARKER,
    JOB_PROGRESS_MARKER,
    LINKS_CAPTURE_MARKER,
    NET_CAPTURE_MARKER,
    WorkerJobLog,
    WorkerJobProgress,
    WorkerRegister,
    WorkerPreviewFrame,
    WorkerScreenshotReply,
    WorkerSessionActionResult,
    WorkerSessionAgentResult,
    WorkerSessionAnnounce,
    WorkerSessionEndAck,
    WorkerSessionStartAck,
    YtdlpResult,
    decode_hub_msg,
    encode_msg,
)
from server.scheduler import HEARTBEAT_INTERVAL
from server.worker import browser_ops
from server.worker.sessions import SessionState
from server.worker._browser_helpers import (
    _LINKS_EXTRACT_JS,
    _VIDEO_DIRECT_RE,
    _VIDEO_STREAM_RE,
    _evaluate_in_frame,
    _looks_like_player_iframe,
)
from server.worker.session_actions import (
    _ActionCtx,
    _SESSION_ACTIONS,
)
import re as _re
from ._base import *  # noqa: F401,F403
from ._base import WORKER_EXIT_CODE_VERSION_MISMATCH, _get_browser_user_agent, _logger, _session_interaction_at
from .profile import _normalise_extracted_profile, parse_attach
from .recipe import _apply_fetch_recipe, _looks_suspect
from .selfupdate import _auto_exit_on_version_mismatch, _auto_fetch_source, _check_github_release_once, _fetch_and_apply_source_from_hub, _fetch_worker_plugins_from_hub, _print_version_mismatch_banner, _versions_meaningfully_differ, default_worker_version
from .translate import _looks_non_english, _translate_to_english
from .video import _make_video_downloader, _parse_dl_progress, detect_yt_dlp
from .workerid import WORKER_ID_FILE, _WorkerIdReassigned, hub_http_base


# Same extension set the hub's objstore uses to route a key to the video tier
# (.48), so "direct as video" here means the presigned URL points at .48.
_VIDEO_ASSET_EXTS = frozenset(
    {".mp4", ".webm", ".m3u8", ".ts", ".mkv", ".mov", ".avi", ".m4v", ".mpd", ".flv"}
)


def _is_video_asset_name(name: str) -> bool:
    k = (name or "").lower()
    dot = k.rfind(".")
    return dot >= 0 and k[dot:] in _VIDEO_ASSET_EXTS


_PUT_CHUNK = 1 << 20


async def _iter_file_chunks(path: Path, chunk: int = _PUT_CHUNK):
    """Async byte stream of a file for httpx; disk reads run off the loop."""
    f = await asyncio.to_thread(open, path, "rb")
    try:
        while True:
            b = await asyncio.to_thread(f.read, chunk)
            if not b:
                break
            yield b
    finally:
        await asyncio.to_thread(f.close)


def _video_via_hub_allowed() -> bool:
    """Development escape hatch (``PAPRIKA_VIDEO_ALLOW_DISK=1``): let a video
    that could not be sent to the video store go through the hub instead.

    Off in production, always. The hub writes an uploaded asset to its own
    job directory before it mirrors it to the object store, and that directory
    is on an NVMe SSD -- so "through the hub" means "onto an SSD", which is
    exactly what video must never do (operator rule, 2026-11-16; see
    ``scratch_pool.video_disk_allowed``). A dev stack has no object store at
    all, and there the hub's disk is the only place a video can go."""
    try:
        from server.worker import scratch_pool

        return scratch_pool.video_disk_allowed()
    except Exception:
        return False


class _UploadsMixin:
    async def _upload_files(
        self,
        assign: HubAssignJob,
        workdir: Path,
        fetch_result,
        already_uploaded: set[str] | None = None,
    ) -> None:
        """Upload page.html, log.txt, and all asset files to the hub.

        ``already_uploaded`` is the set of asset names the incremental
        on_asset_saved callback already shipped during capture; those are
        skipped here so the end-of-fetch reconcile pass only uploads the
        stragglers (page.html, log, late yt-dlp output, and any asset
        whose inline upload failed)."""
        done = already_uploaded if already_uploaded is not None else set()
        await self._upload_log(assign, workdir / "log.txt")
        await self._upload_special(assign, workdir / "page.html", "page.html")
        # Representative-image sidecar (written only when a pick was made).
        _meta_sidecar = workdir / "meta.json"
        if _meta_sidecar.exists():
            await self._upload_special(assign, _meta_sidecar, "meta.json")
        # ② v2 eye: end-of-fetch screenshot sidecar (written only when the
        # fetcher captured one). The hub's perception discovers {job}/final.jpg.
        _shot_sidecar = workdir / "final.jpg"
        if _shot_sidecar.exists():
            await self._upload_special(assign, _shot_sidecar, "final.jpg")
        # Page URL = the URL the user told us to fetch. Every captured
        # asset is "on" this page from the gallery's point of view.
        page_url = assign.url or None
        # Assets
        for a in fetch_result.assets_saved:
            if a["name"] in done:
                continue
            path = Path(a["path"])
            if path.exists():
                if await self._upload_asset(
                    assign,
                    path,
                    a["name"],
                    source_url=a.get("url"),
                    mime=a.get("mime"),
                    page_url=page_url,
                ):
                    done.add(a["name"])
        # yt-dlp may have produced extra files in assets_dir not in
        # assets_saved (since fetcher tracks only its own captures, not
        # yt-dlp downloads). Walk the dir and upload anything we missed.
        # For these we don't have a per-asset source URL, but we can
        # still attach the page_url so the gallery knows where they
        # came from.
        assets_dir = workdir / "assets"
        if assets_dir.exists():
            known = {a["name"] for a in fetch_result.assets_saved}
            for p in assets_dir.iterdir():
                if p.is_file() and p.name not in known and p.name not in done:
                    await self._upload_asset(
                        assign,
                        p,
                        p.name,
                        page_url=page_url,
                    )

    async def _upload_one_session_asset(
        self,
        state: SessionState,
        path: Path,
        *,
        mime: str | None = None,
        source_url: str | None = None,
        page_url: str | None = None,
        timeout: float | None = None,
        asset_name: str | None = None,
    ) -> bool:
        """Upload one file from a session's assets dir to the parent
        job's /assets endpoint. Used by:

          - the passive CDP listener installed at session_start (for
            browser-side image/video/audio responses), and
          - the ``download_video`` action handler (for yt-dlp output).

        Dedupes via ``state.uploaded_assets`` so the same path doesn't
        get re-shipped. Best-effort: errors are logged to stderr but
        don't raise -- a failed asset upload shouldn't kill the
        whole session.

        ``timeout`` overrides the AsyncClient's default 60s ceiling.
        Pass a generous value for big files (yt-dlp output, etc.):
        ``self._http`` is shared across the worker so the default
        can't be raised globally without affecting smaller, latency-
        sensitive calls like screenshot or state polling.

        Returns True if the upload was attempted-and-succeeded, False
        if it was skipped (no upload base, already uploaded) or failed.
        """
        if state.asset_upload_base is None:
            return False
        name = asset_name or path.name
        if name in state.uploaded_assets:
            return False
        # A VIDEO never goes through the hub (it would land on the hub's SSD
        # first): straight to the video store, or not at all.
        if _is_video_asset_name(name) and self._minio_direct_video_enabled():
            _vjid = getattr(state, "job_id", None)
            if await self._send_video_direct(
                state.asset_upload_base, path, name,
                job_id=_vjid, source_url=source_url, mime=mime, page_url=page_url,
            ):
                state.uploaded_assets.add(name)
                if _vjid:
                    try:
                        await self._send(
                            WorkerJobLog(job_id=_vjid, line=ASSET_CAPTURE_MARKER)
                        )
                    except Exception:
                        pass
                return True
            if not _video_via_hub_allowed():
                return False
            # dev box only (PAPRIKA_VIDEO_ALLOW_DISK=1): the POST below
        try:
            with open(path, "rb") as f:
                files = {
                    "file": (
                        name,
                        f,
                        mime or "application/octet-stream",
                    )
                }
                data: dict[str, str] = {"asset_name": name}
                if source_url:
                    data["source_url"] = source_url
                if mime:
                    data["mime"] = mime
                if page_url:
                    data["page_url"] = page_url
                if self.worker_secret:
                    data["secret"] = self.worker_secret
                post_kwargs: dict[str, Any] = {
                    "files": files,
                    "data": data,
                }
                if timeout is not None:
                    post_kwargs["timeout"] = float(timeout)
                r = await self._http.post(
                    state.asset_upload_base,
                    **post_kwargs,
                )
                r.raise_for_status()
            state.uploaded_assets.add(name)
            # Signal the parent job's Live panel to refresh its gallery
            # (ephemeral marker over /events; replaces the periodic
            # /assets.json poll). No-op when there's no parent job.
            _pjid = getattr(state, "job_id", None)
            if _pjid:
                try:
                    await self._send(
                        WorkerJobLog(job_id=_pjid, line=ASSET_CAPTURE_MARKER)
                    )
                except Exception:
                    pass
            return True
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] session asset upload failed: {name}: {e}",
            )
            return False

    async def _upload_asset(
        self,
        assign: HubAssignJob,
        path: Path,
        name: str,
        *,
        source_url: str | None = None,
        mime: str | None = None,
        page_url: str | None = None,
        timeout: float | None = None,
        status_out: dict | None = None,
    ) -> bool:
        """Upload one fetch-mode asset to the hub. Returns True on
        success, False on failure. The bool lets the incremental
        on_asset_saved callback track which names actually landed so the
        end-of-fetch reconcile pass can re-try only the ones that didn't.

        ``status_out``: when given, the HTTP status of the final failed
        attempt is stored under ``"status"`` (absent for network errors), so
        a caller can tell "hub says .48 is full, retry later" (507) apart
        from a hard failure. See ``_upload_video_with_backoff``.
        """
        # A VIDEO goes to the video store directly and to nowhere else: the hub
        # writes every asset it is handed to its own disk first, and that disk
        # is an SSD (see _send_video_direct). No fallback to the POST below.
        if _is_video_asset_name(name) and self._minio_direct_video_enabled():
            if await self._send_video_direct(
                assign.asset_upload_base, path, name,
                job_id=assign.job_id, source_url=source_url, mime=mime,
                page_url=page_url, status_out=status_out,
            ):
                try:
                    await self._send(
                        WorkerJobLog(job_id=assign.job_id, line=ASSET_CAPTURE_MARKER)
                    )
                except Exception:
                    pass
                return True
            if not _video_via_hub_allowed():
                return False
            # dev box only (PAPRIKA_VIDEO_ALLOW_DISK=1): the POST below
        # Worker->MinIO-direct for everything else (off by default;
        # PAPRIKA_WORKER_MINIO_DIRECT=1). Bytes go straight to MinIO, never
        # through the hub event loop. ALWAYS falls back to the legacy
        # bytes-through-hub POST below on any failure OR for files over the size
        # cap (default 100 MB), so no asset is ever lost. Cap via
        # PAPRIKA_WORKER_MINIO_DIRECT_MAX_MB.
        elif self._minio_direct_enabled():
            try:
                _sz = path.stat().st_size
            except OSError:
                _sz = 0
            try:
                _cap_mb = float(os.environ.get("PAPRIKA_WORKER_MINIO_DIRECT_MAX_MB") or 100.0)
            except (TypeError, ValueError):
                _cap_mb = 100.0
            if 0 < _sz <= _cap_mb * 1024 * 1024:
                try:
                    if await self._upload_asset_direct(
                        assign, path, name,
                        source_url=source_url, mime=mime, page_url=page_url, size=_sz,
                    ):
                        try:
                            await self._send(
                                WorkerJobLog(job_id=assign.job_id, line=ASSET_CAPTURE_MARKER)
                            )
                        except Exception:
                            pass
                        return True
                except Exception as e:
                    await self._send(
                        WorkerJobLog(
                            job_id=assign.job_id,
                            line=f"  .. MinIO-direct upload fell back to hub ({name}): {e}",
                        )
                    )
                # fall through to the legacy bytes-through-hub POST
        url = assign.asset_upload_base
        try:
            with open(path, "rb") as f:
                files = {
                    "file": (
                        name,
                        f,
                        mime or "application/octet-stream",
                    )
                }
                data: dict[str, str] = {"asset_name": name}
                if source_url:
                    data["source_url"] = source_url
                if mime:
                    data["mime"] = mime
                if page_url:
                    data["page_url"] = page_url
                if self.worker_secret:
                    data["secret"] = self.worker_secret
                post_kwargs: dict[str, Any] = {"files": files, "data": data}
                if timeout is not None:
                    post_kwargs["timeout"] = float(timeout)
                r = await self._http.post(url, **post_kwargs)
                r.raise_for_status()
            # Signal the Live panel gallery to refresh (event-driven;
            # replaces the periodic /assets.json poll).
            try:
                await self._send(
                    WorkerJobLog(job_id=assign.job_id, line=ASSET_CAPTURE_MARKER)
                )
            except Exception:
                pass
            return True
        except Exception as e:
            if status_out is not None:
                _code = getattr(getattr(e, "response", None), "status_code", None)
                if _code is not None:
                    status_out["status"] = int(_code)
            await self._send(
                WorkerJobLog(
                    job_id=assign.job_id,
                    line=f"  !! asset upload failed: {name}: {e}",
                )
            )
            return False

    async def _upload_video_with_backoff(
        self,
        assign: HubAssignJob,
        path: Path,
        name: str,
        *,
        mime: str | None = None,
        page_url: str | None = None,
        timeout: float | None = None,
    ) -> bool:
        """``_upload_asset`` that waits out a full .48 instead of losing the file.

        The hub answers a VIDEO upload with 507 while the .48 RAM tier is at or
        above ``asset_ram_reject_pct`` (92%), or while its usage can't be
        measured; the hub's own comment says "retry with backoff". Before this,
        nobody did: the deferred-download loop ignored the False, put the video
        in the job result anyway, video-pull made a crawl_video row for it, and
        video-face-extract failed it with NoSuchKey on .48 and .8. On 2026-11-15,
        292 of 339 failed rows in one burst were videos that never reached .48.

        Only 507 is retried. Anything else fails right away, as before. The
        file is still on the worker's disk, so waiting costs nothing but
        scratch space. Stops early when the hub gives up on the job (abandon or
        force-complete).

        Env:
          PAPRIKA_WORKER_VIDEO_FULL_RETRY_S   total wait budget, default 900; 0 = no retry
          PAPRIKA_WORKER_VIDEO_FULL_BACKOFF_S first wait, default 15 (doubles, cap 120)
        """
        try:
            budget = float(os.environ.get("PAPRIKA_WORKER_VIDEO_FULL_RETRY_S") or 900.0)
        except (TypeError, ValueError):
            budget = 900.0
        try:
            delay = float(os.environ.get("PAPRIKA_WORKER_VIDEO_FULL_BACKOFF_S") or 15.0)
        except (TypeError, ValueError):
            delay = 15.0
        cap = 120.0
        job_id = assign.job_id
        deadline = time.monotonic() + max(0.0, budget)
        attempt = 0
        while True:
            attempt += 1
            st: dict = {}
            if await self._upload_asset(
                assign, path, name,
                mime=mime, page_url=page_url, timeout=timeout, status_out=st,
            ):
                if attempt > 1:
                    await self._send(WorkerJobLog(
                        job_id=job_id,
                        line=f"  [downloading] {name}: uploaded on attempt {attempt}",
                    ))
                return True
            if st.get("status") != 507:
                return False
            if (job_id in getattr(self, "_abandoned_job_ids", ())
                    or job_id in getattr(self, "_force_complete_job_ids", ())):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await self._send(WorkerJobLog(
                    job_id=job_id,
                    line=f"  [downloading] {name}: .48 still full after "
                         f"{attempt} attempt(s), giving up",
                ))
                return False
            wait = min(delay * random.uniform(0.75, 1.25), remaining)
            await self._send(WorkerJobLog(
                job_id=job_id,
                line=f"  [downloading] {name}: .48 full (507), "
                     f"retry in {wait:.0f}s (attempt {attempt})",
            ))
            await asyncio.sleep(wait)
            delay = min(delay * 2.0, cap)

    def _minio_direct_enabled(self) -> bool:
        """Gate for the worker->MinIO-direct asset upload (off by default).
        Flip ``PAPRIKA_WORKER_MINIO_DIRECT=1`` to enable; the path always falls
        back to the legacy bytes-through-hub POST on any failure.

        Default was flipped ON on 2026-08-16 and reverted the same day --
        DON'T flip it again without fixing the request amplification first.
        The reasoning for turning it on was that asset bytes through the hub
        are ~45% of hub load, so moving them off should free the hub. What it
        actually did was double the hub's asset REQUEST count: the legacy path
        is one POST per asset, while the direct path still calls the hub twice
        (presign, then complete) and only the bytes bypass it. Measured on one
        hub over 5 minutes with the fleet at ~210 workers:

            9,697 POST /jobs/{id}/assets/presign
            8,909 POST /jobs/{id}/assets/complete
              785 POST /jobs/{id}/assets      (legacy fallback + oversize)

        The hub was request-bound, not byte-bound. Worker->hub completion
        latency (see server/hub/_complete_latency.py) went from a p50 of
        ~0.02-0.3s right after the flip to 28-38s on four of seven hubs two
        hours later, p90 100-107s, and job wall time roughly doubled
        (non-video p50 24.3s -> 38.4s). Object storage was healthy throughout
        (tiers at 43% / 35%, no spill), so this was the hub's per-worker
        message queue backing up behind the extra HTTP traffic.

        Making this a win needs the round trips gone, not just the bytes:
        presign in bulk at dispatch (or derive the key worker-side from an
        already-shared secret) and batch the completes, so a job costs O(1)
        hub requests instead of O(assets)."""
        return (os.environ.get("PAPRIKA_WORKER_MINIO_DIRECT") or "").strip().lower() in (
            "1", "true", "yes", "on",
        )

    def _minio_direct_video_enabled(self) -> bool:
        """Worker->MinIO-direct upload of VIDEO assets. ON by default since
        2026-11-16; ``PAPRIKA_WORKER_MINIO_DIRECT_VIDEO=0`` switches it off.

        On because of the SSD rule: a video sent through the hub is written to
        the hub's NVMe before it reaches .48 (measured 2026-11-15: every one of
        them, 8,347 asset POSTs in 10 minutes on one hub and no presign at
        all). Switching this off puts videos back on that SSD -- it is an
        emergency lever for a broken direct path, not a tuning knob.

        The 2026-08-16 rollback above was about request COUNT: images are many
        per job, and presign+complete doubled the hub's asset requests. Videos
        are ~1 per job (~40/min fleet-wide) but carry most of the bytes, so
        sending only them direct costs the hub ~+40 small requests/min and takes
        the bulk of the upload bytes off it.

        It also closes the hole that made video-face-extract fail with
        NoSuchKey (2026-11-12): bytes-through-hub land on the hub's local cache
        first and are mirrored to .48 afterwards, and the hub's cache-evict
        could rmtree the dir in between (``mirror_file`` then returns silently,
        so the POST still said 200). A direct PUT puts the video on .48 before
        the hub ever records it. Measured 2026-11-16 on one hub over 3 minutes:
        8 of 4,977 files written were videos (0.2%), carrying most of the
        bytes."""
        return (os.environ.get("PAPRIKA_WORKER_MINIO_DIRECT_VIDEO") or "1").strip().lower() not in (
            "0", "false", "no", "off",
        )

    async def _send_video_direct(
        self,
        base: str,
        path: Path,
        name: str,
        *,
        job_id: str | None = None,
        source_url: str | None = None,
        mime: str | None = None,
        page_url: str | None = None,
        status_out: dict | None = None,
    ) -> bool:
        """Put one VIDEO on the video store without the hub seeing a byte.

        ``base`` is the job's asset URL on the hub (``{hub}/jobs/{id}/assets``);
        the hub only signs the upload and records it afterwards.

        This never falls back to the bytes-through-hub POST, and neither may
        its callers (outside a dev box): the hub would write the file to its
        SSD. So what used to be covered by that fallback is handled here:

        * a network error or a 5xx is tried again (``PAPRIKA_WORKER_VIDEO_
          DIRECT_TRIES``, default 3) -- the file is still on the ramdisk;
        * 507 = the store is full. Not retried here: ``_upload_video_with_
          backoff`` owns that wait, and it learns of it through ``status_out``;
        * any other 4xx (the job is gone, bad secret) will not get better;
        * a file over the single-PUT limit (``PAPRIKA_WORKER_MINIO_DIRECT_
          VIDEO_MAX_MB``, default 4096; S3/MinIO stop at 5 GiB) is not
          uploaded at all.

        Returns False when the video is not on the store. One log line says
        why."""
        async def _say(line: str) -> None:
            if job_id:
                try:
                    await self._send(WorkerJobLog(job_id=job_id, line=line))
                    return
                except Exception:
                    pass
            _logger.info(f"[worker {self.worker_id}]{line}")

        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            cap_mb = float(os.environ.get("PAPRIKA_WORKER_MINIO_DIRECT_VIDEO_MAX_MB") or 4096.0)
        except (TypeError, ValueError):
            cap_mb = 4096.0
        if size <= 0:
            await _say(f"  !! video not uploaded: {name}: empty file")
            return False
        if size > cap_mb * 1024 * 1024:
            await _say(
                f"  !! video not uploaded: {name}: {size / 1048576:.0f} MB is over the "
                f"{cap_mb:.0f} MB the video store takes in one upload "
                f"(a video is never sent through the hub)"
            )
            return False
        try:
            tries = max(1, int(os.environ.get("PAPRIKA_WORKER_VIDEO_DIRECT_TRIES") or 3))
        except (TypeError, ValueError):
            tries = 3
        err: Any = None
        for attempt in range(1, tries + 1):
            code = None
            if status_out is not None:
                status_out.pop("status", None)
            try:
                if await self._upload_asset_direct(
                    None, path, name,
                    source_url=source_url, mime=mime, page_url=page_url,
                    size=size, base=base,
                ):
                    return True
                err = "the hub issued no upload URL"
            except Exception as e:
                err = e
                code = getattr(getattr(e, "response", None), "status_code", None)
            if code is not None and status_out is not None:
                status_out["status"] = int(code)
            if code is not None and 400 <= int(code) < 500:
                break
            if code == 507 or attempt >= tries:
                break
            if job_id and (
                job_id in getattr(self, "_abandoned_job_ids", ())
                or job_id in getattr(self, "_force_complete_job_ids", ())
            ):
                break
            await asyncio.sleep(min(2.0 * attempt, 10.0) * random.uniform(0.75, 1.25))
        await _say(
            f"  !! video upload failed: {name}: {err} "
            f"(not sent through the hub: a video is never written to its disk)"
        )
        return False

    async def _upload_asset_direct(
        self,
        assign: HubAssignJob,
        path: Path,
        name: str,
        *,
        source_url: str | None = None,
        mime: str | None = None,
        page_url: str | None = None,
        size: int | None = None,
        base: str | None = None,
    ) -> bool:
        """Upload one asset WORKER->MinIO-direct: ask the hub for a presigned PUT
        URL (tiny, no bytes), PUT the bytes STRAIGHT to MinIO (the hub never sees
        them; the signed URL is the only authorization -- no MinIO creds on the
        worker), then POST /complete so the hub records the .meta sidecar +
        confirms the object. Returns True on full success; False or an
        exception on any failure. What the caller does then depends on the
        kind: a non-video asset falls back to the legacy POST, a video does not
        (see ``_send_video_direct``).

        ``base`` overrides ``assign.asset_upload_base`` (a session has no
        assign). Streams the file from disk; the caller still gates on size
        (per-kind cap)."""
        base = (base or assign.asset_upload_base).rstrip("/")  # {hub}/jobs/{id}/assets
        body: dict[str, Any] = {"filename": name}
        if mime:
            body["mime"] = mime
        if source_url:
            body["source_url"] = source_url
        if page_url:
            body["page_url"] = page_url
        if self.worker_secret:
            body["secret"] = self.worker_secret
        # 1) presign
        r = await self._http.post(base + "/presign", json=body, timeout=30.0)
        r.raise_for_status()
        put_url = (r.json() or {}).get("put_url")
        if not put_url:
            return False
        # 2) PUT bytes STRAIGHT to MinIO (signed URL = auth; not via the hub).
        # Streamed from disk in chunks (a video can be GBs -- the old
        # f.read() held the whole file in worker memory). A presigned S3 PUT
        # needs an explicit Content-Length (MinIO rejects chunked encoding
        # here); httpx then sends the stream without Transfer-Encoding.
        _len = path.stat().st_size if size is None else int(size)
        headers = {"Content-Length": str(_len)}
        if mime:
            headers["Content-Type"] = mime
        # Bound the CONNECT phase separately from read/write: a bare
        # ``timeout=300.0`` applies 300s to connect too, so if MinIO is
        # unreachable from this worker (e.g. an egress-firewall DROP, or the
        # NAS is down) every PUT pins a half-open socket in SYN_SENT for the
        # full 300s, leaking fds until the worker hits its fd budget and
        # recycles (incident 2026-06-24). A short connect timeout fails fast
        # so the caller falls back to the bytes-through-hub POST in seconds,
        # holding no socket; read/write stay generous for large blobs.
        _put_timeout = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)
        pr = await self._http.put(
            put_url, content=_iter_file_chunks(path), headers=headers, timeout=_put_timeout
        )
        pr.raise_for_status()
        # 3) complete -> hub writes the sidecar + HEAD-confirms the object
        cbody = dict(body)
        cbody["size"] = _len
        # The bytes are on the store by now, so a hub that is restarting (502/
        # 503/504, a dropped connection) or has not seen the object yet (409)
        # is worth a moment's patience rather than a second PUT of the file.
        for _try in range(3):
            try:
                cr = await self._http.post(base + "/complete", json=cbody, timeout=30.0)
                cr.raise_for_status()
                break
            except Exception as e:
                _code = getattr(getattr(e, "response", None), "status_code", None)
                if _try >= 2 or (_code is not None and _code not in (409, 500, 502, 503, 504)):
                    raise
                await asyncio.sleep(2.0 * (_try + 1))
        return True

    async def _upload_special(self, assign: HubAssignJob, path: Path, kind: str) -> None:
        # /jobs/{id}/files/{kind} replaces the asset_upload_base path.
        url = assign.asset_upload_base.rsplit("/assets", 1)[0] + f"/files/{kind}"
        try:
            with open(path, "rb") as f:
                files = {"file": (kind, f, "application/octet-stream")}
                data = {}
                if self.worker_secret:
                    data["secret"] = self.worker_secret
                r = await self._http.post(url, files=files, data=data)
                r.raise_for_status()
        except Exception as e:
            await self._send(
                WorkerJobLog(
                    job_id=assign.job_id,
                    line=f"  !! {kind} upload failed: {e}",
                )
            )

    async def _upload_log(self, assign: HubAssignJob, path: Path) -> None:
        if path.exists():
            await self._upload_special(assign, path, "log.txt")

