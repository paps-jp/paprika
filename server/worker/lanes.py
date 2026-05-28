"""Per-job browser lane pool (Phase 4: 1 job : 1 lane).

A `LanePool` pre-spawns N "browser lanes" on worker startup. Each lane has
its own dedicated Xvfb display, Chrome with remote-debugging port, x11vnc,
and noVNC websockify proxy on unique ports.

A "Lane" is one independent track of parallel browser execution -- not an
empty slot to fill, but a long-lived stateful browser instance that keeps
its cookies, login, and other profile state across the jobs that pass
through it. The name was chosen to convey parallelism (a worker has N
lanes running side-by-side) without colliding with the `browser` object
that nodriver exposes for CDP-level operations.

When a job is assigned, the worker acquires one free lane, uses its
Chrome, reports the lane's noVNC URL to the hub, and releases the lane
when the job completes.

Port allocation (lane index `i` ∈ [0, N)):
  - Xvfb display       :{100+i}
  - Chrome             :{9223+i}
  - VNC                :{5901+i}
  - noVNC websockify   :{base_port+i}   (default base_port=6080)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def _log(lane_idx: int, msg: str) -> None:
    log.info("[lane %d] %s", lane_idx, msg)


def _migrate_user_data_dirs(n_lanes: int) -> None:
    """One-time rename of chrome-slot-{i} -> chrome-lane-{i}.

    Carries cookies / login state across the Slot -> Lane rename so users
    don't lose their saved sessions. Idempotent and safe to run on every
    worker boot -- a no-op once the rename has happened. Drop this helper
    one release after the rename ships.
    """
    for i in range(n_lanes):
        old = Path(f"/tmp/chrome-slot-{i}")
        new = Path(f"/tmp/chrome-lane-{i}")
        if old.exists() and not new.exists():
            try:
                old.rename(new)
                log.info("[pool] migrated profile dir %s -> %s", old, new)
            except OSError as e:
                log.warning(
                    "[pool] could not migrate %s -> %s: %s", old, new, e
                )


async def _wait_path(path: str, timeout: float = 8.0) -> bool:
    """Wait until `path` exists (used for Xvfb lock file)."""
    for _ in range(int(timeout / 0.2)):
        if os.path.exists(path):
            return True
        await asyncio.sleep(0.2)
    return False


async def _wait_port(host: str, port: int, timeout: float = 10.0) -> bool:
    """Wait until a TCP connect to (host, port) succeeds."""
    for _ in range(int(timeout / 0.2)):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            await asyncio.sleep(0.2)
    return False


async def _wait_http(url: str, timeout: float = 30.0) -> bool:
    for _ in range(int(timeout / 0.5)):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            await asyncio.sleep(0.5)
    return False


@dataclass
class Lane:
    lane_idx: int
    display_num: int
    chrome_port: int
    vnc_port: int
    novnc_port: int
    public_host: str
    busy: bool = False
    # Supporting processes that never get respawned (Xvfb, fluxbox, x11vnc,
    # websockify). Chrome is tracked separately because the lane may need
    # to bring it back to life on its own.
    processes: list[subprocess.Popen] = field(default_factory=list)
    # Currently running Chrome subprocess for this lane. None during the
    # gap between detected death and successful respawn.
    _chrome_proc: subprocess.Popen | None = None
    # Environment dict (DISPLAY=...) reused when Chrome restarts.
    _env: dict = field(default_factory=dict)
    # Background task that watches Chrome and respawns it if it exits.
    _watchdog_task: asyncio.Task | None = None
    # Set by stop() so the watchdog exits cleanly instead of trying to
    # bring Chrome back up during shutdown.
    _stopping: bool = False
    # True while a job's operator-Chrome-profile tarball is installed
    # in this lane's user-data-dir. Set by use_profile(), cleared by
    # restore_default_profile(). Used as the idempotency flag so a
    # crashed cleanup can't leave the lane permanently rebadged.
    _profile_swap_active: bool = False
    # Name of the operator-set "ambient" default profile currently
    # installed in this lane's user-data-dir, if any. Set by
    # set_ambient_profile(), cleared by clear_ambient_profile().
    # Distinguished from _profile_swap_active because per-job swaps
    # layer ON TOP of the ambient (the .lane-default backup taken at
    # job start contains the ambient, so restore_default_profile()
    # brings the ambient back even though it doesn't know about it).
    _ambient_profile_name: str | None = None
    # Extra ``--load-extension`` paths that aren't sourced from the
    # current profile's ``Default/Extensions/`` dir -- typically
    # hub-managed extensions in ``/tmp/paprika-extensions/<slug>/``.
    # The worker mutates this list (via set_extra_extension_paths())
    # before each Chrome (re)start; _discover_loadable_extensions()
    # appends these to the profile-discovered set. Stored as a list
    # of absolute path strings; the lane doesn't validate them
    # (no manifest scan etc.) because the worker side already did.
    _extra_extension_paths: list[str] = field(default_factory=list)

    @property
    def novnc_url(self) -> str:
        # vnc_lite.html (lite UI): debian-bookworm 版の vnc.html は
        # ui.js:addClipboardHandlers の DOM 要素 null 参照バグを抱えている。
        # vnc_lite.html はクリップボード機能なしの軽量版で、autoconnect/
        # resize/reconnect の query は同じく効く。
        return f"http://{self.public_host}:{self.novnc_port}/vnc_lite.html"

    async def start(self) -> None:
        """Spawn Xvfb, fluxbox, x11vnc, websockify, Chrome for this lane."""
        env = os.environ.copy()
        env["DISPLAY"] = f":{self.display_num}"
        self._env = env

        # stdout silenced; stderr inherits parent (so errors show in docker logs)
        OUT = subprocess.DEVNULL

        # 1) Xvfb -----------------------------------------------------------
        # -ac disables X access control. Safe inside the worker container
        # (everything is root-local, no external X clients), and it means
        # x11vnc can attach without any cookie/xauth dance at all.
        #
        # Clean up any stale lock / socket from a previous crashed run --
        # otherwise Xvfb exits with "Server is already active for display N"
        # and Docker's restart-loop gets stuck forever.
        for stale in (
            f"/tmp/.X{self.display_num}-lock",
            f"/tmp/.X11-unix/X{self.display_num}",
        ):
            try:
                os.remove(stale)
            except FileNotFoundError:
                pass
            except OSError as e:
                _log(self.lane_idx, f"warn: could not remove {stale}: {e}")
        _log(self.lane_idx, f"starting Xvfb :{self.display_num}")
        self.processes.append(
            subprocess.Popen(
                ["Xvfb", f":{self.display_num}", "-screen", "0", "1920x1080x24", "-ac"],
                stdout=OUT,
            )
        )
        lock = f"/tmp/.X{self.display_num}-lock"
        if not await _wait_path(lock, timeout=8.0):
            raise RuntimeError(
                f"lane {self.lane_idx}: Xvfb :{self.display_num} failed to create lock {lock}"
            )

        # 2) Fluxbox(任意の WM)---------------------------------------------
        self.processes.append(
            subprocess.Popen(
                ["fluxbox"],
                env=env,
                stdout=OUT,
            )
        )
        await asyncio.sleep(0.3)

        # 3) x11vnc ----------------------------------------------------------
        # No -auth flag: Xvfb -ac means the display has no access control,
        # so x11vnc can connect without an MIT-MAGIC-COOKIE-1 cookie. We had
        # tried -auth guess earlier, but that makes x11vnc exec the `xauth`
        # CLI (which isn't installed in the worker image), so it crashed
        # with "xauth: not found" before binding the VNC port.
        _log(self.lane_idx, f"starting x11vnc display=:{self.display_num} port={self.vnc_port}")
        self.processes.append(
            subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    f":{self.display_num}",
                    "-nopw",
                    "-forever",
                    "-shared",
                    "-rfbport",
                    str(self.vnc_port),
                    # Belt-and-suspenders: default is "both" already but make
                    # bidirectional clipboard explicit so a stray distro flag
                    # can't silently break the paprika-vnc-lite clipboard panel.
                    # -nosel disables PRIMARY selection sync (Chrome only uses
                    # CLIPBOARD, and PRIMARY adds noise on every mouse drag).
                    "-noprimary",
                    "-nosetprimary",
                    "-quiet",
                ],
                stdout=OUT,
            )
        )
        if not await _wait_port("127.0.0.1", self.vnc_port, timeout=8.0):
            raise RuntimeError(f"lane {self.lane_idx}: x11vnc failed to bind :{self.vnc_port}")

        # 4) websockify (noVNC) ---------------------------------------------
        _log(self.lane_idx, f"starting websockify :{self.novnc_port} -> :{self.vnc_port}")
        self.processes.append(
            subprocess.Popen(
                [
                    "websockify",
                    "--web=/usr/share/novnc",
                    str(self.novnc_port),
                    f"localhost:{self.vnc_port}",
                ],
                stdout=OUT,
            )
        )
        if not await _wait_port("127.0.0.1", self.novnc_port, timeout=8.0):
            raise RuntimeError(
                f"lane {self.lane_idx}: websockify failed to bind :{self.novnc_port}"
            )

        # 5) Chrome ---------------------------------------------------------
        await self._spawn_chrome()
        # 6) Watchdog -------------------------------------------------------
        # If the user clicks the X button on the Chrome window from the
        # noVNC viewer, Chrome exits and the lane becomes unusable. The
        # watchdog notices that and brings Chrome back up so the next job
        # has a working browser. Any job that was mid-flight will fail
        # (its CDP connection is gone), but the lane itself self-heals.
        self._watchdog_task = asyncio.create_task(self._chrome_watchdog())
        _log(self.lane_idx, f"READY  chrome=:{self.chrome_port}  noVNC={self.novnc_url}")

    async def _spawn_chrome(self) -> None:
        """Start (or restart) Chrome on this lane's display."""
        # Suppress the "Chrome didn't shut down correctly -- restore tabs?"
        # bubble that pops up after an unclean exit. Chrome decides whether
        # to show it by reading <user-data-dir>/Default/Preferences:
        # exit_type == "Crashed" triggers the prompt. Flip it back to
        # "Normal" before launching so the new instance starts clean.
        self._mark_prefs_clean()
        _log(self.lane_idx, f"starting Chrome remote-debugging :{self.chrome_port}")
        chrome_args = [
            "google-chrome",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            # Hides Chrome's "unsupported command-line flag: --no-sandbox"
            # infobar and similar automation warnings.
            "--test-type",
            "--disable-features=Translate,OptimizationHints",
            # Also suppresses the session-restore bubble even when our
            # prefs patch above misses an edge case.
            "--disable-session-crashed-bubble",
            "--restore-last-session=false",
            f"--remote-debugging-port={self.chrome_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir=/tmp/chrome-lane-{self.lane_idx}",
            "--window-size=1920,1080",
            "--start-maximized",
        ]
        # Opt-in physical block of new-tab creation at the WebContents
        # layer. kBlockNewWebContents (Chromium internal flag) makes
        # WebContentsImpl::AddNewContents refuse every new tab/window
        # request before any UI is shown. Effect:
        #   * window.open() returns null instead of opening a popup
        #   * <a target="_blank"> clicks are silently dropped (no nav)
        # Useful for sites whose popups are pure ads. Breaks sites that
        # rely on new-window navigation. The CDP-level TAB_KILL hook in
        # agent_runner is a softer alternative that lets navigation
        # through via same-origin redirect.
        if os.environ.get("CHROME_BLOCK_NEW_TABS", "0") not in ("0", "false", "no"):
            chrome_args.append("--block-new-web-contents")
            _log(self.lane_idx, "  --block-new-web-contents (CHROME_BLOCK_NEW_TABS=1)")
        # Auto-load any operator-uploaded extensions present in the
        # profile's Default/Extensions/ dir. We pass them explicitly
        # via --load-extension because Chrome's preference verifier
        # detects "this profile moved between installs" and disables
        # extensions registered via Preferences. --load-extension
        # bypasses the verifier (Chrome treats them as developer-
        # mode unpacked). The manifest's "key" field (preserved
        # from the original CRX) keeps the same extension ID, so
        # storage / state keyed by ID still matches.
        ext_paths = self._discover_loadable_extensions()
        if ext_paths:
            chrome_args.append("--load-extension=" + ",".join(ext_paths))
            _log(self.lane_idx, f"  --load-extension: {len(ext_paths)} extension(s)")
        chrome_args.append("about:blank")
        self._chrome_proc = subprocess.Popen(
            chrome_args,
            env=self._env,
            stdout=subprocess.DEVNULL,
        )
        ok = await _wait_http(
            f"http://localhost:{self.chrome_port}/json/version",
            timeout=30.0,
        )
        if not ok:
            raise RuntimeError(
                f"lane {self.lane_idx}: Chrome :{self.chrome_port} "
                f"failed to respond on /json/version"
            )

    def set_extra_extension_paths(self, paths: list[str]) -> None:
        """Replace the lane's hub-managed extension path list. The
        worker calls this with the paths returned from
        ``WorkerAgent.loaded_extension_paths()`` before each Chrome
        (re)start, so newly-uploaded extensions become active on the
        next Chrome bounce without any per-lane plumbing.
        """
        # Defensive copy + drop entries whose manifest disappeared on
        # disk since the worker enumerated them. Lane is also called
        # from the watchdog restart path, so we don't want a deleted
        # extension to fail Chrome startup with an "invalid path".
        clean: list[str] = []
        for p in paths or []:
            try:
                if Path(p, "manifest.json").exists():
                    clean.append(str(p))
            except Exception:
                continue
        self._extra_extension_paths = clean

    def _discover_loadable_extensions(self) -> list[str]:
        """Enumerate extensions to pass to ``--load-extension``.

        Two sources are combined:

        1) Profile-local extensions discovered under the lane's
           user-data-dir at::

               /tmp/chrome-lane-N/Default/Extensions/<id>/<version>/

           For each ``<id>`` we pick the lexicographically-highest
           ``<version>`` subdir that contains a parseable
           ``manifest.json``. Chrome stores versions like
           ``1.2.3_0`` which sorts correctly for this purpose
           (within an extension; we don't compare across).
           ``Temp/`` is Chrome's scratch dir for in-flight updates --
           skipped.

        2) Hub-managed extensions whose paths were pushed in via
           ``set_extra_extension_paths()`` (typically
           ``/tmp/paprika-extensions/<slug>/`` populated by the
           worker's hub-fetch on connect).

        Returns an empty list when neither source has anything
        (= caller skips the --load-extension flag entirely).
        """
        import json

        paths: list[str] = []
        # Built-in Paprika Agent extension (fixed; shipped in the repo,
        # not operator-uploaded). Always loaded first so the worker can
        # reach Chrome capabilities CDP can't (genuine page zoom, ...).
        # Path is relative to this module: server/worker/lanes.py ->
        # server/web/extensions/paprika-agent.
        try:
            agent_dir = (
                Path(__file__).resolve().parents[1]
                / "web" / "extensions" / "paprika-agent"
            )
            if (agent_dir / "manifest.json").exists():
                paths.append(str(agent_dir))
        except Exception:
            pass
        ext_root = Path(f"/tmp/chrome-lane-{self.lane_idx}/Default/Extensions")
        if ext_root.exists():
            for ext_id_dir in sorted(ext_root.iterdir()):
                if not ext_id_dir.is_dir() or ext_id_dir.name == "Temp":
                    continue
                # Iterate versions newest-first (descending) and stop
                # at the first one with a readable manifest.
                for ver_dir in sorted(ext_id_dir.iterdir(), reverse=True):
                    if not ver_dir.is_dir():
                        continue
                    manifest = ver_dir / "manifest.json"
                    if not manifest.exists():
                        continue
                    try:
                        json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
                    except Exception:
                        continue
                    paths.append(str(ver_dir))
                    break
        # Hub-managed extensions are appended AFTER profile-local ones
        # so an ID collision (operator uploaded the same extension at
        # both locations) lets the hub-managed copy override -- Chrome
        # loads in argument order, last wins for storage namespacing.
        for p in self._extra_extension_paths:
            if p not in paths:
                paths.append(p)
        return paths

    def _mark_prefs_clean(self) -> None:
        prefs = Path(f"/tmp/chrome-lane-{self.lane_idx}/Default/Preferences")
        if not prefs.exists():
            return
        try:
            data = json.loads(prefs.read_text())
            profile = data.get("profile")
            if isinstance(profile, dict):
                profile["exit_type"] = "Normal"
                profile["exited_cleanly"] = True
                prefs.write_text(json.dumps(data))
        except Exception as e:
            _log(self.lane_idx, f"warn: could not sanitize Preferences: {e}")

    async def _chrome_watchdog(self) -> None:
        """Bring Chrome back up if it exits (user closed the window etc.)."""
        backoff = 1.0
        try:
            while not self._stopping:
                await asyncio.sleep(2.0)
                proc = self._chrome_proc
                if proc is None or proc.poll() is None:
                    continue  # still alive
                code = proc.returncode
                _log(
                    self.lane_idx,
                    f"Chrome :{self.chrome_port} exited (code={code}); "
                    f"respawning in {backoff:.0f}s",
                )
                await asyncio.sleep(backoff)
                if self._stopping:
                    return
                try:
                    await self._spawn_chrome()
                    _log(self.lane_idx, f"Chrome :{self.chrome_port} respawned")
                    backoff = 1.0
                except Exception as e:
                    _log(self.lane_idx, f"Chrome respawn failed: {e}; will retry")
                    backoff = min(backoff * 2, 30.0)
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
        self._stopping = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._chrome_proc is not None:
            try:
                self._chrome_proc.kill()
            except Exception:
                pass
            self._chrome_proc = None
        for p in self.processes:
            try:
                p.kill()
            except Exception:
                pass
        self.processes.clear()

    # ------ per-job profile swap ---------------------------------------
    # When a job sets ``options.use_profile``, the worker downloads the
    # uploaded tarball, extracts it to a temp dir, and calls
    # ``use_profile()`` on the lane. The lane stops its Chrome, moves
    # its current ``/tmp/chrome-lane-N`` aside, swaps the extracted
    # profile in, restarts Chrome on the same port. The original lane
    # profile is restored on ``restore_default_profile()`` (called from
    # the job's finally block + on session end).
    #
    # Why this is safe: Chrome is killed before we touch the dir, so
    # the on-disk profile lock is released. The watchdog is paused
    # while the swap is in flight (otherwise it would try to respawn
    # Chrome mid-rename and race us).

    async def use_profile(self, profile_dir: Path) -> None:
        """Replace this lane's user-data-dir with ``profile_dir`` and
        restart Chrome. The original lane state is kept aside and
        restored by ``restore_default_profile()``. Idempotent: a
        second call is a no-op if a swap is already active.
        """
        if self._profile_swap_active:
            return
        lane_dir = Path(f"/tmp/chrome-lane-{self.lane_idx}")
        backup_dir = Path(f"/tmp/chrome-lane-{self.lane_idx}.lane-default")
        _log(self.lane_idx, f"profile swap: installing {profile_dir} into {lane_dir}")
        # Pause the watchdog so it doesn't fight us during the swap.
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        # Stop Chrome.
        if self._chrome_proc is not None:
            try:
                self._chrome_proc.kill()
                self._chrome_proc.wait(timeout=5)
            except Exception:
                pass
            self._chrome_proc = None
        # Move the lane's current profile aside. If a previous swap
        # crashed mid-way and left a stale .lane-default, remove it
        # first -- the running lane_dir is the authoritative state.
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        if lane_dir.exists():
            try:
                lane_dir.rename(backup_dir)
            except OSError:
                # Cross-device or race. Copy then remove as fallback.
                shutil.copytree(lane_dir, backup_dir, dirs_exist_ok=True)
                shutil.rmtree(lane_dir, ignore_errors=True)
        # Move the operator's extracted profile into place.
        try:
            profile_dir.rename(lane_dir)
        except OSError:
            shutil.copytree(profile_dir, lane_dir, dirs_exist_ok=True)
            shutil.rmtree(profile_dir, ignore_errors=True)
        # Re-spawn Chrome + watchdog.
        # Spawn Chrome inside a try/finally so the watchdog ALWAYS
        # gets restarted, even when the immediate spawn fails. The
        # watchdog will retry the spawn on its 2-second loop, which
        # is the existing recovery mechanism. Previous code did
        # ``await self._spawn_chrome(); start_watchdog`` -- if the
        # spawn raised, the watchdog never started and the lane died
        # permanently (Chrome zombies + lane_dir empty). Caused all
        # production workers to lose lane 0 after the operator's
        # first default-profile change.
        try:
            await self._spawn_chrome()
        except Exception as e:
            _log(self.lane_idx, f"profile swap spawn failed: {e!r}; watchdog will retry")
        self._watchdog_task = asyncio.create_task(self._chrome_watchdog())
        self._profile_swap_active = True
        _log(self.lane_idx, "profile swap: Chrome up with operator profile")

    # ------ ambient (default) profile install ---------------------------
    # set_ambient_profile / clear_ambient_profile work on the SAME lane
    # user-data-dir slot as use_profile / restore_default_profile but
    # are semantically different: per-job swaps come and go, ambient
    # is "what the lane looks like when not running a job". noVNC
    # viewers see the ambient on idle lanes. The two layers compose
    # cleanly because restore_default_profile() restores whatever was
    # in lane_dir BEFORE the per-job swap -- if that was the ambient,
    # the lane goes back to the ambient automatically.
    #
    # Refuses to operate when a per-job swap is in flight (would
    # corrupt the .lane-default backup). The worker is expected to
    # retry on the next lane release.

    async def set_ambient_profile(
        self,
        profile_dir: Path,
        profile_name: str,
    ) -> bool:
        """Install ``profile_dir`` as the lane's ambient (= default)
        Chrome user-data-dir. Returns True on success, False when
        the lane was busy (per-job swap active). The caller can
        retry after the next ``restore_default_profile()``.

        Same dance as ``use_profile()`` but doesn't touch
        ``_profile_swap_active``; the per-job swap layer is
        orthogonal.
        """
        if self._profile_swap_active:
            return False
        # Idempotent: same name already installed -> no-op.
        if self._ambient_profile_name == profile_name:
            return True
        lane_dir = Path(f"/tmp/chrome-lane-{self.lane_idx}")
        _log(self.lane_idx, f"ambient profile install: {profile_name!r} -> {lane_dir}")
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if self._chrome_proc is not None:
            try:
                self._chrome_proc.kill()
                self._chrome_proc.wait(timeout=5)
            except Exception:
                pass
            self._chrome_proc = None
        # Replace lane_dir's content. Unlike use_profile() we do NOT
        # back up the previous content -- the operator explicitly
        # asked for this profile to be the default; the previous
        # ambient (or empty lane state) is discarded. clear_ambient_-
        # profile() resets to empty.
        if lane_dir.exists():
            shutil.rmtree(lane_dir, ignore_errors=True)
        try:
            shutil.copytree(profile_dir, lane_dir)
        except Exception as e:
            _log(self.lane_idx, f"ambient profile copy failed: {e!r}")
            lane_dir.mkdir(parents=True, exist_ok=True)
            self._ambient_profile_name = None
            # Still bring Chrome back up so the lane is usable.
            await self._spawn_chrome()
            self._watchdog_task = asyncio.create_task(self._chrome_watchdog())
            return False
        # Spawn Chrome inside a try/finally so the watchdog ALWAYS
        # gets restarted, even when the immediate spawn fails. The
        # watchdog will retry the spawn on its 2-second loop, which
        # is the existing recovery mechanism. Previous code did
        # ``await self._spawn_chrome(); start_watchdog`` -- if the
        # spawn raised, the watchdog never started and the lane died
        # permanently (Chrome zombies + lane_dir empty). Caused all
        # production workers to lose lane 0 after the operator's
        # first default-profile change.
        try:
            await self._spawn_chrome()
        except Exception as e:
            _log(self.lane_idx, f"profile swap spawn failed: {e!r}; watchdog will retry")
        self._watchdog_task = asyncio.create_task(self._chrome_watchdog())
        self._ambient_profile_name = profile_name
        _log(self.lane_idx, f"ambient profile {profile_name!r} live (noVNC viewers see it now)")
        return True

    async def clear_ambient_profile(self) -> bool:
        """Revert the lane to an empty stock user-data-dir. Returns
        True on success / no-op, False when blocked by an in-flight
        per-job swap.
        """
        if self._profile_swap_active:
            return False
        if self._ambient_profile_name is None:
            return True
        lane_dir = Path(f"/tmp/chrome-lane-{self.lane_idx}")
        _log(self.lane_idx, "ambient profile clear: reverting to lane stock")
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if self._chrome_proc is not None:
            try:
                self._chrome_proc.kill()
                self._chrome_proc.wait(timeout=5)
            except Exception:
                pass
            self._chrome_proc = None
        if lane_dir.exists():
            shutil.rmtree(lane_dir, ignore_errors=True)
        lane_dir.mkdir(parents=True, exist_ok=True)
        # Spawn Chrome inside a try/finally so the watchdog ALWAYS
        # gets restarted, even when the immediate spawn fails. The
        # watchdog will retry the spawn on its 2-second loop, which
        # is the existing recovery mechanism. Previous code did
        # ``await self._spawn_chrome(); start_watchdog`` -- if the
        # spawn raised, the watchdog never started and the lane died
        # permanently (Chrome zombies + lane_dir empty). Caused all
        # production workers to lose lane 0 after the operator's
        # first default-profile change.
        try:
            await self._spawn_chrome()
        except Exception as e:
            _log(self.lane_idx, f"profile swap spawn failed: {e!r}; watchdog will retry")
        self._watchdog_task = asyncio.create_task(self._chrome_watchdog())
        self._ambient_profile_name = None
        return True

    async def restore_default_profile(self) -> None:
        """Undo a prior ``use_profile()`` swap. No-op when no swap is
        active. Called from the job's finally block; also fires
        defensively on session end / lane teardown so a crashed job
        can't leave the lane stuck on the operator's profile.
        """
        if not self._profile_swap_active:
            return
        lane_dir = Path(f"/tmp/chrome-lane-{self.lane_idx}")
        backup_dir = Path(f"/tmp/chrome-lane-{self.lane_idx}.lane-default")
        _log(self.lane_idx, "profile swap: restoring lane default")
        # Pause watchdog + stop Chrome (same dance as use_profile).
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if self._chrome_proc is not None:
            try:
                self._chrome_proc.kill()
                self._chrome_proc.wait(timeout=5)
            except Exception:
                pass
            self._chrome_proc = None
        # Discard the operator profile -- any cookies / state set
        # during the job stay confined to that scratch dir.
        if lane_dir.exists():
            shutil.rmtree(lane_dir, ignore_errors=True)
        # Move the backup back into place. If there's no backup
        # (first use of the lane or a corrupt state) leave an empty
        # dir; Chrome will rebuild defaults on startup.
        if backup_dir.exists():
            try:
                backup_dir.rename(lane_dir)
            except OSError:
                shutil.copytree(backup_dir, lane_dir, dirs_exist_ok=True)
                shutil.rmtree(backup_dir, ignore_errors=True)
        else:
            lane_dir.mkdir(parents=True, exist_ok=True)
        # Spawn Chrome inside a try/finally so the watchdog ALWAYS
        # gets restarted, even when the immediate spawn fails. The
        # watchdog will retry the spawn on its 2-second loop, which
        # is the existing recovery mechanism. Previous code did
        # ``await self._spawn_chrome(); start_watchdog`` -- if the
        # spawn raised, the watchdog never started and the lane died
        # permanently (Chrome zombies + lane_dir empty). Caused all
        # production workers to lose lane 0 after the operator's
        # first default-profile change.
        try:
            await self._spawn_chrome()
        except Exception as e:
            _log(self.lane_idx, f"profile swap spawn failed: {e!r}; watchdog will retry")
        self._watchdog_task = asyncio.create_task(self._chrome_watchdog())
        self._profile_swap_active = False
        _log(self.lane_idx, "profile swap: lane default restored")

    async def screenshot(
        self,
        *,
        max_width: int | None = 480,
        quality: int = 5,
        timeout: float = 5.0,
    ) -> bytes:
        """Grab one frame of this lane's Xvfb display and return JPEG bytes.

        Uses ffmpeg's x11grab demuxer (already installed in the worker
        image). Connects to display ":<display_num>". Always single-frame
        (no streaming) and optionally downscaled, so it stays cheap enough
        to call every few seconds per lane.
        """
        vf_filters: list[str] = []
        if max_width is not None and max_width > 0:
            # Force even dimensions to keep libjpeg happy (-2 = round to /2).
            vf_filters.append(f"scale={int(max_width)}:-2")
        # Clamp quality to ffmpeg's valid mjpeg range.
        q = max(2, min(31, int(quality)))
        cmd: list[str] = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "x11grab",
            "-video_size",
            "1920x1080",
            "-i",
            f":{self.display_num}",
            "-frames:v",
            "1",
        ]
        if vf_filters:
            cmd += ["-vf", ",".join(vf_filters)]
        cmd += [
            "-f",
            "image2",
            "-vcodec",
            "mjpeg",
            "-q:v",
            str(q),
            "pipe:1",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            raise RuntimeError(f"lane {self.lane_idx}: ffmpeg x11grab timed out after {timeout}s")
        if proc.returncode != 0:
            raise RuntimeError(
                f"lane {self.lane_idx}: ffmpeg exited {proc.returncode}: "
                f"{stderr.decode(errors='replace').strip()[:200]}"
            )
        if not stdout:
            raise RuntimeError(f"lane {self.lane_idx}: ffmpeg produced no output")
        return stdout


class LanePool:
    """A fixed pool of `Lane`s. Pre-spawned on `start_all`."""

    def __init__(
        self,
        n: int,
        public_host: str = "localhost",
        base_novnc_port: int = 6080,
    ) -> None:
        # One-time migration of pre-rename profile directories. No-op once
        # the new dirs already exist; remove this call one release after
        # the Slot -> Lane rename ships.
        _migrate_user_data_dirs(n)
        self.lanes = [
            Lane(
                lane_idx=i,
                display_num=100 + i,
                chrome_port=9223 + i,
                vnc_port=5901 + i,
                novnc_port=base_novnc_port + i,
                public_host=public_host,
            )
            for i in range(n)
        ]
        self._lock = asyncio.Lock()

    async def start_all(self) -> None:
        for s in self.lanes:
            await s.start()
        log.info(
            "[pool] started %d lane(s); noVNC ports: %s",
            len(self.lanes),
            [s.novnc_port for s in self.lanes],
        )

    def stop_all(self) -> None:
        for s in self.lanes:
            s.stop()

    async def acquire(self, lane_hint: int | None = None) -> Lane | None:
        """If `lane_hint` is None: return any free lane (or None).
        If `lane_hint` is set: wait until THAT lane is free, then take it.
        Returns None for hint pointing outside range.
        """
        if lane_hint is not None:
            if not (0 <= lane_hint < len(self.lanes)):
                return None
            target = self.lanes[lane_hint]
            while True:
                async with self._lock:
                    if not target.busy:
                        target.busy = True
                        return target
                await asyncio.sleep(0.5)
        async with self._lock:
            for s in self.lanes:
                if not s.busy:
                    s.busy = True
                    return s
            return None

    def release(self, lane: Lane) -> None:
        lane.busy = False

    def stats(self) -> dict:
        return {
            "total": len(self.lanes),
            "busy": sum(1 for s in self.lanes if s.busy),
        }
