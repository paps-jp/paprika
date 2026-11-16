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
from server.worker import scratch_pool
from .video import _make_video_downloader, _parse_dl_progress, detect_yt_dlp
from ._mix_uploads import _is_video_asset_name
from .workerid import WORKER_ID_FILE, _WorkerIdReassigned, hub_http_base


# Worker-internal disk pressure threshold. Above this %, _run_assigned_job
# fails the assignment fast (before acquiring a lane) and runs the
# emergency cleanup so the next dispatch has a fighting chance. Hub side
# also filters at this threshold in pick_worker, so dispatches only slip
# through during the heartbeat lag window (≤10s).
_DISK_PRESSURE_FAIL_PCT = 90.0


class _AbandonedJob(Exception):
    """Raised inside the deferred-download task when the hub has already
    finished with the job (failed / cancelled / deleted).

    Not an error condition: it is the unwind path that skips the uploads and
    the completion send while still running the ``finally`` that hands the
    node's ramdisk back. See ``_abandon_video_job``.
    """


class _NoVideoRam(Exception):
    """Raised inside the deferred-download task when no room could be had on
    the ramdisk for the video.

    Video files are NEVER written to a disk (operator rule, 2026-11-16: the
    ramdisk exists to spare the node's SSDs). So this is not an error to work
    around: the job is finished without its video, and says so.
    """


def _looks_like_enospc(msg: object) -> bool:
    """True when a yt-dlp failure message reads as "the filesystem is full".

    Only consulted for downloads placed on the shared ramdisk pool, where a
    neighbouring CT can legitimately fill the tmpfs between our admission
    check and our writes -- that case is worth one more try IN PLACE after a
    pause (yt-dlp carries on from its .part), unlike an ordinary failure.
    """
    s = str(msg or "").lower()
    return (
        "no space left" in s
        or "enospc" in s
        or "errno 28" in s
        or "disk quota exceeded" in s
    )


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

    from server.worker.lanes import chrome_lane_root

    # Lane dirs are only in /tmp when PAPRIKA_CHROME_LANE_ROOT is unset; on a
    # node-tmpfs deployment they live there instead (docs/ramdisk-chrome-lane.md).
    # Globbing the wrong root here would silently stop reclaiming Chrome's
    # caches -- which is precisely what this disk-pressure path exists for.
    _lanes = str(chrome_lane_root()).rstrip("/")

    cleaned = 0
    # (pattern, kind) — kind is "dir" or "file".
    targets: list[tuple[str, str]] = [
        # yt-dlp HLS fragment scratch — single biggest hog during video DLs.
        ("/root/.cache/yt-dlp", "dir"),
        # Chrome on-disk caches: GPU + shader + Code Cache + HTTP cache.
        # Safe to nuke (Chrome rebuilds on next launch; worst-case is a
        # warm-up pause on the first hit). Cookies / Preferences untouched.
        (f"{_lanes}/chrome-lane-*/Default/Cache", "dir"),
        (f"{_lanes}/chrome-lane-*/Default/Code Cache", "dir"),
        (f"{_lanes}/chrome-lane-*/Default/GPUCache", "dir"),
        (f"{_lanes}/chrome-lane-*/Default/Service Worker/CacheStorage", "dir"),
        (f"{_lanes}/chrome-lane-*/ShaderCache", "dir"),
        (f"{_lanes}/chrome-lane-*/GraphiteDawnCache", "dir"),
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


# ---- deferred video download: cookies for the browser-less yt-dlp -----------
#
# The deferred download runs after the lane's Chrome is gone, so the only
# cookies it can carry are the ones dumped from the browser at the end of the
# fetch (``FetchOptions.on_complete_dump_cookies``). That jar is the WHOLE
# profile (page host, CDN hosts the player talked to, trackers); we hand
# yt-dlp only the entries whose domain is related to a host the download will
# actually contact, written as a Netscape cookies.txt.

_DEFERRED_COOKIE_FILE_PREFIX = "ytdlp_cookies_deferred_"


def _host_of(url: str | None) -> str:
    try:
        h = (urlsplit(url or "").hostname or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def _deferred_cookie_hosts(targets: list[dict], page_url: str | None) -> list[str]:
    """Hosts a deferred download may contact: each target's stream URL and
    referer, plus the page itself. Deduplicated, order preserved, no www."""
    out: list[str] = []
    for u in [page_url] + [
        v for t in (targets or []) for v in (t.get("url"), t.get("referer"))
    ]:
        h = _host_of(u)
        if h and h not in out:
            out.append(h)
    return out


def _cookie_domain_matches(domain: str, hosts: list[str]) -> bool:
    d = (domain or "").lower().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    if not d:
        return False
    for h in hosts:
        if d == h or h.endswith("." + d) or d.endswith("." + h):
            return True
    return False


def _netscape_cookies_for_hosts(jar: list[dict] | None, hosts: list[str]) -> tuple[str, int]:
    """Render the CDP cookie dump (``Network.getAllCookies`` as dicts) as a
    Netscape cookies.txt restricted to ``hosts`` (see
    ``_cookie_domain_matches``). Returns ``(text, n_cookies)``; ``n_cookies``
    is 0 when nothing matched (the caller then skips the file).

    Netscape line: domain, include-subdomains flag, path, secure, expires
    (unix seconds; 0 = session cookie -- CDP reports -1), name, value."""
    lines = ["# Netscape HTTP Cookie File"]
    n = 0
    for c in jar or []:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "")
        if not _cookie_domain_matches(domain, hosts):
            continue
        name = str(c.get("name") or "")
        if not name:
            continue
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        try:
            exp = int(float(c.get("expires") or 0))
        except (TypeError, ValueError):
            exp = 0
        if exp < 0:
            exp = 0
        value = str(c.get("value") or "")
        # Tabs / newlines inside a value would break the line format.
        value = value.replace("\t", " ").replace("\n", " ").replace("\r", " ")
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{exp}\t{name}\t{value}")
        n += 1
    return "\n".join(lines) + "\n", n


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

                # Directory for assets + page.html + log.txt: ALWAYS on the
                # shared ramdisk pool (2026-11-16). The page's images are
                # never written to the CT disk (an SSD) any more than its
                # video is; with no room the job fails instead. Any of the
                # existing rmtree(workdir) paths stay correct: a pool claim
                # whose dir is gone is self-healing (scratch_pool).
                workdir = await scratch_pool.acquire_small_wait(
                    f"paprika-{job_id}-", self.worker_id,
                )
                if workdir is None:
                    raise RuntimeError(
                        "no room on the ramdisk for this job's files "
                        "(they are never written to disk): "
                        + scratch_pool.why_small_refused()
                    )
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

                # Detach big-video downloads from the lane. When the
                # operator asked for video AND this isn't a keep_session
                # job, DETECT streams during capture but defer the
                # (often 10+ min) yt-dlp download to a background task so
                # the lane is freed immediately. The job sits in phase
                # "downloading" until the background task uploads the
                # video and sends the final WorkerJobComplete. keep_session
                # is excluded -- there the operator drives download_video()
                # interactively inside the live session.
                # (keep_session itself is only read into its local further
                # down, so test the option directly here.)
                _defer_video = (
                    bool(getattr(assign.options, "download_video", False))
                    and not bool(getattr(assign.options, "keep_session", False))
                )
                # The deferred yt-dlp runs after this lane's Chrome is gone,
                # so its only chance at the session's cookies (page host AND
                # the CDN hosts the player authenticated against) is the
                # end-of-fetch jar dump. Tee that dump into a holder; the
                # host-registry save (save_cb) keeps running unchanged.
                # Without this tee the dump only happens when the hub set
                # save_cookies_host.
                _deferred_jar: dict = {}
                dump_cb = save_cb
                if _defer_video:
                    async def dump_cb(jar, _inner=save_cb):
                        _deferred_jar["jar"] = [
                            c for c in (jar or []) if isinstance(c, dict)
                        ]
                        if _inner is not None:
                            await _inner(jar)

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
                # What the recipe replay actually did. Reported back in
                # JobResult.facts["recipe"] -- until 2026-11 this return
                # value was dropped, so nobody could tell whether a recipe
                # ran, let alone whether it helped.
                _recipe_report: dict = {}
                async def _recipe_cb(tab):
                    if _picked_recipe:
                        _rep = await _apply_fetch_recipe(tab, _picked_recipe, _log)
                        try:
                            _errs = list((_rep or {}).get("errors") or [])
                            _recipe_report.update({
                                "pattern": str(_picked_recipe.get("pattern") or "")[:300],
                                "description": str(_picked_recipe.get("description") or "")[:120],
                                "created_by": str(_picked_recipe.get("created_by") or ""),
                                "created_from_job": str(_picked_recipe.get("created_from_job") or ""),
                                "ran": int((_rep or {}).get("ran") or 0),
                                "ok": int((_rep or {}).get("ok") or 0),
                                "n_errors": len(_errs),
                                "errors": [str(e)[:160] for e in _errs[:3]],
                            })
                        except Exception:
                            pass

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
                    on_complete_dump_cookies=dump_cb,
                    on_browser_ready=ready_cb,
                    on_browser_closing=closing_cb,
                    on_after_navigate=_recipe_cb if _picked_recipe else None,
                    network_log=fetch_network_log,
                    # V: operator-managed URL deny list from Settings.
                    asset_url_blacklist=list(getattr(assign, "asset_url_blacklist", []) or []),
                    on_asset_saved=_on_asset_saved,
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
                #
                # Timed (2026-08-16): this serial POST chain runs AFTER the
                # last job-log line, so it was invisible in the worker log
                # while accounting for the bulk of a job's wall time. The
                # measurement rides WorkerJobComplete.upload_ms.
                _upl_t0 = time.monotonic()
                await self._upload_files(assign, workdir, result, uploaded_names)
                _upload_ms = (time.monotonic() - _upl_t0) * 1000.0
                _logger.info(
                    "[%s] uploads settled in %.1fs (%d asset(s) shipped inline)",
                    job_id, _upload_ms / 1000.0, len(uploaded_names),
                )

                # Build the JobResult Pydantic object with hub-side hrefs.
                # page_url: every fetch-mode asset belongs to the single
                # page the operator asked us to grab (assign.url). The
                # fetcher tracks per-asset source URL + mime but not the
                # initiating document URL, so we stamp the job URL on
                # every entry -- same shape the assets.json + .meta/
                # sidecar pipeline uses.
                page_url_for_assets = assign.url or None
                # A VIDEO the hub never accepted (507 while .48 is full, or any
                # other failure on both the inline and the reconcile upload)
                # must not be listed: video-pull would make a crawl_video row
                # for it and vfe could only fail it with NoSuchKey. Images keep
                # the old behaviour (they go to .47, which is never gated).
                _vid_not_up = [
                    a["name"] for a in result.assets_saved
                    if _is_video_asset_name(a["name"])
                    and a["name"] not in uploaded_names
                ]
                if _vid_not_up:
                    _log(
                        f"  !! {len(_vid_not_up)} video asset(s) not uploaded, "
                        f"left out of the result: {', '.join(_vid_not_up[:3])}"
                    )
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
                    if a["name"] not in _vid_not_up
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
                # Objective facts for the outcome verifier (measure only).
                # Any video the capture itself saved is still on disk here;
                # videos from the deferred download are probed over there.
                try:
                    _facts: dict = {}
                    if _recipe_report:
                        _facts["recipe"] = dict(_recipe_report)
                    from server.worker import media_facts as _mf
                    _vid_paths = [
                        assets_dir / a["name"] for a in result.assets_saved
                        if _mf.is_video_name(a.get("name") or "")
                    ]
                    if _vid_paths:
                        _media = await _mf.probe_paths(_vid_paths)
                        if _media:
                            _facts["media"] = _media
                    if _facts:
                        job_result.facts = _facts
                except Exception:
                    _logger.debug("[%s] facts build failed", job_id, exc_info=True)
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
                        cookie_jar=_deferred_jar.get("jar"),
                    )
                else:
                    await self._send(
                        WorkerJobComplete(
                            job_id=job_id,
                            result=job_result,
                            sent_at=time.time(),
                            upload_ms=_upload_ms,
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
                    # A screencast relay opened against this lane must not
                    # keep streaming once the lane goes back to the pool --
                    # the next job to grab it would otherwise leak frames
                    # of someone else's page to a stale viewer.
                    self._close_screencasts_for_lane(lane.lane_idx)
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

    async def _deferred_cookies_file(
        self,
        assign: HubAssignJob,
        targets: list[dict],
        page_url: str | None,
        cookie_jar: list[dict] | None,
    ) -> tuple[Path | None, str]:
        """Pick the cookies.txt for a deferred download, or None.

        1. the browser's end-of-fetch jar, cut down to the page / stream /
           referer hosts (this is what carries per-play session cookies);
        2. else the hub's per-host registry for the page host (operator
           logins; what media.py and the download tier already use).

        Returns ``(path_or_None, human note for the job log)``. Never raises:
        a cookie problem must not cost the download."""
        from types import SimpleNamespace

        ref_url = page_url or getattr(assign, "url", None) or (
            (targets[0].get("url") if targets else None)
        )
        hosts = _deferred_cookie_hosts(targets, ref_url)
        jar_n = len(cookie_jar or [])
        if cookie_jar:
            text, n = _netscape_cookies_for_hosts(cookie_jar, hosts)
            if n:
                try:
                    fd, p = tempfile.mkstemp(
                        prefix=_DEFERRED_COOKIE_FILE_PREFIX, suffix=".txt",
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(text)
                    return Path(p), (
                        f"{n} browser cookie(s) for {len(hosts)} host(s) "
                        f"({', '.join(hosts[:4])})"
                    )
                except OSError as e:
                    return None, f"none (browser jar write failed: {e})"
        # Fallback: the hub's host registry (operator-saved logins).
        path = None
        try:
            ns = SimpleNamespace(
                asset_upload_base=getattr(assign, "asset_upload_base", None),
            )
            path = await self._fetch_cookies_txt_for(ref_url, ns, None)
        except Exception:
            path = None
        if path:
            return path, (
                f"host registry cookies.txt for {_host_of(ref_url)} "
                f"(browser jar: {jar_n} cookie(s), none for {', '.join(hosts[:4]) or '-'})"
            )
        return None, (
            f"none (browser jar: {jar_n} cookie(s), none for "
            f"{', '.join(hosts[:4]) or '-'}; registry: empty)"
        )

    def _spawn_deferred_video_download(
        self,
        assign: HubAssignJob,
        targets: list[dict],
        base_result,
        page_url: str | None,
        cookie_jar: list[dict] | None = None,
        resume: dict | None = None,
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

        ``cookie_jar`` is the browser's end-of-fetch cookie dump (CDP
        ``Network.getAllCookies`` as dicts). The entries related to the
        page / stream / referer hosts are written to a Netscape cookies.txt
        and passed to yt-dlp (``--cookies``), so cookie-gated streams --
        session cookies the player set on the CDN -- survive the handoff
        to the browser-less download. ``None`` / no match falls back to
        the hub's per-host registry (``/hosts/{host}/cookies.txt``), which
        holds operator logins but not per-play session cookies. The file
        is removed when the download finishes.

        That temp dir is on the shared node-tmpfs pool
        (server/worker/scratch_pool.py) and nowhere else: when the pool has
        no room the download waits for some and is given up if none comes --
        video is never written to the CT disk (_acquire_video_ram).

        ``resume`` is the manifest of a download the previous process handed
        over on its way out of a recycle (server/worker/resume.py). The
        download then runs in the SAME directories with the SAME arguments,
        which is all yt-dlp needs to carry on from its ``.part``.
        """
        import os as _os

        from server.worker import resume as _resume
        from server.worker import scratch_pool as _pool

        job_id = assign.job_id

        async def _run() -> None:
            from core.fetcher import run_ytdlp

            _prefix = f"paprika-vid-{job_id}-"
            # A directory handed over by the previous process counts only if
            # it is RAM-backed: one left on the CT disk by an older version
            # is not carried on with (video is never written to a disk).
            _resumed_dirs = [
                Path(_d) for _d in ((resume or {}).get("dl_dirs") or [])
                if Path(_d).is_dir() and _pool.video_dir_ok(_d)
            ]
            if _resumed_dirs:
                tmp = _resumed_dirs[-1]
                _on_pool = bool((resume or {}).get("on_pool"))
                _dl_dirs: list[Path] = list(_resumed_dirs)
            else:
                # The shared node-tmpfs pool, or nowhere. This task is THE
                # writer of video bytes (1-2GB per video, up to 2h resident),
                # and those bytes must never land on the CT disk -- the
                # node's SSDs are what the ramdisk exists to spare. When the
                # pool cannot take the download right now we wait for room,
                # and give the video up if none comes
                # (_acquire_video_ram). See server/worker/scratch_pool.py.
                tmp = await self._acquire_video_ram(job_id, _prefix)
                _on_pool = tmp is not None and _pool.is_ram(tmp)
                # Every directory that may hold output: one, or none at all.
                _dl_dirs = [tmp] if tmp is not None else []
            _logger.info(
                "[%s] video scratch: %s (%s%s)", job_id, tmp,
                "shared ramdisk pool" if _on_pool
                else "no room on the ramdisk" if tmp is None
                else "local disk",
                ", picked up after a restart" if _resumed_dirs else "",
            )
            _resumes = int((resume or {}).get("resumes") or 0) + (1 if resume else 0)
            try:
                _manifest_base = {
                    "worker_id": self.worker_id,
                    "created": (resume or {}).get("created") or time.time(),
                    "resumes": _resumes,
                    "assign": assign.model_dump(mode="json"),
                    "base_result": base_result.model_dump(mode="json"),
                    "targets": [dict(_t) for _t in targets],
                    "page_url": page_url,
                }
            except Exception:
                # Not serialisable -> this download simply cannot be resumed.
                _manifest_base = None

            def _save_manifest() -> None:
                # What a restarted worker needs to pick this download up
                # again. Rewritten whenever the directories change.
                if _manifest_base is None or not _resume.enabled():
                    return
                _m = dict(_manifest_base)
                _m["dl_dirs"] = [str(_d) for _d in _dl_dirs]
                _m["on_pool"] = _on_pool
                _m["cookies_file"] = str(cookies_file) if cookies_file else None
                _resume.save(job_id, _m)
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
                if "frame=" in line and "fps=" in line:
                    # ffmpeg is doing the download (live stream, or a stream
                    # yt-dlp hands over to it). It keeps nothing to carry on
                    # from, so a restart must not try to resume this one.
                    try:
                        self._bg_video_noresume.add(job_id)
                    except AttributeError:
                        self._bg_video_noresume = {job_id}
                try:
                    _prog = _parse_dl_progress(line.lstrip())
                except Exception:
                    _prog = None
                if _prog is None:
                    return
                if _prog.get("eta"):
                    # What the recycle reads to decide whether this download
                    # is worth putting itself off for (_maybe_begin_recycle).
                    self._note_download_left(job_id, _prog["eta"])
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
            _upload_failed = 0
            # ffprobe facts for each uploaded video (outcome verifier input).
            _media_facts: list = []
            cookies_file: Path | None = None
            _no_ram = False
            try:
                if not _dl_dirs:
                    raise _NoVideoRam(job_id)
                await self._send(WorkerJobLog(
                    job_id=job_id,
                    line=(
                        f"  [downloading] {len(targets)} video target(s) "
                        f"in background…"
                        if not resume else
                        f"  [downloading] picked up again after a worker "
                        f"restart ({len(targets)} target(s), time {_resumes})"
                    ),
                ))
                _kept_ck = (resume or {}).get("cookies_file")
                if _kept_ck and Path(_kept_ck).is_file():
                    cookies_file, _ck_note = (
                        Path(_kept_ck), "kept from before the restart",
                    )
                else:
                    cookies_file, _ck_note = await self._deferred_cookies_file(
                        assign, targets, page_url, cookie_jar,
                    )
                await self._send(WorkerJobLog(
                    job_id=job_id, line=f"  [downloading] cookies: {_ck_note}",
                ))
                _save_manifest()
                _more = sum(1 for _t in targets if _t.get("url"))
                for t in targets:
                    u = t.get("url")
                    ref = t.get("referer")
                    if not u:
                        continue
                    # yt-dlp's estimate covers this file only; the recycle
                    # also needs to know how many come after it.
                    _more -= 1
                    self._note_download_stage(job_id, more=_more)
                    if job_id in self._abandoned_job_ids:
                        # The hub finished with this job while we were on an
                        # earlier target. Starting the next download would
                        # hold the node's ramdisk for another hour for an
                        # asset nobody can accept.
                        break
                    _cur["key"] = u
                    _cur["label"] = (
                        u.split("?", 1)[0].rsplit("/", 1)[-1] or u
                    )[:64]
                    _cur_last[0] = 0.0  # let this target's first marker emit now
                    ok, msg = await asyncio.to_thread(
                        run_ytdlp, u, _dl_dirs[-1],
                        referer=ref, timeout=dl_timeout, log=_dl_log,
                        cookies_file=cookies_file,
                    )
                    if not ok and _on_pool and _looks_like_enospc(msg):
                        # A neighbouring CT won the race between our
                        # admission check and its own writes. Give the pool
                        # a moment and carry on IN PLACE -- yt-dlp resumes
                        # from its .part. Never on the CT disk.
                        try:
                            _pause = float(
                                _os.environ.get("PAPRIKA_VIDEO_ENOSPC_WAIT_S") or 30.0
                            )
                        except (TypeError, ValueError):
                            _pause = 30.0
                        await self._send(WorkerJobLog(
                            job_id=job_id,
                            line=f"  [downloading] ramdisk pool full -- "
                                 f"carrying on in place in {_pause:.0f}s "
                                 f"(video is never written to disk)",
                        ))
                        await asyncio.sleep(max(0.0, _pause))
                        _cur_last[0] = 0.0
                        ok, msg = await asyncio.to_thread(
                            run_ytdlp, u, _dl_dirs[-1],
                            referer=ref, timeout=dl_timeout, log=_dl_log,
                            cookies_file=cookies_file,
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
                # Fetched but not on the hub yet: an exit now loses it all the
                # same, and the upload may be waiting out a full store.
                self._note_download_stage(job_id, stage="upload")
                # Upload every completed file (skip in-progress parts).
                _video_ext = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".ts"}
                if job_id in self._abandoned_job_ids:
                    # Terminal on the hub: the asset POST would 404 and the
                    # bytes are already unwanted. Fall through to the finally,
                    # which releases the scratch directory.
                    raise _AbandonedJob(job_id)
                for p in sorted(_f for _d in _dl_dirs for _f in _d.iterdir()):
                    if not p.is_file():
                        continue
                    low = p.name.lower()
                    if low.endswith((".part", ".ytdl")) or ".part-" in low:
                        continue
                    mime = "video/mp4" if p.suffix.lower() in _video_ext else None
                    if not await self._upload_video_with_backoff(
                        assign, p, p.name,
                        mime=mime, page_url=page_url, timeout=900.0,
                    ):
                        # Not on the hub/.48: listing it anyway made video-pull
                        # create a crawl_video row that vfe could only fail
                        # with NoSuchKey (2026-11-15).
                        _upload_failed += 1
                        continue
                    try:
                        sz = p.stat().st_size
                    except Exception:
                        sz = 0
                    added.append(AssetInfo(
                        name=p.name, size=sz, mime=mime,
                        url=None, page_url=page_url,
                        href=f"/jobs/{job_id}/assets/{p.name}",
                    ))
                    # Measure the file while it is still on local disk
                    # (facts for the outcome verifier; never a verdict).
                    try:
                        from server.worker import media_facts as _mf
                        if (
                            _mf.enabled() and _mf.is_video_name(p.name)
                            and len(_media_facts) < _mf.MAX_FILES
                        ):
                            _media_facts.append(
                                await asyncio.to_thread(_mf.probe_file, p)
                            )
                    except Exception:
                        pass
                await self._send(WorkerJobLog(
                    job_id=job_id,
                    line=f"  [downloading] done: {len(added)} video asset(s) "
                         f"uploaded"
                         + (f", {_upload_failed} failed (left out of the result)"
                            if _upload_failed else ""),
                ))
            except _NoVideoRam:
                _no_ram = True
                _logger.info(
                    "[%s] video NOT downloaded: no room on the ramdisk "
                    "(video is never written to disk)", job_id,
                )
                try:
                    await self._send(WorkerJobLog(
                        job_id=job_id,
                        line="  [downloading] no room on the ramdisk for the "
                             "video; NOT downloaded (video is never written "
                             "to disk)",
                    ))
                except Exception:
                    pass
            except _AbandonedJob:
                _logger.info(
                    "[%s] deferred download abandoned: hub already finished "
                    "with this job; dropping %d scratch dir(s)",
                    job_id, len(_dl_dirs),
                )
            except Exception as e:
                await self._send(WorkerJobLog(
                    job_id=job_id,
                    line=f"  [downloading] error: {type(e).__name__}: {e}",
                ))
            finally:
                # This process is seeing the download through to its end (or
                # is being stopped in an orderly way): nothing to resume.
                # The recycle's os._exit never gets here, which is exactly
                # how its manifests survive.
                _resume.remove(job_id)
                # The cookies.txt is plaintext session state -- drop it as
                # soon as yt-dlp is done with it (same as media.py).
                if cookies_file is not None:
                    try:
                        cookies_file.unlink()
                    except OSError:
                        pass
                # Force-complete recovery: hub asked us to wrap up early
                # (HubForceCompleteJob -> _force_complete_video_job set the
                # flag + SIGTERM'd the in-flight yt-dlp). Salvage any
                # .part / .ytdl files in tmp by remuxing them into a
                # playable .mp4 via ffmpeg, then run them through the
                # SAME upload loop above. The ffmpeg "-c copy" remux
                # repackages whatever the MPEG-TS / fMP4 buffer holds
                # without re-encoding -- typically a clean playable
                # truncation of the stream so far.
                _is_abandoned = job_id in self._abandoned_job_ids
                _is_force = (not _is_abandoned) and (
                    job_id in self._force_complete_job_ids
                )
                if _is_force:
                    try:
                        await self._send(WorkerJobLog(
                            job_id=job_id,
                            line="  [downloading] force-complete: salvaging partial...",
                        ))
                    except Exception:
                        pass
                    _salvaged = []
                    for _d in _dl_dirs:
                        try:
                            _salvaged.extend(
                                await self._salvage_partial_downloads(_d)
                            )
                        except Exception:
                            pass
                    for _p in _salvaged:
                        try:
                            if not await self._upload_asset(
                                assign, _p, _p.name,
                                mime="video/mp4",
                                page_url=page_url, timeout=900.0,
                            ):
                                # The hub asked us to wrap up, so no backoff
                                # here -- but never list a file it doesn't have.
                                _upload_failed += 1
                                continue
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
                # release() drops the pool claim too, so the node's admission
                # accounting frees up the instant the bytes do.
                for _d in _dl_dirs:
                    _pool.release(_d)
                if _is_abandoned:
                    # The hub expects no result here: the job is already
                    # failed / cancelled / deleted, and a late
                    # WorkerJobComplete would either 404 or overwrite whatever
                    # a redrive on another worker produced. The scratch is
                    # freed above, which is the whole point of abandoning.
                    # (Guarded by a flag rather than an early ``return``: a
                    # return inside ``finally`` would also swallow the
                    # CancelledError of a worker shutting down.)
                    self._abandoned_job_ids.discard(job_id)
                    self._force_complete_job_ids.discard(job_id)
                else:
                    # ALWAYS finish the job so it can never hang in
                    # "downloading" -- include whatever video assets landed.
                    try:
                        base_result.assets = list(base_result.assets) + added
                    except Exception:
                        pass
                    # Report what was measured. ``deferred`` records that a
                    # download WAS attempted, so "tried and got nothing" is
                    # distinguishable from "the page had no video".
                    try:
                        _f = dict(getattr(base_result, "facts", None) or {})
                        _f["media"] = list(_f.get("media") or []) + _media_facts
                        _f["deferred"] = {
                            "targets": len(targets or []),
                            "files": len(added),
                            "upload_failed": _upload_failed,
                        }
                        if _no_ram:
                            # "wanted a video, had nowhere in RAM to put it"
                            # -- distinguishable from a download that failed.
                            _f["deferred"]["no_ram"] = True
                        base_result.facts = _f
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
                            sent_at=time.time(),
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
        task.add_done_callback(
            lambda t: (getattr(self, "_bg_video_left", None) or {}).pop(job_id, None)
        )
        task.add_done_callback(
            lambda t: (getattr(self, "_bg_video_stage", None) or {}).pop(job_id, None)
        )
        task.add_done_callback(
            lambda t: (getattr(self, "_bg_video_noresume", None) or set()).discard(job_id)
        )

    async def _acquire_video_ram(self, job_id: str, prefix: str) -> Path | None:
        """A directory for one video download: on the ramdisk, or None.

        Video files are never written to a disk -- the node's SSDs are what
        the ramdisk exists to spare (operator rule, 2026-11-16). Before that
        rule a download the pool turned down went to the CT disk instead, and
        measured over an hour that was 5,977 of 10,964 downloads.

        So when the pool has no room we wait for some: nothing is held while
        waiting (the lane is already released, no bytes are on the ramdisk),
        and a download ending anywhere on the node frees room. After
        PAPRIKA_VIDEO_RAM_WAIT_S seconds (default 300) the video is given up
        and the job finishes without it. PAPRIKA_VIDEO_ALLOW_DISK=1 restores
        the disk fallback for a development box that has no ramdisk at all.
        """
        from server.worker import scratch_pool as _pool

        need = _pool.video_reserve_bytes()
        try:
            wait_s = float(os.environ.get("PAPRIKA_VIDEO_RAM_WAIT_S") or 300.0)
        except (TypeError, ValueError):
            wait_s = 300.0
        deadline = time.monotonic() + max(0.0, wait_s)
        said = False
        while True:
            d = _pool.acquire(prefix, self.worker_id, need)
            if d is not None:
                return d
            if _pool.video_disk_allowed():
                return Path(tempfile.mkdtemp(prefix=prefix))
            left = deadline - time.monotonic()
            if left <= 0 or job_id in getattr(self, "_abandoned_job_ids", ()):
                return None
            if not said:
                said = True
                why = _pool.why_refused(need, self.worker_id) or "the ramdisk is contended"
                _logger.info(
                    "[%s] video waits for room on the ramdisk, up to %.0fs: %s",
                    job_id, wait_s, why,
                )
                try:
                    await self._send(WorkerJobLog(
                        job_id=job_id,
                        line=f"  [downloading] waiting for room on the ramdisk "
                             f"(up to {wait_s:.0f}s): {why}",
                    ))
                except Exception:
                    pass
            await asyncio.sleep(min(10.0, max(0.5, left)))

    async def _hub_job_wants_download(self, job_id: str) -> str:
        """Why the hub no longer wants ``job_id``'s video from us, or "".

        The opposite bias from :meth:`_hub_job_is_finished`: there, no answer
        means "keep downloading"; here it means "do not start". Resuming for
        a job that is gone would hold the ramdisk for an hour to upload an
        asset nobody can accept -- so only a positive answer counts.
        """
        base = (getattr(self, "hub_http_url", "") or "").rstrip("/")
        if not base or getattr(self, "_http", None) is None:
            return "no hub link"
        last = "no answer from the hub"
        for _try in range(3):
            if _try:
                await asyncio.sleep(3.0)
            try:
                r = await self._http.get(f"{base}/jobs/{job_id}", timeout=10.0)
            except Exception as e:
                last = f"hub unreachable ({type(e).__name__})"
                continue
            if r.status_code == 404:
                return "the job is gone"
            if r.status_code != 200:
                last = f"hub answered {r.status_code}"
                continue
            try:
                body = r.json() or {}
            except Exception:
                last = "unreadable answer"
                continue
            status = str(body.get("status") or "").split(".")[-1].lower()
            owner = body.get("worker_id")
            if owner and owner != self.worker_id:
                return f"the job now belongs to {owner}"
            # ``completed`` is still wanted: the hub closes the fetch first
            # and takes the video afterwards (see _hub_job_is_finished).
            if status in ("downloading", "running", "completed"):
                return ""
            return f"the job is {status or 'in an unknown state'}"
        return last

    async def _resume_cut_downloads(self) -> int:
        """Pick up the downloads the previous process handed over.

        Runs once, after the first hub connection of this process. Each
        manifest is checked against the hub before a byte is fetched, and the
        hub is told (``resumed=True``) so it stops waiting to fail the job.
        Returns the number of downloads started."""
        from server.worker import resume as _resume
        from server.worker import scratch_pool as _pool

        started = 0
        try:
            manifests = _resume.resumable(self.worker_id)
        except Exception:
            _logger.warning(
                f"[worker {self.worker_id}] resume: could not read the "
                f"manifests", exc_info=True,
            )
            return 0
        for m in manifests:
            job_id = str(m.get("job_id") or "")
            if not job_id:
                continue
            why = ""
            assign = base_result = None
            try:
                assign = HubAssignJob.model_validate(m["assign"])
                base_result = JobResult.model_validate(m["base_result"])
            except Exception as e:
                why = f"unreadable manifest ({type(e).__name__})"
            if not why:
                try:
                    why = await self._hub_job_wants_download(job_id)
                except Exception as e:
                    why = f"could not ask the hub ({type(e).__name__})"
            if why:
                _logger.info(
                    f"[worker {self.worker_id}] resume: dropping {job_id}: {why}",
                )
                for _d in m.get("dl_dirs") or []:
                    try:
                        _pool.release(Path(_d))
                    except Exception:
                        pass
                _resume.discard(m, rmdirs=False)
                continue
            try:
                await self._send(WorkerJobProgress(
                    job_id=job_id, phase="downloading", resumed=True,
                ))
            except Exception:
                pass
            _logger.info(
                f"[worker {self.worker_id}] resume: picking {job_id} up again "
                f"({len(m.get('targets') or [])} target(s), "
                f"time {int(m.get('resumes') or 0) + 1})",
            )
            self._spawn_deferred_video_download(
                assign, list(m.get("targets") or []), base_result,
                m.get("page_url"), resume=m,
            )
            started += 1
        return started

    async def _abandon_video_job(self, job_id: str, reason: str) -> int:
        """Stop an in-flight deferred download whose job the hub has ENDED.

        The mirror image of ``_force_complete_video_job``: that one wraps a
        download up because the hub still wants a result, this one drops it
        because the hub has moved on. Nothing we upload afterwards can be
        accepted -- the row is ``failed`` / ``cancelled``, or gone entirely --
        so every further second costs a slice of the node ramdisk (0.1-2 GB,
        charged to THIS CT's memory cgroup) and the bandwidth to fill it, for
        bytes that are already garbage.

        Left to itself the download runs to ``PAPRIKA_VIDEO_DOWNLOAD_TIMEOUT_S``
        (2h by default). Measured on 2026-08-16: 345 of the 398 scratch
        directories fleet-wide belonged to jobs the hub had already finished.

        Same mechanics as force-complete: flag the id, SIGTERM only the
        yt-dlp / ffmpeg descendants whose argv carries ``paprika-vid-{job_id}``,
        and let the deferred task's ``finally`` release the directory.
        Idempotent. Returns the number of processes signalled.
        """
        from server.worker.agent.video import _terminate_ytdlp_descendants_for_job

        try:
            self._abandoned_job_ids.add(job_id)
        except Exception:
            pass
        killed = 0
        try:
            killed = await asyncio.to_thread(
                _terminate_ytdlp_descendants_for_job, job_id,
            )
        except Exception:
            _logger.warning(
                "[%s] abandon: descendant SIGTERM failed", job_id, exc_info=True,
            )
        _logger.info(
            "[%s] abandoning deferred download (%s): SIGTERM'd %d "
            "yt-dlp/ffmpeg descendant(s)", job_id, reason, killed,
        )
        return killed

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

