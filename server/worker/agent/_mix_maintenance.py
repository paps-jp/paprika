"""WorkerAgent mixin: idle-tab reaper + disk/tmp cleanup + cookies.txt.

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


class _MaintenanceMixin:
    async def _idle_tab_reaper_loop(self) -> None:
        # Configurable cadence; default 60 s is frequent enough that
        # an idle lane looks clean within a minute of a job ending,
        # infrequent enough to be negligible load.
        interval = float(os.environ.get("PAPRIKA_IDLE_TAB_REAP_INTERVAL_S") or 60.0)
        try:
            while True:
                await asyncio.sleep(interval)
                if self.lane_pool is None:
                    continue
                for lane in self.lane_pool.lanes:
                    # Only touch genuinely-idle lanes. busy=True covers
                    # in-flight jobs AND held keep_session sessions, so
                    # we never close a tab someone is actually using.
                    if lane.busy:
                        continue
                    try:
                        await self._reap_lane_tabs(lane)
                    except Exception as e:
                        _logger.info(
                            f"[worker {self.worker_id}] idle tab reap "
                            f"lane #{lane.lane_idx} failed: "
                            f"{type(e).__name__}: {e}",
                        )
        except asyncio.CancelledError:
            return

    async def _reap_lane_tabs(self, lane) -> None:
        """Close every ``page`` tab on an idle lane except the first.

        Pure HTTP DevTools -- no nodriver attach. Chrome exposes:
          GET /json/list            -> [{id, type, url, ...}, ...]
          GET /json/close/{id}      -> closes that target
        ``type`` values other than ``"page"`` (service_worker,
        background_page, iframe, ...) are left alone so extension
        workers from an ambient profile survive.
        """
        import httpx

        base = f"http://localhost:{lane.chrome_port}"
        async with make_async_client(timeout=5.0) as cli:
            try:
                r = await cli.get(f"{base}/json/list")
                r.raise_for_status()
                targets = r.json()
            except Exception:
                # Chrome not reachable (restarting / mid-profile-swap)
                # -- skip this lane this round.
                return
            pages = [t for t in targets if t.get("type") == "page"]
            if len(pages) <= 1:
                return
            # Keep the first page target (usually the lane's original
            # about:blank from startup); close the rest.
            keep_id = pages[0].get("id")
            closed = 0
            for t in pages[1:]:
                tid = t.get("id")
                if not tid or tid == keep_id:
                    continue
                try:
                    await cli.get(f"{base}/json/close/{tid}")
                    closed += 1
                except Exception:
                    pass
            if closed:
                _logger.info(
                    f"[worker {self.worker_id}] idle tab reap "
                    f"lane #{lane.lane_idx}: closed {closed} leftover "
                    f"tab(s) (kept 1)",
                )

    async def _disk_cleanup_loop(self) -> None:
        """Periodically prune stale /tmp/paprika-* dirs left behind by
        crashes / ungraceful teardown. Idempotent; safe to run while
        new sessions are starting (age threshold + active-session check)."""
        interval = float(
            os.environ.get("PAPRIKA_TMP_SWEEP_INTERVAL_S") or 1800.0,  # 30 min
        )
        min_age = float(
            os.environ.get("PAPRIKA_TMP_SWEEP_MIN_AGE_S") or 1800.0,  # 30 min
        )
        # One pass immediately on startup so a worker that just came up
        # from a previous crash starts clean. Subsequent passes are spaced
        # by ``interval``.
        first = True
        try:
            while True:
                if first:
                    first = False
                else:
                    await asyncio.sleep(interval)
                try:
                    n, bytes_freed = await asyncio.to_thread(
                        self._sweep_tmp_orphans, min_age,
                    )
                    if n:
                        _logger.info(
                            f"[worker {self.worker_id}] tmp sweep: "
                            f"removed {n} orphan dir(s), "
                            f"~{bytes_freed // (1024*1024)} MiB freed",
                        )
                except Exception as e:
                    _logger.info(
                        f"[worker {self.worker_id}] tmp sweep failed: "
                        f"{type(e).__name__}: {e}",
                    )
        except asyncio.CancelledError:
            return

    def _sweep_tmp_orphans(self, min_age_s: float) -> tuple[int, int]:
        """One sweep pass. Synchronous (run in a thread because rmtree of
        multi-GB dirs is blocking). Returns ``(removed_count, bytes_freed)``."""
        import time as _t

        tmp = Path("/tmp")
        if not tmp.exists():
            return (0, 0)
        # Snapshot of live session IDs so we don't race a session_end
        # that fires mid-sweep. Slight race risk on a session that just
        # started but hasn't populated self._sessions yet -- the
        # min_age_s threshold (default 30 min) is the belt to the
        # active-set's suspenders.
        live_sids: set[str] = set(self._sessions.keys())
        # Job IDs whose deferred yt-dlp download is STILL RUNNING (lane
        # is freed, session is gone from self._sessions, but the bg
        # task is appending bytes to paprika-vid-<jobid>-*/). Without
        # this, a long single-file mp4 download with the 7200s timeout
        # would be swept out from under the live yt-dlp around the 30
        # min mark. See _TMP_SWEEP_PREFIXES comment.
        bg_tasks = getattr(self, "_bg_video_tasks", None) or {}
        live_jobs: set[str] = (
            set(bg_tasks.values()) if isinstance(bg_tasks, dict) else set()
        )
        # Combined keep-set: any tmpdir whose name contains one of
        # these strings is preserved this pass.
        keep_tokens: set[str] = {x for x in (live_sids | live_jobs) if x}
        now = _t.time()
        removed = 0
        freed = 0
        try:
            entries = list(tmp.iterdir())
        except Exception:
            return (0, 0)
        for entry in entries:
            name = entry.name
            if name in self._TMP_SWEEP_PROTECTED:
                continue
            if not any(name.startswith(p) for p in self._TMP_SWEEP_PREFIXES):
                continue
            try:
                if not entry.is_dir():
                    continue
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age < min_age_s:
                continue
            # If the dir name embeds a live session id OR an in-flight
            # background-download job id, keep it. Names follow
            # paprika-ses-<sid>-<rand> / paprika-profile-<key>-<rand> /
            # paprika-vid-<jobid>-<rand> / paprika-<jobid>-<rand>. The
            # substring match is loose but safe-direction: false
            # positives only KEEP a stale dir an extra cycle; the
            # protection NEVER deletes a live dir.
            if any(tok in name for tok in keep_tokens):
                continue
            try:
                # Best-effort directory size for the log line, capped so
                # we don't os.walk a huge tree just for telemetry.
                size = 0
                for root, _, files in os.walk(entry):
                    for f in files:
                        try:
                            size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                        if size > 100 * 1024 * 1024 * 1024:  # cap at 100 GiB
                            break
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
                freed += size
            except Exception:
                # Best-effort: skip and try again next sweep.
                continue
        return (removed, freed)

    async def _fetch_cookies_txt_for(self, target_url, state, log=None):
        """Fetch a Netscape cookies.txt for ``target_url``'s host from
        the hub's /hosts registry and write it to a temp file.

        Returns the temp Path on success, ``None`` when there are no
        cookies / the hub is unreachable / anything else goes wrong
        (caller then runs yt-dlp unauthenticated). The hub base URL
        is derived from ``state.asset_upload_base`` (which the hub
        set to ``http://<hub>/jobs/{id}/assets`` at session start).
        """
        import re as _re
        import tempfile
        from urllib.parse import urlparse

        import httpx

        try:
            host = (urlparse(target_url).hostname or "").lower()
        except Exception:
            host = ""
        if not host:
            return None
        if host.startswith("www."):
            host = host[4:]
        # Derive hub base from the asset upload base
        # (http://hub:8000/jobs/{id}/assets -> http://hub:8000).
        base = getattr(state, "asset_upload_base", None) or ""
        m = _re.match(r"^(https?://[^/]+)", base)
        if not m:
            return None
        hub_base = m.group(1)
        url = f"{hub_base}/hosts/{host}/cookies.txt"
        try:
            async with make_async_client(timeout=10.0) as cli:
                r = await cli.get(url)
                if r.status_code != 200:
                    return None
                body = r.text
        except Exception as e:
            if log:
                log(f"[download_video] cookies fetch failed for {host}: {e}")
            return None
        if not body or not body.strip() or body.strip() == "# Netscape HTTP Cookie File":
            return None
        try:
            fd, path = tempfile.mkstemp(prefix="ytdlp_cookies_", suffix=".txt")
            import os as _os

            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            return Path(path)
        except Exception:
            return None

