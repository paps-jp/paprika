"""``paprika.exe`` のエントリポイント。

起動順序:
  1. setup_logging()
  2. data_dir 決定 (exe と同階層の ``data/`` がデフォ)
  3. preflight: Chromium 検出 + 初回 DL、VC++ Redist 確認
  4. 空きポート探索 (Redis + hub)
  5. RedisSupervisor で同梱 redis-server.exe を spawn
  6. 環境変数を hub 用に整える (PAPRIKA_DATA_DIR / PAPRIKA_REDIS_URL 等)
  7. hub を asyncio task として in-process 起動
  8. worker (1 lane) を subprocess で起動
  9. pywebview + tray で UI を表示

終了:
  * tray menu [終了] → ``UiShell.on_quit`` → worker stop → hub stop
    → Redis stop → exit(0)
  * Ctrl+C (--console モード) も同じ shutdown ハンドラに繋ぐ
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path

from server._logging import setup_logging
from windows.preflight import run_preflight
from windows.redis_supervisor import RedisSupervisor
from windows.ui_shell import UiShell
from windows.worker_supervisor import WindowsWorkerSupervisor


log = logging.getLogger(__name__)


def _default_data_dir() -> Path:
    """The default ``data/`` directory.

    Portable design: data lives next to paprika.exe so the operator can
    move / copy / delete the whole folder without orphaning anything in
    AppData or the registry. Falls back to CWD in dev mode.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "data"
    return Path.cwd() / "data"


def _find_free_port(preferred: int, max_tries: int = 20) -> int:
    for n in range(max_tries):
        port = preferred + n
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(
        f"No free TCP port near {preferred}"
    )


# ---------------------------------------------------------------------------
# Hub + worker lifecycle (in-process, asyncio task)
# ---------------------------------------------------------------------------


class BackendSupervisor:
    """Owns the hub uvicorn task + the worker subprocess for this
    paprika.exe instance. UiShell calls .start() then .stop() from the
    tray Quit menu."""

    def __init__(
        self,
        *,
        data_dir: Path,
        hub_port: int,
        redis_url: str,
        chromium_path: Path,
    ) -> None:
        self.data_dir = data_dir
        self.hub_port = hub_port
        self.redis_url = redis_url
        self.chromium_path = chromium_path
        self._hub_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None  # uvicorn Server instance

    def start(self) -> None:
        """Spin up hub on a background thread (uvicorn doesn't play
        nicely with pywebview's main-thread event loop on Windows).
        Worker is started in-process from inside the hub event loop --
        same pattern as ``python -m server --mode all``.
        """
        # Wire env so server.hub.app.lifespan picks these up.
        os.environ["PAPRIKA_DATA_DIR"] = str(self.data_dir.resolve())
        os.environ["PAPRIKA_REDIS_URL"] = self.redis_url
        # Chromium path used by lanes_win.py when it spawns the
        # browser. (windows/worker_win.py reads this env.)
        os.environ["PAPRIKA_CHROMIUM_PATH"] = str(self.chromium_path)
        # Lock the hub to localhost; the operator can override with
        # --bind 0.0.0.0 when they want LAN access (= "use my desktop
        # as a fleet worker for the rest of the household").
        os.environ.setdefault("PAPRIKA_HUB_HOST", "127.0.0.1")

        # The hub doesn't read PAPRIKA_REDIS_URL / PAPRIKA_DATA_DIR
        # from env -- on the fleet path, server/__main__.py mutates
        # ``hub_config`` directly from argparse. We do the equivalent
        # here BEFORE uvicorn imports server.hub.app (the lifespan
        # reads ``config.redis_url`` / ``config.data_dir`` at startup
        # time, so as long as we set them now they win).
        from server.hub._state import config as hub_config
        hub_config.data_dir = self.data_dir
        hub_config.redis_url = self.redis_url
        hub_config.public_base_url = f"http://127.0.0.1:{self.hub_port}"

        # Swap server.hub.runner.execute_in_sandbox with the Windows
        # native subprocess version. Fleet 版は docker run paprika-runner、
        # Windows 単機は ``python -u tempfile.py``. Codegen-loop / rerun
        # の呼び出し側 (server/hub/_jobrunner.py 等) は無改修のまま
        # 切り替わる。
        import server.hub.runner as _hub_runner
        from windows import runner_sandbox as _win_sandbox
        _hub_runner.execute_in_sandbox = _win_sandbox.execute_in_sandbox
        _hub_runner.is_runner_available = _win_sandbox.is_runner_available
        _hub_runner.sweep_orphan_runners = _win_sandbox.sweep_orphan_runners
        _hub_runner.ExecResult = _win_sandbox.ExecResult
        log.info("sandbox: monkey-patched server.hub.runner with windows.runner_sandbox")

        self._hub_thread = threading.Thread(
            target=self._run_uvicorn,
            name="paprika-hub",
            daemon=True,
        )
        self._hub_thread.start()

    def _run_uvicorn(self) -> None:
        """uvicorn main loop on a thread. Stores ``self._server`` so
        ``.stop()`` can signal a graceful shutdown.

        Wrap every step in try/except + log.exception so a crash during
        ``server.hub.app`` import (= PyInstaller bundle missing a hidden
        import, frozen-mode Path issue, etc.) is recorded in the file
        log instead of silently terminating the thread."""
        log.info("hub thread: importing uvicorn")
        try:
            import uvicorn
        except Exception:
            log.exception("hub thread: uvicorn import failed")
            return

        log.info("hub thread: importing server.hub.app (this loads ALL routes)")
        try:
            # Pre-import the FastAPI app so any import error surfaces
            # HERE with a stack trace, rather than getting swallowed
            # by uvicorn's import_from_string adapter (which formats
            # the error as a plain string and is harder to read).
            import server.hub.app  # noqa: F401
        except Exception:
            log.exception("hub thread: server.hub.app import failed")
            return

        log.info("hub thread: building uvicorn.Config (host=%s port=%d)",
                 os.environ.get("PAPRIKA_HUB_HOST", "127.0.0.1"), self.hub_port)
        # Pass ``log_config=None`` so uvicorn doesn't overwrite our root
        # logger config (which holds the file handler we set up in
        # ``_add_file_logger``). Without this, GUI-mode runs lose every
        # log line after uvicorn starts -- the file_log handler gets
        # silently dropped.
        config = uvicorn.Config(
            "server.hub.app:app",
            host=os.environ.get("PAPRIKA_HUB_HOST", "127.0.0.1"),
            port=self.hub_port,
            log_level="info",
            access_log=True,
            reload=False,
            log_config=None,
            # Worker WS tolerance — see server/__main__.py for rationale.
            ws_ping_interval=30.0,
            ws_ping_timeout=120.0,
        )
        try:
            self._server = uvicorn.Server(config)
        except Exception:
            log.exception("hub thread: uvicorn.Server() raised")
            return

        log.info("hub thread: calling server.run() (will block until shutdown)")
        try:
            self._server.run()
        except SystemExit as e:
            log.error("hub thread: server.run() exited with SystemExit %s", e.code)
        except Exception:
            log.exception("hub thread: uvicorn server.run() raised")
        log.info("hub thread: exiting")

    def stop(self) -> None:
        """Ask uvicorn to exit, wait for the thread to drain."""
        if self._server is not None:
            log.info("requesting hub shutdown")
            self._server.should_exit = True
        if self._hub_thread is not None:
            self._hub_thread.join(timeout=10.0)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paprika.exe",
        description="paprika (Windows portable)",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Where paprika stores jobs / cookies / Chromium / Redis "
             "(default: <exe-folder>/data/)",
    )
    p.add_argument(
        "--hub-port",
        type=int,
        default=8000,
        help="Preferred hub HTTP port (auto-bump if busy). Default: 8000",
    )
    p.add_argument(
        "--no-ui",
        action="store_true",
        help="Headless mode: don't open the pywebview window or tray. "
             "The hub still serves at http://127.0.0.1:{port}/ for "
             "external browsers / curl. Ctrl+C to exit.",
    )
    p.add_argument(
        "--console",
        action="store_true",
        help="Show stdout/stderr in a console window (default: hidden "
             "for tray-style operation).",
    )
    return p


def _add_file_logger(data_dir: Path) -> None:
    """In GUI mode (no console window) stdout/stderr go nowhere. Add a
    rotating-style log file under data/logs/ so the operator can read
    "why did paprika.exe just close" after the fact.

    Rotation: keep the previous run as ``.0`` so the file doesn't grow
    forever and the operator always has 2 generations of context."""
    import logging as _logging
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "paprika.log"
    if log_path.exists():
        prev = log_dir / "paprika.0.log"
        try:
            if prev.exists():
                prev.unlink()
            log_path.rename(prev)
        except OSError:
            pass
    fh = _logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setLevel(_logging.INFO)
    fh.setFormatter(_logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    _logging.getLogger().addHandler(fh)
    log.info("file log: %s", log_path)


def _wait_for_hub_ready(hub_url: str, *, timeout_s: float = 30.0) -> bool:
    """Poll the hub's /health endpoint until it answers OK.

    GUI mode's pywebview navigates immediately on ``create_window``;
    without this gate the very first thing the operator sees is
    "127.0.0.1 接続拒否" because uvicorn hasn't started listening yet.
    Returns True on success, False on timeout."""
    import urllib.request
    import time as _time
    deadline = _time.monotonic() + timeout_s
    url = hub_url.rstrip("/") + "/health"
    while _time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception:
            _time.sleep(0.2)
    return False


def main() -> int:
    setup_logging()
    args = _build_parser().parse_args()
    data_dir = (args.data_dir or _default_data_dir()).resolve()
    # File logger BEFORE anything else, so even an exception in
    # preflight is recorded somewhere readable.
    try:
        _add_file_logger(data_dir)
    except Exception:
        log.exception("could not set up file logger")
    log.info("paprika starting; data_dir=%s", data_dir)

    # --- preflight --------------------------------------------------
    pre = run_preflight(data_dir)
    if not pre.ok:
        # Render a tk dialog listing missing deps and bail. In --no-ui
        # mode we just print to stderr.
        log.error("preflight failed: %s", [m.key for m in pre.missing])
        if not args.no_ui:
            try:
                from windows.preflight_dialog import show_missing_deps_dialog
                show_missing_deps_dialog(pre.missing)
            except Exception:
                pass
        return 2

    # --- Redis ------------------------------------------------------
    redis = RedisSupervisor(data_dir=data_dir / "redis")
    try:
        redis.start()
    except Exception:
        log.exception("could not start bundled Redis")
        return 3

    # --- Hub --------------------------------------------------------
    hub_port = _find_free_port(args.hub_port)
    backend = BackendSupervisor(
        data_dir=data_dir,
        hub_port=hub_port,
        redis_url=redis.url,
        chromium_path=pre.chromium_path,
    )
    backend.start()

    hub_url = f"http://127.0.0.1:{hub_port}"
    log.info("hub URL: %s", hub_url)

    # Block until the hub answers /health. WITHOUT this, pywebview /
    # the browser navigates the moment ``shell.run()`` is called and
    # the operator sees "127.0.0.1 接続拒否" while uvicorn is still
    # initialising. Once /health returns 200, the listener is up.
    if not _wait_for_hub_ready(hub_url, timeout_s=30.0):
        log.error(
            "hub didn't become ready at %s within 30s; aborting", hub_url
        )
        redis.stop()
        return 4
    log.info("hub is ready at %s", hub_url)

    # --- First-run setup: Chrome visibility ------------------------
    # If the operator has never been asked, pop a tkinter dialog
    # BEFORE starting the worker (= before Chrome would flash on the
    # desktop). The choice is persisted so subsequent paprika.exe
    # launches don't ask again. Skipped in --no-ui mode (= headless
    # smoke tests / CI -- defaults to headless).
    if not args.no_ui:
        try:
            from server.hub._state import state as _state
            if _state.settings is not None and not _state.settings.is_set("worker_chrome_headless"):
                from windows.firstrun_dialog import ask_chrome_visibility
                picked = ask_chrome_visibility()
                if picked is None:
                    picked = "headless"  # safe default if dialog dismissed
                _state.settings.update({"worker_chrome_headless": picked == "headless"})
                log.info("first-run: worker_chrome_headless=%s", picked == "headless")
        except Exception:
            log.exception("first-run dialog failed; using existing settings")

    # --- Worker (1 lane: bundled Chromium + WorkerAgent) ------------
    worker = WindowsWorkerSupervisor(
        hub_ws_url=hub_url.replace("http://", "ws://"),
        chromium_path=pre.chromium_path,
        chrome_user_data_dir=data_dir / "chrome",
    )
    try:
        worker.start()
    except Exception:
        log.exception("worker startup failed; UI will still work but jobs cannot run")
        worker = None

    # --- UI ---------------------------------------------------------
    def _shutdown() -> None:
        log.info("shutdown sequence start")
        try:
            if worker is not None:
                worker.stop()
        except Exception:
            log.exception("worker stop failed")
        try:
            backend.stop()
        finally:
            redis.stop()
        log.info("shutdown complete")

    if args.no_ui:
        # Headless: wait for SIGINT. backend keeps running on its
        # thread.
        log.info("headless mode; open %s in a browser. Ctrl+C to quit.", hub_url)
        try:
            signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
            while True:
                signal.pause() if hasattr(signal, "pause") else __import__("time").sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            _shutdown()
        return 0

    # Normal: pywebview window + tray. Blocks until [Quit].
    shell = UiShell(
        hub_url=hub_url,
        on_quit=_shutdown,
        icon_path=_paprika_icon_path(),
    )
    shell.run()
    return 0


def _paprika_icon_path() -> Path | None:
    """Locate the bundled paprika.ico.

    Frozen mode (= paprika.exe): the icon was added as PyInstaller
    data and ends up under ``sys._MEIPASS``. Dev mode (= running
    ``python -m windows.main``): read straight from the source tree.
    Returns None if neither exists -- callers fall back to no icon.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent  # windows/
    candidate = base / "paprika.ico"
    return candidate if candidate.exists() else None


if __name__ == "__main__":
    sys.exit(main())
