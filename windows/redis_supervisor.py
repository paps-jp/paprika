"""Bundled Redis server lifecycle (Windows-only).

paprika ships tporadowski/redis (https://github.com/tporadowski/redis) ~5MB
in ``windows/bin/redis/`` and starts it on paprika.exe launch. Operator
never sees Redis, never installs it, never knows it exists -- the hub
just talks to ``redis://127.0.0.1:<port>``.

Why same-version Redis as fleet (vs SQLite + asyncio.Queue):
  * 既存 RedisJobStore コードがそのまま動く (実装ゼロ)
  * paprika-runner sandbox の publish_log がプロセス境界を越える
  * Pub/Sub の信頼性 / scaling 性が prod と同じ
  * 配布物 +5MB だけ
  * 裏で 1 プロセス常駐するだけでユーザ可視性ゼロ

Settings タブの「External SQL DSN」欄が空の場合のみこれが起動する。
DSN が入っていれば外部 store を使い、Redis は使わない (v1.1 の機能)。
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)


def _find_free_port(preferred: int = 6379, max_tries: int = 20) -> int:
    """Pick the first free TCP port at or above ``preferred``.

    Default 6379 = Redis canonical. If the operator has Redis (or
    Memurai) running standalone they get +1 instead of a startup error.
    paprika treats whatever port it ends up with as authoritative for
    the rest of the process lifetime.
    """
    for n in range(max_tries):
        port = preferred + n
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(
        f"No free port near {preferred} after {max_tries} tries"
    )


def _resource_path(*parts: str) -> Path:
    """Resolve a path inside the bundled redis/ folder.

    Under PyInstaller --onedir the redis/ folder is inside the
    extracted dist. ``sys._MEIPASS`` is set under --onefile to a
    temp extract dir; in --onedir mode we use the dir of sys.executable
    instead. Dev mode (= running ``python -m windows.main`` from a
    checkout) reads from ``windows/bin/redis/`` directly so devs can
    iterate without re-packaging.
    """
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent / "bin"
    return base.joinpath(*parts)


class RedisSupervisor:
    """Owns one redis-server.exe subprocess for the paprika lifetime.

    Usage::

        sup = RedisSupervisor(data_dir=Path("data/redis"))
        port = sup.start()              # blocks until ready, returns port
        os.environ["PAPRIKA_REDIS_URL"] = f"redis://127.0.0.1:{port}"
        try:
            run_hub_and_worker()
        finally:
            sup.stop()
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        preferred_port: int = 6379,
        bind: str = "127.0.0.1",
    ) -> None:
        self.data_dir = data_dir
        self.preferred_port = preferred_port
        self.bind = bind
        self.port: int | None = None
        self._proc: subprocess.Popen | None = None

    def start(self) -> int:
        """Spawn redis-server.exe. Returns the chosen TCP port.

        Blocks until Redis answers a PING (timeout 5s). Raises
        ``RuntimeError`` if the binary is missing or the server didn't
        come up.
        """
        exe = _resource_path("redis", "redis-server.exe")
        if not exe.exists():
            raise RuntimeError(
                f"Bundled redis-server.exe not found at {exe}. "
                f"PyInstaller spec is missing the redis/ data folder."
            )

        self.port = _find_free_port(self.preferred_port)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # CLI args -- no config file needed for our 1-process embed.
        # We disable both RDB snapshots ``--save ""`` and AOF;
        # paprika's job ledger lives on disk via the FileSystem-backed
        # JobStore writes (data/jobs/<id>/), so Redis here is used
        # purely as a Pub/Sub bus + transient queue. Losing the Redis
        # in-memory state on a paprika.exe crash is acceptable -- the
        # next start replays from disk.
        #
        # (We tried ``--save "60 1"`` originally; the tporadowski Win
        # build parses this as a SINGLE argv element instead of two,
        # producing "Not enough parameters available for --save" at
        # startup. Disabling snapshotting sidesteps the parser issue
        # AND removes a class of "Windows file lock + AV scanner
        # blocks the dump.rdb rename" failures.)
        args = [
            str(exe),
            "--port", str(self.port),
            "--bind", self.bind,
            # CRITICAL: never accept connections from outside localhost.
            # The host PC may be on a shared LAN; Redis without auth on a
            # routable port is a known foot-gun.
            "--protected-mode", "yes",
            "--dir", str(self.data_dir.resolve()),
            "--save", "",
            "--appendonly", "no",
            # Silence to stderr; paprika's logger picks it up via the
            # child process's inherited stderr.
            "--loglevel", "notice",
        ]
        log.info("starting bundled redis-server on 127.0.0.1:%d", self.port)
        # Hide the cmd window on Windows.
        creationflags = 0
        if sys.platform == "win32":
            # CREATE_NO_WINDOW = 0x08000000
            creationflags = 0x08000000

        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )

        # Wait until PING succeeds. tporadowski/redis usually comes up
        # in <500ms; the 5s budget is generous.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._ping():
                log.info("bundled redis ready on :%d (data=%s)",
                         self.port, self.data_dir)
                return self.port
            if self._proc.poll() is not None:
                # Process exited during startup -- read stderr for the
                # actual reason (port collision, permissions, etc.).
                tail = (self._proc.stderr.read() or b"").decode(
                    "utf-8", errors="replace"
                )[-2000:]
                raise RuntimeError(
                    f"redis-server.exe exited during startup "
                    f"(exit code {self._proc.returncode}). stderr tail:\n{tail}"
                )
            time.sleep(0.1)
        # Timeout -- kill the proc and surface a clear error.
        self.stop()
        raise RuntimeError(
            f"bundled redis didn't answer PING on :{self.port} within 5s"
        )

    def _ping(self) -> bool:
        """Lightweight liveness probe. Connect + send ``*1\\r\\n$4\\r\\nPING\\r\\n``,
        expect ``+PONG\\r\\n``. Faster than spinning up redis-py just for
        the startup wait."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", self.port))
                s.sendall(b"*1\r\n$4\r\nPING\r\n")
                return s.recv(64).startswith(b"+PONG")
        except OSError:
            return False

    def stop(self) -> None:
        """Graceful stop: SIGTERM (Windows: terminate()), wait 3s, then
        kill. Idempotent; safe to call from atexit / finally."""
        if self._proc is None or self._proc.poll() is not None:
            return
        log.info("stopping bundled redis on :%s", self.port)
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except OSError:
            # Already dead -- nothing to do.
            pass
        self._proc = None

    @property
    def url(self) -> str | None:
        """Convenience: ``redis://127.0.0.1:{port}`` once start() has run."""
        if self.port is None:
            return None
        return f"redis://127.0.0.1:{self.port}"
