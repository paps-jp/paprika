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
    ) -> bool:
        """Upload one fetch-mode asset to the hub. Returns True on
        success, False on failure. The bool lets the incremental
        on_asset_saved callback track which names actually landed so the
        end-of-fetch reconcile pass can re-try only the ones that didn't.
        """
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
            await self._send(
                WorkerJobLog(
                    job_id=assign.job_id,
                    line=f"  !! asset upload failed: {name}: {e}",
                )
            )
            return False

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

