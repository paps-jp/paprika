"""WorkerAgent mixin: session start/action/agent/teardown.

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


class _SessionsMixin:
    async def _handle_session_start(self, msg: HubSessionStart) -> None:
        """Reserve a lane, attach nodriver, install tab-killer, ack hub."""
        import nodriver as uc

        sid = msg.session_id
        ack = WorkerSessionStartAck(session_id=sid)
        # Tracked outside the try so the except can release the lane even
        # when the failure happens AFTER acquire() but BEFORE the
        # SessionState is registered in self._sessions (the rollback
        # below pops self._sessions, which is empty in that window).
        lane = None
        try:
            if self.lane_pool is None:
                raise RuntimeError("worker has no lane pool")
            if sid in self._sessions:
                raise RuntimeError(f"session {sid} already exists on this worker")

            lane = await self.lane_pool.acquire(lane_hint=msg.lane_hint)
            if lane is None:
                raise RuntimeError(f"no free lane (lane_hint={msg.lane_hint})")

            # Operator-uploaded Chrome profile, if any. Same shape as
            # the /jobs path: download tarball, extract, re-point the
            # lane's user-data-dir, restart Chrome. Failure is
            # non-fatal -- the session still starts, just on the
            # lane's default profile. Restored on session end via
            # _teardown_session_state.
            profile_url = getattr(msg, "profile_url", None)
            if profile_url:
                pdir = await self._get_profile_for_job(
                    profile_url=profile_url,
                    profile_name=getattr(msg, "profile_name", None),
                    profile_etag=getattr(msg, "profile_etag", None),
                    scratch_key=sid,
                )
                if pdir is not None:
                    try:
                        await lane.use_profile(pdir)
                        _logger.info(
                            f"[session {sid}] operator profile installed "
                            f"into lane #{lane.lane_idx}",
                        )
                    except Exception as e:
                        _logger.info(
                            f"[session {sid}] lane.use_profile failed: "
                            f"{type(e).__name__}: {e} -- continuing "
                            f"with lane default",
                        )

            assets_dir = Path(tempfile.mkdtemp(prefix=f"paprika-ses-{sid}-"))
            (assets_dir / "assets").mkdir(parents=True, exist_ok=True)

            # Derive the parent job_id from the asset_upload_base URL
            # so dispatcher actions (fetch_refresh, etc.) that need it
            # can read state.job_id directly without re-parsing the
            # URL every time. Shape:
            #   http://hub:8000/jobs/{job_id}/assets
            # Split on "/jobs/" and take the segment up to "/assets".
            derived_job_id: str | None = None
            if msg.asset_upload_base:
                try:
                    after_jobs = msg.asset_upload_base.split("/jobs/", 1)[1]
                    derived_job_id = after_jobs.split("/", 1)[0] or None
                except Exception:
                    derived_job_id = None

            state = SessionState(
                session_id=sid,
                lane=lane,
                assets_dir=assets_dir / "assets",
                # Inherits from HubSessionStart -- the hub fills this in
                # when the session belongs to a parent job (codegen-loop
                # mode) so we know where to upload page.capture() output.
                asset_upload_base=msg.asset_upload_base,
                job_id=derived_job_id,
            )
            self._sessions[sid] = state

            # Attach nodriver in the same pattern as agent_runner.
            chrome = await uc.start(
                host="localhost",
                port=lane.chrome_port,
                browser_executable_path=sys.executable,
            )
            try:
                from urllib.parse import urlparse

                parsed = urlparse(chrome.websocket_url)
                if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
                    new_netloc = f"localhost:{lane.chrome_port}"
                    chrome.websocket_url = chrome.websocket_url.replace(
                        parsed.netloc,
                        new_netloc,
                        1,
                    )
            except Exception:
                pass
            state.browser = chrome
            _session_ua = await _get_browser_user_agent(chrome)

            # Aggressively reduce the browser to one tab so the next
            # navigation starts from a clean state. Done via CDP
            # Target.getTargets / closeTarget rather than nodriver's
            # cached tabs list, which can lag CDP state.
            try:
                await browser_ops.force_single_tab(
                    chrome,
                    log=lambda s: _logger.info(
                        f"[session {sid}] {s}",
                    ),
                )
            except Exception as e:
                _logger.info(
                    f"[worker {self.worker_id}] session {sid} "
                    f"force_single_tab at start failed: "
                    f"{type(e).__name__}: {e}",
                )

            target_url = msg.initial_url or "about:blank"
            # Grab the existing tab without navigating yet (initial url
            # is "about:blank" -- no-op). We need the tab in hand so we
            # can attach CDP listeners BEFORE the first real navigation,
            # otherwise the initial page's images/videos slip past us.
            state.tab = await chrome.get("about:blank", new_tab=False)

            # NOTE: install_tab_killer was removed in the multi-tab
            # refactor (Phase 1). Previously this enforced "1 lane =
            # 1 tab" by CDP-killing every new ``Target`` Chrome
            # created. We now let new tabs open freely so the
            # upcoming operator API (Phase 2: ``page.new_tab()``,
            # ``session.pages``, popup events) can address them as
            # first-class objects. JS-level same-origin
            # ``target="_blank"`` rewriting (``TAB_HOOKS_ENABLED``)
            # remains in place for one more release cycle so most
            # navigation still collapses into the main tab while the
            # operator API is being designed.
            # Passively persist every image/video/audio response the
            # browser fetches while this session is alive. Each saved
            # file is immediately uploaded to the parent job's /assets
            # so the gallery fills as the script crawls (mirrors what
            # fetch mode does for one-shot jobs).
            async def _on_session_asset_saved(path: Path, info: dict) -> None:
                await self._upload_one_session_asset(
                    state,
                    path,
                    mime=info.get("mime"),
                    source_url=info.get("url"),
                    page_url=info.get("document_url"),
                )

            # Session-wide video downloader. Created here (BEFORE the
            # CDP listener is hooked) so the listener can call its
            # ``maybe_download`` the moment an .m3u8 / .mpd appears
            # in network traffic -- yt-dlp starts merging segments
            # without waiting for the SDK to call page.download_video().
            # The same closures are also reused by _handle_session_agent
            # and the explicit download_video action handler, so all
            # three paths share one downloaded-URLs set (= no double-
            # download when an SDK call follows the passive trigger).
            async def _on_session_video_saved_open(path: Path, info: dict) -> None:
                try:
                    await self._upload_one_session_asset(
                        state,
                        path,
                        mime=info.get("mime"),
                        source_url=info.get("url"),
                        page_url=info.get("document_url"),
                    )
                except Exception as e:
                    _logger.info(
                        f"[session {sid}] session-open video upload "
                        f"failed: {type(e).__name__}: {e}"
                    )

            # job_log routes "operator-visible" downloader lines
            # (detection, progress, save / fail) to the parent
            # job's Live panel via WorkerJobLog -- on top of the
            # usual worker-stderr logging. Only wires up when the
            # session is bound to a parent job (SDK sets this via
            # parent_job_id, set by the codegen-loop runner's
            # PAPRIKA_JOB_ID env var).
            def _maybe_send_job_log(line: str) -> None:
                pjid = getattr(state, "job_id", None)
                if not pjid:
                    return
                try:
                    asyncio.ensure_future(
                        self._send(WorkerJobLog(job_id=pjid, line=line))
                    )
                except Exception:
                    pass

            # Stream captured-network deltas to the parent job's Live panel
            # Network tab (ephemeral netcap markers over the one /events
            # pipe), mirroring the fetch-mode url-capture poller. Sessions
            # don't run fetch()'s poller, so without this codegen-loop /
            # manual sessions never populate the Network tab live. Emits
            # only the delta since the last cycle (client dedups by URL);
            # _maybe_send_job_log no-ops when there's no parent job.
            # Cancelled in _teardown_session_state; the iteration cap is a
            # backstop against a leaked task if teardown is ever skipped.
            async def _netcap_streamer():
                _idx = 0
                for _ in range(2600):  # ~3900s >= session absolute_ttl
                    try:
                        await asyncio.sleep(1.5)
                    except asyncio.CancelledError:
                        return
                    try:
                        _nl = getattr(state, "network_log", None) or []
                        if len(_nl) > _idx:
                            _delta = _nl[_idx:]
                            _idx = len(_nl)
                            _net = [
                                {
                                    "url": _e.get("url", ""),
                                    "mime": _e.get("mime", ""),
                                    "size": _e.get("size"),
                                    "saved": bool(_e.get("saved")),
                                    "source": _e.get("source", ""),
                                }
                                for _e in _delta
                                if isinstance(_e, dict) and _e.get("url")
                            ]
                            if _net:
                                _maybe_send_job_log(
                                    NET_CAPTURE_MARKER
                                    + json.dumps({"net": _net}, ensure_ascii=False)
                                )
                    except Exception:
                        pass

            try:
                _old_nct = getattr(state, "netcap_task", None)
                if _old_nct is not None and not _old_nct.done():
                    _old_nct.cancel()
            except Exception:
                pass
            state.netcap_task = asyncio.ensure_future(_netcap_streamer())

            _raw_downloader, drain_video_session = _make_video_downloader(
                assets_dir=state.assets_dir,
                min_asset_size=int(
                    os.environ.get("MIN_ASSET_SIZE_BYTES", "0") or 0
                ),
                on_saved=_on_session_video_saved_open,
                log=lambda s: _logger.info(f"[session {sid}] {s}"),
                job_id_for_logs=f"session-{sid}",
                job_log=_maybe_send_job_log,
                session_id=sid,
                # Top-level page URL referer fallback for cross-origin
                # iframe player streams (e.g. supjav's supremejav iframe).
                # state.last_response tracks the most recent top-level
                # document load, so its url is the page the operator is
                # actually on -- the referer the CDN expects.
                page_url_provider=lambda: (
                    (state.last_response or {}).get("url")
                    if isinstance(getattr(state, "last_response", None), dict)
                    else None
                ),
                user_agent=_session_ua,
            )
            # Asset URL blacklist wrapper (V + Y): glob/regex deny list.
            # passive on_stream_detected inside install_session_asset_capture
            # already filters at the capture layer; this wrapper covers the
            # explicit SDK call path (page.download_video(url=...)) so the
            # same list governs both. See core/url_blacklist.py for syntax.
            from core.url_blacklist import compile_blacklist as _compile_blacklist_yt
            _video_bl_matcher = _compile_blacklist_yt(
                getattr(msg, "asset_url_blacklist", []) or ()
            )

            def maybe_download_video_session(url, referer=""):
                if url:
                    hit = _video_bl_matcher.match(url)
                    if hit is not None:
                        _logger.info(
                            f"[session {sid}] yt-dlp BLOCK (blacklist={hit!r}) {url[:120]}"
                        )
                        return None
                return _raw_downloader(url, referer)

            state.video_downloader = maybe_download_video_session
            state.video_drainer = drain_video_session

            # Passive "last main-document response" tracker so
            # page.last_response() always reflects whatever the most
            # recent top-level navigation returned -- including
            # click-induced ones where _capture_nav_response can't
            # bracket the call.
            def _set_last_response(info: dict) -> None:
                state.last_response = info
            try:
                await browser_ops.install_last_response_tracker(
                    state.tab,
                    on_response_captured=_set_last_response,
                    log=lambda s: _logger.info(f"[session {sid}] {s}"),
                )
            except Exception as e:
                _logger.info(
                    f"[session {sid}] last_response tracker install failed "
                    f"(non-fatal): {type(e).__name__}: {e}"
                )

            await browser_ops.install_session_asset_capture(
                state.tab,
                state.assets_dir,
                on_saved=_on_session_asset_saved,
                log=lambda s: _logger.info(f"[session {sid}] {s}"),
                # Share the session's dedup set so URLs already captured
                # on an earlier page aren't saved again with _1/_2/...
                # suffixes when the script revisits them.
                seen_urls=state.seen_asset_urls,
                # Hub-managed min-size filter (Settings → "Asset
                # capture"). 0 = save everything; otherwise the
                # passive listener drops anything below the
                # threshold without writing or uploading it.
                min_asset_size_bytes=getattr(msg, "min_asset_size_bytes", 0) or 0,
                # Asset URL blacklist (V): operator-managed substring
                # deny list pulled from Settings. The same list is also
                # checked in maybe_download_video_session below so
                # blocked URLs don't trigger yt-dlp.
                url_blacklist=tuple(getattr(msg, "asset_url_blacklist", []) or ()),
                # Feed the session's network_log list so the Live
                # panel "Network" tab can display all observed media
                # traffic and let the operator cherry-pick assets.
                network_log=state.network_log,
                # Auto-fire yt-dlp on every .m3u8 / .mpd response so
                # HLS/DASH streams are merged into a playable mp4
                # without an explicit SDK call. Idempotent on URL.
                on_stream_detected=maybe_download_video_session,
                # iframe + nested-iframe deep network trace. ALWAYS on
                # for a capturing session: the asset dir is always
                # present here, and many video sites (supjav, DMM
                # litevideo, embedded players) stream HLS *inside a
                # cross-origin iframe*. Without deep-trace the parent
                # CDP target never sees those .m3u8 requests, so
                # on_stream_detected never fires and a video that
                # visibly plays during the session is captured as
                # zero bytes (job 8a10c9289262: video played, nothing
                # downloaded, because it was submitted download_video=
                # False so deep-trace stayed deferred and the script's
                # goal never called page.download_video()).
                #
                # Mirrors the same always-on decision in core/fetcher
                # (fetch path). The old download_video gate + the
                # page.download_video() late-enable hook left a hole:
                # a session that merely *plays* a video (rather than
                # explicitly downloading it) was invisible. The CDP
                # attach overhead is acceptable for a video-archiving
                # tool where any played stream should be preserved.
                enable_iframe_deep_trace=True,
            )
            # Per-host cookies auto-injected by the hub. Install them via
            # CDP Network.setCookies BEFORE the initial navigation so the
            # very first request already carries the session. Skipping
            # silently when the list is empty/None keeps non-cookie hosts
            # on the existing zero-config path.
            if msg.cookies:
                try:
                    from nodriver import cdp as _cdp

                    from core.fetcher import _to_cdp_cookie_params

                    params = _to_cdp_cookie_params(msg.cookies)
                    if params:
                        await state.tab.send(_cdp.network.set_cookies(cookies=params))
                        _logger.info(
                            f"[worker {self.worker_id}] session {sid} "
                            f"installed {len(params)} cookie(s) before "
                            f"navigation "
                            f"({len(msg.cookies) - len(params)} dropped)",
                        )
                    else:
                        _logger.info(
                            f"[worker {self.worker_id}] session {sid} "
                            f"all cookies dropped as invalid",
                        )
                except Exception as e:
                    # Best-effort: the script can still set_cookies()
                    # manually if the auto-injection failed (e.g. one
                    # of the cookies had an unexpected field).
                    _logger.info(
                        f"[worker {self.worker_id}] session {sid} cookie "
                        f"injection failed: {type(e).__name__}: {e}",
                    )

            # Now do the actual navigation -- hooks are armed. Use the
            # low-level cdp.page.navigate so the tab keeps the same CDP
            # session id; ``state.tab.get()`` (the nodriver high-level
            # helper) can re-attach to a fresh target, which detaches
            # the listeners we just installed and makes
            # network.get_response_body() fail with -32000.
            if target_url != "about:blank":
                try:
                    from nodriver import cdp as _cdp

                    await state.tab.send(_cdp.page.navigate(target_url))
                except Exception:
                    # Best-effort: the script can page.goto() to retry.
                    pass
                # Wait for the initial page to reach DOM-ready BEFORE we ack
                # the hub. Otherwise ``async with cli.session(url) as page:``
                # returns while the page is still loading and the caller's
                # first ``page.click(...)`` hits NO_MATCH on an unparsed DOM.
                # Bounded by NAV_LOAD_TIMEOUT_S (kept under the hub's 60s
                # start_session timeout, so this never trips it).
                try:
                    await browser_ops.wait_for_load(
                        state.tab,
                        lambda s: _logger.info(f"[session {sid}] {s}"),
                    )
                except Exception:
                    pass

            ack.lane_idx = lane.lane_idx
            ack.novnc_url = lane.novnc_url
            _logger.info(
                f"[worker {self.worker_id}] session {sid} -> lane #{lane.lane_idx} at {target_url}",
            )
        except Exception as e:
            # Roll back partial state on error.
            ack.error = f"{type(e).__name__}: {e}"
            state = self._sessions.pop(sid, None)
            if state is not None:
                if state.browser is not None:
                    try:
                        await state.browser.stop()
                    except Exception:
                        pass
                if state.lane is not None and self.lane_pool is not None:
                    self.lane_pool.release(state.lane)
            elif lane is not None and self.lane_pool is not None:
                # Lane was acquired but the failure happened before the
                # SessionState was registered (operator-profile install
                # or mkdtemp threw between acquire() and
                # self._sessions[sid] = state). Without releasing it
                # here the lane leaks permanently -- the classic
                # "no free lane / fleet at capacity" after a transient
                # profile-fetch error.
                self.lane_pool.release(lane)
            _logger.info(
                f"[worker {self.worker_id}] session {sid} start failed: {ack.error}",
            )
        # ---- abort checkpoint -------------------------------------
        # The hub's start_session has a bounded wait_for() (default
        # 60s). If we took longer than that, the hub will have
        # given up + likely sent a HubSessionEnd to release the
        # lane. That end message couldn't find a session because we
        # were still constructing this one, so _handle_session_end
        # parked the sid in self._aborted_sessions. Notice it now,
        # before we return from session_start, and tear ourselves
        # down so the lane goes back to the pool. Without this, a
        # slow initial navigation leaks a lane every time.
        if sid in self._aborted_sessions:
            self._aborted_sessions.discard(sid)
            aborted = self._sessions.pop(sid, None)
            if aborted is not None:
                _logger.info(
                    f"[worker {self.worker_id}] session {sid} aborted "
                    f"by hub during start; releasing lane",
                )
                await self._teardown_session_state(sid, aborted)
            if not ack.error:
                ack.error = "session aborted by hub during start"
        try:
            await self._send(ack)
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] failed to send session_start_ack: {e}"
            )

    async def _handle_session_action(self, msg: HubSessionAction) -> None:
        """Dispatch one action against a bound session via browser_ops."""
        sid = msg.session_id
        rid = msg.request_id
        reply = WorkerSessionActionResult(
            session_id=sid,
            request_id=rid,
            status="OK",
        )
        state = self._sessions.get(sid)
        if state is None:
            reply.status = f"ERR: session {sid} not found on this worker"
            try:
                await self._send(reply)
            except Exception:
                pass
            return

        t0 = time.time()

        # Quiet log function for the session path -- per-action log
        # lines just go to stderr for now. Sessions don't have an
        # operator log stream like jobs do.
        def _slog(line: str) -> None:
            _logger.info(f"[session {sid}] {line}")

        # Fetch-owned sessions are read-only: the fetch loop is driving
        # the tab and a write action mid-fetch would race. Whether a kind
        # is allowed mid-fetch (read-only) or is session-level is read off
        # its registry ``_ActionSpec`` -- the single source of truth.
        action = msg.action or {}
        kind = action.get("kind") or ""
        spec = _SESSION_ACTIONS.get(kind)
        # A read-only evaluate (forensics probe) is also permitted
        # mid-fetch. The hub's forensics loop pre-flights every probe
        # against a safety regex (rejects navigate / click / submit /
        # cookie+storage writes / POST fetch / DOM mutation), so the JS
        # only READS the page -- as safe as outline / screenshot / state,
        # which are already allowed. The ``read_only`` flag is set ONLY
        # by server/hub/routes/forensics.py; the normal /evaluate route
        # never sets it, so operator writes mid-fetch stay blocked.
        _is_ro_evaluate = kind == "evaluate" and bool(action.get("read_only"))
        if (
            state.is_fetch_owned
            and not (spec is not None and spec.read_only)
            and not _is_ro_evaluate
        ):
            _ro_kinds = sorted(k for k, s in _SESSION_ACTIONS.items() if s.read_only)
            reply.status = (
                f"ERR: session {sid} is owned by a running fetch job; "
                f"only read-only actions are allowed "
                f"({_ro_kinds} + read-only evaluate). "
                f"Got: {kind!r}"
            )
            reply.elapsed_ms = int((time.time() - t0) * 1000)
            try:
                await self._send(reply)
            except Exception:
                pass
            return

        # Pick the target tab. ``action.get('page_id')`` overrides the
        # session's default; falls back to default_page_id otherwise.
        # Session-level kinds use state.lock; per-tab kinds use the
        # page's own lock so two pages in the same session can run
        # actions in parallel.
        target_pid = action.get("page_id") or state.default_page_id
        if spec is not None and spec.session_level:
            chosen_lock = state.lock
        else:
            chosen_lock = state.page_locks.get(target_pid) or state.lock

        async with chosen_lock:
            try:
                # For per-tab kinds, look up the target Tab. For
                # session-level kinds, fall back to default (used only
                # for the "snapshot URL" bookkeeping below).
                tab = state.pages.get(target_pid) if target_pid else None
                if tab is None and not (spec is not None and spec.session_level):
                    reply.status = (
                        f"ERR: unknown page_id {target_pid!r} (known: {sorted(state.pages.keys())})"
                    )
                    reply.elapsed_ms = int((time.time() - t0) * 1000)
                    try:
                        await self._send(reply)
                    except Exception:
                        pass
                    return

                # Snapshot the current URL into visited_urls so the
                # visited=true marker works for the next outline. Only
                # meaningful for per-tab kinds (tab exists). ``cur`` is
                # initialised first so the ctx below always has it (a
                # session-level kind has tab=None and never assigns it).
                cur = ""
                if tab is not None:
                    try:
                        cur = await tab.evaluate("document.location.href")
                    except Exception:
                        cur = ""
                    if cur:
                        state.note_url(browser_ops.canon_url(cur))

                # Plugin-style dispatch: every explicit action kind is
                # handled by its decorated ``_act_<kind>`` method in the
                # _SESSION_ACTIONS registry. The generic mutating kinds
                # (click / fill / scroll / navigate / back / ...) have no
                # per-kind handler -- they fall to the catch-all ``else``
                # which delegates uniformly to ``browser_ops.execute``.
                ctx = _ActionCtx(
                    state=state, tab=tab, action=action, reply=reply,
                    cur=cur, slog=_slog, t0=t0, msg=msg,
                )
                if spec is not None:
                    await spec.fn(self, ctx)
                else:
                    # click / fill / press_key / scroll / navigate /
                    # back / wait -- delegate to browser_ops.execute
                    # which understands these kinds.
                    #
                    # For nav-kind actions (navigate / back / forward /
                    # history_first) we also capture the main document's
                    # HTTP response (status, headers, final URL) so the
                    # SDK can surface a Playwright-compatible Response
                    # object to the caller. Non-nav kinds return resp=None.
                    nav_kinds = ("navigate", "back", "forward", "history_first")
                    if action.get("kind") in nav_kinds:
                        status_str, resp_info = await browser_ops.execute_nav_with_response(
                            tab, action, _slog,
                        )
                        reply.status = status_str
                        if resp_info:
                            # Put the HTTP response info under reply.result
                            # as a dict; existing nav callers expect
                            # result=None so we use {"response": {...}}
                            # to keep the new key namespaced and easy to
                            # spot.
                            reply.result = {"response": resp_info}
                    else:
                        reply.status = await browser_ops.execute(
                            tab,
                            action,
                            _slog,
                        )
            except Exception as e:
                reply.status = f"ERR: {type(e).__name__}: {e}"
                _slog(f"action {msg.action.get('kind')!r} crashed: {e}")

        reply.elapsed_ms = int((time.time() - t0) * 1000)
        try:
            await self._send(reply)
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] failed to send session_action_result: {e}",
            )

    async def _handle_session_agent(self, msg: HubSessionAgent) -> None:
        """Run a localised agent loop on the session's bound tab.

        Equivalent to ``await page.agent(goal, max_steps, engine)`` in
        the SDK. Each ``step`` is observe -> ask engine -> execute, up
        to ``max_steps`` iterations or until the engine emits
        ``done`` / ``end``. The session's existing nodriver tab is
        reused (no separate Lane needed).

        ``msg.engine`` selects the driver:

          - ``"qwen"``:     Qwen-VL via agent_service /act (selectors).
          - ``"cogagent"``: CogAgent via cogagent_service /act (pixel
                            boxes -> execute_vision_action).
          - ``"auto"``:     CogAgent first; if its action looks suspect
                            (corner / repeat / out-of-viewport), retry
                            this step via Qwen-VL.
        """
        sid = msg.session_id
        rid = msg.request_id
        result = WorkerSessionAgentResult(
            session_id=sid,
            request_id=rid,
        )
        state = self._sessions.get(sid)
        if state is None:
            result.error = f"session {sid} not found"
            try:
                await self._send(result)
            except Exception:
                pass
            return

        # Lazy imports so the session_agent path doesn't pay for them
        # on workers that never call it.
        import httpx
        from nodriver import cdp as _cdp

        from server.worker import browser_ops as bops

        engine = (getattr(msg, "engine", None) or "auto").lower()
        # v2 cleanup: cogagent / vision-agent retired. The auto + cogagent
        # branches inside this function are now dead code reachable only
        # via the engine literal which Pydantic now rejects ("qwen"/"auto"
        # only). Force-route both to qwen so even a hand-crafted WS
        # message can't dive into the CogAgent codepath that depends on
        # the deleted ``browser_ops.execute_vision_action``. Full
        # surgical extraction of the cogagent branches is a follow-up
        # cleanup that requires careful test coverage.
        if engine in ("auto", "cogagent"):
            engine = "qwen"

        # Env knobs (read per-request so changes are hot-reloadable
        # without a worker restart).
        agent_url = os.environ.get(
            "AGENT_URL",
            "http://<worker-host>:8001",
        ).rstrip("/")
        agent_timeout_s = float(
            os.environ.get("AGENT_REQUEST_TIMEOUT_S", "180"),
        )
        send_screenshots = os.environ.get("AGENT_SEND_SCREENSHOTS", "0") not in ("0", "false", "no")
        cogagent_url = os.environ.get(
            "COGAGENT_URL",
            "http://<gpu-host>:15083",
        ).rstrip("/")
        cogagent_timeout_s = float(
            os.environ.get("COGAGENT_REQUEST_TIMEOUT_S", "120"),
        )
        agent_llm_url = os.environ.get(
            "AGENT_LLM_URL",
            "http://<gpu-host>:15082",
        ).rstrip("/")
        agent_llm_model = os.environ.get(
            "AGENT_MODEL_NAME",
            "qwen2.5-vl-72b",
        )

        def _slog(line: str) -> None:
            _logger.info(f"[session {sid} agent] {line}")

        # ---- JP/CN -> EN goal translation -----------------------------
        # CogAgent (and to a lesser extent Qwen-VL prompt templates) has
        # known weakness with Japanese task descriptions. We translate
        # once up front via the configured chat LLM (Qwen2.5-VL is
        # bilingual). Always-English goals short-circuit the cheap
        # _looks_non_english check.
        translated_goal = msg.goal
        if _looks_non_english(msg.goal):
            _slog(f"goal looks non-english; translating: {msg.goal!r}")
            translated_goal = await _translate_to_english(
                msg.goal,
                agent_llm_url=agent_llm_url,
                model_name=agent_llm_model,
                timeout_s=30.0,
                log=_slog,
            )
            if translated_goal != msg.goal:
                _slog(f"translated -> {translated_goal!r}")

        # ---- per-engine step helpers ----------------------------------
        # Both return a (action_dict, outcome_hint) pair where
        # outcome_hint is "OK" on success or "ERR: ..." / "NO_MATCH"
        # otherwise. action_dict has the same shape across engines so
        # the executor below doesn't care which produced it.

        # Shared history of compact strings; both engines see the same
        # tail. Qwen wants "kind -> outcome", CogAgent wants
        # "CLICK(...) <desc>" -- we just send "kind -> outcome" to both
        # for simplicity; CogAgent tolerates the shorter format.
        history: list[str] = []
        # Parallel structured trace of (n, engine, kind, outcome) shipped
        # back in WorkerSessionAgentResult.steps so the SDK can print
        # them to the job log. ``history`` stays as compact strings
        # because the engines see them in their /act prompts and the
        # format matters there; ``step_events`` is just for the
        # operator-facing trace.
        step_events: list[dict[str, Any]] = []
        # Last CogAgent box for the suspect-detector's "loop" check.
        last_cogagent_box: dict | None = None
        last_action: dict | None = None
        completed = False
        steps_taken = 0
        summary: str | None = None
        # Track visited URLs so the video downloader can detect
        # "the page just navigated to *.mp4" between steps. Without
        # this, popup_policy=follow on gallery / streaming sites would
        # leave the click successful but the actual MP4 unsaved
        # (Chrome's media player swallows the response before our
        # CDP listeners see it).
        session_visited_urls: list[str] = []

        # Video downloader: prefer the session-wide closures hooked up
        # at session_open (they share the downloaded-URLs set with the
        # passive on_response m3u8 trigger and the explicit
        # download_video action -- so the same URL isn't re-downloaded
        # by three different code paths). Fall back to a local pair
        # only when the session pre-dates the wiring (e.g. fetch-owned
        # sessions that never went through _handle_session_open).
        if state.video_downloader is not None and state.video_drainer is not None:
            maybe_download_video = state.video_downloader
            drain_video_downloads = state.video_drainer
        else:
            async def _on_session_video_saved(path: Path, info: dict) -> None:
                try:
                    await self._upload_one_session_asset(
                        state,
                        path,
                        mime=info.get("mime"),
                        source_url=info.get("url"),
                        page_url=info.get("document_url"),
                    )
                except Exception as e:
                    _slog(f"video upload failed: {type(e).__name__}: {e}")

            _agent_ua = await _get_browser_user_agent(state.browser) if state.browser else None
            maybe_download_video, drain_video_downloads = _make_video_downloader(
                assets_dir=state.assets_dir,
                min_asset_size=int(os.environ.get("MIN_ASSET_SIZE_BYTES", "0") or 0),
                on_saved=_on_session_video_saved,
                log=lambda s: _slog(s),
                job_id_for_logs=f"session-{sid}",
                session_id=sid,
                page_url_provider=lambda: (
                    (state.last_response or {}).get("url")
                    if isinstance(getattr(state, "last_response", None), dict)
                    else None
                ),
                user_agent=_agent_ua,
            )

        async def _viewport(tab) -> tuple[int, int]:
            try:
                vp_str = await tab.evaluate(
                    "JSON.stringify({w: window.innerWidth, h: window.innerHeight})"
                )
                import json as _json

                vp = _json.loads(vp_str or "{}")
                return int(vp.get("w") or 1280), int(vp.get("h") or 720)
            except Exception:
                return 1280, 720

        async def _ask_qwen(
            client, current_url: str, outline: str, screenshot_b64: str | None, step: int
        ) -> tuple[dict, str | None]:
            req = {
                "goal": translated_goal,
                "url": current_url or "",
                "ax_tree": outline,
                "history": history[-10:],
                "step": step,
                "max_steps": msg.max_steps,
            }
            if screenshot_b64:
                req["image_b64"] = screenshot_b64
            try:
                r = await client.post(f"{agent_url}/act", json=req, timeout=agent_timeout_s)
                r.raise_for_status()
                payload = r.json()
            except Exception as e:
                return ({"kind": "unknown"}, f"ERR: qwen /act: {e}")
            # Report this step's token usage to the hub so qwen's agent-loop
            # traffic shows in #engines. agent-service /act returns no usage
            # block today -> tokens 0 (still counts the request); real tokens
            # arrive once agent_service surfaces `usage` in ActResponse.
            try:
                _u = payload.get("usage") or {}
                await self.report_engine_usage(
                    model=agent_llm_model,
                    prompt_tokens=_u.get("prompt_tokens"),
                    completion_tokens=_u.get("completion_tokens"),
                    source="agent",
                )
            except Exception:
                pass
            return (payload.get("action") or {"kind": "unknown"}, None)

        async def _ask_cogagent(
            client,
            screenshot_b64: str,
            viewport_w: int,
            viewport_h: int,
        ) -> tuple[dict, str | None]:
            req = {
                "task": translated_goal,
                "image_b64": screenshot_b64,
                "image_width": viewport_w,
                "image_height": viewport_h,
                "history": history[-20:],
                "platform": "WIN",
                "answer_format": "Action-Operation",
                "max_new_tokens": 512,
                "temperature": 0.0,
            }
            try:
                r = await client.post(f"{cogagent_url}/act", json=req, timeout=cogagent_timeout_s)
                r.raise_for_status()
                payload = r.json()
            except Exception as e:
                return ({"kind": "unknown"}, f"ERR: cogagent /act: {e}")
            return (payload.get("action") or {"kind": "unknown"}, None)

        async def _execute(
            action: dict,
            viewport_w: int,
            viewport_h: int,
        ) -> str:
            kind = action.get("kind") or "unknown"
            # Selector-shaped actions (from Qwen) go through bops.execute.
            # Box-shaped actions (from CogAgent) go through
            # execute_vision_action. Detect by which fields are present.
            if action.get("box"):
                try:
                    return await bops.execute_vision_action(
                        state.tab,
                        action,
                        _slog,
                        viewport_width=viewport_w,
                        viewport_height=viewport_h,
                    )
                except Exception as e:
                    return f"ERR: {type(e).__name__}: {e}"
            else:
                try:
                    return await bops.execute(state.tab, action, _slog)
                except Exception as e:
                    return f"ERR: {type(e).__name__}: {e}"

        async with state.lock:
            try:
                async with make_async_client(timeout=agent_timeout_s) as client:
                    for step in range(1, max(1, msg.max_steps) + 1):
                        # ---- observe ---------------------------------
                        try:
                            current_url = await state.tab.evaluate("document.location.href")
                        except Exception:
                            current_url = ""
                        if current_url:
                            canon = bops.canon_url(current_url)
                            state.note_url(canon)
                            # Detect URL change since last step and
                            # fire off a video downloader if we landed
                            # on a media URL. mirrors the same logic
                            # in _run_vision_agent_job so page.agent()
                            # gets the same MP4-from-popup-follow
                            # behaviour as mode=vision-agent jobs.
                            if not session_visited_urls or session_visited_urls[-1] != canon:
                                prev_url = session_visited_urls[-1] if session_visited_urls else ""
                                session_visited_urls.append(canon)
                                maybe_download_video(current_url, prev_url)

                        viewport_w, viewport_h = await _viewport(state.tab)

                        # CogAgent always needs a screenshot. Qwen only
                        # when configured. Grab once and share.
                        png_b64: str | None = None
                        if engine != "qwen" or send_screenshots:
                            try:
                                png_b64 = await state.tab.send(
                                    _cdp.page.capture_screenshot(format_="png"),
                                )
                            except Exception as e:
                                _slog(f"screenshot failed: {e}")

                        # Qwen also wants the page outline.
                        outline: str | None = None
                        if engine in ("qwen", "auto"):
                            try:
                                outline = await bops.outline(
                                    state.tab,
                                    visited_urls=state.visited_urls,
                                )
                            except Exception as e:
                                outline = ""
                                _slog(f"outline failed: {e}")

                        # ---- choose engine + ask ---------------------
                        action: dict = {"kind": "unknown"}
                        ask_err: str | None = None
                        used_engine = engine

                        if engine == "qwen":
                            action, ask_err = await _ask_qwen(
                                client,
                                current_url,
                                outline or "",
                                png_b64 if send_screenshots else None,
                                step,
                            )
                        elif engine == "cogagent":
                            if png_b64 is None:
                                ask_err = "cogagent needs screenshot but capture failed"
                            else:
                                action, ask_err = await _ask_cogagent(
                                    client,
                                    png_b64,
                                    viewport_w,
                                    viewport_h,
                                )
                        else:  # auto
                            # 1) Try CogAgent first.
                            if png_b64 is None:
                                _slog("auto: no screenshot; falling back to qwen directly")
                                action, ask_err = await _ask_qwen(
                                    client,
                                    current_url,
                                    outline or "",
                                    None,
                                    step,
                                )
                                used_engine = "qwen"
                            else:
                                cog_action, cog_err = await _ask_cogagent(
                                    client,
                                    png_b64,
                                    viewport_w,
                                    viewport_h,
                                )
                                suspect: str | None = None
                                if cog_err:
                                    suspect = cog_err
                                else:
                                    suspect = _looks_suspect(
                                        cog_action,
                                        viewport_w=viewport_w,
                                        viewport_h=viewport_h,
                                        last_box=last_cogagent_box,
                                    )
                                if suspect:
                                    _slog(
                                        f"[auto step {step}] cogagent "
                                        f"suspect ({suspect}); falling back to qwen"
                                    )
                                    action, ask_err = await _ask_qwen(
                                        client,
                                        current_url,
                                        outline or "",
                                        png_b64 if send_screenshots else None,
                                        step,
                                    )
                                    used_engine = "qwen"
                                else:
                                    action = cog_action
                                    used_engine = "cogagent"
                                    # Remember box for next step's
                                    # loop-detection.
                                    last_cogagent_box = (
                                        cog_action.get("box")
                                        if isinstance(cog_action, dict)
                                        else None
                                    )

                        if ask_err:
                            result.error = ask_err
                            _slog(f"abort step {step}: {ask_err}")
                            break

                        kind = action.get("kind") or "unknown"
                        last_action = action

                        # ---- execute --------------------------------
                        if kind in ("done", "end"):
                            completed = True
                            summary = action.get("summary") or action.get("action_text") or "done"
                            steps_taken = step
                            _slog(f"[step {step}/{msg.max_steps} {used_engine}] done: {summary}")
                            step_events.append(
                                {
                                    "n": step,
                                    "engine": used_engine,
                                    "kind": "done",
                                    "outcome": summary,
                                }
                            )
                            break
                        if kind == "unknown":
                            _slog(
                                f"[step {step}/{msg.max_steps} {used_engine}] "
                                f"unknown action; aborting"
                            )
                            history.append("unknown -> abort")
                            step_events.append(
                                {
                                    "n": step,
                                    "engine": used_engine,
                                    "kind": "unknown",
                                    "outcome": "abort",
                                }
                            )
                            steps_taken = step
                            break
                        if kind == "capture":
                            _slog(
                                f"[step {step}/{msg.max_steps} {used_engine}] "
                                f"capture inside page.agent() ignored"
                            )
                            history.append("capture(skipped)")
                            step_events.append(
                                {
                                    "n": step,
                                    "engine": used_engine,
                                    "kind": "capture",
                                    "outcome": "skipped (capture inside agent ignored)",
                                }
                            )
                            steps_taken = step
                            continue

                        outcome = await _execute(action, viewport_w, viewport_h)
                        _slog(f"[step {step}/{msg.max_steps} {used_engine}] {kind} -> {outcome}")
                        history.append(f"{kind} -> {outcome}")
                        step_events.append(
                            {
                                "n": step,
                                "engine": used_engine,
                                "kind": kind,
                                "outcome": outcome,
                            }
                        )
                        steps_taken = step

                if not completed and steps_taken == 0 and not result.error:
                    result.error = "agent emitted no actions"
            except Exception as e:
                result.error = f"{type(e).__name__}: {e}"
                _slog(f"crashed: {e}")
            finally:
                # Wait for any pending video downloads (httpx single
                # file or yt-dlp HLS) BEFORE returning control to the
                # caller's script. The script's next ``await
                # page.wait_for(...)`` or page.goto() expects to see
                # the video already in the gallery; without this
                # drain a yt-dlp invocation could still be merging
                # segments when the script moves on or the worker
                # tears the lane down.
                try:
                    await drain_video_downloads()
                except Exception as e:
                    _slog(f"video-drain crashed: {e}")

        result.completed = completed
        result.steps_taken = steps_taken
        result.summary = summary
        result.last_action = last_action
        result.steps = step_events
        try:
            await self._send(result)
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] failed to send session_agent_result: {e}",
            )

    async def _teardown_session_state(self, sid: str, state) -> None:
        """Common teardown logic: reset tabs to about:blank, stop the
        browser handle, release the lane. Extracted so both the
        normal session_end path AND the abort-on-arrival path
        (_aborted_sessions checkpoint in session_start) can reuse it.
        Best-effort throughout -- per-step failures are swallowed
        because the lane MUST be released no matter what."""
        # Cancel any URL-capture polling tasks installed by
        # browser_ops.install_session_asset_capture. They run in a
        # tight while loop reading window.__paprika_url_capture; if we
        # don't cancel them BEFORE the tab is killed they'll keep
        # raising "tab closed" errors until the event loop notices.
        try:
            tab = getattr(state, "tab", None)
            tasks = getattr(tab, "_paprika_url_capture_tasks", None) if tab else None
            if tasks:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                tasks.clear()
        except Exception:
            pass

        # Cancel the per-session netcap streamer (Live-panel Network feed).
        try:
            _nct = getattr(state, "netcap_task", None)
            if _nct is not None and not _nct.done():
                _nct.cancel()
            state.netcap_task = None
        except Exception:
            pass

        # Drain pending video downloads first. Sessions that triggered
        # an HLS yt-dlp or a direct mp4 fetch via the passive m3u8
        # listener may still have ffmpeg merging segments / httpx
        # streaming bytes when the operator (or upstream code) ends
        # the session -- tearing Chrome down would kill the
        # subprocess / cancel the stream mid-write and leave a
        # corrupt partial file (or nothing at all).
        #
        # drain() itself implements an idle-window policy: it keeps
        # waiting as long as bytes / yt-dlp log lines keep flowing
        # (default 45s of zero progress before abandoning) up to a
        # hard wall-clock cap (default 30 min). The outer
        # asyncio.wait_for here is the matching safety net at the
        # same cap -- if drain() somehow hangs without polling its
        # own deadline, this releases the lane.
        if state.video_drainer is not None:
            hard_cap = float(
                os.environ.get("PAPRIKA_VIDEO_DRAIN_HARD_S", "3600.0")
            )
            try:
                await asyncio.wait_for(
                    state.video_drainer(), timeout=hard_cap + 30.0,
                )
            except asyncio.TimeoutError:
                _logger.info(
                    f"[worker {self.worker_id}] session {sid} video "
                    f"drain hit outer safety timeout after "
                    f"{hard_cap + 30:.0f}s; tearing down anyway"
                )
            except Exception as e:
                _logger.info(
                    f"[worker {self.worker_id}] session {sid} video "
                    f"drain failed: {type(e).__name__}: {e}"
                )

        # Persist session-end snapshots BEFORE teardown closes the tab
        # and discards state.network_log. The Live panel's Network /
        # Links tabs read these files when the session is gone --
        # without this dump, opening Live on a completed job shows
        # empty Network and (for non-fetch jobs) empty Links.
        # Bounded by an overall timeout so a flaky hub can't stall the
        # lane release path.
        try:
            await asyncio.wait_for(
                self._dump_session_to_parent_job(sid, state),
                timeout=15.0,
            )
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] session {sid} dump "
                f"to parent job failed: {type(e).__name__}: {e}",
            )
        try:
            if state.browser is not None:
                try:
                    await browser_ops.force_single_tab(
                        state.browser,
                        log=lambda s: _logger.info(
                            f"[session {sid}] {s}",
                        ),
                    )
                except Exception as e:
                    _logger.info(
                        f"[worker {self.worker_id}] session {sid} "
                        f"force_single_tab teardown failed: "
                        f"{type(e).__name__}: {e}",
                    )
                if state.tab is not None:
                    try:
                        await state.tab.get("about:blank", new_tab=False)
                    except Exception:
                        pass
                try:
                    await state.browser.stop()
                except Exception:
                    pass
        finally:
            # Restore the lane's default profile if this session
            # was running with an operator-uploaded profile. Idempotent
            # via Lane._profile_swap_active -- the call is a no-op
            # when no swap is active.
            if state.lane is not None:
                try:
                    await state.lane.restore_default_profile()
                except Exception as e:
                    _logger.info(
                        f"[worker {self.worker_id}] session {sid} "
                        f"restore_default_profile failed: "
                        f"{type(e).__name__}: {e}",
                    )
            if state.lane is not None and self.lane_pool is not None:
                self.lane_pool.release(state.lane)
            # Mirror the lane release into self._in_flight so the
            # next heartbeat reports the worker as free. _in_flight
            # was kept incremented past the original
            # _run_assigned_job finish for keep_session sessions so
            # the scheduler wouldn't over-dispatch onto a lane the
            # session was holding. Decrement here closes that loop.
            # Best-effort: clamped at 0 since we can't easily tell
            # whether THIS session is one that kept in_flight bumped
            # (vs. a regular session_start-style session that never
            # bumped it).
            if hasattr(state, "job_id") and state.job_id:
                try:
                    self._in_flight = max(0, self._in_flight - 1)
                except Exception:
                    pass
            # keep_session sessions own a worker-side workdir that
            # outlived the fetch. Clean it up now that the browser is
            # really gone -- otherwise it'd leak on disk forever.
            try:
                wd = getattr(state, "workdir", None)
                if wd is not None:
                    shutil.rmtree(wd, ignore_errors=True)
            except Exception:
                pass
            # Session assets_dir is rooted at /tmp/paprika-ses-<sid>-<rand>/
            # (see _handle_session_start). Until now nothing cleaned it,
            # so every closed session leaked its yt-dlp partials / HLS
            # segment buffers / screenshot dumps -- on busy workers this
            # accumulated multi-GB per orphan and filled the root FS,
            # causing Chrome+WebSocket crashes. Drop the whole mkdtemp
            # root (parent of state.assets_dir) now that teardown is
            # done. assets_dir was uploaded to the hub during the fetch
            # so on-disk copy is no longer needed.
            try:
                ad = getattr(state, "assets_dir", None)
                if ad is not None:
                    # state.assets_dir is <scratch>/assets; drop the
                    # whole <scratch> the mkdtemp made.
                    shutil.rmtree(Path(ad).parent, ignore_errors=True)
            except Exception:
                pass

    async def _dump_session_to_parent_job(self, sid: str, state) -> None:
        """Persist a session's network log + final-page link list to
        the parent job's directory so the Live panel still has data
        after the session closes.

        Sibling URLs of ``state.asset_upload_base``:

          * ``POST {hub}/jobs/{pid}/network``         (the network log)
          * ``POST {hub}/jobs/{pid}/links_snapshot``  (the final links)

        Both endpoints are best-effort -- a failure here is logged but
        not raised so the lane still gets released. No-op when the
        session has no parent (``asset_upload_base`` unset)."""
        if not state.asset_upload_base:
            return
        # Derive sibling URLs by stripping the trailing /assets. Robust
        # against base URLs that already include a job_id path (which
        # asset_upload_base always does -- see _asset_upload_url).
        base = state.asset_upload_base
        if base.endswith("/assets"):
            base = base[: -len("/assets")]

        # ---- network log ------------------------------------------------
        entries = list(state.network_log or [])
        if entries:
            try:
                await self._http.post(
                    f"{base}/network",
                    json={
                        "secret": self.worker_secret or "",
                        "session_id": sid,
                        "entries": entries,
                    },
                    timeout=10.0,
                )
            except Exception as e:
                _logger.info(
                    f"[session {sid}] network dump POST failed: {type(e).__name__}: {e}",
                )

        # ---- last-page links snapshot -----------------------------------
        # state.tab may already be None for sessions that died early; the
        # evaluate call also can't reach a tab that crashed. Both paths
        # are tolerated -- an empty link list is a valid snapshot.
        tab = getattr(state, "tab", None)
        if tab is None:
            return
        try:
            raw = await asyncio.wait_for(
                tab.evaluate(_LINKS_EXTRACT_JS),
                timeout=5.0,
            )
        except Exception:
            raw = None
        items: list = []
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    items = parsed
            except Exception:
                pass
        elif isinstance(raw, list):
            items = raw
        # Best-effort current URL -- the snapshot is more useful with it.
        cur = ""
        try:
            cur_raw = await asyncio.wait_for(
                tab.evaluate("location.href"),
                timeout=2.0,
            )
            if isinstance(cur_raw, str):
                cur = cur_raw
        except Exception:
            pass
        try:
            await self._http.post(
                f"{base}/links_snapshot",
                json={
                    "secret": self.worker_secret or "",
                    "session_id": sid,
                    "current_url": cur,
                    "links": items,
                },
                timeout=10.0,
            )
            # Signal the Live panel to refresh its Links tab (ephemeral
            # marker over /events; replaces the periodic /links poll).
            _pjid = getattr(state, "job_id", None)
            if _pjid:
                try:
                    await self._send(
                        WorkerJobLog(job_id=_pjid, line=LINKS_CAPTURE_MARKER)
                    )
                except Exception:
                    pass
        except Exception as e:
            _logger.info(
                f"[session {sid}] links snapshot POST failed: {type(e).__name__}: {e}",
            )

    async def _force_end_all_sessions(self, reason: str) -> int:
        """Tear down every session currently held by this worker and
        return their lanes to the pool. Called from the WS-reconnect
        path so that a hub bounce can't leave the worker holding lanes
        the hub no longer knows about.

        Without this, `commit e5c0f35`-style deploys produced the
        scenario diagnosed on job fb84e2a6da7b: hub's in-memory session
        registry got wiped on restart but the worker still believed
        both lanes were occupied -> every subsequent attempt at
        session_start was rejected with "no free lane" until the
        operator manually `docker compose restart worker`'d.

        Returns the number of sessions force-closed. Best-effort:
        per-session cleanup errors are swallowed since the goal is
        "get back to a known-clean state", not to surface tab-close
        races.
        """
        if not self._sessions:
            return 0
        sids = list(self._sessions.keys())
        _logger.info(
            f"[worker {self.worker_id}] force-ending {len(sids)} session(s) "
            f"({reason}): {', '.join(sids)}",
        )
        for sid in sids:
            state = self._sessions.pop(sid, None)
            if state is None:
                continue
            try:
                if state.browser is not None:
                    try:
                        await state.browser.stop()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                if state.lane is not None and self.lane_pool is not None:
                    self.lane_pool.release(state.lane)
            except Exception:
                pass
        return len(sids)

    async def _handle_session_end(self, msg: HubSessionEnd) -> None:
        """Release the lane and detach nodriver. Tabs reset to about:blank
        so the next session sees a clean browser.

        If the session isn't in ``self._sessions`` yet, the end message
        likely arrived while ``_handle_session_start`` is still in
        flight (e.g. the hub's session-start ``wait_for`` timed out
        and is now telling us to abort). Mark the sid in
        ``self._aborted_sessions`` so the in-flight start tears itself
        down right before sending its ack.
        """
        sid = msg.session_id
        ack = WorkerSessionEndAck(session_id=sid)
        state = self._sessions.pop(sid, None)
        if state is None:
            # Mark for abort -- in-flight session_start will see this.
            self._aborted_sessions.add(sid)
            ack.error = f"session {sid} not present (queued for abort if start is still in flight)"
        else:
            await self._teardown_session_state(sid, state)
            _logger.info(f"[worker {self.worker_id}] session {sid} ended")
        try:
            await self._send(ack)
        except Exception as e:
            _logger.info(f"[worker {self.worker_id}] failed to send session_end_ack: {e}")

