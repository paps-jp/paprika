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
from ._mix_run import _RunMixin
from ._mix_selfupdate import _SelfUpdateMixin
from ._mix_sessions import _SessionsMixin
from ._mix_jobexec import _JobExecMixin
from ._mix_profile import _ProfileExtMixin
from ._mix_maintenance import _MaintenanceMixin
from ._mix_preview import _PreviewMixin
from ._mix_uploads import _UploadsMixin


class WorkerAgent(
    _RunMixin,
    _SelfUpdateMixin,
    _SessionsMixin,
    _JobExecMixin,
    _ProfileExtMixin,
    _MaintenanceMixin,
    _PreviewMixin,
    _UploadsMixin,
):
    PAPRIKA_AGENT_ID = "gmhfgiloilioklcofcinlemifjjaeppe"

    _TMP_SWEEP_PROTECTED = {
        "paprika-profile-cache",     # canonical sync cache root
        "paprika-extensions",        # CRX cache, populated on register
    }

    _TMP_SWEEP_PREFIXES = (
        "paprika-ses-",
        "paprika-profile-",
        "paprika-vid-",
        "paprika-",                  # legacy / job tmpdirs (paprika-<jobid>-<rand>)
    )


    def __init__(
        self,
        hub_ws_url: str,
        worker_id: str,
        max_concurrent: int = 1,
        labels: dict[str, str] | None = None,
        chrome_host: str | None = None,
        chrome_port: int | None = None,
        worker_secret: str | None = None,
        novnc_url: str | None = None,
        lane_pool=None,  # Optional[LanePool]
    ) -> None:
        # hub_ws_url like ws://hub:8000  (no /...)
        self.hub_ws_url = hub_ws_url.rstrip("/")
        self.hub_http_url = hub_http_base(self.hub_ws_url)
        self.worker_id = worker_id
        self.max_concurrent = max_concurrent
        self.labels = labels or {}
        self.chrome_host = chrome_host
        self.chrome_port = chrome_port
        self.worker_secret = worker_secret
        self.novnc_url = novnc_url
        self.lane_pool = lane_pool

        # --- push-based preview: self-capture watched lanes + push to hub ---
        # Armed by HubPreviewSubscribe (an admin is watching); self-quiesces
        # when _preview_until lapses (hub stops refreshing = nobody watching).
        self._preview_lanes: set[int] | None = None
        self._preview_until = 0.0          # time.monotonic() expiry of interest
        self._preview_interval = 10.0
        self._preview_max_width = 320
        self._preview_quality = 5

        self._send_lock = asyncio.Lock()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._sem = asyncio.Semaphore(max_concurrent)
        self._in_flight = 0
        # --- self-recycle (drain-after-N) ---------------------------------
        # After this many completed assignments, stop accepting new jobs,
        # let in-flight finish, then exit so docker restarts us fresh --
        # clears any leaked processes / memory the way Selenium Grid's
        # SE_DRAIN_AFTER_SESSION_COUNT does. 0 disables.
        self._jobs_done = 0
        self._draining = False
        # Rolling self-update state (set when we detect a hub-advertised
        # version mismatch and start draining for an update). Replaces
        # the old "immediately fetch + exit(42)" thundering-herd flow.
        # See _drain_and_self_update() for the full sequence.
        self._pending_update_to: str | None = None
        self._update_gate: asyncio.Event = asyncio.Event()
        self._update_jitter_s: float = 0.0
        self._self_update_task: asyncio.Task | None = None
        # Heartbeat kick: when set, the heartbeat loop wakes immediately
        # instead of waiting the full HEARTBEAT_INTERVAL (10s). The
        # job-exec / session-start / lane-pool paths set this whenever the
        # worker's effective lane occupancy changes (acquire/release) so
        # the hub's view of in_flight catches up within ms instead of up
        # to a full interval -- closing the over-dispatch race where the
        # hub picks a worker that's actually full but whose last heartbeat
        # still reported "1 free" (incident 2026-06-16: w50182 mass-drain).
        self._heartbeat_kick: asyncio.Event = asyncio.Event()
        # Set of in-flight deferred-video-DL job_ids the hub has asked us
        # to wrap up gracefully (HubForceCompleteJob). The deferred-DL task
        # checks this in its finally block: when set, partial .part files
        # are ffmpeg-remuxed into playable .mp4 and uploaded so the job
        # ends as ``completed`` with ``result.partial=True`` instead of
        # vanishing into nothing. Cleared by the deferred task's
        # done_callback on exit.
        self._force_complete_job_ids: set[str] = set()
        try:
            self._recycle_after = int(os.environ.get("WORKER_RECYCLE_AFTER_JOBS", "200"))
        except (TypeError, ValueError):
            self._recycle_after = 200
        # --- self-heal (shutdown-on-failure) ------------------------------
        # If we can't keep a live hub link for this many seconds, exit so
        # docker restarts us clean instead of flapping/ghosting in place
        # (cf. Selenium SE_NODE_REGISTER_PERIOD + SHUTDOWN_ON_FAILURE).
        # _last_link_ok is the monotonic time of the last successful
        # heartbeat (= proof the WS to the hub is alive). 0 disables.
        try:
            self._reconnect_giveup_s = float(os.environ.get("WORKER_RECONNECT_GIVEUP_S", "120"))
        except (TypeError, ValueError):
            self._reconnect_giveup_s = 120.0
        self._last_link_ok = 0.0
        # Most recent (cpu_pct, mem_pct, disk_pct, disk_free_gb, load1)
        # sample captured by the heartbeat loop. Read by the per-job disk
        # preflight in _mix_jobexec without re-walking /proc, and reset
        # to all-zeros until the first heartbeat fires.
        self._last_resources: tuple[float, float, float, float, float] = (
            0.0, 0.0, 0.0, 0.0, 0.0,
        )
        # Robust hung-event-loop watchdog ("worker self-diagnosis, done right").
        # An off-loop daemon thread pokes the event loop via call_soon_threadsafe;
        # if the loop fails to run the poke for _wd_threshold_s it is GENUINELY
        # wedged (a sync call hogging the loop) -- NOT merely busy / starved.
        # call_soon callbacks still run under load (unlike the heartbeat the old
        # _reconnect_giveup_s keyed on, which false-fired on a busy worker and
        # stormed the fleet on all-at-once deploys -- that check is disabled, see
        # run()). Per-worker jitter desynchronises the fleet so a real wedge
        # never produces a lockstep exit. PAPRIKA_WORKER_WATCHDOG=0 disables it.
        self._wd_enabled = (
            os.environ.get("PAPRIKA_WORKER_WATCHDOG", "1").strip().lower()
            not in ("0", "false", "no", "off")
        )
        try:
            _wd_base = float(os.environ.get("PAPRIKA_WORKER_WATCHDOG_THRESHOLD_S", "300"))
        except (TypeError, ValueError):
            _wd_base = 300.0
        self._wd_threshold_s = max(60.0, _wd_base) + random.uniform(0.0, 60.0)
        self._wd_check_s = 30.0
        self._wd_last_pong = 0.0
        # v2 -- second wedge signal. A loop that still runs the call_soon poke
        # (so the pong check passes) but whose coroutines are stuck (no
        # successful hub heartbeat) is ALSO wedged, just async-style: a hung
        # await rather than a blocked loop. This is the DOMINANT heavy-site /
        # monsnode failure the pong check alone misses. Fire when _last_link_ok
        # has been stale far longer than the old _reconnect_giveup_s 120s that
        # false-fired on busy workers -- default ~10min (+jitter), so a normal
        # ~90s reconnect or a 120s load-induced heartbeat miss never trips it;
        # only a genuinely stuck worker does. Set
        # PAPRIKA_WORKER_WATCHDOG_LINK_THRESHOLD_S=0 to disable just this arm
        # (the loop-wedge arm above stays active).
        try:
            _wd_link_base = float(
                os.environ.get("PAPRIKA_WORKER_WATCHDOG_LINK_THRESHOLD_S", "600")
            )
        except (TypeError, ValueError):
            _wd_link_base = 600.0
        self._wd_link_threshold_s = (
            max(300.0, _wd_link_base) + random.uniform(0.0, 60.0)
            if _wd_link_base > 0
            else 0.0
        )
        # v3 -- INBOUND liveness. _last_link_ok only proves our SENDS succeed,
        # which on a stale proxied WS (worker<->nginx alive, nginx<->hub upstream
        # dead/wedged) keep "succeeding" while no hub consumes us -> a reaped
        # ghost the pong + link arms both miss. _last_inbound_ok is the monotonic
        # time of the last frame RECEIVED from the hub (the hub now echoes
        # HubExpectedVersion every heartbeat). Inbound silence past this threshold
        # while we believe we are connected => no hub serves us => exit + the
        # reconnect re-homes us via the consistent hash. Self-enabling: 0 (off)
        # until the first inbound, reset to 0 on every disconnect, so a
        # not-yet-upgraded hub or a reconnect window never false-fires. Default
        # ~5min (> the 120s ping_timeout stall window, << multi-hour ghosts).
        # 0 disables this arm.
        self._last_inbound_ok = 0.0
        try:
            _wd_inb_base = float(
                os.environ.get("PAPRIKA_WORKER_WATCHDOG_INBOUND_THRESHOLD_S", "300")
            )
        except (TypeError, ValueError):
            _wd_inb_base = 300.0
        self._wd_inbound_threshold_s = (
            max(180.0, _wd_inb_base) + random.uniform(0.0, 60.0)
            if _wd_inb_base > 0
            else 0.0
        )
        # Active session_id -> SessionState. Sessions hold the
        # nodriver browser/tab attached to a Lane between actions.
        self._sessions: dict[str, SessionState] = {}
        # session_ids that the hub has asked us to abort but which
        # weren't yet in ``self._sessions`` when the end-message
        # arrived (e.g. the hub's session_start ``wait_for(future,
        # timeout=60)`` fired before the worker finished setting up).
        # ``_handle_session_start`` checks this set right before
        # sending its ack and self-aborts if the sid is present, so a
        # slow initial navigation can't leak a lane forever.
        self._aborted_sessions: set[str] = set()
        self._http: httpx.AsyncClient | None = None
        # Local cache for operator-uploaded Chrome profiles. Filled by
        # HubProfileSync messages (broadcast on POST /profiles/{name})
        # and on every WS handshake (hub re-syncs its full state).
        # Map: profile_name -> {etag, dir, size_bytes}. The directory
        # holds an extracted "User Data"-shaped tree ready to copy
        # into a lane's user-data-dir slot. Lives under /tmp so it's
        # discarded on container restart (the hub re-sync at the next
        # WS handshake rebuilds it).
        self._profile_cache: dict[str, dict] = {}
        self._profile_cache_lock = asyncio.Lock()
        self._profile_cache_root = Path("/tmp/paprika-profile-cache")
        # Hub-managed extension cache: every enabled extension on the
        # hub gets fetched + extracted here so every lane can pass
        # them via --load-extension on Chrome startup. Mirrors the
        # profile-cache shape: one dir per extension slug, etag for
        # cache-busting on re-upload. The lane code reads this on
        # each Chrome launch (no per-lane state needed).
        self._extension_cache: dict[str, dict] = {}
        self._extension_cache_lock = asyncio.Lock()
        self._extension_cache_root = Path("/tmp/paprika-extensions")
        # Name of the operator-set "default" profile, mirrored from
        # HubProfileSync.is_default. When set, the worker proactively
        # installs this profile into every idle lane so noVNC viewers
        # see the operator's logged-in Chrome on lanes that haven't
        # run a job yet. None = no default set, lanes stay on the
        # baked-in chrome-lane-N user-data-dir.
        self._ambient_default_name: str | None = None

    async def _send(self, msg) -> None:
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            await ws.send(encode_msg(msg))

    async def report_engine_usage(
        self,
        *,
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        engine_slug: str = "",
        source: str = "",
    ) -> None:
        """Best-effort: tell the hub how many tokens a worker-side LLM call
        used, so it lands in the shared engine_usage counter (the qwen
        vision/agent traffic the hub can't see on its own). Needs SOME
        identity (model or slug); tokens may be 0 (still counts a request).
        Never raises -- usage accounting must not break a session action."""
        try:
            if not (model or engine_slug):
                return
            from server.protocol import WorkerEngineUsage
            await self._send(
                WorkerEngineUsage(
                    model=str(model or ""),
                    engine_slug=str(engine_slug or ""),
                    prompt_tokens=max(0, int(prompt_tokens or 0)),
                    completion_tokens=max(0, int(completion_tokens or 0)),
                    source=str(source or ""),
                )
            )
        except Exception:
            pass

    @property
    def capabilities(self) -> WorkerCapabilities:
        # If this worker runs a lane pool, expose each lane's noVNC URL so
        # the hub's admin UI can link from a live-screenshot tile straight
        # to the matching VNC viewer.
        lane_urls: list[str] = []
        if self.lane_pool is not None:
            lane_urls = [s.novnc_url for s in self.lane_pool.lanes]
        return WorkerCapabilities(
            max_concurrent=self.max_concurrent,
            labels=self.labels,
            chrome_attach_host=self.chrome_host,
            chrome_attach_port=self.chrome_port,
            chrome_version=None,
            has_yt_dlp=detect_yt_dlp(),
            version=default_worker_version(),
            novnc_url=self.novnc_url,
            lane_novnc_urls=lane_urls,
            supports_preview_push=True,
        )

