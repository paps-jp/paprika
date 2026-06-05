"""WorkerAgent mixin: noVNC preview screenshot/subscribe loop.

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


class _PreviewMixin:
    async def _handle_screenshot(self, req: HubScreenshotRequest) -> None:
        """Take a screenshot of the requested lane and reply over the WS."""
        import base64

        reply = WorkerScreenshotReply(
            req_id=req.req_id,
            lane_idx=req.lane_idx,
        )
        try:
            if self.lane_pool is None:
                raise RuntimeError("worker has no lane pool")
            if not (0 <= req.lane_idx < len(self.lane_pool.lanes)):
                raise RuntimeError(
                    f"lane_idx {req.lane_idx} out of range (have {len(self.lane_pool.lanes)} lanes)"
                )
            lane = self.lane_pool.lanes[req.lane_idx]
            jpeg = await lane.screenshot(
                max_width=req.max_width,
                quality=req.quality,
            )
            reply.jpeg_b64 = base64.b64encode(jpeg).decode("ascii")
        except Exception as e:
            reply.error = str(e)
        try:
            await self._send(reply)
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] failed to send screenshot reply: {e}"
            )

    def _on_preview_subscribe(self, msg: HubPreviewSubscribe) -> None:
        """Hub says an admin is watching us: (re)arm + extend the self-capture
        loop's interest window. None lanes = all of our active lanes."""
        if msg.lanes is None:
            n = len(self.lane_pool.lanes) if self.lane_pool is not None else 0
            self._preview_lanes = set(range(n))
        else:
            self._preview_lanes = {int(x) for x in msg.lanes}
        self._preview_interval = max(2.0, float(msg.interval_s or 10.0))
        self._preview_max_width = int(msg.max_width or 320)
        self._preview_quality = int(msg.quality or 5)
        self._preview_until = time.monotonic() + max(5.0, float(msg.ttl_s or 30.0))

    async def _preview_capture_loop(self) -> None:
        """While an admin is watching (interest not expired), capture each
        watched lane on a timer and PUSH it to the hub. The hub caches it in
        Redis for the #screens grid, so the grid never triggers a live
        capture. Self-quiesces (cheap idle poll) once the interest window
        lapses -- the hub stops refreshing it when nobody's watching."""
        import base64

        while True:
            try:
                lanes = self._preview_lanes
                if (
                    lanes
                    and self.lane_pool is not None
                    and time.monotonic() < self._preview_until
                ):
                    for li in sorted(lanes):
                        if not (0 <= li < len(self.lane_pool.lanes)):
                            continue
                        try:
                            jpeg = await self.lane_pool.lanes[li].screenshot(
                                max_width=self._preview_max_width,
                                quality=self._preview_quality,
                            )
                        except Exception:
                            continue
                        try:
                            await self._send(
                                WorkerPreviewFrame(
                                    lane_idx=li,
                                    jpeg_b64=base64.b64encode(jpeg).decode("ascii"),
                                    width=self._preview_max_width,
                                    ts=time.time(),
                                )
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(self._preview_interval)
                else:
                    # Not subscribed / interest expired: idle poll for re-arm.
                    await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2.0)

