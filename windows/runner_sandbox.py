"""Windows-native paprika-runner sandbox (drop-in for server.hub.runner).

Fleet 版 (``server/hub/runner.py``) は ``docker run paprika-runner:latest``
で codegen が生成した script を Linux container で隔離実行する。
Windows 単機では Docker を要求できないので、ここで「tempfile に code を
書いて ``python -u`` でサブプロセス起動」の軽量サンドボックスに置き換える。

公開シグネチャは fleet 版と**完全互換**:

  execute_in_sandbox(code, *, timeout_s, extra_env, on_line) -> ExecResult
  is_runner_available() -> tuple[bool, str]
  sweep_orphan_runners() -> int

``windows.main:BackendSupervisor.start()`` が hub 起動前に
``server.hub.runner`` のこれらシンボルを本モジュールの実装に
**monkey-patch** することで、hub / codegen-loop コードは無改修のまま
Windows でも codegen-loop が回るようになる。

セキュリティモデル:
  * Linux fleet 版: Docker container + cgroup + read-only FS + network
    isolation (paprika-runner-net) + 512MB / 1cpu / 100 pids cap
  * Windows 版: tempdir + subprocess + env scrubbing + timeout
    + sys.executable (= 同梱 python)。**メモリ / ネットワーク隔離は無い**

  Windows 版が弱いのは事実だが、「自分の Windows で自分が動かす script」
  というユースケース (= ローカル paprika 1 ユーザ) では許容範囲。
  複数ユーザに公開する用途には fleet 版 (Linux + Docker) を案内する。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Fleet 版と同名の並列実行 cap。複数 codegen-loop / rerun が同時に
# runner を spawn しても CPU / FD を食い潰さないようにする。Windows
# 単機なら 2 がデフォ妥当。env で override 可能 (fleet 版と互換)。
_MAX_CONCURRENT = int(os.environ.get("PAPRIKA_RUNNER_MAX_CONCURRENT", "2"))
_concurrent_sem = asyncio.Semaphore(_MAX_CONCURRENT)


@dataclass
class ExecResult:
    """Outcome of one sandbox execution. Field-for-field compatible
    with ``server.hub.runner.ExecResult`` so callers can swap
    implementations without code changes."""

    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    elapsed_ms: int
    timed_out: bool = False
    spawn_error: str | None = None

    @property
    def short_summary(self) -> str:
        if self.spawn_error:
            return f"spawn error: {self.spawn_error}"
        if self.timed_out:
            return f"timed out after {self.elapsed_ms}ms"
        return f"exit {self.exit_code} in {self.elapsed_ms}ms"


def _project_root() -> Path:
    """Project root for the runner subprocess's PYTHONPATH.

    Dev mode: ``<repo-root>`` so ``import paprika_client`` resolves to
    the in-tree copy under ``client/python/``.
    Frozen (PyInstaller --onedir): ``sys._MEIPASS`` (the extract dir
    that holds the bundled python + server/ + client/python/)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    # windows/runner_sandbox.py -> windows/ -> repo root
    return Path(__file__).resolve().parents[1]


def _build_runner_env(extra_env: dict[str, str] | None) -> dict[str, str]:
    """Construct the subprocess environment.

    DO NOT pass the operator's full env to the runner -- secrets like
    GITHUB_TOKEN / AWS_* / etc. would leak into untrusted generated
    code. We whitelist only what the runner needs to actually run
    paprika_client scripts.
    """
    proj = _project_root()
    pp = str(proj)
    # paprika_client lives under client/python/ in the source tree.
    # Add both so `import paprika_client` works in dev and in bundle.
    client_dir = proj / "client" / "python"
    if client_dir.exists():
        pp = f"{client_dir}{os.pathsep}{pp}"

    base = {
        # Identity + path basics needed by virtually every Python
        # subprocess on Windows. SystemRoot lets the C runtime locate
        # core DLLs; without it Python.exe itself often fails to start.
        "PATH": os.environ.get("PATH", ""),
        "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
        "TEMP": os.environ.get("TEMP", os.environ.get("TMP", "")),
        "TMP": os.environ.get("TMP", os.environ.get("TEMP", "")),
        # paprika resolves modules from PYTHONPATH (PyInstaller bundle
        # already has them; dev mode needs the repo root).
        "PYTHONPATH": pp,
        # Line-buffer stdout/stderr so on_line() sees output as it's
        # produced, not in one giant batch at exit.
        "PYTHONUNBUFFERED": "1",
        # Mirror the env-name fleet 版 uses for the hub base URL.
        "PAPRIKA_HUB": os.environ.get("PAPRIKA_HUB", ""),
    }
    # On Windows, USERPROFILE / APPDATA / LOCALAPPDATA are sometimes
    # needed by httpx / urllib (for the SSL cert store under recent
    # Python). Pass them through but never any other env.
    for k in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH"):
        v = os.environ.get(k)
        if v:
            base[k] = v
    if extra_env:
        base.update(extra_env)
    return base


async def execute_in_sandbox(
    code: str,
    *,
    timeout_s: float = 180.0,
    extra_env: dict[str, str] | None = None,
    on_line=None,
) -> ExecResult:
    """Run ``code`` in an isolated Windows tempdir subprocess.

    Signature-compatible with ``server.hub.runner.execute_in_sandbox``.
    Stdout / stderr are streamed line-by-line to ``on_line(label, text)``
    in real time so the admin UI's Live tab updates as the script runs,
    not just at the end.

    The semaphore cap (``PAPRIKA_RUNNER_MAX_CONCURRENT``, default 2)
    prevents the operator's PC from being flooded by 10 simultaneous
    codegen attempts.
    """
    workdir = Path(tempfile.mkdtemp(prefix="paprika-runner-"))
    script_path = workdir / "script.py"
    # PyInstaller frozen mode: don't write CRLF, keep the script as
    # UTF-8 LF so the embedded Python parses it identically to dev mode.
    script_path.write_text(code, encoding="utf-8", newline="\n")

    env = _build_runner_env(extra_env)

    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW: hide the cmd window of the child python.
        # Without this, every codegen attempt pops a console flash.
        creationflags = 0x08000000

    async with _concurrent_sem:
        t0 = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(script_path),
                cwd=str(workdir),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
        except FileNotFoundError as e:
            return ExecResult(
                success=False, exit_code=None,
                stdout="", stderr="",
                elapsed_ms=int((time.time() - t0) * 1000),
                spawn_error=f"python executable not found: {e}",
            )
        except Exception as e:
            return ExecResult(
                success=False, exit_code=None,
                stdout="", stderr="",
                elapsed_ms=int((time.time() - t0) * 1000),
                spawn_error=str(e),
            )

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _pump(stream, chunks: list[bytes], label: str) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    return
                chunks.append(line)
                if on_line is not None:
                    try:
                        on_line(
                            label,
                            line.decode("utf-8", errors="replace").rstrip("\r\n"),
                        )
                    except Exception:
                        pass

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _pump(proc.stdout, stdout_chunks, "stdout"),
                    _pump(proc.stderr, stderr_chunks, "stderr"),
                    proc.wait(),
                ),
                timeout=timeout_s,
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            return ExecResult(
                success=(proc.returncode == 0),
                exit_code=proc.returncode,
                stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
                stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
                elapsed_ms=elapsed_ms,
                timed_out=False,
            )
        except TimeoutError:
            log.warning("sandbox timed out after %.1fs", timeout_s)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            # Give pumps a moment to flush buffered output.
            await asyncio.sleep(0.3)
            elapsed_ms = int((time.time() - t0) * 1000)
            return ExecResult(
                success=False,
                exit_code=proc.returncode,
                stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
                stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
                elapsed_ms=elapsed_ms,
                timed_out=True,
            )
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (TimeoutError, Exception):
                pass
            raise
        finally:
            # Cleanup the tempdir. Best-effort: Windows sometimes
            # holds the script file briefly after the child exits.
            try:
                import shutil
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass


async def is_runner_available() -> tuple[bool, str]:
    """Windows 版は常に True (= 同梱 python が runner として使える)。
    Fleet 版は docker image inspect で判定するので、ここで signature を
    合わせておけば admin UI の /codegen/info / health バッジが同じ
    payload を期待できる。"""
    if not Path(sys.executable).exists():
        return False, "python executable not found"
    return True, "ok (windows native subprocess)"


async def sweep_orphan_runners() -> int:
    """Windows 版では Docker container を sweep する必要がないので no-op。
    Fleet 版は paprika-runner-* container を kill して回るが、native
    subprocess の場合は親 paprika.exe の終了で自動的に reap される。"""
    return 0
