"""Windows 単機 worker: 同梱 Chromium を起動 + ``WorkerAgent`` を hub に attach。

Fleet 版 ``server/worker/lanes.py`` の ``LanePool`` は Xvfb + 物理 noVNC
port + N 並列 lane を前提に動く。Windows 単機ではそれが必要以上に
重いので、本モジュールでは以下まで割り切る:

  * Chrome は **1 lane だけ** (Windows の物理 display で起動)
  * Xvfb なし (Windows の display server をそのまま使う)
  * noVNC ブリッジは **v1.1 送り** (TightVNC + websockify 同梱は
    インストールサイズと安定性のトレードオフ。v1.0 では Live タブの
    画面表示無しで動かす ‒ ジョブ実行 / cookies 保存 / fetch /
    pap.walk / page.download_video は全部動く)

WorkerAgent はもともと「外部で起動済 Chrome に CDP attach」する
モードを持ってる (``chrome_host`` / ``chrome_port`` 引数)。そのモードを
Windows でも使うことで、Linux fleet 版コード (server/worker/agent.py)
は無改修のまま再利用できる。

呼び出しフロー (windows/main.py より):

    sup = WindowsWorkerSupervisor(
        hub_ws_url="ws://127.0.0.1:8000",
        chromium_path=preflight_result.chromium_path,
        chrome_user_data_dir=data_dir / "chrome",
    )
    sup.start_in_background()   # 別 thread で asyncio.run(agent.run())
    ...
    sup.stop()
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chrome lifecycle
# ---------------------------------------------------------------------------


def _free_port(preferred: int, *, tries: int = 20) -> int:
    """Pick the first free TCP port at or above ``preferred``."""
    for n in range(tries):
        port = preferred + n
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port near {preferred}")


def _wait_for_cdp(port: int, *, timeout_s: float = 15.0) -> bool:
    """Poll Chrome's DevTools endpoint until it answers (or timeout).

    nodriver / WorkerAgent doesn't tolerate connect-before-ready --
    this drains the race so the WS handshake to the hub doesn't
    happen before Chrome can accept attach requests."""
    import urllib.request
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


class _ChromeProc:
    """Owns one Chromium subprocess + the user-data-dir under it."""

    def __init__(
        self,
        *,
        chromium_path: Path,
        user_data_dir: Path,
        debugging_port: int,
        extra_args: list[str] | None = None,
    ) -> None:
        self.chromium_path = chromium_path
        self.user_data_dir = user_data_dir
        self.debugging_port = debugging_port
        self.extra_args = extra_args or []
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        """Spawn chrome.exe with CDP enabled. Blocks until /json/version
        returns OK or 30s timeout (Windows cold-start with a fresh
        user-data-dir can take 10-15s for First-Run init)."""
        if not self.chromium_path.exists():
            raise RuntimeError(f"Chromium not found at {self.chromium_path}")
        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        args = [
            str(self.chromium_path),
            f"--remote-debugging-port={self.debugging_port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--password-store=basic",
            # Chrome by default flashes notifications on first
            # navigation; quiet them so the operator's screen isn't
            # spammed when paprika runs in the background.
            "--disable-notifications",
            # nodriver and CDP attach need a target window. Without
            # this Chrome can launch with no visible tabs (= "GPU
            # process died, restart" loop on some Windows configs).
            "about:blank",
            *self.extra_args,
        ]

        creationflags = 0
        if sys.platform == "win32":
            # NOTE: we used to set CREATE_NO_WINDOW here for a clean
            # tray-style launch, but on some Windows configs that flag
            # produces a Chromium that never finishes init when paired
            # with --remote-debugging-port (the helper child processes
            # inherit the same window-less console and never signal
            # ready). Spawning Chromium in its own process group is
            # enough to avoid a cmd flash; skip CREATE_NO_WINDOW.
            creationflags = 0x00000200  # CREATE_NEW_PROCESS_GROUP

        log.info(
            "starting Chromium for worker on :%d (user-data=%s)",
            self.debugging_port, self.user_data_dir,
        )
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )

        if not _wait_for_cdp(self.debugging_port, timeout_s=30.0):
            # Surface why: did the subprocess die? Pull stderr if so.
            rc = self._proc.poll()
            stderr_tail = ""
            if rc is not None:
                try:
                    stderr_tail = (self._proc.stderr.read() or b"").decode(
                        "utf-8", errors="replace"
                    )[-1500:]
                except Exception:
                    pass
                log.error(
                    "Chromium exited during startup (rc=%s). stderr tail:\n%s",
                    rc, stderr_tail,
                )
            else:
                log.error(
                    "Chromium still running but /json/version unreachable "
                    "on :%d after 30s -- inspecting handle",
                    self.debugging_port,
                )
            self.stop()
            raise RuntimeError(
                f"Chromium didn't expose CDP on :{self.debugging_port} "
                f"within 30s (rc={rc}). stderr: {stderr_tail[:300]}"
            )

    def stop(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        log.info("stopping Chromium on :%s", self.debugging_port)
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except OSError:
            pass
        self._proc = None


# ---------------------------------------------------------------------------
# WindowsWorkerSupervisor: Chrome + WorkerAgent をペアで管理
# ---------------------------------------------------------------------------


class WindowsWorkerSupervisor:
    """Windows 単機 worker の lifecycle owner。

    paprika.exe 起動時に start() / 終了時に stop() を呼ぶ。中で:

      1. 空きポートを 9223 / 6080 から探す
      2. _ChromeProc で Chrome 起動 (CDP enabled)
      3. WorkerAgent を ``chrome_host=127.0.0.1, chrome_port=9223``
         で組み立て
      4. 別 thread (= asyncio.run) で agent.run() を回す

    fleet 版 ``LanePool`` を使わないので noVNC ブリッジは無効。Live
    タブの画面表示は無効になるが、ジョブ実行 (fetch / pap.walk /
    page.download_video / cookies 保存 / extensions) は全部動く。
    """

    def __init__(
        self,
        *,
        hub_ws_url: str,
        chromium_path: Path,
        chrome_user_data_dir: Path,
        worker_id: str | None = None,
        chrome_port_preferred: int = 9223,
    ) -> None:
        self.hub_ws_url = hub_ws_url.rstrip("/")
        self.chromium_path = chromium_path
        self.chrome_user_data_dir = chrome_user_data_dir
        self.worker_id = worker_id
        self.chrome_port_preferred = chrome_port_preferred
        self._chrome: _ChromeProc | None = None
        self._agent_thread: threading.Thread | None = None
        self._agent_loop: asyncio.AbstractEventLoop | None = None
        self._agent_task: asyncio.Task | None = None
        self._stop_evt = threading.Event()

    def start(self) -> None:
        """Spin up Chrome + spawn the WorkerAgent thread.
        Blocks until Chrome answers CDP probe."""
        chrome_port = _free_port(self.chrome_port_preferred)

        # Resolve the Settings tab's ``worker_chrome_headless`` toggle.
        # The user can flip this at runtime; the change applies on the
        # next paprika.exe start because Chrome is launched once at boot.
        # Default: GUI mode (= operator can see the browser on their
        # desktop, easier to debug "what's it doing now?").
        extra_chrome_args: list[str] = []
        try:
            from server.hub._state import state as _state
            if _state.settings is not None:
                headless = bool(_state.settings.get("worker_chrome_headless", False))
                if headless:
                    # ``--headless=new`` (since Chrome 109) uses the
                    # same render path as GUI Chrome → fewer surprises
                    # vs the legacy ``--headless`` which sometimes
                    # picks a different code path for SVG / video /
                    # WebGL. ``--disable-gpu`` is the long-time
                    # companion flag on Windows headless to avoid the
                    # "DXGI" probe that hangs on some configs.
                    extra_chrome_args.extend(["--headless=new", "--disable-gpu"])
                    log.info(
                        "Chromium will start in headless mode "
                        "(settings.worker_chrome_headless=True)"
                    )
        except Exception:
            # Settings not ready yet (= hub still booting) -- fall back
            # to GUI mode silently. The settings reader is best-effort.
            log.debug("settings unavailable for headless toggle", exc_info=True)

        self._chrome = _ChromeProc(
            chromium_path=self.chromium_path,
            user_data_dir=self.chrome_user_data_dir,
            debugging_port=chrome_port,
            extra_args=extra_chrome_args,
        )
        self._chrome.start()

        # Resolve worker_id. The fleet helper ``default_worker_id()``
        # writes to ~/.paprika/worker_id; we honour the same path so
        # Windows installs that use the SDK from outside paprika see a
        # stable worker_id across paprika.exe restarts.
        from server.worker.agent import default_worker_id, WorkerAgent

        worker_id = self.worker_id or default_worker_id()

        # Advertise a placeholder novnc_url so the hub scheduler treats
        # this worker as having a Chrome attach surface (= it passes the
        # ``lane_novnc_urls or novnc_url`` filter in pick_worker).
        # The URL itself isn't reachable in v1.0 (TightVNC + websockify
        # bundling is v1.1); admin UI's Live tab won't render a viewer,
        # but jobs still dispatch + run + complete.
        placeholder_novnc = (
            f"http://127.0.0.1:{chrome_port}/novnc-placeholder"
        )

        # Build a single-lane "pool" pointing at the bundled Chromium
        # we just started. The agent's session_start path requires a
        # lane_pool (no shared-chrome session path exists), so this
        # stub is what makes paprika-client SDK scripts work on
        # Windows portable. See windows/lane_pool_stub.py for the
        # interface compromise notes (no profile swap, no Live noVNC).
        from windows.lane_pool_stub import _SingleLanePool

        lane_pool = _SingleLanePool(
            chrome_port=chrome_port,
            novnc_url=placeholder_novnc,
            user_data_dir=self.chrome_user_data_dir,
        )

        agent = WorkerAgent(
            hub_ws_url=self.hub_ws_url,
            worker_id=worker_id,
            max_concurrent=1,
            labels={
                "platform": "windows",
                "edition": "portable",
            },
            # Both chrome_host and chrome_port still get set so old
            # code paths (e.g. the heartbeat that mirrors them) see a
            # consistent value, but the session_start handler now
            # follows the lane_pool branch because lane_pool is set.
            chrome_host="127.0.0.1",
            chrome_port=chrome_port,
            worker_secret=os.environ.get("PAPRIKA_WORKER_SECRET"),
            novnc_url=placeholder_novnc,
            lane_pool=lane_pool,
        )

        # WorkerAgent has a built-in reconnect loop, so we just spin it
        # up on a thread and forget. shutdown is via task.cancel().
        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._agent_loop = loop
            try:
                self._agent_task = loop.create_task(agent.run())
                loop.run_until_complete(self._agent_task)
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("worker agent crashed")
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
                log.info("worker agent thread exited")

        t = threading.Thread(target=_run, name="paprika-worker", daemon=True)
        t.start()
        self._agent_thread = t
        log.info(
            "worker started: id=%s chrome=:%d hub=%s",
            worker_id, chrome_port, self.hub_ws_url,
        )

    def stop(self) -> None:
        """Cancel the agent task + stop Chrome. Idempotent."""
        log.info("stopping worker")
        # Cancel the agent's run() coroutine from the agent's own loop.
        if self._agent_loop is not None and self._agent_task is not None:
            try:
                self._agent_loop.call_soon_threadsafe(self._agent_task.cancel)
            except RuntimeError:
                # Loop already closed.
                pass
        if self._agent_thread is not None:
            self._agent_thread.join(timeout=5.0)
        if self._chrome is not None:
            self._chrome.stop()
            self._chrome = None


# ---------------------------------------------------------------------------
# Compat alias: older code in this module exposed `WindowsLane`. Keep an
# alias pointing at the new supervisor so external import paths don't
# break, even though the public API moved to WindowsWorkerSupervisor.
# ---------------------------------------------------------------------------

WindowsLane = WindowsWorkerSupervisor  # noqa: F401
