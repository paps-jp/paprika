"""Sandbox executor: ``docker run paprika-runner`` per LLM-generated script.

Each call to :func:`execute_in_sandbox` spawns a fresh ephemeral
container, pipes the Python source over stdin, captures stdout/stderr,
enforces resource limits, and returns the result. The runner image is
network-restricted to the compose default network so it can reach the
hub's ``/sessions/*`` API but nothing else.

Used by :mod:`server.hub.iterative_codegen`. Not user-facing.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

PAPRIKA_RUNNER_IMAGE = os.environ.get(
    "PAPRIKA_RUNNER_IMAGE",
    "paprika-runner:latest",
)
PAPRIKA_RUNNER_NETWORK = os.environ.get(
    "PAPRIKA_RUNNER_NETWORK",
    "paprika_default",
)
# Inside-the-runner URL the hub serves on. The runner reaches it via
# the docker-compose internal network.
PAPRIKA_RUNNER_HUB_URL = os.environ.get(
    "PAPRIKA_RUNNER_HUB_URL",
    "http://hub:8000",
)
# Concurrent runner cap (per hub). Each running runner holds an
# asyncio.Semaphore slot.
PAPRIKA_RUNNER_MAX_CONCURRENT = int(
    os.environ.get("PAPRIKA_RUNNER_MAX_CONCURRENT", "3"),
)


_concurrent_sem = asyncio.Semaphore(PAPRIKA_RUNNER_MAX_CONCURRENT)


# Prefix every spawned runner container with this so:
#   * ``docker stop`` can target it by name in the timeout / cancel paths
#   * ``sweep_orphan_runners()`` at hub startup can find / kill leftovers
#     from a previous hub restart that left containers running.
# Random suffix appended at spawn time.
_RUNNER_NAME_PREFIX = "paprika-runner-"


async def sweep_orphan_runners() -> int:
    """List every ``paprika-runner-*`` container currently running on
    the host and forcibly remove it. Called once at hub startup.

    Rationale: a previous hub process can crash / be SIGKILL'd while
    a sandbox runner is in flight. The runner container survives
    (``--rm`` only fires on clean exit) and keeps polling the hub for
    a session that's long gone. In production we saw a single such
    runner hammer the hub with 404s for 4 days, eventually starving
    worker WS keepalive pings and dropping 22 of 25 workers.

    Sweep is best-effort: docker CLI errors are swallowed so a partial
    failure doesn't block hub startup.
    """
    try:
        p = await asyncio.create_subprocess_exec(
            "docker",
            "ps",
            "--format",
            "{{.Names}}",
            "--filter",
            f"name={_RUNNER_NAME_PREFIX}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(p.communicate(), timeout=10.0)
        if p.returncode != 0:
            return 0
    except Exception:
        return 0
    names = [n for n in out.decode("utf-8", errors="replace").splitlines() if n.strip()]
    if not names:
        return 0
    killed = 0
    for name in names:
        await _docker_stop_best_effort(name)
        killed += 1
    return killed


@dataclass
class ExecResult:
    """Outcome of one sandbox execution."""

    success: bool  # True iff exit_code == 0 and not timed_out
    exit_code: int | None  # None if container never reported one
    stdout: str
    stderr: str
    elapsed_ms: int
    timed_out: bool = False
    spawn_error: str | None = None  # docker run itself failed

    @property
    def short_summary(self) -> str:
        if self.spawn_error:
            return f"spawn error: {self.spawn_error}"
        if self.timed_out:
            return f"timed out after {self.elapsed_ms}ms"
        return f"exit {self.exit_code} in {self.elapsed_ms}ms"


def _resource_args() -> list[str]:
    """Common `docker run` flags for resource isolation."""
    return [
        "--rm",  # auto-remove the container
        "--init",  # PID 1 reaps zombies + handles SIGTERM
        "--network",
        PAPRIKA_RUNNER_NETWORK,
        "--memory",
        "512m",
        "--cpus",
        "1.0",
        "--pids-limit",
        "100",
        "--read-only",
        "--tmpfs",
        "/tmp:size=64m,exec",
        "-e",
        f"PAPRIKA_HUB={PAPRIKA_RUNNER_HUB_URL}",
        "-e",
        "PYTHONUNBUFFERED=1",
    ]


async def _docker_stop_best_effort(name: str) -> None:
    """Try ``docker stop`` then ``docker kill`` so a misbehaving runner
    container can't survive a parent ``proc.kill()``.

    SIGKILL'ing the ``docker run`` CLI doesn't propagate into the
    container (the CLI dies before it can forward anything), so without
    this helper a long-running script -- e.g. an LLM-generated crawl
    loop that swallows the session-expired exception and never exits
    -- becomes an orphan that polls the hub forever. ``thirsty_heisenberg``
    was a real case in production (4 days of 404 spam to a session that
    had been reaped, eventually wedging the WS event loop).
    """
    # docker stop sends SIGTERM, then SIGKILL after the timeout. With
    # --init in the container the SIGTERM reaches the script (PID 2)
    # and tears down httpx clients etc.
    for argv in (
        ["docker", "stop", "-t", "5", name],
        ["docker", "kill", name],
        ["docker", "rm", "-f", name],
    ):
        try:
            p = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(p.wait(), timeout=10.0)
            if p.returncode == 0:
                return
        except Exception:
            continue


async def execute_in_sandbox(
    code: str,
    *,
    timeout_s: float = 180.0,
    extra_env: dict[str, str] | None = None,
    on_line=None,
) -> ExecResult:
    """Run ``code`` in a fresh paprika-runner container.

    The code is piped over stdin to ``python -`` so we don't have to
    write it to disk inside the runner. Stdout / stderr are captured
    in full (truncate at call site if needed). After ``timeout_s`` the
    container is killed.

    ``on_line(stream, text)`` is called once per output line as it
    arrives (``stream`` is ``"stdout"`` or ``"stderr"``). Used by the
    iterative-codegen orchestrator to stream subprocess output into
    the job's live log. Trailing newlines are stripped before the call.

    Concurrency: this function awaits a hub-global semaphore so we
    can't spawn more than :data:`PAPRIKA_RUNNER_MAX_CONCURRENT`
    runners simultaneously. Excess callers queue.

    Container is launched with a deterministic ``--name`` (see
    ``_RUNNER_NAME_PREFIX``) so timeouts / cancels can reach the
    actual container via ``docker stop``, not just the ``docker run``
    CLI parent.
    """
    import uuid

    container_name = f"{_RUNNER_NAME_PREFIX}{uuid.uuid4().hex[:12]}"
    args = ["docker", "run", "-i", "--name", container_name] + _resource_args()
    for k, v in (extra_env or {}).items():
        args += ["-e", f"{k}={v}"]
    args += [PAPRIKA_RUNNER_IMAGE, "-"]

    async with _concurrent_sem:
        t0 = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            return ExecResult(
                success=False,
                exit_code=None,
                stdout="",
                stderr="",
                elapsed_ms=int((time.time() - t0) * 1000),
                spawn_error=f"docker CLI not available: {e}",
            )
        except Exception as e:
            return ExecResult(
                success=False,
                exit_code=None,
                stdout="",
                stderr="",
                elapsed_ms=int((time.time() - t0) * 1000),
                spawn_error=str(e),
            )

        # Feed code in, then close stdin so the runner sees EOF.
        try:
            proc.stdin.write(code.encode("utf-8"))
            await proc.stdin.drain()
        except Exception:
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass

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
                        on_line(label, line.decode("utf-8", errors="replace").rstrip("\r\n"))
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
            # Stop the container BEFORE killing the docker-run CLI so
            # the SIGTERM reaches the script (the CLI dies on SIGKILL
            # too fast to forward anything). Without this, the inner
            # container survives -- see _docker_stop_best_effort.
            await _docker_stop_best_effort(container_name)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            # Give pumps a moment to flush whatever they had buffered.
            await asyncio.sleep(0.5)
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
            # Operator hit "stop" on the live panel (or the hub is
            # shutting down). The orchestrator coroutine is being
            # cancelled; propagate that to the docker subprocess so
            # we don't leave a runner container running forever.
            await _docker_stop_best_effort(container_name)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            # Best-effort drain; we re-raise CancelledError below so
            # callers see the cancel rather than a result.
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (TimeoutError, Exception):
                pass
            raise


async def is_runner_available() -> tuple[bool, str]:
    """Quick health check: does the paprika-runner image exist
    locally? Used by /codegen/info and admin UI badges."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "image",
            "inspect",
            PAPRIKA_RUNNER_IMAGE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0:
            return True, "ok"
        return False, stderr.decode("utf-8", errors="replace")[:200]
    except FileNotFoundError:
        return False, "docker CLI not available"
    except TimeoutError:
        return False, "docker inspect timed out"
    except Exception as e:
        return False, str(e)
