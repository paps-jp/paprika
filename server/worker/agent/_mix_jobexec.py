"""WorkerAgent mixin: assigned-job execution + fetch options/callbacks + deferred video.

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


# Worker-internal disk pressure threshold. Above this %, _run_assigned_job
# fails the assignment fast (before acquiring a lane) and runs the
# emergency cleanup so the next dispatch has a fighting chance. Hub side
# also filters at this threshold in pick_worker, so dispatches only slip
# through during the heartbeat lag window (≤10s).
_DISK_PRESSURE_FAIL_PCT = 90.0


def _emergency_disk_cleanup() -> None:
    """Prune worker-internal transient state to recover disk when a CT hits
    >90% full. Called from the per-job preflight after the job is failed.

    Targets are ordered cheapest-to-rebuild first so a single pass usually
    frees enough without taking down anything the operator cares about
    (login state in Chrome profile cookies/Preferences is NOT touched).

    Out of scope: the CT-level containerd bloat (30G snapshots seen on
    w11/w18 / 2026-06-06). That lives outside the docker container's
    mount namespace; the per-CT paprika-worker-housekeep systemd timer
    (scripts/install-worker-housekeep.sh) handles it.
    """
    import glob as _glob

    cleaned = 0
    # (pattern, kind) — kind is "dir" or "file".
    targets: list[tuple[str, str]] = [
        # yt-dlp HLS fragment scratch — single biggest hog during video DLs.
        ("/root/.cache/yt-dlp", "dir"),
        # Chrome on-disk caches: GPU + shader + Code Cache + HTTP cache.
        # Safe to nuke (Chrome rebuilds on next launch; worst-case is a
        # warm-up pause on the first hit). Cookies / Preferences untouched.
        ("/tmp/chrome-lane-*/Default/Cache", "dir"),
        ("/tmp/chrome-lane-*/Default/Code Cache", "dir"),
        ("/tmp/chrome-lane-*/Default/GPUCache", "dir"),
        ("/tmp/chrome-lane-*/Default/Service Worker/CacheStorage", "dir"),
        ("/tmp/chrome-lane-*/ShaderCache", "dir"),
        ("/tmp/chrome-lane-*/GraphiteDawnCache", "dir"),
        # Chrome's own /tmp leak — single biggest hog seen in the wild
        # (2026-06-06: 24k stale files / ~9G per CT across the fleet after
        # 5 days of jobs). Chrome spills two patterns into the system /tmp
        # whenever a renderer is SIGKILLed before its cleanup runs (which
        # happens on every lane swap, every Xvfb restart, every container
        # SIGTERM): ".com.google.Chrome.*" (~9.7M each) and "scoped_dir*"
        # (~52M each). They build up indefinitely because no one owns
        # cleanup. Glob match here; the live Chrome process holds an open
        # fd on its CURRENT entry so rmtree-ignore-errors skips it.
        ("/tmp/.com.google.Chrome.*", "file"),
        ("/tmp/scoped_dir*", "dir"),
        # Per-job scratch from completed jobs. Live jobs hold an open fd on
        # their workdir, so rmtree-ignore-errors skips active content; the
        # job's own finally cleans up after itself anyway.
        ("/tmp/paprika-*", "dir"),
        # Chrome renderer scratch dirs left behind by killed renderers.
        ("/tmp/.org.chromium.*", "dir"),
        # Stale /tmp video downloads from prior jobs that didn't clean up.
        ("/tmp/*.mp4", "file"),
        ("/tmp/*.ts", "file"),
        ("/tmp/*.m4s", "file"),
        ("/tmp/*.webm", "file"),
    ]
    for pattern, kind in targets:
        for path in _glob.glob(pattern):
            try:
                if kind == "dir":
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
                cleaned += 1
            except OSError:
                pass
    _logger.info(
        f"emergency disk cleanup: removed {cleaned} transient target(s)",
    )


class _JobExecMixin:
    async def _run_assigned_job(self, assign: HubAssignJob) -> None:
        job_id = assign.job_id
        # Disk-pressure preflight: fail-fast before claiming a lane or
        # bumping in_flight. The hub's pick_worker skips disk-pressured
        # workers, but a heartbeat lag (≤10s) can let one through; this
        # is the final defence. Reads the snapshot the heartbeat loop
        # already captured -- no extra /proc walk. Pre-2026-06-06 builds
        # leave _last_resources at all-zeros, so this is a no-op until a
        # real heartbeat populates it.
        _, _, _disk_pct, _disk_free_gb, _ = getattr(
            self, "_last_resources", (0.0, 0.0, 0.0, 0.0, 0.0),
        )
        if _disk_pct > _DISK_PRESSURE_FAIL_PCT:
            _logger.warning(
                f"[{job_id}] worker disk pressure {_disk_pct:.0f}% "
                f"({_disk_free_gb:.1f}GB free) > {_DISK_PRESSURE_FAIL_PCT:.0f}%"
                f" -- failing job + running emergency cleanup",
            )
            try:
                _emergency_disk_cleanup()
            except Exception as e:
                _logger.info(
                    f"emergency cleanup raised (continuing): "
                    f"{type(e).__name__}: {e}",
                )
            await self._send(
                WorkerJobFailed(
                    job_id=job_id,
                    error=(
                        f"worker disk pressure "
                        f"({_disk_pct:.0f}% used, "
                        f"{_disk_free_gb:.1f}GB free); "
                        f"cleanup triggered, retry shortly"
                    ),
                )
            )
            return
        async with self._sem:
            self._in_flight += 1
            # Kick the heartbeat loop so the hub's stale in_flight view
            # catches up within ms (vs. up to a full HEARTBEAT_INTERVAL).
            # Without this, a concurrent pick on the hub saw the worker
            # as still having capacity right after the lane was acquired,
            # over-dispatched, and triggered the "no free lane in pool"
            # cascade. See _heartbeat_loop docstring (incident 2026-06-16).
            try:
                self._heartbeat_kick.set()
            except Exception:
                pass
            lane = None
            if self.lane_pool is not None:
                # If hub specified a lane_hint (attach_to_job), wait for THAT
                # specific lane to be free. Otherwise grab any free lane.
                lane = await self.lane_pool.acquire(lane_hint=assign.lane_hint)
                if lane is None:
                    await self._send(
                        WorkerJobFailed(
                            job_id=job_id,
                            error=(
                                f"lane_hint {assign.lane_hint} out of range"
                                if assign.lane_hint is not None
                                else "no free lane in pool"
                            ),
                        )
                    )
                    self._in_flight -= 1
                    try:
                        self._heartbeat_kick.set()
                    except Exception:
                        pass
                    return
            # Track whether we swapped the lane's profile in so the
            # finally block can restore it on every code path.
            _profile_swapped = False
            # Pre-bind the names the finally block reads. If something
            # explodes BEFORE the original assignment further down
            # (where these come from assign.options) the finally would
            # otherwise hit UnboundLocalError and mask the real error.
            keep_session = False
            inspect_sid = assign.session_id
            # Per-job log file handle, opened further down once the
            # workdir exists. Pre-bound so the finally can close it on
            # EVERY exit path. The success and fetch-crash paths close it
            # explicitly, but any OTHER exception (e.g. page.html write
            # failing on a full disk, or a callback/option build raising
            # before the inner fetch try) escapes to the outer except
            # below and used to skip both closes -- leaking one fd per
            # failed job until the worker hit RLIMIT_NOFILE and crashed
            # with "Too many open files: '/tmp/paprika-<jobid>-...'".
            log_fp = None
            try:
                await self._send(
                    WorkerJobAccepted(
                        job_id=job_id,
                        novnc_url=lane.novnc_url if lane else None,
                        lane_idx=lane.lane_idx if lane else None,
                    )
                )

                # If the hub told us to use an operator-uploaded
                # Chrome profile, fetch + extract it and re-point the
                # lane's user-data-dir at it BEFORE we hand the lane
                # to nodriver. Best-effort: a failed fetch falls back
                # to the lane default rather than failing the job --
                # the operator will notice the missing cookies on
                # their own faster than the LLM would interpret a
                # "could not pull profile" error.
                profile_url = getattr(assign, "profile_url", None)
                if profile_url and lane is not None:
                    pdir = await self._get_profile_for_job(
                        profile_url=profile_url,
                        profile_name=getattr(assign, "profile_name", None),
                        profile_etag=getattr(assign, "profile_etag", None),
                        scratch_key=job_id,
                    )
                    if pdir is not None:
                        try:
                            await lane.use_profile(pdir)
                            _profile_swapped = True
                            _logger.info(
                                f"[{job_id}] operator profile installed into lane #{lane.lane_idx}",
                            )
                        except Exception as e:
                            _logger.info(
                                f"[{job_id}] lane.use_profile failed: "
                                f"{type(e).__name__}: {e} -- "
                                f"continuing with lane default",
                            )

                # Local tempdir for assets + page.html + log.txt
                workdir = Path(tempfile.mkdtemp(prefix=f"paprika-{job_id}-"))
                assets_dir = workdir / "assets"
                assets_dir.mkdir(parents=True, exist_ok=True)
                log_path = workdir / "log.txt"
                log_fp = open(log_path, "a", encoding="utf-8", buffering=1)

                # Log callback: write file + ship to hub via WorkerJobLog
                send_lock = self._send_lock

                def _log(line: str) -> None:
                    line = line.rstrip()
                    # log_fp gets closed before the keep_session
                    # post-fetch block runs ("... is now interactive"
                    # etc.); writes to a closed file raise ValueError
                    # which previously took the whole job down with
                    # "worker crashed: ValueError: I/O operation on
                    # closed file." Stderr + WS log keep working so
                    # the operator-facing log doesn't lose lines.
                    try:
                        if not log_fp.closed:
                            log_fp.write(line + "\n")
                    except (ValueError, OSError):
                        pass
                    _logger.info(f"[{job_id}] {line}")
                    asyncio.ensure_future(self._send(WorkerJobLog(job_id=job_id, line=line)))

                # ---- Job banner ---------------------------------------
                # Surface the requested URL + key options at the very top
                # of every job log. Without this, fetch logs jump straight
                # into "downloaded N cookies" / "saved page.html" and the
                # operator scrolling back through Live can't tell which
                # URL the job was actually pointed at (a real complaint
                # for jobs with many redirects or job-ID-only filenames).
                _mode = (getattr(assign.options, "mode", None) or "fetch")
                _url = getattr(assign, "url", "") or "(no url)"
                _log(f"=== job {job_id}  mode={_mode} ===")
                _log(f"==> URL: {_url}")
                _opt = assign.options
                _opt_bits: list[str] = []
                _mw = getattr(_opt, "max_wait_seconds", None)
                if _mw is not None:
                    _opt_bits.append(f"max_wait={_mw}s")
                _ca = getattr(_opt, "capture_assets", None)
                if _ca is not None:
                    _opt_bits.append(f"capture_assets={bool(_ca)}")
                if getattr(_opt, "scroll", None):
                    _opt_bits.append("scroll=True")
                _up = getattr(_opt, "use_profile", None)
                if _up:
                    _opt_bits.append(f"profile={_up!r}")
                if getattr(_opt, "keep_session", None):
                    _opt_bits.append("keep_session=True")
                if getattr(_opt, "goal", None):
                    _g = str(_opt.goal)
                    _opt_bits.append(
                        f"goal={(_g[:80] + '…') if len(_g) > 80 else _g!r}"
                    )
                if _opt_bits:
                    _log("    options: " + ", ".join(_opt_bits))

                # NOTE: the v1 "vision-agent" mode (CogAgent screenshot
                # loop + pixel-space actions) was removed in the v2
                # cleanup. The hub rejects mode="vision-agent" at the
                # protocol layer (Pydantic) and the dispatcher never
                # routes such jobs here. Worker code that drove it
                # (_run_vision_agent_job, _handle_session_agent's
                # cogagent branch, _ask_cogagent helpers) is left as
                # dead code reachable only via legacy paths that no
                # longer fire; a follow-up cleanup can rip it out
                # without protocol or behavioural changes.

                # Fetch mode: single-shot HTML + assets capture.
                # LLM-driven jobs (mode=codegen-loop) are orchestrated by
                # the hub and never reach this code path -- they spawn a
                # sandboxed paprika-runner that drives the browser via
                # /sessions/* HTTP, not via the worker's job pipeline.
                # The old per-step agent loop was removed in PR-14a.

                # Build the "after fetch, save cookies back to host"
                # callback. The hub gives us ``assign.save_cookies_host``
                # already normalised; we host-filter the dumped jar
                # client-side so we don't store noise (cross-site
                # tracker cookies) and PUT to /hosts/{host}. The
                # hub URL is derived from asset_upload_base (the only
                # absolute hub URL the worker already knows).
                save_cb = None
                save_host_for_cb = assign.save_cookies_host
                if save_host_for_cb:
                    save_cb = self._make_cookie_save_callback(
                        assign,
                        save_host_for_cb,
                        _log,
                    )

                # Register this fetch as a read-only inspectable
                # session so the admin UI can call /sessions/{id}/
                # cookies / outline / screenshot / state while the
                # fetch is running. on_ready fires after CDP is set
                # up but before navigation; on_closing fires in
                # fetch's finally before browser.stop(). Together
                # they guarantee the session is alive for exactly
                # the inspectable window.
                ready_cb = None
                closing_cb = None
                inspect_sid = assign.session_id
                keep_session = bool(getattr(assign.options, "keep_session", False))
                # Network log for the fetch-mode path: tracked here
                # and shared with the inspect session so the Live
                # panel "Network" tab can display real-time traffic.
                fetch_network_log: list = []
                if inspect_sid and lane is not None:
                    ready_cb, closing_cb = self._make_fetch_session_callbacks(
                        inspect_sid,
                        lane,
                        assets_dir,
                        _log,
                        job_id=job_id,
                        keep_session=keep_session,
                        network_log=fetch_network_log,
                    )

                # Pre-baked per-host recipe (HostRecord.fetch_recipes).
                # The hub stamps the picked recipe onto options.fetch_recipe
                # before dispatch; we wrap it in a callback that fires
                # right after the initial Page.navigate(). Best-effort:
                # recipe failures are logged but don't fail the fetch.
                _picked_recipe = getattr(assign.options, "fetch_recipe", None)
                async def _recipe_cb(tab):
                    if _picked_recipe:
                        await _apply_fetch_recipe(tab, _picked_recipe, _log)

                # Incremental asset upload (resilience). The fetcher fires
                # this once per asset right after it's written to
                # assets_dir; we ship each one to the hub immediately so a
                # mid-fetch failure (worker disconnect, crash, hub restart)
                # leaves the already-captured assets in the gallery instead
                # of discarding the whole batch -- the legacy behaviour
                # uploaded nothing until fetch() returned successfully.
                # The end-of-fetch _upload_files() pass then reconciles
                # (page.html, log, late yt-dlp output, and anything whose
                # inline upload failed). uploaded_names dedupes the two
                # passes so a file is never shipped twice.
                uploaded_names: set[str] = set()
                page_url_for_assets = assign.url or None

                async def _on_asset_saved(path, info):
                    try:
                        name = (info or {}).get("name") or path.name
                    except Exception:
                        return
                    if name in uploaded_names:
                        return
                    ok = await self._upload_asset(
                        assign,
                        path,
                        name,
                        source_url=(info or {}).get("url"),
                        mime=(info or {}).get("mime"),
                        page_url=page_url_for_assets,
                        timeout=300.0,
                    )
                    if ok:
                        uploaded_names.add(name)

                fetch_opts = self._build_fetch_options(
                    assign.url,
                    assign.options,
                    assets_dir,
                    _log,
                    lane=lane,
                    cookies_to_install=assign.cookies,
                    on_complete_dump_cookies=save_cb,
                    on_browser_ready=ready_cb,
                    on_browser_closing=closing_cb,
                    on_after_navigate=_recipe_cb if _picked_recipe else None,
                    network_log=fetch_network_log,
                    # V: operator-managed URL deny list from Settings.
                    asset_url_blacklist=list(getattr(assign, "asset_url_blacklist", []) or []),
                    on_asset_saved=_on_asset_saved,
                )
                # Detach big-video downloads from the lane. When the
                # operator asked for video AND this isn't a keep_session
                # job, DETECT streams during capture but defer the
                # (often 10+ min) yt-dlp download to a background task so
                # the lane is freed immediately. The job sits in phase
                # "downloading" until the background task uploads the
                # video and sends the final WorkerJobComplete. keep_session
                # is excluded -- there the operator drives download_video()
                # interactively inside the live session.
                _defer_video = (
                    bool(getattr(assign.options, "download_video", False))
                    and not keep_session
                )
                fetch_opts.defer_video_download = _defer_video
                try:
                    result = await fetch(fetch_opts)
                except Exception as e:
                    _log(f"  !! fetch crashed: {type(e).__name__}: {e}")
                    # Salvage: ship any assets captured before the crash
                    # that the incremental on_asset_saved callback didn't
                    # already upload (the file mid-write when fetch raised,
                    # or one whose inline upload failed). Without this they
                    # would be rmtree'd below, unsent -- a contributor to
                    # "errored job has empty assets". Best-effort; never
                    # let salvage failures mask the original error.
                    salvaged = 0
                    try:
                        if assets_dir and assets_dir.exists():
                            for _p in sorted(assets_dir.iterdir()):
                                if not _p.is_file() or _p.name in uploaded_names:
                                    continue
                                if await self._upload_asset(
                                    assign,
                                    _p,
                                    _p.name,
                                    page_url=page_url_for_assets,
                                    timeout=300.0,
                                ):
                                    uploaded_names.add(_p.name)
                                    salvaged += 1
                        if salvaged:
                            _log(
                                f"  ... salvaged {salvaged} captured "
                                f"asset(s) despite fetch error"
                            )
                    except Exception as _sx:
                        _log(
                            f"  (salvage pass failed: "
                            f"{type(_sx).__name__}: {_sx})"
                        )
                    log_fp.close()
                    await self._upload_log(assign, log_path)
                    await self._send(
                        WorkerJobFailed(
                            job_id=job_id,
                            error=f"{type(e).__name__}: {e}",
                        )
                    )
                    shutil.rmtree(workdir, ignore_errors=True)
                    return

                # Persist page.html + log to local workdir
                page_path = workdir / "page.html"
                page_path.write_text(result.html, encoding="utf-8")
                # ② v2 eye: persist the end-of-fetch screenshot (if captured)
                # to the workdir ROOT as final.jpg (NOT under assets/, else it
                # would leak into the gallery). The hub's perception discovers
                # root final.jpg; _upload_files ships it as a special file.
                _shot = getattr(result, "screenshot", b"") or b""
                if _shot:
                    try:
                        (workdir / "final.jpg").write_bytes(_shot)
                    except Exception as _e:
                        _log(f"  (final.jpg write failed: {type(_e).__name__}: {_e})")
                # Sidecar: the live-DOM representative-image pick (true
                # naturalWidth cascade). /jobs/{id}/meta prefers this over
                # re-parsing page.html so the thumbnail is a real cover
                # image rather than the site logo. Skipped when nothing
                # qualified; old jobs without it fall back to meta.py.
                _rep = getattr(result, "representative_image", None) or {}
                if _rep.get("url"):
                    try:
                        (workdir / "meta.json").write_text(
                            json.dumps({"representative_image": _rep}, ensure_ascii=False),
                            encoding="utf-8",
                        )
                    except Exception as _e:
                        _log(f"  (meta.json write failed: {type(_e).__name__}: {_e})")
                log_fp.close()

                # Upload all outputs to hub. Assets already shipped by the
                # incremental on_asset_saved callback are skipped; this
                # pass reconciles page.html, log, late yt-dlp output and
                # any inline-upload failures.
                await self._upload_files(assign, workdir, result, uploaded_names)

                # Build the JobResult Pydantic object with hub-side hrefs.
                # page_url: every fetch-mode asset belongs to the single
                # page the operator asked us to grab (assign.url). The
                # fetcher tracks per-asset source URL + mime but not the
                # initiating document URL, so we stamp the job URL on
                # every entry -- same shape the assets.json + .meta/
                # sidecar pipeline uses.
                page_url_for_assets = assign.url or None
                asset_infos = [
                    AssetInfo(
                        name=a["name"],
                        size=a["size"],
                        mime=a.get("mime"),
                        url=a.get("url"),
                        page_url=page_url_for_assets,
                        href=f"/jobs/{job_id}/assets/{a['name']}",
                    )
                    for a in result.assets_saved
                ]
                job_result = JobResult(
                    job_id=job_id,
                    status=JobStatus.completed,
                    html_href=f"/jobs/{job_id}/page.html",
                    log_href=f"/jobs/{job_id}/log.txt",
                    assets=asset_infos,
                    assets_failed=result.assets_failed,
                    video_detection=getattr(result, "video_detection", {}) or {},
                    video_urls_seen=list(getattr(result, "video_urls_seen", []) or []),
                    iframe_srcs=list(getattr(result, "iframe_srcs", []) or []),
                    ytdlp_results=[
                        YtdlpResult(**r) for r in getattr(result, "ytdlp_results", []) or []
                    ],
                    visited_urls=list(getattr(result, "visited_urls", []) or []),
                    # Raw occlusion report from the live-DOM overlay probe
                    # (core/fetcher.probe_occlusion). The worker only MEASURES
                    # -- the hub classifies this into the 課題(review) bucket
                    # (server/hub/_review.py).
                    occlusion=dict(getattr(result, "occlusion", {}) or {}),
                )
                # Deferred video download: capture is done and the
                # image assets are uploaded, but a (big) video was
                # detected. Mark the job "downloading", then run yt-dlp
                # in a detached background task that uploads the video
                # and sends the FINAL WorkerJobComplete. The lane is
                # released by the finally below (the download doesn't
                # need the browser), so other jobs can use it meanwhile.
                _deferred_targets = list(
                    getattr(result, "deferred_video_targets", []) or []
                )
                if _defer_video and _deferred_targets:
                    await self._send(
                        WorkerJobProgress(job_id=job_id, phase="downloading")
                    )
                    _logger.info(
                        f"[{job_id}] video deferred to background "
                        f"({len(_deferred_targets)} target(s)); lane released, "
                        f"phase=downloading",
                    )
                    self._spawn_deferred_video_download(
                        assign,
                        _deferred_targets,
                        job_result,
                        page_url_for_assets,
                    )
                else:
                    await self._send(
                        WorkerJobComplete(
                            job_id=job_id,
                            result=job_result,
                        )
                    )
                # keep_session: hand the (now post-fetch) browser /
                # session over to the operator instead of tearing down.
                # Concretely:
                #   * flip is_fetch_owned=False so write actions are
                #     allowed via /sessions/{sid}/action,
                #   * stash the upload base + workdir on the state so
                #     POST /jobs/{id}/refresh can flush new assets and
                #     so session_end can rmtree the right directory,
                #   * seed uploaded_assets with the names the fetcher
                #     already shipped, so the next refresh only picks
                #     up assets captured during operator interaction,
                #   * skip the workdir rmtree + lane release (both run
                #     when the operator DELETEs the session instead).
                if keep_session and inspect_sid:
                    sess = self._sessions.get(inspect_sid)
                    if sess is not None:
                        sess.is_fetch_owned = False
                        sess.asset_upload_base = assign.asset_upload_base
                        sess.job_id = job_id
                        sess.workdir = workdir
                        # The network_log is already shared via
                        # _make_fetch_session_callbacks (same list
                        # object), so no transfer needed here.
                        try:
                            for p in Path(assets_dir).rglob("*"):
                                if p.is_file():
                                    sess.uploaded_assets.add(p.name)
                        except Exception:
                            pass
                        # Stderr only -- log_fp was closed a few lines
                        # above (post-fetch cleanup) and writing to it
                        # would raise ValueError("I/O operation on
                        # closed file") which propagates as
                        # "worker crashed: ValueError: ..." and kills
                        # the keepalive transition. The operator-facing
                        # log already got the fetch's own completion
                        # lines; this banner is for the worker stderr
                        # (visible in docker logs) only.
                        _logger.info(
                            f"[{job_id}]   ... keep_session: session "
                            f"{inspect_sid} is now interactive "
                            f"(use POST /jobs/{job_id}/refresh to "
                            f"flush new assets / refresh links)",
                        )
                else:
                    shutil.rmtree(workdir, ignore_errors=True)
            except Exception as e:
                try:
                    await self._send(
                        WorkerJobFailed(
                            job_id=job_id,
                            error=f"worker crashed: {type(e).__name__}: {e}",
                        )
                    )
                except Exception:
                    pass
            finally:
                # Always close the per-job log file. The success and
                # fetch-crash paths close it explicitly (so _upload_log /
                # _upload_files read a flushed file), but any other
                # exception lands in the outer except above and would
                # otherwise leak this fd -- the root cause of the slow
                # "[Errno 24] Too many open files: '/tmp/paprika-...'"
                # worker crash. close() is idempotent, so this is a
                # no-op on the paths that already closed it.
                if log_fp is not None:
                    try:
                        if not log_fp.closed:
                            log_fp.close()
                    except Exception:
                        pass
                # Restore the lane's default profile when we swapped
                # one in. Skipped for keep_session+use_profile because
                # the session is still using the operator profile via
                # the same Chrome; _teardown_session_state restores it
                # when the session actually ends.
                if (
                    _profile_swapped
                    and lane is not None
                    and not (keep_session and inspect_sid in self._sessions)
                ):
                    try:
                        await lane.restore_default_profile()
                    except Exception as e:
                        _logger.info(
                            f"[{job_id}] lane.restore_default_profile "
                            f"failed: {type(e).__name__}: {e}",
                        )
                # In keep_session mode the lane is held by the live
                # session -- it's released when the operator DELETEs
                # /sessions/{sid} (which calls _teardown_session_state).
                if (
                    lane is not None
                    and self.lane_pool is not None
                    and not (keep_session and inspect_sid in self._sessions)
                ):
                    self.lane_pool.release(lane)
                # For keep_session jobs the lane stays held by the
                # live session; keeping _in_flight incremented mirrors
                # that to the hub via the next WorkerHeartbeat, so the
                # scheduler doesn't see this worker as fully idle and
                # over-dispatch onto a lane that's actually pinned
                # (= the "no free lane in pool" cascade after burst
                # tests + keep_session). _in_flight is finally
                # decremented when the session ends in
                # _teardown_session_state() below.
                if not (keep_session and inspect_sid in self._sessions):
                    self._in_flight = max(0, self._in_flight - 1)
                    # Lane released + in_flight decremented -- kick the
                    # heartbeat so the hub sees us as free immediately
                    # (vs up to a full HEARTBEAT_INTERVAL of staleness).
                    # The same kick fires for the keep_session branch when
                    # _teardown_session_state actually releases the lane.
                    try:
                        self._heartbeat_kick.set()
                    except Exception:
                        pass
                # fd-budget gate: every job end checks the process's open-fd
                # count and flips into drain mode if it crossed the configured
                # threshold. The drain loop in _mix_run exits when in_flight
                # reaches 0 so docker restarts us fresh; this lets a leak
                # degrade gracefully instead of crashing mid-job with "Too
                # many open files" (incident 2026-06-16, w50148: 1024 soft
                # ulimit + 20+ Chrome subprocesses x ~40fd each = 1k fds
                # easily blown). Idempotent: setting _draining a second time
                # is a no-op; the recycle still fires on the first
                # heartbeat that sees in_flight==0.
                try:
                    self._check_fd_budget_and_maybe_drain()
                except Exception:
                    # Worst case the recycle doesn't fire; the periodic fleet
                    # restart is the safety-net. Don't crash the job-finally.
                    pass

    def _check_fd_budget_and_maybe_drain(self) -> None:
        """Inspect ``/proc/self/fd`` and flip ``self._draining`` when the
        worker has crossed ``PAPRIKA_WORKER_FD_RESTART_THRESHOLD`` (default
        800 -- ~78% of the container's default soft RLIMIT_NOFILE=1024).
        Cheap (one readdir of /proc/self/fd) so safe to call on every job
        end. No-op when already draining."""
        if getattr(self, "_draining", False):
            return
        try:
            threshold = int(
                os.environ.get("PAPRIKA_WORKER_FD_RESTART_THRESHOLD") or 800
            )
        except Exception:
            threshold = 800
        if threshold <= 0:
            return
        try:
            fd_count = len(os.listdir("/proc/self/fd"))
        except Exception:
            return
        if fd_count < threshold:
            return
        _logger = logging.getLogger("server.worker.agent._mix_jobexec")
        _logger.warning(
            "[worker %s] fd budget exceeded (%d >= %d): draining for "
            "recycle (docker will restart after in-flight jobs drain)",
            getattr(self, "worker_id", "?"),
            fd_count,
            threshold,
        )
        self._draining = True

    def _build_fetch_options(
        self,
        url: str,
        opts: JobOptions,
        assets_dir: Path,
        log,
        lane=None,
        cookies_to_install: list[dict] | None = None,
        on_complete_dump_cookies=None,
        on_browser_ready=None,
        on_browser_closing=None,
        on_after_navigate=None,
        network_log: list | None = None,
        asset_url_blacklist: list[str] | None = None,
        on_asset_saved=None,
    ) -> FetchOptions:
        # Server-side normalization (Swagger 'string' guard etc)
        def _norm(v):
            if v is None or not isinstance(v, str):
                return v
            s = v.strip()
            return None if (not s or s.lower() == "string") else s

        attach = _norm(opts.attach)
        clone_profile = _norm(opts.clone_chrome_profile)

        attach_host: str | None = None
        attach_port: int | None = None
        user_data_dir: Path | None = None
        # Lane-pool mode wins: each job uses its dedicated Chrome.
        if lane is not None:
            attach_host = "localhost"
            attach_port = lane.chrome_port
            log(
                f"  ... lane #{lane.lane_idx} acquired  "
                f"chrome=localhost:{lane.chrome_port}  "
                f"noVNC={lane.novnc_url}"
            )
        elif attach:
            attach_host, attach_port = parse_attach(attach)
        elif clone_profile:
            user_data_dir = clone_chrome_profile(clone_profile, log=log)
        elif self.chrome_host and self.chrome_port:
            attach_host = self.chrome_host
            attach_port = self.chrome_port
            log(f"  ... using worker's pre-running Chrome at {attach_host}:{attach_port}")

        return FetchOptions(
            url=url,
            wait_seconds=opts.wait_seconds,
            settle_seconds=opts.settle_seconds,
            idle_seconds=opts.idle_seconds,
            max_wait_seconds=opts.max_wait_seconds,
            scroll=opts.scroll,
            scroll_step=opts.scroll_step,
            scroll_max=opts.scroll_max,
            scroll_early_after=opts.scroll_early_after,
            post_click_seconds=opts.post_click_seconds,
            download_video=bool(getattr(opts, "download_video", False)),
            cookies_from=_norm(opts.cookies_from),
            referer=_norm(opts.referer),
            user_data_dir=user_data_dir,
            attach_host=attach_host,
            attach_port=attach_port,
            # In lane-pool mode the Chrome is dedicated; reuse its tab.
            attach_new_tab=(lane is None),
            # keep_open=True only in keep_session mode. The worker
            # then transitions the fetch-owned session into an
            # interactive one (is_fetch_owned=False) and leaves the
            # browser running so the operator can drive it via noVNC.
            keep_open=bool(getattr(opts, "keep_session", False)),
            headless=opts.headless,
            assets_dir=assets_dir if opts.capture_assets else None,
            log=log,
            cookies_to_install=cookies_to_install,
            on_complete_dump_cookies=on_complete_dump_cookies,
            on_browser_ready=on_browser_ready,
            on_browser_closing=on_browser_closing,
            on_after_navigate=on_after_navigate,
            # Hub-managed min-size filter (Settings → "Asset capture").
            min_asset_size_bytes=int(getattr(opts, "min_asset_size_bytes", 0) or 0),
            # Asset URL blacklist (V). Caller passes the list it pulled
            # from HubAssignJob; applied at fetcher's on_response so
            # blocked URLs never reach disk or yt-dlp.
            asset_url_blacklist=list(asset_url_blacklist or []),
            network_log=network_log,
            # Incremental upload: fire per asset as it lands so a
            # mid-fetch failure (worker disconnect / crash / hub restart)
            # doesn't discard everything captured so far. None disables.
            on_asset_saved=on_asset_saved,
        )

    def _make_fetch_session_callbacks(
        self,
        session_id: str,
        lane,
        assets_dir: Path,
        log,
        *,
        job_id: str | None = None,
        keep_session: bool = False,
        network_log: list | None = None,
    ):
        """Build (on_browser_ready, on_browser_closing) callbacks that
        register a SessionState for the duration of a fetch.

        Sharing the lane's existing tab is fine: nodriver allows
        multiple CDP clients per Chrome, and our session_action
        handlers only do read-only CDP calls when ``is_fetch_owned``.
        The session is removed in the on-closing callback BEFORE
        ``browser.stop()`` so subsequent /sessions/{id}/* requests
        get a clean 404 instead of operating on a torn-down tab.

        keep_session=True: the on_closing skips the unregister so the
        session lives past fetch return. The fetch job handler is
        responsible for then flipping is_fetch_owned=False and seeding
        the upload metadata so /jobs/{job_id}/refresh can flush new
        assets captured during operator interaction.

        network_log: shared list that the CDP asset-capture listener
        populates. Wired into the SessionState so ``kind="network"``
        session actions return live data while the fetch is running.
        """

        async def _on_ready(browser, tab) -> None:
            try:
                state = SessionState(
                    session_id=session_id,
                    lane=lane,
                    assets_dir=assets_dir,
                    is_fetch_owned=True,
                    job_id=job_id,
                )
                # Share the caller's network_log list so the Network
                # tab can read live data while the fetch is running.
                if network_log is not None:
                    state.network_log = network_log
                state.browser = browser
                state.tab = tab
                self._sessions[session_id] = state
                # network_log is populated by the fetcher's own CDP
                # handlers (core.fetcher on_response / on_finished).
                # No separate install_session_asset_capture needed --
                # the shared list reference means the Network tab gets
                # entries as the fetcher processes each response.
                log(f"  ... registered fetch-owned session {session_id} (read-only inspection)")
            except Exception as e:
                log(f"  !! could not register fetch session ({type(e).__name__}: {e})")

        async def _on_closing() -> None:
            # keep_session: the fetch finishes but the session lives
            # on. The fetch job handler will mutate the state in place
            # (is_fetch_owned=False, asset_upload_base=...) right after
            # this callback returns. Don't pop or browser.stop() will
            # run on an empty self._sessions entry next teardown.
            if keep_session:
                log(f"  ... keeping fetch session {session_id} alive (keep_session=True)")
                return
            try:
                gone = self._sessions.pop(session_id, None)
                if gone is not None:
                    log(f"  ... unregistered fetch-owned session {session_id}")
            except Exception as e:
                log(f"  !! could not unregister fetch session ({type(e).__name__}: {e})")

        return _on_ready, _on_closing

    def _spawn_deferred_video_download(
        self,
        assign: HubAssignJob,
        targets: list[dict],
        base_result,
        page_url: str | None,
    ) -> None:
        """Run a fetch job's deferred yt-dlp download(s) in a DETACHED
        background task, upload the resulting video(s) to the job's
        /assets, then send the FINAL WorkerJobComplete.

        Called after the lane has been released (the download only needs
        the stream URL + referer, not the live browser), so the job sits
        in phase "downloading" without pinning a Chrome lane. Uses its
        OWN temp dir (not the job workdir, which the caller rmtree's) and
        a generous per-download timeout so big VODs aren't killed
        mid-stream (the old inline path capped at 600s and died ~50%).
        """
        import os as _os
        import shutil as _shutil
        import tempfile as _tempfile

        job_id = assign.job_id

        async def _run() -> None:
            from core.fetcher import run_ytdlp

            tmp = Path(_tempfile.mkdtemp(prefix=f"paprika-vid-{job_id}-"))
            dl_timeout = int(
                _os.environ.get("PAPRIKA_VIDEO_DOWNLOAD_TIMEOUT_S", "7200")
            )
            _loop = asyncio.get_running_loop()
            # Current download identity for the Live panel progress bar
            # (set before each target below).  run_ytdlp processes the
            # targets sequentially, so a single holder is unambiguous.
            _cur = {"key": None, "label": None}
            # monotonic time of the last throttled progress marker; reset
            # per target so each download's first update emits promptly.
            _cur_last = [0.0]

            def _emit_progress(line: str) -> None:
                # Schedule a WorkerJobLog send from this worker THREAD.
                # The hub treats JOB_PROGRESS_MARKER lines as ephemeral
                # (broadcast to live viewers, never persisted), so this
                # drives the Live panel's progress bars without flooding
                # log.txt.
                try:
                    _loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(
                            self._send(WorkerJobLog(job_id=job_id, line=line))
                        )
                    )
                except RuntimeError:
                    pass

            def _dl_log(line: str) -> None:
                # Worker stderr for the raw line; ALSO parse live progress
                # and drive the Live panel's per-download progress bar via
                # an ephemeral marker.  fetch / recipe mode has no
                # _make_video_downloader, so this deferred path is where
                # its progress bars come from.
                _logger.info("[%s] [downloading] %s", job_id, line)
                try:
                    _prog = _parse_dl_progress(line.lstrip())
                except Exception:
                    _prog = None
                if _prog is None:
                    return
                if _prog.get("label"):
                    _cur["label"] = _prog["label"]
                _st = _prog.get("state")
                # Throttle downloading/muxing markers to ~1/s -- yt-dlp emits
                # 20+ progress lines/sec on a direct file, and one
                # WorkerJobLog per line floods the hub WS (observed
                # destabilising the worker connection).  start/done always
                # pass through.
                _now = time.monotonic()
                if (_st in ("downloading", "muxing")
                        and _now - _cur_last[0] < 1.0):
                    return
                _cur_last[0] = _now
                _payload = {
                    "key": _cur["key"] or "video",
                    "label": _cur["label"] or _cur["key"] or "video",
                }
                _payload.update(_prog)
                _emit_progress(JOB_PROGRESS_MARKER + json.dumps(_payload))

            added: list = []
            try:
                await self._send(WorkerJobLog(
                    job_id=job_id,
                    line=f"  [downloading] {len(targets)} video target(s) "
                         f"in background…",
                ))
                for t in targets:
                    u = t.get("url")
                    ref = t.get("referer")
                    if not u:
                        continue
                    _cur["key"] = u
                    _cur["label"] = (
                        u.split("?", 1)[0].rsplit("/", 1)[-1] or u
                    )[:64]
                    _cur_last[0] = 0.0  # let this target's first marker emit now
                    ok, msg = await asyncio.to_thread(
                        run_ytdlp, u, tmp,
                        referer=ref, timeout=dl_timeout, log=_dl_log,
                    )
                    # Resolve this target's progress bar (success or fail)
                    # so it doesn't stick at the last %.
                    try:
                        await self._send(WorkerJobLog(
                            job_id=job_id,
                            line=JOB_PROGRESS_MARKER + json.dumps(
                                {"key": u, "state": "done"}),
                        ))
                    except Exception:
                        pass
                    if not ok:
                        await self._send(WorkerJobLog(
                            job_id=job_id,
                            line=f"  [downloading] FAIL {u}: {str(msg)[:200]}",
                        ))
                # Upload every completed file (skip in-progress parts).
                _video_ext = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".ts"}
                for p in sorted(tmp.iterdir()):
                    if not p.is_file():
                        continue
                    low = p.name.lower()
                    if low.endswith((".part", ".ytdl")) or ".part-" in low:
                        continue
                    mime = "video/mp4" if p.suffix.lower() in _video_ext else None
                    await self._upload_asset(
                        assign, p, p.name,
                        mime=mime, page_url=page_url, timeout=900.0,
                    )
                    try:
                        sz = p.stat().st_size
                    except Exception:
                        sz = 0
                    added.append(AssetInfo(
                        name=p.name, size=sz, mime=mime,
                        url=None, page_url=page_url,
                        href=f"/jobs/{job_id}/assets/{p.name}",
                    ))
                await self._send(WorkerJobLog(
                    job_id=job_id,
                    line=f"  [downloading] done: {len(added)} video asset(s) "
                         f"uploaded",
                ))
            except Exception as e:
                await self._send(WorkerJobLog(
                    job_id=job_id,
                    line=f"  [downloading] error: {type(e).__name__}: {e}",
                ))
            finally:
                # Force-complete recovery: hub asked us to wrap up early
                # (HubForceCompleteJob -> _force_complete_video_job set the
                # flag + SIGTERM'd the in-flight yt-dlp). Salvage any
                # .part / .ytdl files in tmp by remuxing them into a
                # playable .mp4 via ffmpeg, then run them through the
                # SAME upload loop above. The ffmpeg "-c copy" remux
                # repackages whatever the MPEG-TS / fMP4 buffer holds
                # without re-encoding -- typically a clean playable
                # truncation of the stream so far.
                _is_force = job_id in self._force_complete_job_ids
                if _is_force:
                    try:
                        await self._send(WorkerJobLog(
                            job_id=job_id,
                            line="  [downloading] force-complete: salvaging partial...",
                        ))
                    except Exception:
                        pass
                    try:
                        _salvaged = await self._salvage_partial_downloads(tmp)
                    except Exception:
                        _salvaged = []
                    for _p in _salvaged:
                        try:
                            await self._upload_asset(
                                assign, _p, _p.name,
                                mime="video/mp4",
                                page_url=page_url, timeout=900.0,
                            )
                            sz = _p.stat().st_size
                            added.append(AssetInfo(
                                name=_p.name, size=sz, mime="video/mp4",
                                url=None, page_url=page_url,
                                href=f"/jobs/{job_id}/assets/{_p.name}",
                            ))
                            await self._send(WorkerJobLog(
                                job_id=job_id,
                                line=f"  [downloading] salvaged {_p.name} "
                                     f"({sz/1024/1024:.1f} MB)",
                            ))
                        except Exception as _ex:
                            try:
                                await self._send(WorkerJobLog(
                                    job_id=job_id,
                                    line=f"  [downloading] salvage upload "
                                         f"failed for {_p.name}: {_ex}",
                                ))
                            except Exception:
                                pass
                _shutil.rmtree(tmp, ignore_errors=True)
                # ALWAYS finish the job so it can never hang in
                # "downloading" -- include whatever video assets landed.
                try:
                    base_result.assets = list(base_result.assets) + added
                except Exception:
                    pass
                if _is_force:
                    try:
                        base_result.partial = True
                    except Exception:
                        pass
                    # Clear the flag now that we've handled this job.
                    try:
                        self._force_complete_job_ids.discard(job_id)
                    except Exception:
                        pass
                try:
                    await self._send(WorkerJobComplete(
                        job_id=job_id, result=base_result,
                    ))
                except Exception:
                    pass

        task = asyncio.create_task(_run())
        # Track {task: job_id} so two consumers can find the in-flight
        # downloads:
        #   * the worker shutdown path can await them,
        #   * _disk_cleanup_loop can keep paprika-vid-<jobid>-* dirs
        #     alive for as long as the download is running (the lane
        #     is already released and the session is gone from
        #     self._sessions, so without this protection a long single-
        #     file mp4 -- 2h cap, no per-segment dir-mtime bump -- could
        #     get swept out from under the live yt-dlp process).
        if not hasattr(self, "_bg_video_tasks") or not isinstance(
            getattr(self, "_bg_video_tasks", None), dict
        ):
            self._bg_video_tasks = {}
        self._bg_video_tasks[task] = job_id
        task.add_done_callback(lambda t: self._bg_video_tasks.pop(t, None))

    async def _force_complete_video_job(self, job_id: str, reason: str) -> None:
        """Hub-driven graceful wrap-up of an in-flight deferred video DL.

        Flow (mirrors HubForceCompleteJob's docstring):
          1. Stamp ``job_id`` into ``_force_complete_job_ids`` so the
             deferred task's finally block knows to salvage partial files.
          2. SIGTERM any yt-dlp / ffmpeg descendants whose argv mentions
             this job's tmp dir (``paprika-vid-{job_id}``). Other jobs'
             downloads are left alone -- belt & braces for shared-worker
             scenarios.
          3. The in-flight ``run_ytdlp`` returns once its subprocess exits;
             the finally block does ffmpeg-remux + upload + JobComplete.
        Idempotent: a second HubForceCompleteJob for the same job is a
        no-op (set membership + already-killed subprocess).
        """
        from server.worker.agent.video import _terminate_ytdlp_descendants_for_job
        try:
            self._force_complete_job_ids.add(job_id)
        except Exception:
            pass
        try:
            await self._send(WorkerJobLog(
                job_id=job_id,
                line=f"  [downloading] hub force-complete: {reason or '(no reason)'}",
            ))
        except Exception:
            pass
        try:
            _killed = await asyncio.to_thread(
                _terminate_ytdlp_descendants_for_job, job_id,
            )
            _logger.info(
                "[%s] force-complete: SIGTERM'd %d yt-dlp/ffmpeg descendants",
                job_id, _killed,
            )
        except Exception:
            _logger.warning(
                "[%s] force-complete: descendant SIGTERM failed", job_id,
                exc_info=True,
            )

    async def _salvage_partial_downloads(self, tmp: "Path") -> list:
        """Remux any partial yt-dlp output in ``tmp`` into playable .mp4 files.

        yt-dlp writes downloads as ``<name>.mp4.part`` (or the variant
        ``<name>.mp4.ytdl`` index). After a SIGTERM mid-stream the ``.part``
        file holds whatever MPEG-TS / fMP4 chunks the ffmpeg mux had already
        committed; ``ffmpeg -c copy -fflags +genpts`` repackages that into a
        seekable .mp4 without re-encoding. Returns the list of newly-created
        ``.mp4`` paths the caller should upload.

        Best-effort: if ffmpeg isn't installed, or remux fails (truly empty
        / unreadable input), the file is skipped. Already-finalised ``.mp4``
        files in tmp aren't touched (yt-dlp moves .part -> .mp4 on success).
        """
        import shutil as _sh
        import subprocess as _sp
        import asyncio as _aio

        out: list = []
        ffmpeg = _sh.which("ffmpeg")
        if not ffmpeg:
            return out
        # Targets: every .part / .mp4.part / .ts in tmp (plus the rare
        # finalised .mp4 yt-dlp may have already produced).
        candidates = []
        for p in sorted(tmp.iterdir()):
            if not p.is_file():
                continue
            low = p.name.lower()
            if low.endswith(".part"):
                candidates.append(p)
            elif low.endswith(".ts"):
                candidates.append(p)
        for src in candidates:
            try:
                # Derive output name: strip .part suffix or change .ts -> .mp4.
                # Add `-salvaged` so it doesn't clash with any finalised file
                # yt-dlp might have raced to write.
                if src.name.lower().endswith(".part"):
                    base = src.name[:-len(".part")]
                else:
                    base = src.stem + ".mp4"
                if base.lower().endswith(".mp4"):
                    stem = base[:-4]
                else:
                    stem = base
                dst = src.with_name(f"{stem}-salvaged.mp4")
                # ffmpeg copies streams (no re-encode), tolerates truncation.
                proc = await _aio.create_subprocess_exec(
                    ffmpeg, "-y",
                    "-fflags", "+genpts+igndts",
                    "-err_detect", "ignore_err",
                    "-i", str(src),
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(dst),
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
                try:
                    await _aio.wait_for(proc.wait(), timeout=120.0)
                except _aio.TimeoutError:
                    try: proc.kill()
                    except Exception: pass
                    continue
                if dst.is_file() and dst.stat().st_size > 1024:
                    out.append(dst)
            except Exception:
                continue
        return out

    def _make_cookie_save_callback(self, assign, save_host: str, log):
        """Return an async callable suitable for FetchOptions.on_complete_dump_cookies.

        The callback receives the post-fetch cookie jar (list of dicts),
        filters it down to cookies whose domain matches ``save_host``
        (so we don't store cross-site tracker noise under this host),
        and PUTs the resulting list to the hub's host registry. The
        hub's existing /hosts/{host} endpoint handles upsert,
        timestamp updates, and projection at injection time -- we
        just hand it the raw jar slice.
        """
        # Derive the hub base URL from asset_upload_base, which is the
        # only absolute hub URL we already know on the worker. Shape:
        # http://hub:8000/jobs/{id}/assets  ->  http://hub:8000
        # Splitting on "/jobs/" is stable across the /api/ rename
        # (was "/api/" before) and keeps working if assets ever live
        # behind a sub-path proxy.
        try:
            base = assign.asset_upload_base.split("/jobs/", 1)[0]
        except Exception:
            base = None

        async def _save_cb(jar: list[dict]) -> None:
            if not base:
                log("  ... cookie save skipped: cannot derive hub base url")
                return
            host_norm = (save_host or "").strip().lower()
            if host_norm.startswith("www."):
                host_norm = host_norm[4:]
            if not host_norm:
                log("  ... cookie save skipped: no host")
                return
            # Host-filter: cookies whose domain matches the registry
            # host (exact, suffix, or parent). Without this we'd store
            # 100+ third-party tracker cookies in every record.
            filtered: list[dict] = []
            for c in jar or []:
                if not isinstance(c, dict):
                    continue
                dom = (c.get("domain") or "").lower().lstrip(".")
                if not dom:
                    continue
                if dom.startswith("www."):
                    dom = dom[4:]
                if (
                    dom == host_norm
                    or dom.endswith("." + host_norm)
                    or host_norm.endswith("." + dom)
                ):
                    filtered.append(c)

            # Always upsert -- the operator wants every visited host
            # to surface in the Hosts tab, even sites that set no
            # first-party cookies (so they can curate manually later).
            # Safety net: when the new dump has ZERO matching cookies
            # but the existing record has some, preserve them so a
            # casual revisit doesn't wipe a saved login.
            url = f"{base}/hosts/{host_norm}"
            existing_notes = None
            existing_cookies: list[dict] = []
            existed = False
            try:
                async with make_async_client(timeout=10.0) as cli:
                    g = await cli.get(url)
                    if g.status_code == 200:
                        rec = g.json() or {}
                        existing_notes = rec.get("notes")
                        existing_cookies = list(rec.get("cookies") or [])
                        existed = True
            except Exception:
                pass

            if filtered:
                cookies_to_save = filtered
                kind_label = (
                    f"replaced ({len(filtered)} cookie(s))"
                    if existed
                    else f"created ({len(filtered)} cookie(s))"
                )
            elif existing_cookies:
                # Existing record + no new matches → keep what we had.
                cookies_to_save = existing_cookies
                kind_label = (
                    f"refreshed timestamp only "
                    f"(kept {len(existing_cookies)} existing cookie(s); "
                    f"none matched in this fetch)"
                )
            else:
                # Brand-new host with no matching cookies → empty
                # marker entry so the Hosts tab shows "I visited
                # this" without forcing the operator to do anything.
                cookies_to_save = []
                kind_label = "marker created (0 cookie(s) matched this host)"

            notes = existing_notes
            if not notes:
                notes = f"auto-saved by fetch job {assign.job_id}"

            try:
                async with make_async_client(timeout=15.0) as cli:
                    r = await cli.put(
                        url,
                        json={
                            "cookies": cookies_to_save,
                            "notes": notes,
                        },
                    )
                if r.status_code in (200, 201):
                    log(
                        f"  ... cookie save: PUT /hosts/{host_norm} "
                        f"-- {kind_label} [http {r.status_code}]"
                    )
                else:
                    log(f"  !! cookie save failed: http {r.status_code} {r.text[:200]}")
            except Exception as e:
                log(f"  !! cookie save crashed: {type(e).__name__}: {e}")

        return _save_cb

    async def resolve_engine(
        self,
        slug: str,
        fallback_kind: str = "chat",
    ) -> dict | None:
        """Ask the hub for the full config of an engine.

        Used by ``page.ask`` (chat) and the in-coming ``page.agent``
        engine-registry path so worker code doesn't need to know
        endpoints, models, or API keys directly -- the operator owns
        all of that in the admin UI.

        ``slug`` may be ``"auto"`` to mean "pick the promoted engine of
        ``fallback_kind``"; otherwise it's the literal engine slug.
        Returns the dict the hub's ``/engines/.../resolve`` endpoint
        produced, or None if the resolve failed (caller falls back to
        the legacy AGENT_LLM_URL env path).

        Best-effort: any HTTP error is swallowed + logged so a
        misconfigured engine record can't kill the action.
        """
        slug = (slug or "").strip().lower() or "auto"
        if slug == "auto":
            url = f"{self.hub_http_url.rstrip('/')}/engines/auto/{fallback_kind}/resolve"
        else:
            url = f"{self.hub_http_url.rstrip('/')}/engines/{slug}/resolve"
        body: dict = {}
        if self.worker_secret:
            body["secret"] = self.worker_secret
        try:
            r = await self._http.post(url, json=body, timeout=10.0)
            if r.status_code == 404:
                # No registry entry -- caller falls back to legacy env
                # path. Don't log noisily, this is the common "engine
                # registry not seeded yet" case on fresh deploys.
                return None
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                return None
            return data
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] resolve_engine({slug}) "
                f"failed: {type(e).__name__}: {e}; falling back to "
                f"AGENT_LLM_URL",
            )
            return None

    async def resolve_worker_agent_engine(self):
        """Resolve the operator-selected page.agent backend engine
        (hub Settings ``worker_agent_engine_slug``, set via the Engines-tab
        "use this engine for page.agent" checkbox).

        Returns:
          * ``dict``  -- the selected engine's resolved config.
          * ``False`` -- a CLEAN 404 = NO engine selected => page.agent is
                         DISABLED (the caller refuses the agent loop).
          * ``None``  -- a transient error (network / 5xx) => the caller
                         keeps the legacy AGENT_URL path so a hub hiccup
                         doesn't take page.agent down.
        """
        url = f"{self.hub_http_url.rstrip('/')}/engines/worker-agent-resolve"
        body: dict = {}
        if self.worker_secret:
            body["secret"] = self.worker_secret
        try:
            r = await self._http.post(url, json=body, timeout=10.0)
            if r.status_code == 404:
                return False  # no engine selected -> page.agent disabled
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] resolve_worker_agent_engine "
                f"failed: {type(e).__name__}: {e}; keeping legacy AGENT_URL",
            )
            return None

