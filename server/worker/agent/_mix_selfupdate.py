"""WorkerAgent mixin: rolling drain + self-update trigger.

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


class _SelfUpdateMixin:
    async def _maybe_begin_self_update(
        self, expected: str | None, *, source: str,
    ) -> None:
        """Start the rolling drain + self-update if our version meaningfully
        differs from the hub-advertised ``expected``.

        Idempotent: a no-op when an update is already in flight or auto-exit is
        disabled. Shared by the register handshake AND the periodic
        ``HubExpectedVersion`` heartbeat advert, so a worker self-updates WITHOUT
        needing a hub restart to force a reconnect. The hub's ``HubUpdateGate``
        still caps concurrency, so triggering many workers at once just makes
        them all drain + queue -- the fetch/exit rollout stays batched."""
        local_version = default_worker_version()
        if not _versions_meaningfully_differ(
            local=local_version, expected=expected or "",
        ):
            return
        _print_version_mismatch_banner(
            local=local_version, expected=expected or "", source=source,
        )
        if not (_auto_exit_on_version_mismatch() and self._self_update_task is None):
            # warn-only mode, or a self-update is already draining -> nothing to do.
            return
        self._pending_update_to = expected or ""
        self._draining = True
        self._update_gate.clear()
        try:
            await self._send(WorkerDraining(to_version=self._pending_update_to))
        except Exception:
            _logger.warning(
                f"[worker {self.worker_id}] could not notify hub of "
                f"drain-for-update; proceeding anyway",
                exc_info=True,
            )
        _logger.info(
            f"[worker {self.worker_id}] begin rolling self-update -> "
            f"{(expected or '')[:12]} (trigger: {source})"
        )
        self._self_update_task = asyncio.create_task(self._drain_and_self_update())

    async def _drain_and_self_update(self) -> None:
        """Drain in-flight work, await the hub's update slot, sleep
        for the assigned jitter, fetch source, exit(42).

        Replaces the previous "fetch + exit as soon as version
        mismatch is seen" pattern that caused the whole fleet to go
        dark simultaneously.  Concurrency is hub-gated
        (PAPRIKA_ROLLING_UPDATE_MAX_PARALLEL workers at a time) and
        spread further by per-worker jitter.

        Returns normally only on success or when self-update is
        disabled; on success calls ``sys.exit(WORKER_EXIT_CODE_
        VERSION_MISMATCH)`` so the docker restart policy boots a
        fresh container on the new code.
        """
        target = self._pending_update_to or "?"
        wid = self.worker_id
        # How long to wait for in-flight work before forcing the
        # update. Default 10 min: most video DLs finish; a stuck
        # one shouldn't block the whole fleet's update train.
        deadline_s = float(
            os.environ.get("PAPRIKA_DRAIN_DEADLINE_S", "600")
        )
        poll_s = 5.0
        start = time.monotonic()

        _logger.info(
            f"[worker {wid}] drain-for-update -> {target[:12]}: "
            f"waiting for in-flight work to finish "
            f"(deadline {deadline_s:.0f}s)"
        )

        # ---- Phase 1: drain in-flight work ----
        while True:
            elapsed = time.monotonic() - start
            ifc = self._in_flight
            scs = len(self._sessions)
            if ifc <= 0 and scs <= 0:
                _logger.info(
                    f"[worker {wid}] drain-for-update: idle "
                    f"(elapsed {elapsed:.0f}s); requesting update slot"
                )
                break
            if elapsed >= deadline_s:
                _logger.warning(
                    f"[worker {wid}] drain-for-update: deadline "
                    f"({deadline_s:.0f}s) reached with in_flight={ifc} "
                    f"sessions={scs}; forcing update anyway"
                )
                break
            try:
                await asyncio.sleep(poll_s)
            except asyncio.CancelledError:
                _logger.info(f"[worker {wid}] drain-for-update: cancelled")
                return

        # ---- Phase 2: wait for the hub's update gate ----
        # The hub caps concurrent updaters; we wait until it
        # green-lights us. If the hub goes away, fall back to
        # updating anyway after a generous timeout so a single dead
        # hub can't strand the fleet on old code forever.
        gate_timeout_s = float(
            os.environ.get("PAPRIKA_UPDATE_GATE_TIMEOUT_S", "900")
        )
        try:
            await asyncio.wait_for(
                self._update_gate.wait(),
                timeout=gate_timeout_s,
            )
            _logger.info(
                f"[worker {wid}] drain-for-update: hub granted update slot"
            )
        except asyncio.TimeoutError:
            _logger.warning(
                f"[worker {wid}] drain-for-update: hub gate timed out "
                f"({gate_timeout_s:.0f}s); proceeding without grant"
            )
        except asyncio.CancelledError:
            _logger.info(f"[worker {wid}] drain-for-update: cancelled at gate")
            return

        # ---- Phase 3: hub-assigned jitter ----
        if self._update_jitter_s > 0:
            _logger.info(
                f"[worker {wid}] drain-for-update: "
                f"sleeping {self._update_jitter_s:.1f}s jitter before fetch"
            )
            try:
                await asyncio.sleep(self._update_jitter_s)
            except asyncio.CancelledError:
                _logger.info(f"[worker {wid}] drain-for-update: cancelled in jitter")
                return

        # ---- Phase 4: fetch tarball ----
        if _auto_fetch_source():
            _logger.info(
                f"[worker {wid}] drain-for-update: fetching source from hub..."
            )
            try:
                applied = await _fetch_and_apply_source_from_hub(
                    hub_http_url=self.hub_http_url,
                    log_prefix=f"[worker {wid}]",
                )
                if applied:
                    _logger.info(
                        f"[worker {wid}] drain-for-update: "
                        f"source applied; restarting on new code"
                    )
                else:
                    _logger.warning(
                        f"[worker {wid}] drain-for-update: "
                        f"source fetch did not apply (see prior log); "
                        f"restarting on existing code"
                    )
            except Exception:
                _logger.warning(
                    f"[worker {wid}] drain-for-update: source fetch failed",
                    exc_info=True,
                )

        # ---- Phase 5: exit so docker restart picks up new code ----
        _logger.info(
            f"[worker {wid}] drain-for-update: exit({WORKER_EXIT_CODE_VERSION_MISMATCH})"
        )
        sys.exit(WORKER_EXIT_CODE_VERSION_MISMATCH)

