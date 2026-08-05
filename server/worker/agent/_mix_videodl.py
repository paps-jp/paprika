"""WorkerAgent mixin: downloader-role video download (ramdisk tier).

Part of the agent/ package; methods reach siblings via self (MRO).

This is the worker half of the video-download offload described in
``docs/ramdisk-video-tier.md``. A worker started with
``PAPRIKA_WORKER_ROLE=downloader`` runs NO Chrome/lane pool -- it only serves
``HubAssignVideoDownload`` handoffs:

    hub -> HubAssignVideoDownload -> [this mixin]
        assets_dir = shared host-tmpfs ramdisk (bind-mounted at
                     PAPRIKA_DOWNLOAD_DIR, default /var/paprika/vid)
        cookies    = rebuilt from {hub}/hosts/{host}/cookies.txt, or the live
                     snapshot forwarded for ``gated`` streams
        download   = the EXISTING _make_video_downloader closure (all the
                     referer-chain / stall / ETA / frag-fail gates come free)
        upload     = the EXISTING _upload_asset helpers (HubAssignVideoDownload
                     exposes a job_id alias so they work unchanged)
    -> WorkerVideoDownloadComplete

Deliberately additive: nothing here runs on a browser-role worker, and a
failure reports ok=False so the hub can re-drive the video through the legacy
in-browser path.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

from server.protocol import (
    HubAssignVideoDownload,
    WorkerJobLog,
    WorkerVideoDownloadComplete,
)

from ._base import _logger


#: Where the shared host-tmpfs ramdisk is bind-mounted inside the CT.
#: Matches ``mp0: /ram/pvid,mp=/var/paprika/vid`` in the uniform worker CT
#: config (docs/ramdisk-video-tier.md §5).
_DEFAULT_DOWNLOAD_DIR = "/var/paprika/vid"


def download_dir_from_env() -> Path:
    return Path(os.environ.get("PAPRIKA_DOWNLOAD_DIR") or _DEFAULT_DOWNLOAD_DIR)


def role_from_env() -> str:
    """'downloader' | 'browser'. Unknown values fall back to 'browser' so a
    typo can never silently take a worker out of the browse fleet."""
    r = (os.environ.get("PAPRIKA_WORKER_ROLE") or "browser").strip().lower()
    return "downloader" if r == "downloader" else "browser"


def download_pool_key_from_env() -> str | None:
    """Identity of the shared tmpfs pool (typically the Proxmox node name).
    All downloader workers bind-mounting the same host tmpfs must report the
    same key -- the hub caps concurrency PER pool, not per worker."""
    v = (os.environ.get("PAPRIKA_DOWNLOAD_POOL_KEY") or "").strip()
    return v or None


def download_slots_from_env() -> int:
    """Concurrent downloads the shared pool permits
    = floor(tmpfs_size / (max_video * merge_factor)). 0 = unset."""
    try:
        return max(0, int(os.environ.get("PAPRIKA_DOWNLOAD_SLOTS") or 0))
    except (TypeError, ValueError):
        return 0


def ramdisk_free_bytes(path: Path) -> int:
    """Free bytes on the ramdisk mount. Used for the local admission check so
    a worker never starts a download the shared tmpfs can't hold."""
    try:
        st = os.statvfs(str(path))
        return int(st.f_bavail) * int(st.f_frsize)
    except Exception:
        return 0


class _VideoDownloadMixin:
    """Serves HubAssignVideoDownload on downloader-role workers."""

    async def _handle_assign_video_download(
        self, assign: HubAssignVideoDownload
    ) -> None:
        """Download one video to the ramdisk, upload it to the parent job's
        assets, then report terminal status. Never raises into the WS loop."""
        started = time.time()
        ok = False
        n_files = 0
        bytes_total = 0
        message = ""
        work: Path | None = None
        cookies_path: Path | None = None

        def _log(line: str) -> None:
            _logger.info("[dl %s] %s", assign.download_id, line)

        async def _job_log_async(line: str) -> None:
            # Mirror operator-meaningful lines into the PARENT job's Live panel
            # so the handoff is visible where the operator is already looking.
            try:
                await self._send(
                    WorkerJobLog(job_id=assign.parent_job_id, line=line)
                )
            except Exception:
                pass

        def _job_log(line: str) -> None:
            try:
                asyncio.ensure_future(_job_log_async(line))
            except Exception:
                pass

        try:
            root = download_dir_from_env()
            root.mkdir(parents=True, exist_ok=True)

            # Admission check: refuse rather than fill the shared ramdisk.
            # Reporting ok=False lets the hub fall back to the legacy path.
            min_free = int(
                os.environ.get("PAPRIKA_DOWNLOAD_MIN_FREE_MB") or 2048
            ) * 1024 * 1024
            free = ramdisk_free_bytes(root)
            if free and free < min_free:
                message = (
                    f"ramdisk low on space ({free / 1048576:.0f} MB free "
                    f"< {min_free / 1048576:.0f} MB); declining handoff"
                )
                _log(message)
                return

            work = Path(tempfile.mkdtemp(prefix=f"paprika-vid-{assign.parent_job_id}-", dir=str(root)))

            # Cookies: normal path rebuilds them from the hub's host registry
            # (browser-independent); a gated handoff carries a live snapshot.
            cookies_path = await self._video_dl_cookies(assign, _log)

            saved: list[Path] = []

            async def _on_saved(path: Path, info: dict) -> None:
                nonlocal n_files, bytes_total
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                # HubAssignVideoDownload exposes .job_id + .asset_upload_base,
                # so the existing uploader works unmodified.
                uploaded = await self._upload_asset(
                    assign,
                    path,
                    path.name,
                    source_url=info.get("url") or assign.url,
                    mime=info.get("mime"),
                    page_url=info.get("document_url") or None,
                )
                if uploaded:
                    n_files += 1
                    bytes_total += size
                    saved.append(path)
                # Free the ramdisk immediately -- the tier must never
                # accumulate (docs/ramdisk-video-tier.md §6).
                try:
                    path.unlink()
                except OSError:
                    pass

            from server.worker.agent.video import _make_video_downloader

            referer = (assign.referer_chain or [""])[0] or ""
            page_url = None
            for cand in assign.referer_chain or []:
                if cand and cand != referer:
                    page_url = cand
                    break

            maybe_download, drain = _make_video_downloader(
                assets_dir=work,
                min_asset_size=assign.min_asset_size,
                on_saved=_on_saved,
                log=_log,
                job_id_for_logs=assign.parent_job_id,
                job_log=_job_log,
                page_url_provider=(lambda: page_url),
                user_agent=assign.user_agent,
                session_id=None,  # no live session on the download tier
            )
            if cookies_path is not None:
                os.environ.setdefault(
                    "PAPRIKA_YTDLP_COOKIES_FILE", str(cookies_path)
                )

            _job_log(f"  ⇢ ramdisk tier picked up video: {assign.url[:100]}")
            maybe_download(assign.url, referer)
            await drain()

            ok = n_files > 0
            message = (
                f"{n_files} file(s), {bytes_total / 1048576:.1f} MB in "
                f"{time.time() - started:.0f}s"
                if ok
                else "no file produced"
            )
            _job_log(
                f"  {'✅' if ok else '!!'} ramdisk tier {'saved' if ok else 'failed'}: {message}"
            )
        except Exception as e:  # never kill the WS loop
            message = f"{type(e).__name__}: {e}"
            _log(f"video download failed: {message}")
        finally:
            # Always reclaim the ramdisk workdir, success or not.
            if work is not None:
                try:
                    shutil.rmtree(work, ignore_errors=True)
                except Exception:
                    pass
            if cookies_path is not None:
                try:
                    cookies_path.unlink()
                except OSError:
                    pass
            try:
                await self._send(
                    WorkerVideoDownloadComplete(
                        download_id=assign.download_id,
                        parent_job_id=assign.parent_job_id,
                        ok=ok,
                        n_files=n_files,
                        bytes_total=bytes_total,
                        message=message,
                    )
                )
            except Exception:
                pass

    async def _video_dl_cookies(
        self, assign: HubAssignVideoDownload, log
    ) -> Path | None:
        """Build a Netscape cookies.txt for this download, or None.

        Preference order mirrors the design: a live snapshot (gated handoff,
        must be spent before its token expires) wins, else re-fetch the
        per-host jar from the hub registry -- which is what makes the handoff
        browser-independent in the first place."""
        if assign.cookies:
            try:
                fd, path = tempfile.mkstemp(prefix="ytdlp_cookies_", suffix=".txt")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("# Netscape HTTP Cookie File\n")
                    for c in assign.cookies:
                        domain = str(c.get("domain") or "")
                        if not domain:
                            continue
                        flag = "TRUE" if domain.startswith(".") else "FALSE"
                        cpath = str(c.get("path") or "/")
                        secure = "TRUE" if c.get("secure") else "FALSE"
                        expires = int(c.get("expires") or 0)
                        name = str(c.get("name") or "")
                        value = str(c.get("value") or "")
                        f.write(
                            f"{domain}\t{flag}\t{cpath}\t{secure}\t{expires}\t{name}\t{value}\n"
                        )
                return Path(path)
            except Exception as e:
                log(f"cookie snapshot write failed: {e}")
                return None
        if not assign.cookies_url:
            return None
        try:
            r = await self._http.get(assign.cookies_url, timeout=10.0)
            r.raise_for_status()
            body = r.text or ""
        except Exception as e:
            log(f"cookies fetch failed: {e}")
            return None
        if not body.strip() or body.strip() == "# Netscape HTTP Cookie File":
            return None
        try:
            fd, path = tempfile.mkstemp(prefix="ytdlp_cookies_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            return Path(path)
        except Exception as e:
            log(f"cookies write failed: {e}")
            return None
