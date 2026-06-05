"""WorkerAgent mixin: chrome-profile + extension sync/reconcile.

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


class _ProfileExtMixin:
    async def _handle_profile_sync(self, msg: HubProfileSync) -> None:
        """Prefetch (or update) the cached extraction for one profile.

        On etag match the download is a no-op, BUT we still pass
        through to ``_reconcile_ambient_default`` because the same
        broadcast is used by the hub to flip is_default for an
        already-cached profile (e.g. operator hits "set as default"
        on a profile that's been on the worker for days). A fresh
        etag triggers a fresh download + atomic swap before the
        ambient reconcile.
        """
        async with self._profile_cache_lock:
            cur = self._profile_cache.get(msg.name)
            already_current = (
                cur and cur.get("etag") == msg.etag and Path(cur.get("dir", "")).exists()
            )
            if not already_current:
                _logger.info(
                    f"[worker {self.worker_id}] profile cache: "
                    f"prefetching {msg.name!r} ({msg.size_bytes} bytes)",
                )
        if already_current:
            # Skip the download, still reconcile the ambient state
            # so a "set as default" on an already-cached profile
            # actually installs into lanes.
            await self._reconcile_ambient_default(msg.name, msg.is_default)
            return
        # Download outside the cache lock so other syncs (different
        # names) can proceed in parallel. We re-acquire the lock to
        # swap the cache entry once the new extraction is ready.
        new_dir = await self._fetch_to_temp(msg.url, scratch_key=f"sync-{msg.name}")
        if new_dir is None:
            return  # _fetch_to_temp logged the failure
        # _fetch_to_temp returns ``<scratch>/"User Data"`` -- after we
        # move the "User Data" subdir into the canonical cache below,
        # the parent scratch dir is left orphaned in /tmp. Track it
        # here so the post-rename cleanup can drop it (otherwise every
        # sync leaks one empty paprika-profile-sync-<name>-<rand>/ in
        # /tmp; over days the dentry count fills small VM root FSes).
        scratch_parent = new_dir.parent
        # Move into the canonical cache location atomically (rename
        # is atomic on the same filesystem; both paths are in /tmp).
        self._profile_cache_root.mkdir(parents=True, exist_ok=True)
        canonical = self._profile_cache_root / msg.name
        async with self._profile_cache_lock:
            old_dir = canonical.with_suffix(".old")
            if canonical.exists():
                try:
                    if old_dir.exists():
                        shutil.rmtree(old_dir, ignore_errors=True)
                    canonical.rename(old_dir)
                except OSError:
                    shutil.rmtree(canonical, ignore_errors=True)
            try:
                new_dir.rename(canonical)
            except OSError:
                shutil.copytree(new_dir, canonical, dirs_exist_ok=True)
                shutil.rmtree(new_dir, ignore_errors=True)
            # Drop the previous version now that the new one is in
            # place. In-flight jobs that already snapshotted the old
            # dir are unaffected because Lane.use_profile copies out
            # before launching Chrome.
            shutil.rmtree(old_dir, ignore_errors=True)
            # Drop the now-empty scratch parent (see comment above).
            shutil.rmtree(scratch_parent, ignore_errors=True)
            self._profile_cache[msg.name] = {
                "etag": msg.etag,
                "dir": str(canonical),
                "size_bytes": msg.size_bytes,
            }
        _logger.info(
            f"[worker {self.worker_id}] profile cache: ready {msg.name!r} (etag={msg.etag})",
        )
        # Operator-set default handling. The hub tags the broadcast
        # with is_default=True for the current default profile and
        # is_default=False for everything else. Workers maintain a
        # tiny state machine so noVNC viewers see the operator's
        # logged-in Chrome on EVERY lane (not just lanes that
        # happened to run a job recently).
        await self._reconcile_ambient_default(msg.name, msg.is_default)

    async def _sync_extensions_from_hub(self) -> None:
        """Pull the hub's enabled-extension set into this worker's
        local cache. Called once on register so every lane started
        afterwards picks them up via --load-extension. Idempotent:
        skips downloads whose etag matches the cached one, and
        evicts cache entries that no longer exist on the hub (so a
        deleted extension stops loading on the next Chrome bounce).
        """
        import tarfile as _tarfile

        import httpx

        # Use the http(s) hub base derived from the WS URL at startup.
        base = (self.hub_http_url or "").rstrip("/")
        if not base:
            _logger.info(
                f"[worker {self.worker_id}] extension sync: no hub_http_url set; skipping",
            )
            return
        try:
            async with make_async_client(timeout=30.0) as client:
                r = await client.get(f"{base}/extensions")
                if r.status_code != 200:
                    _logger.info(
                        f"[worker {self.worker_id}] extension sync: "
                        f"hub returned HTTP {r.status_code}; "
                        f"skipping",
                    )
                    return
                payload = r.json()
        except Exception as e:
            _logger.info(
                f"[worker {self.worker_id}] extension sync: "
                f"list fetch failed ({type(e).__name__}: {e})",
            )
            return
        # Skip built-in extensions (e.g. paprika-agent): they ship in
        # the repo and are loaded straight from the code tree by the
        # lane -- there's no tarball to download (the /extensions list
        # only surfaces them for operator visibility), so trying to
        # fetch one just logs a spurious 404.
        items = [
            it for it in (payload.get("extensions") or [])
            if it.get("enabled", True) and not it.get("builtin")
        ]
        wanted: dict[str, dict] = {it["slug"]: it for it in items if it.get("slug")}
        self._extension_cache_root.mkdir(parents=True, exist_ok=True)
        # Evict cache entries for extensions removed on the hub.
        async with self._extension_cache_lock:
            stale = [s for s in self._extension_cache if s not in wanted]
        for slug in stale:
            target = self._extension_cache_root / slug
            try:
                shutil.rmtree(target, ignore_errors=True)
            except Exception:
                pass
            async with self._extension_cache_lock:
                self._extension_cache.pop(slug, None)
            _logger.info(
                f"[worker {self.worker_id}] extension cache: evicted {slug!r}",
            )
        # Sync each wanted extension.
        for slug, meta in wanted.items():
            etag = str(meta.get("etag") or "")
            async with self._extension_cache_lock:
                cur = self._extension_cache.get(slug)
            target = self._extension_cache_root / slug
            up_to_date = (
                cur is not None
                and cur.get("etag") == etag
                and target.exists()
                and (target / "manifest.json").exists()
            )
            if up_to_date:
                continue
            # Download the tarball + extract atomically (extract to
            # a sibling .new dir, then rename into place).
            try:
                async with make_async_client(timeout=120.0) as client:
                    async with client.stream(
                        "GET",
                        f"{base}/extensions/{slug}/download",
                    ) as resp:
                        if resp.status_code != 200:
                            _logger.info(
                                f"[worker {self.worker_id}] extension "
                                f"{slug!r}: download HTTP "
                                f"{resp.status_code}",
                            )
                            continue
                        tmp_tar = self._extension_cache_root / f".{slug}.tar.gz.dl"
                        with open(tmp_tar, "wb") as f:
                            async for chunk in resp.aiter_bytes(64 * 1024):
                                f.write(chunk)
            except Exception as e:
                _logger.info(
                    f"[worker {self.worker_id}] extension {slug!r}: "
                    f"download failed ({type(e).__name__}: {e})",
                )
                continue
            # Extract.
            new_target = self._extension_cache_root / f".{slug}.new"
            shutil.rmtree(new_target, ignore_errors=True)
            new_target.mkdir(parents=True, exist_ok=True)
            try:
                with _tarfile.open(tmp_tar, "r:gz") as tf:
                    # Hub already validated against path traversal,
                    # but defence in depth.
                    for m in tf.getmembers():
                        if m.issym() or m.islnk():
                            continue
                        full = (new_target / m.name).resolve()
                        try:
                            full.relative_to(new_target.resolve())
                        except ValueError:
                            _logger.info(
                                f"[worker {self.worker_id}] extension "
                                f"{slug!r}: skipped traversal entry "
                                f"{m.name!r}",
                            )
                            continue
                    tf.extractall(new_target)
            except Exception as e:
                _logger.info(
                    f"[worker {self.worker_id}] extension {slug!r}: "
                    f"extract failed ({type(e).__name__}: {e})",
                )
                shutil.rmtree(new_target, ignore_errors=True)
                try:
                    tmp_tar.unlink()
                except OSError:
                    pass
                continue
            try:
                tmp_tar.unlink()
            except OSError:
                pass
            # The tarball top-level entry is the slug-named dir, so
            # the extracted layout is new_target/<slug>/manifest.json.
            # Move that inner dir into the canonical target.
            inner = new_target / slug
            if not (inner / "manifest.json").exists():
                # Be tolerant if the hub-side packing changed shape.
                if (new_target / "manifest.json").exists():
                    inner = new_target
                else:
                    _logger.info(
                        f"[worker {self.worker_id}] extension {slug!r}: "
                        f"manifest.json not found post-extract",
                    )
                    shutil.rmtree(new_target, ignore_errors=True)
                    continue
            # Atomic swap into place.
            async with self._extension_cache_lock:
                if target.exists():
                    old = target.with_suffix(".old")
                    shutil.rmtree(old, ignore_errors=True)
                    try:
                        target.rename(old)
                    except OSError:
                        shutil.rmtree(target, ignore_errors=True)
                try:
                    inner.rename(target)
                except OSError:
                    shutil.copytree(inner, target, dirs_exist_ok=True)
                if new_target.exists():
                    shutil.rmtree(new_target, ignore_errors=True)
                self._extension_cache[slug] = {
                    "etag": etag,
                    "dir": str(target),
                    "size_bytes": int(meta.get("size_bytes") or 0),
                    "version": str(meta.get("version") or ""),
                    "name": str(meta.get("name") or slug),
                }
            _logger.info(
                f"[worker {self.worker_id}] extension cache: "
                f"ready {slug!r} (v{meta.get('version') or '?'}, "
                f"etag={etag})",
            )

    async def _reconcile_ambient_default(
        self,
        name: str,
        is_default: bool,
    ) -> None:
        """Install / clear the ambient default profile based on a
        HubProfileSync signal. Idempotent.

        * is_default=True, name == current ambient -> no-op
        * is_default=True, name != current ambient -> install ``name``
          on every idle lane (lanes with an in-flight per-job swap
          are skipped; they'll inherit the new ambient on next
          release via the existing .lane-default restore path)
        * is_default=False, name == current ambient -> clear ambient
          on every idle lane (lane reverts to stock)
        * is_default=False, name != current ambient -> no-op (the
          broadcast is just telling us this profile isn't default;
          nothing to do)
        """
        cur = self._ambient_default_name
        if is_default:
            if cur == name:
                return
            cached = self._profile_cache.get(name)
            if not cached or not Path(cached.get("dir", "")).exists():
                # cache didn't make it to disk somehow; bail out
                # so we don't try to install nothing.
                return
            cdir = Path(cached["dir"])
            if self.lane_pool is None:
                return
            installed = 0
            skipped = 0
            for lane in self.lane_pool.lanes:
                if lane.busy:
                    skipped += 1
                    continue
                ok = await lane.set_ambient_profile(cdir, name)
                if ok:
                    installed += 1
                else:
                    skipped += 1
            self._ambient_default_name = name
            _logger.info(
                f"[worker {self.worker_id}] ambient default -> "
                f"{name!r}: installed on {installed} lane(s), "
                f"{skipped} skipped (busy)",
            )
        else:
            if cur != name:
                return
            if self.lane_pool is None:
                return
            cleared = 0
            for lane in self.lane_pool.lanes:
                if lane.busy:
                    continue
                ok = await lane.clear_ambient_profile()
                if ok:
                    cleared += 1
            self._ambient_default_name = None
            _logger.info(
                f"[worker {self.worker_id}] ambient default "
                f"cleared (was {name!r}): {cleared} lane(s) "
                f"reverted to stock",
            )

    async def _handle_profile_delete(self, msg: HubProfileDelete) -> None:
        """Drop the cached copy of a profile. No-op if not cached.
        Also clears the ambient install if this was the default
        profile (lanes revert to stock).
        """
        # If the deleted profile was the ambient default, clear it
        # on lanes FIRST so we don't leave an orphaned reference to
        # a cache dir that's about to be rmtree'd.
        await self._reconcile_ambient_default(msg.name, is_default=False)
        async with self._profile_cache_lock:
            self._profile_cache.pop(msg.name, None)
        canonical = self._profile_cache_root / msg.name
        if canonical.exists():
            shutil.rmtree(canonical, ignore_errors=True)
            _logger.info(
                f"[worker {self.worker_id}] profile cache: evicted {msg.name!r}",
            )

    def _write_agent_extension_policy(self) -> None:
        """Write the Chrome managed policy that force-installs the
        built-in Paprika Agent extension from the hub-served CRX +
        update manifest. Best-effort; idempotent."""
        import json as _json
        from pathlib import Path as _Path

        hub = (self.hub_http_url or "").rstrip("/")
        if not hub:
            return
        update_url = f"{hub}/agent-ext/updates.xml"
        policy = {
            "ExtensionInstallForcelist": [
                f"{self.PAPRIKA_AGENT_ID};{update_url}"
            ],
        }
        pol_dir = _Path("/etc/opt/chrome/policies/managed")
        pol_dir.mkdir(parents=True, exist_ok=True)
        pol_file = pol_dir / "paprika-agent.json"
        pol_file.write_text(_json.dumps(policy, indent=2), encoding="utf-8")
        _logger.info(
            f"[worker {self.worker_id}] wrote agent force-install "
            f"policy ({self.PAPRIKA_AGENT_ID} <- {update_url})",
        )

    def loaded_extension_paths(self) -> list[str]:
        """Return the on-disk paths to every cached hub-managed
        extension. Called by the lane code on Chrome launch to build
        the ``--load-extension`` arg list. Order is stable (sorted by
        slug) so chrome's load order is deterministic for debugging.
        """
        out: list[str] = []
        # Read under the lock for a consistent snapshot, then release.
        snap: list[tuple[str, dict]] = []
        # The lock is asyncio.Lock so we can't use it from a sync method;
        # accessing the dict is safe enough -- writers swap whole keys
        # atomically and we only read.
        for slug in sorted(self._extension_cache.keys()):
            snap.append((slug, self._extension_cache[slug]))
        for slug, entry in snap:
            d = entry.get("dir")
            if not d:
                continue
            p = Path(d)
            if not (p / "manifest.json").exists():
                continue
            out.append(str(p))
        return out

    async def _fetch_to_temp(
        self,
        profile_url: str,
        *,
        scratch_key: str,
        log=None,
    ) -> Path | None:
        """Download + extract a profile tarball to a NEW temp dir.

        Returns the extracted dir on success, ``None`` on any
        network / archive failure. Caller decides whether to install
        it directly (job path: rename into the lane), promote it to
        the cache (sync path: rename into _profile_cache_root), or
        clean it up.
        """
        import tarfile

        import httpx

        scratch = Path(tempfile.mkdtemp(prefix=f"paprika-profile-{scratch_key}-"))
        tar_path = scratch / "profile.tar.gz"
        extract_dir = scratch / "User Data"
        try:
            async with make_async_client(timeout=60.0) as cli:
                async with cli.stream("GET", profile_url) as r:
                    r.raise_for_status()
                    with open(tar_path, "wb") as f:
                        async for chunk in r.aiter_bytes(64 * 1024):
                            f.write(chunk)
        except Exception as e:
            msg = f"  ... profile fetch failed: {type(e).__name__}: {e} (URL: {profile_url})"
            if log:
                log(msg)
            else:
                _logger.info(msg)
            shutil.rmtree(scratch, ignore_errors=True)
            return None
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                safe_root = extract_dir.resolve()
                for m in tar.getmembers():
                    dest = (extract_dir / m.name).resolve()
                    if not str(dest).startswith(str(safe_root)):
                        raise ValueError(f"tarball member escapes extract dir: {m.name}")
                tar.extractall(extract_dir)
        except Exception as e:
            msg = f"  ... profile extract failed: {type(e).__name__}: {e}"
            if log:
                log(msg)
            else:
                _logger.info(msg)
            shutil.rmtree(scratch, ignore_errors=True)
            return None
        finally:
            try:
                tar_path.unlink()
            except OSError:
                pass

        # Belt-and-suspenders normalisation: the hub now normalises
        # archives at upload time, but caches from before that
        # change (= tarballs accepted by an old hub build) keep
        # whatever top-level structure the operator originally
        # uploaded -- typically "Profile 10/" or similar named
        # profile. Chrome at startup reads <user-data-dir>/Default/,
        # so an un-remapped tarball means Chrome silently sees no
        # profile content. We detect + fix here too so existing
        # caches don't have to be re-uploaded.
        try:
            _normalise_extracted_profile(extract_dir, log=log)
        except Exception as e:
            msg = f"  ... profile extract: post-normalisation failed: {type(e).__name__}: {e}"
            if log:
                log(msg)
            else:
                _logger.info(msg)
            # Don't fail outright -- the unnormalised tree might
            # still be partially useful (Chrome generates a fresh
            # Default/ on first run and the operator can re-upload).
        return extract_dir

    async def _get_profile_for_job(
        self,
        *,
        profile_url: str,
        profile_name: str | None,
        profile_etag: str | None,
        scratch_key: str,
        log=None,
    ) -> Path | None:
        """Resolve a profile to an extracted directory ready for
        Lane.use_profile().

        Lookup order:
          1. Local cache (HubProfileSync prefetch): if name + etag
             match, copy the cached tree to a per-job scratch dir
             and return that. The cache stays untouched so other
             concurrent jobs can also use it.
          2. On-demand fetch from ``profile_url`` (existing path).

        ``None`` return means both failed -- the caller should log
        and continue with the lane's default profile rather than
        failing the job (operator can tell from missing cookies +
        worker stderr).
        """
        # 1) Cache hit?
        if profile_name and profile_etag:
            async with self._profile_cache_lock:
                cur = self._profile_cache.get(profile_name)
            if cur and cur.get("etag") == profile_etag:
                cached_dir = Path(cur["dir"])
                if cached_dir.exists():
                    out = (
                        Path(
                            tempfile.mkdtemp(
                                prefix=f"paprika-profile-{scratch_key}-",
                            )
                        )
                        / "User Data"
                    )
                    try:
                        shutil.copytree(cached_dir, out)
                        if log:
                            log(f"  ... profile {profile_name!r} from local cache")
                        else:
                            _logger.info(
                                f"[worker {self.worker_id}] profile cache HIT: {profile_name!r}",
                            )
                        return out
                    except Exception as e:
                        # Cache copy failed -- shouldn't normally
                        # happen on a same-fs copytree, but fall
                        # through to the network path rather than
                        # failing the job.
                        _logger.info(
                            f"[worker {self.worker_id}] profile cache "
                            f"copy failed: {type(e).__name__}: {e} -- "
                            f"falling back to fetch",
                        )
                        shutil.rmtree(out.parent, ignore_errors=True)
        # 2) On-demand fetch.
        return await self._fetch_to_temp(
            profile_url,
            scratch_key=scratch_key,
            log=log,
        )

