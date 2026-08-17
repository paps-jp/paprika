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
from server.worker import scratch_pool
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

    async def _abandoned_download_loop(self) -> None:
        """Drop deferred downloads whose job the hub has already finished.

        A deferred yt-dlp download outlives the job it belongs to. The lane is
        released before it starts, so the hub is free to fail the job (reaper
        timeout, worker disconnect, redrive elsewhere) or the pipeline is free
        to delete it -- while yt-dlp keeps writing up to
        ``PAPRIKA_VIDEO_DOWNLOAD_TIMEOUT_S`` (2h) into a directory on the node
        ramdisk that is charged to THIS CT's memory cgroup.

        Nothing tells the worker. Measured across the fleet on 2026-08-16: 398
        scratch directories, only 53 of which belonged to a job that was still
        running -- 220 ``failed``, 40 ``completed`` elsewhere, 80 deleted
        outright. That is the leak this loop closes at the source, ahead of the
        30-minute sweeper that can only collect a directory once the download
        has finally given up on it.

        Polling rather than a hub push on purpose: the job can be ended by ANY
        hub (or by ``DELETE /jobs/{id}`` from the pipeline), not just the one
        holding our WebSocket, and a push would have to cross that boundary.
        One GET per in-flight download per interval, and only for downloads
        past ``grace`` -- a worker with nothing deferred makes no requests.
        """
        interval = float(
            os.environ.get("PAPRIKA_ABANDON_POLL_INTERVAL_S") or 120.0,
        )
        # Don't judge a download that has only just started: the hub row can
        # still be catching up to the phase change (and a job legitimately
        # sits in ``running`` for a moment before the deferred DL is stamped
        # ``downloading``).
        grace = float(os.environ.get("PAPRIKA_ABANDON_GRACE_S") or 180.0)
        if interval <= 0:
            return
        started: dict[str, float] = {}
        # ``failed`` seen once is not acted on. A row can read failed while we
        # are perfectly alive -- the downloading reaper marks a job failed when
        # it believes the worker is gone fleet-wide, which a WS flap is enough
        # to produce -- so a second identical reading a poll later is required
        # before killing work that may still be wanted. A deleted or cancelled
        # job needs no such patience: nobody is waiting for it either way.
        pending: dict[str, str] = {}
        try:
            while True:
                await asyncio.sleep(interval)
                bg = getattr(self, "_bg_video_tasks", None) or {}
                live = {j for j in bg.values() if j} if isinstance(bg, dict) else set()
                now = time.monotonic()
                for job_id in live:
                    started.setdefault(job_id, now)
                for job_id in [j for j in started if j not in live]:
                    started.pop(job_id, None)
                    pending.pop(job_id, None)
                for job_id in sorted(live):
                    if job_id in self._abandoned_job_ids:
                        continue
                    if now - started.get(job_id, now) < grace:
                        continue
                    verdict = await self._hub_job_is_finished(job_id)
                    if verdict is None:
                        # Unreachable hub, or the job is still wanted. Either
                        # way this is not evidence to kill a download on.
                        pending.pop(job_id, None)
                        continue
                    if verdict == "failed" and pending.pop(job_id, None) != "failed":
                        pending[job_id] = "failed"
                        continue
                    pending.pop(job_id, None)
                    await self._abandon_video_job(job_id, verdict)
        except asyncio.CancelledError:
            return

    async def _hub_job_is_finished(self, job_id: str) -> str | None:
        """Reason string when the hub is done with ``job_id``, else None.

        None means "no answer" -- an unreachable hub, a timeout, an
        unparseable body. That has to read as "keep downloading": a network
        blip must never be the thing that kills a 90%-complete video.
        """
        try:
            base = (self.hub_http_url or "").rstrip("/")
            if not base or self._http is None:
                return None
            r = await self._http.get(f"{base}/jobs/{job_id}", timeout=10.0)
        except Exception:
            return None
        if r.status_code == 404:
            return "deleted"
        if r.status_code != 200:
            return None
        try:
            status = str((r.json() or {}).get("status") or "").split(".")[-1].lower()
        except Exception:
            return None
        # ``completed`` is deliberately NOT terminal here. It reads like the
        # safest possible signal and is the opposite: sampled across the fleet
        # on 2026-08-16, 26 scratch directories belonged to jobs the hub had
        # marked completed and 20 of them had a live yt-dlp still writing --
        # the fetch phase completes first and the deferred download uploads
        # its video afterwards. Abandoning on ``completed`` would throw away
        # the video on every ordinary job.
        if status in ("failed", "cancelled", "canceled"):
            return status
        return None

    async def _disk_cleanup_loop(self) -> None:
        """Periodically prune stale /tmp/paprika-* dirs left behind by
        crashes / ungraceful teardown, plus Chrome's own abandoned scratch
        (see _TMP_SWEEP_CHROME_PREFIXES). Idempotent; safe to run while
        new sessions are starting (age threshold + active-session check)."""
        interval = float(
            os.environ.get("PAPRIKA_TMP_SWEEP_INTERVAL_S") or 1800.0,  # 30 min
        )
        min_age = float(
            os.environ.get("PAPRIKA_TMP_SWEEP_MIN_AGE_S") or 1800.0,  # 30 min
        )
        # Chrome's leftovers get a longer window than ours: we can prove a
        # paprika-* dir is dead from the live session/job sets, but Chrome's
        # entries carry no id to match on, so age is the ONLY evidence. 60
        # min is what the CT-side timer (scripts/worker-housekeep.sh) has
        # been using in production, so this inherits a proven threshold
        # rather than inventing one. 0 disables the Chrome half entirely.
        chrome_min_age = float(
            os.environ.get("PAPRIKA_TMP_SWEEP_CHROME_MIN_AGE_S") or 3600.0,
        )
        # Core dumps (see _sweep_core_dumps). Short window on purpose: these
        # are 0.2-8 GB each on the CT rootfs = the node's thin pool, so the
        # cost of holding one an extra half hour is far higher than for a
        # /tmp entry. 0 disables the core half.
        core_min_age = float(
            os.environ.get("PAPRIKA_CORE_SWEEP_MIN_AGE_S") or 120.0,
        )
        # Chrome's download dir (see _sweep_chrome_downloads). Same thin-pool
        # cost as a core dump, but unlike a dump -- which the kernel writes in
        # seconds -- a legitimate transfer can stall for minutes and must not
        # be unlinked mid-flight. So this inherits the Chrome-scratch window
        # rather than the 2-minute core one. 0 disables it.
        downloads_min_age = float(
            os.environ.get("PAPRIKA_DOWNLOADS_SWEEP_MIN_AGE_S") or 1800.0,
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
                    n, bytes_freed, n_chrome = await asyncio.to_thread(
                        self._sweep_tmp_orphans, min_age, chrome_min_age,
                    )
                    if n:
                        _logger.info(
                            f"[worker {self.worker_id}] tmp sweep: "
                            f"removed {n} orphan entr(ies) "
                            f"({n_chrome} chrome), "
                            f"~{bytes_freed // (1024*1024)} MiB freed",
                        )
                except Exception as e:
                    _logger.info(
                        f"[worker {self.worker_id}] tmp sweep failed: "
                        f"{type(e).__name__}: {e}",
                    )
                # Separate try: a failure sweeping /tmp must not skip the
                # half that keeps the thin pool alive.
                if core_min_age > 0:
                    try:
                        n_core, core_freed = await asyncio.to_thread(
                            self._sweep_core_dumps, core_min_age,
                        )
                        if n_core:
                            _logger.info(
                                f"[worker {self.worker_id}] core sweep: "
                                f"removed {n_core} core dump(s), "
                                f"~{core_freed // (1024*1024)} MiB freed",
                            )
                    except Exception as e:
                        _logger.info(
                            f"[worker {self.worker_id}] core sweep failed: "
                            f"{type(e).__name__}: {e}",
                        )
                # Third try, same reasoning: Chrome's download dir sits on the
                # same thin pool as the core dumps and is the bigger leak.
                if downloads_min_age > 0:
                    try:
                        n_dl, dl_freed = await asyncio.to_thread(
                            self._sweep_chrome_downloads, downloads_min_age,
                        )
                        if n_dl:
                            _logger.info(
                                f"[worker {self.worker_id}] downloads sweep: "
                                f"removed {n_dl} file(s), "
                                f"~{dl_freed // (1024*1024)} MiB freed",
                            )
                    except Exception as e:
                        _logger.info(
                            f"[worker {self.worker_id}] downloads sweep failed: "
                            f"{type(e).__name__}: {e}",
                        )
        except asyncio.CancelledError:
            return

    def _tmp_sweep_roots(self) -> list[Path]:
        """Directories the sweep walks.

        The system temp dir always: it holds our own mkdtemp() scratch, and
        Chrome's leftovers too whenever its TMPDIR is NOT redirected (no
        ramdisk, or a lane that failed to prepare one). Plus every lane
        TMPDIR on the ramdisk, because that is where Chrome's scratch goes
        once it IS redirected -- and leaving those unswept would trade a
        bounded disk leak for an unbounded RAM one on a mount shared by the
        whole node. ``PAPRIKA_TMP_SWEEP_ROOT`` overrides the system dir so a
        test can sweep a sandbox.
        """
        roots = [Path(os.environ.get("PAPRIKA_TMP_SWEEP_ROOT") or "/tmp")]
        try:
            from server.worker.lanes import chrome_lane_tmp_roots

            roots += chrome_lane_tmp_roots()
        except Exception:
            # Older worker source / import trouble: sweeping the system tmp
            # alone is the pre-existing behaviour, never a reason to abort.
            pass
        out: list[Path] = []
        for r in roots:
            try:
                if r.exists() and r not in out:
                    out.append(r)
            except OSError:
                continue
        return out

    def _sweep_tmp_orphans(
        self, min_age_s: float, chrome_min_age_s: float = 3600.0,
    ) -> tuple[int, int, int]:
        """One sweep pass. Synchronous (run in a thread because rmtree of
        multi-GB dirs is blocking). Returns
        ``(removed_count, bytes_freed, chrome_removed_count)``."""
        import time as _t

        roots = self._tmp_sweep_roots()
        if not roots:
            return (0, 0, 0)
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
        # Chrome's entries carry no id to match on, so the keep-set above
        # cannot speak for them -- but one shape MUST be spared: the
        # singleton socket dir of a running browser. Its mtime is its
        # creation time (seen a day stale on a live Chrome), so the age
        # guard would not save it. Resolve the live ones exactly, via the
        # symlink each lane profile keeps pointing at its own.
        chrome_keep: set[str] = set()
        try:
            from server.worker.lanes import chrome_live_socket_dirs

            chrome_keep = chrome_live_socket_dirs()
        except Exception:
            # Without that evidence we cannot tell a live socket dir from a
            # leaked one, and guessing wrong costs a lane. Sit the Chrome
            # half out this pass; the next one will have it.
            chrome_min_age_s = 0.0
        now = _t.time()
        removed = 0
        freed = 0
        chrome_removed = 0
        entries: list[Path] = []
        for root in roots:
            try:
                entries += list(root.iterdir())
            except Exception:
                continue
        for entry in entries:
            name = entry.name
            if name in self._TMP_SWEEP_PROTECTED:
                continue
            is_chrome = chrome_min_age_s > 0 and any(
                name.startswith(p) for p in self._TMP_SWEEP_CHROME_PREFIXES
            )
            if not is_chrome and not any(
                name.startswith(p) for p in self._TMP_SWEEP_PREFIXES
            ):
                continue
            try:
                # Ours are always directories. Chrome's are a mix:
                # scoped_dir*/.org.chromium.* are dirs, the shm segments
                # (.com.google.Chrome.*) are plain files -- which is why
                # they survived every previous version of this sweep.
                is_dir = entry.is_dir()
                if not is_dir and not is_chrome:
                    continue
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age < (chrome_min_age_s if is_chrome else min_age_s):
                continue
            if is_chrome and chrome_keep:
                try:
                    if os.path.realpath(str(entry)) in chrome_keep:
                        continue
                except OSError:
                    continue
            # If the dir name embeds a live session id OR an in-flight
            # background-download job id, keep it. Names follow
            # paprika-ses-<sid>-<rand> / paprika-profile-<key>-<rand> /
            # paprika-vid-<jobid>-<rand> / paprika-<jobid>-<rand>. The
            # substring match is loose but safe-direction: false
            # positives only KEEP a stale dir an extra cycle; the
            # protection NEVER deletes a live dir.
            #
            # Chrome's entries are exempt because they carry no id to match
            # -- age is their only evidence, hence the longer window above.
            if not is_chrome and any(tok in name for tok in keep_tokens):
                continue
            try:
                if is_dir:
                    # Best-effort directory size for the log line, capped so
                    # we don't os.walk a huge tree just for telemetry.
                    size = 0
                    for root, _, files in os.walk(entry):
                        for f in files:
                            try:
                                size += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
                            if size > 100 * 1024 * 1024 * 1024:  # cap 100 GiB
                                break
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    size = entry.stat().st_size
                    entry.unlink()
                removed += 1
                freed += size
                if is_chrome:
                    chrome_removed += 1
            except Exception:
                # Best-effort: skip and try again next sweep.
                continue
        # Shared ramdisk pool (server/worker/scratch_pool.py): same contract,
        # but STRICTLY owner-scoped -- the other CTs on this node write into
        # the same tmpfs and are invisible from in here, so a name-only sweep
        # would delete a neighbour's live download. Also refresh our own
        # claims, otherwise a 2h download's reservation ages out and the node
        # over-admits while we are still writing.
        try:
            scratch_pool.touch_own(self.worker_id)
            _pr, _pf = scratch_pool.sweep_own(
                self.worker_id, keep_tokens, min_age_s,
            )
            removed += _pr
            freed += _pf
        except Exception:
            pass
        # The one class of garbage owner-scoping cannot collect: directories
        # whose owner no longer exists at all (CT rebuilt, worker id changed).
        # Nobody is left to run the sweep above for them, so the surviving
        # workers on the node do it -- under much stricter evidence. See
        # scratch_pool.sweep_orphans / lanes.sweep_chrome_orphans.
        try:
            _or, _of = scratch_pool.sweep_orphans(self.worker_id)
            removed += _or
            freed += _of
            if _or:
                # Its own line, at the agent's logger: the summary below folds
                # these into the /tmp count, and "we deleted another CT's data"
                # is exactly the event an operator must be able to find.
                _logger.info(
                    f"[worker {self.worker_id}] scratch-pool orphan reclaim: "
                    f"{_or} dir(s) of vanished workers, "
                    f"~{_of // (1024*1024)} MiB back to the node ramdisk",
                )
        except Exception:
            pass
        try:
            from server.worker.lanes import (
                sweep_chrome_orphans,
                touch_chrome_owner,
            )

            touch_chrome_owner()
            _cr, _cf = sweep_chrome_orphans(
                self.worker_id,
                float(os.environ.get("PAPRIKA_CHROME_ORPHAN_MIN_AGE_S") or 43200.0),
            )
            removed += _cr
            freed += _cf
            chrome_removed += _cr
            if _cr:
                _logger.info(
                    f"[worker {self.worker_id}] chrome orphan reclaim: "
                    f"{_cr} lane root(s) of vanished workers, "
                    f"~{_cf // (1024*1024)} MiB back to the node ramdisk",
                )
        except Exception:
            pass
        return (removed, freed, chrome_removed)

    # Core dumps do NOT land in any tmp root, so the sweep above cannot see
    # them: the node's kernel.core_pattern is the default "core", which means
    # the CRASHING PROCESS'S CWD -- /app for everything the worker spawns --
    # and that is the CT's rootfs, i.e. the node's LVM thin pool.
    #
    # Measured on foyer 2026-08-13: 51-101 files / 3.8-8.0 GB per CT, ~105 GB
    # across 22 CTs in five days (~1 GB/CT/day, Chrome crashing 10-20x/day per
    # worker). pve/data went 41% -> 100% and seven CTs dropped into ext4
    # emergency read-only, which is NOT recoverable from inside the CT.
    #
    # server/__main__.py drops RLIMIT_CORE to 0 at worker startup, which stops
    # new dumps at the source. This sweep is the second layer: it clears what
    # earlier versions already wrote, and it still bounds the footprint if the
    # rlimit is opted out of (PAPRIKA_ALLOW_CORE_DUMPS=1) or a child resets it.
    #
    # ``core`` with no suffix is a legitimate name for the dump too (the
    # pattern has no %p unless kernel.core_uses_pid is set), and /app/core is
    # ALSO the paprika ``core/`` package directory -- so this must never delete
    # a directory. is_file() below is load-bearing, not defensive noise.
    _CORE_DUMP_RE = _re.compile(r"^core(\.\d+)?$")

    def _core_dump_roots(self) -> list[Path]:
        """Where dumps land: the worker's CWD (/app in the container).

        ``PAPRIKA_CORE_SWEEP_ROOT`` overrides it (tests, or a node whose
        core_pattern points somewhere else).
        """
        raw = os.environ.get("PAPRIKA_CORE_SWEEP_ROOT")
        try:
            root = Path(raw) if raw else Path.cwd()
            return [root] if root.is_dir() else []
        except OSError:
            return []

    def _sweep_core_dumps(self, min_age_s: float) -> tuple[int, int]:
        """Delete stale core dumps. Returns ``(removed_count, bytes_freed)``.

        The age guard keeps us off a dump the kernel is still writing: a
        multi-GB core takes seconds to land, and unlinking it mid-write only
        wastes the crash evidence (the kernel holds the fd, so no space is
        reclaimed until it finishes anyway).
        """
        import time as _t

        now = _t.time()
        removed = 0
        freed = 0
        for root in self._core_dump_roots():
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not self._CORE_DUMP_RE.match(entry.name):
                    continue
                try:
                    if not entry.is_file():   # /app/core is the source package
                        continue
                    st = entry.stat()
                    if (now - st.st_mtime) < min_age_s:
                        continue
                    size = st.st_size
                    entry.unlink()
                except OSError:
                    continue
                removed += 1
                freed += size
        return (removed, freed)

    # Chrome's own download directory.
    #
    # yt-dlp writes into the ramdisk scratch pool, but a *browser-native*
    # download -- a click, an auto-download link, a Content-Disposition
    # response -- is saved by Chrome to its configured download dir. Nothing
    # in paprika configures that, so it stays the default $HOME/Downloads,
    # which in the worker container is /root/Downloads on the container's
    # writable layer = the node's LVM thin pool. The ramdisk never sees these
    # bytes, so none of the scratch-pool machinery bounds them.
    #
    # Measured 2026-08-17 on depot: 21.1 GB across 34 CTs = 9% of a 234 GB
    # pool, growing ~350 MB/day/CT with no ceiling. Roughly half of it was
    # abandoned ``Unconfirmed NNNNNN.crdownload`` partials, which Chrome never
    # cleans up because the session died mid-transfer. It also propagates: a
    # vzdump seed taken from a running CT bakes its Downloads into every CT
    # restored from it (all 16 restored that day started at an identical
    # 442 MB).
    #
    # Nothing reads this directory back -- no code path in the repo references
    # it -- so its entire contents are garbage. The only risk is unlinking a
    # transfer that is still running, which the mtime guard covers: an active
    # download keeps its mtime fresh.
    def _download_sweep_roots(self) -> list[Path]:
        """Chrome's download dir: ``$HOME/Downloads`` in the container.

        ``PAPRIKA_DOWNLOADS_SWEEP_ROOT`` overrides it (tests, or a lane
        pointed somewhere else).
        """
        raw = os.environ.get("PAPRIKA_DOWNLOADS_SWEEP_ROOT")
        try:
            root = Path(raw) if raw else (Path.home() / "Downloads")
            return [root] if root.is_dir() else []
        except (OSError, RuntimeError):
            # RuntimeError: Path.home() with no resolvable HOME.
            return []

    def _sweep_chrome_downloads(self, min_age_s: float) -> tuple[int, int]:
        """Delete stale browser downloads. Returns ``(removed, bytes_freed)``.

        Files at any depth: Chrome nests a download in a subdirectory when the
        site sends one, and leaving those unreachable would defeat the sweep.
        Directories are never removed -- an empty one costs nothing, and
        rmdir'ing the root out from under a live Chrome would make it fall
        back to a location we do not sweep at all.
        """
        import time as _t

        now = _t.time()
        removed = 0
        freed = 0
        for root in self._download_sweep_roots():
            try:
                entries = list(root.rglob("*"))
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                    if (now - st.st_mtime) < min_age_s:
                        continue
                    size = st.st_size
                    entry.unlink()
                except OSError:
                    continue
                removed += 1
                freed += size
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

