"""Single-lane LanePool stub for Windows portable build.

Fleet 版の ``server/worker/lanes.py`` は N 個の独立した Chrome instance
を Xvfb + x11vnc + websockify と束ねた "lane" として並列に管理する。
Windows 単機では:

  * 単機なので並列 lane の旨味が薄い (= 1 ユーザの作業を直列に回せば十分)
  * Xvfb は無い (OS の物理 display を使う)
  * TightVNC + websockify の同梱は v1.1 (Live タブの画面表示は v1.0 では諦め)

なので、本モジュールでは LanePool / Lane の interface だけを実装して、
内部は ``WindowsWorkerSupervisor`` が起動した「1 つの Chromium」を
pool として扱う。これで ``server/worker/agent.py`` の lane 経由パス
(session_start → lane.acquire() → CDP attach) が無改修で動く。

割り切り (v1.0):

  * ``use_profile()`` / ``restore_default_profile()``: ``log.warning``
    だけ吐いて no-op。動的 profile 切替は v1.1 (Chrome を再起動して
    user-data-dir を差し替える方式になる)
  * ``set_ambient_profile()`` / ``clear_ambient_profile()``: 同上
  * ``set_extra_extension_paths()``: パスを記録するだけ。次回
    Chrome 起動時にしか効かない
  * ``screenshot()``: 直接 CDP の ``Page.captureScreenshot`` を叩いて
    PNG を返す。Live タブ JPEG viewer は v1.1 だが、admin UI の
    "現在のページ" バッジは見える
  * ``start()`` / ``stop()``: 全て no-op。Chrome の lifecycle は
    ``WindowsWorkerSupervisor._ChromeProc`` が ownership
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class _WinLane:
    """Single-Chrome "lane" backed by the bundled Chromium that
    ``WindowsWorkerSupervisor`` already started. Implements the subset
    of ``server.worker.lanes.Lane`` that ``server.worker.agent``
    actually calls."""

    def __init__(
        self,
        *,
        chrome_port: int,
        novnc_url: str,
        user_data_dir: Path,
    ) -> None:
        # === Public attributes that agent.py reads as ``lane.X`` ===
        self.lane_idx: int = 0
        self.chrome_port: int = chrome_port
        self.novnc_url: str = novnc_url
        # Mirrors the Linux Lane.user_data_dir field; profile-aware
        # paths (extension cache install etc.) read this.
        self.user_data_dir: Path = user_data_dir
        # agent.py flips ``lane.busy`` during a job and clears it on
        # release. Used for "stuck" reconciliation after a hub
        # disconnect. We honour the same protocol.
        self.busy: bool = False
        # Set by agent.set_extra_extension_paths(); not consumed in
        # v1.0 (no Chrome restart on extension change) but recorded
        # so v1.1 has the right migration target.
        self._extra_extension_paths: list[str] = []

    # ---- Lifecycle (Chrome owned by WindowsWorkerSupervisor) ----

    async def start(self) -> None:
        """No-op: Chromium is started by WindowsWorkerSupervisor and
        outlives any single lane.acquire/release cycle."""
        return None

    def stop(self) -> None:
        """No-op: same reasoning as start()."""
        return None

    # ---- Profile management (deferred to v1.1) -------------------

    async def use_profile(self, profile_dir: Path) -> None:
        """v1.0 stub. Linux fleet swaps the chrome user-data-dir
        symlink + restarts chrome to install an operator-uploaded
        profile. Windows portable currently can't do that without
        tearing down + restarting the single bundled Chromium, which
        would invalidate every in-flight session and is a v1.1
        feature. We log a clear warning so the operator understands
        why their ``use_profile=`` option didn't take effect, but
        the job still runs (on the lane's default user-data-dir)."""
        log.warning(
            "[lane 0] use_profile(%s) skipped: Windows portable v1.0 "
            "doesn't support dynamic profile swap. The job will run "
            "in the default browser profile.",
            profile_dir,
        )

    async def restore_default_profile(self) -> None:
        """v1.0 stub. Mirror of use_profile()."""
        return None

    async def set_ambient_profile(
        self,
        cdir: Path,
        name: str,
    ) -> bool:
        """v1.0 stub. "Ambient default profile" is the fleet feature
        where every idle lane installs the operator-set default
        profile so noVNC viewers see the logged-in Chrome on lanes
        that haven't run a job yet. Without dynamic profile swap
        this can't work; return False so the worker reports "skipped
        (busy)" to the hub log."""
        log.warning(
            "[lane 0] set_ambient_profile(%s) skipped: Windows portable "
            "v1.0 has no dynamic profile swap.",
            name,
        )
        return False

    async def clear_ambient_profile(self) -> bool:
        log.warning(
            "[lane 0] clear_ambient_profile skipped: Windows portable v1.0"
        )
        return False

    def set_extra_extension_paths(self, paths: list[str]) -> None:
        """Record extension paths for the NEXT Chrome start. Current
        Chrome instance is unaffected. v1.1 will restart Chromium when
        the extension set changes."""
        self._extra_extension_paths = list(paths)
        if paths:
            log.info(
                "[lane 0] recorded %d extension path(s); active on next "
                "paprika.exe restart",
                len(paths),
            )

    # ---- Screenshot -- best-effort raw CDP -----------------------

    async def screenshot(
        self,
        *,
        format: str = "jpeg",  # noqa: A002
        quality: int = 60,
        max_width: int | None = None,
        timeout: float = 5.0,
    ) -> bytes:
        """Capture the current page via raw CDP.

        Used by:
          * admin UI Live tab lane-preview thumbnails (small JPEG)
          * codegen attempt screenshots (judge_llm.py)

        Linux fleet uses ``lane.screenshot`` which goes through the
        already-attached CDP session; here we open a fresh short-lived
        CDP WebSocket because the worker's nodriver session might be
        mid-action. Returns raw bytes (PNG/JPEG per ``format``).

        v1.0: falls back to a 1×1 placeholder PNG if CDP capture fails
        so callers that always-expect-bytes don't break."""
        try:
            from windows._cdp_screenshot import grab_screenshot
            return await asyncio.wait_for(
                grab_screenshot(
                    chrome_port=self.chrome_port,
                    format=format,
                    quality=quality,
                    max_width=max_width,
                ),
                timeout=timeout,
            )
        except Exception as e:
            log.debug("lane screenshot failed: %s", e)
            # Tiny placeholder PNG (1×1 transparent) so the caller's
            # ``len(bytes) > 0`` checks still pass.
            return (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
                b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                b"\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff?\x00\x05\xfe\x02"
                b"\xfeA\xb9\xd7\xfe\x00\x00\x00\x00IEND\xaeB`\x82"
            )


class _SingleLanePool:
    """A pool of exactly one ``_WinLane``. Implements the subset of
    ``server.worker.lanes.LanePool`` that ``server.worker.agent``
    actually calls.

    ``acquire()`` blocks (within the agent's session_start handler)
    until the single lane is free. Concurrency cap is therefore 1 ==
    matches the worker's ``max_concurrent`` for Windows single-user.
    """

    def __init__(
        self,
        *,
        chrome_port: int,
        novnc_url: str,
        user_data_dir: Path,
    ) -> None:
        self.lanes: list[_WinLane] = [_WinLane(
            chrome_port=chrome_port,
            novnc_url=novnc_url,
            user_data_dir=user_data_dir,
        )]
        # asyncio.Lock so an in-flight job blocks the next acquire()
        # cleanly. The hub's pick_worker already filters on
        # ``in_flight < capacity`` so this is belt-and-braces.
        self._lock = asyncio.Lock()

    async def start_all(self) -> None:
        """Chrome is already up (WindowsWorkerSupervisor started it).
        Just sanity-log."""
        log.info(
            "single-lane pool ready (chrome=:%d, profile=%s)",
            self.lanes[0].chrome_port,
            self.lanes[0].user_data_dir,
        )

    def stop_all(self) -> None:
        """No-op: Chrome owned by WindowsWorkerSupervisor."""
        return None

    async def acquire(self, lane_hint: int | None = None) -> _WinLane | None:
        """Return the single lane, waiting for in-flight job to release.

        ``lane_hint=0`` is honoured (always matches the only lane);
        any other non-None value returns None so the caller surfaces
        "lane_hint out of range" instead of silently colliding with
        the lone lane."""
        if lane_hint is not None and lane_hint != 0:
            return None
        # Cooperative wait: an in-flight job holds the lock until
        # release() is called.
        await self._lock.acquire()
        lane = self.lanes[0]
        lane.busy = True
        return lane

    def release(self, lane: _WinLane) -> None:
        """Mark the lane free + drop the acquire lock."""
        lane.busy = False
        try:
            self._lock.release()
        except RuntimeError:
            # Already released (e.g. double-release on error paths).
            # Idempotent so callers don't have to guard.
            pass

    def stats(self) -> dict:
        return {
            "n_lanes": 1,
            "busy": int(self.lanes[0].busy),
            "novnc_urls": [self.lanes[0].novnc_url],
        }
