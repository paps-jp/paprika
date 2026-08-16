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
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def _log(lane_idx: int, msg: str) -> None:
    log.info("[lane %d] %s", lane_idx, msg)


# --------------------------------------------------------------------------
# Chrome lane root
# --------------------------------------------------------------------------
# Chrome's user-data-dir is the worker's dominant *disk* writer. Measured on
# loft 2026-08-04: with browsing active the node's 20 CTs pushed 47 MB/s at
# the LVM-thin pool; with 42 yt-dlp downloads still running but browsing idle,
# 0.6 MB/s. The video downloads were already on the node tmpfs
# (server/worker/scratch_pool.py) -- what was left was essentially all Chrome.
#
# PAPRIKA_CHROME_LANE_ROOT points the lane dirs at a second node tmpfs
# bind-mounted into the CT, so those bytes are charged to host RAM instead of
# the thin pool. Unset (or not a real tmpfs) keeps the historical /tmp
# behaviour, so this is inert until the infrastructure exists.
# See docs/ramdisk-chrome-lane.md.
#
# OWNER SCOPING: the mount is ONE tmpfs shared by every worker CT on the node,
# exactly like the download pool, and each worker owns
# ``<mount>/<worker_id>/``. It is deliberately NOT a per-CT mount at
# ``/ram/chrome/<CTID>``: PVE refuses to clone a CT that has a bind mountpoint
# at all (API2/LXC.pm: "unable to clone mountpoint (type bind)"), so a path
# carrying the CTID could never be produced by cloning -- while a path that is
# byte-identical on every CT is copied around freely and stays correct.
# worker_id is derived from the CT's LAN IP ("w50150"), so a cloned CT lands
# on a fresh directory with no coordination.
#
# INVARIANT: a lane dir and its ``.lane-default`` backup MUST live under the
# same mount. use_profile() renames one onto the other; a cross-device rename
# degrades into a ~160MB copytree on every profile swap, which would make this
# change a pessimisation rather than an optimisation. Both come from the
# helpers below for exactly that reason -- never rebuild these paths by hand.

_DEFAULT_CHROME_LANE_ROOT = Path("/tmp")

#: Resolved once per process (the mount can't appear mid-run: docker binds are
#: rprivate, so a host-side mount after container start never reaches us).
_chrome_lane_root: Path | None = None
_chrome_lane_owner: str | None = None


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _owner_id() -> str:
    if _chrome_lane_owner:
        return _chrome_lane_owner
    # Defensive only: __main__ calls init_chrome_lane_root() before any lane
    # spawns. Imported lazily because agent imports this module.
    try:
        from server.worker.agent import default_worker_id

        return default_worker_id()
    except Exception:
        return ""


def _resolve_chrome_lane_root() -> Path:
    from server.worker import scratch_pool as _sp

    raw = (os.environ.get("PAPRIKA_CHROME_LANE_ROOT") or "").strip()
    if not raw or raw == str(_DEFAULT_CHROME_LANE_ROOT):
        return _DEFAULT_CHROME_LANE_ROOT
    mount = Path(raw)
    # Strict tmpfs check, same rationale as scratch_pool: when the mp isn't
    # set up docker silently creates a plain directory on the CT rootfs, and
    # we would write Chrome's profile churn to the exact device this exists
    # to spare while the logs claim success.
    if not _sp._is_tmpfs(mount):
        # INFO, not WARNING: this is the expected state on every node that has
        # not been through scripts/setup-chrome-ramdisk.sh yet, and the default
        # points at the mount so a node self-activates once the mp lands. The
        # genuinely unexpected failures below stay at WARNING.
        log.info(
            "[pool] chrome lane root %s is not a tmpfs -- using %s "
            "(chrome profiles stay on the CT disk)",
            mount, _DEFAULT_CHROME_LANE_ROOT,
        )
        return _DEFAULT_CHROME_LANE_ROOT
    owner = _owner_id()
    if not owner:
        log.warning(
            "[pool] chrome lane root %s: no worker id to scope by -- falling "
            "back to %s (an unscoped dir would collide with the other CTs "
            "sharing this ramdisk)",
            mount, _DEFAULT_CHROME_LANE_ROOT,
        )
        return _DEFAULT_CHROME_LANE_ROOT
    root = mount / owner
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning(
            "[pool] chrome lane root %s not usable (%s) -- falling back to %s",
            root, e, _DEFAULT_CHROME_LANE_ROOT,
        )
        return _DEFAULT_CHROME_LANE_ROOT
    # Local _env_int, not scratch_pool's: that one maps 0 back to the default,
    # so the guard could never be switched off. 0 here means "no guard", which
    # is a legitimate choice on a small ramdisk where 256MB is a big slice.
    min_free_mb = _env_int("PAPRIKA_CHROME_ROOT_MIN_FREE_MB", 256)
    try:
        st = os.statvfs(str(root))
        free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
    except Exception:
        free_mb = 0
    if free_mb < min_free_mb:
        # Chrome cannot degrade gracefully on a full filesystem the way the
        # download path can (scratch_pool just returns None and uses disk) --
        # it dies and takes the lane with it. Refuse up front instead.
        log.warning(
            "[pool] chrome lane root %s has %d MB free (< %d) -- falling back to %s",
            root, free_mb, min_free_mb, _DEFAULT_CHROME_LANE_ROOT,
        )
        return _DEFAULT_CHROME_LANE_ROOT
    return root


def init_chrome_lane_root(worker_id: str) -> Path:
    """Bind the lane root to this worker's owner directory.

    Called from ``server/__main__.py`` once worker_id is known and before any
    lane spawns, so the decision (and the reason for any fallback) lands in
    the startup log rather than being inferred from a lane minutes later.
    """
    global _chrome_lane_root, _chrome_lane_owner
    _chrome_lane_owner = (worker_id or "").strip() or None
    _chrome_lane_root = None
    return chrome_lane_root()


def chrome_lane_root() -> Path:
    """Root directory holding every lane's Chrome user-data-dir."""
    global _chrome_lane_root
    if _chrome_lane_root is None:
        _chrome_lane_root = _resolve_chrome_lane_root()
    return _chrome_lane_root


def chrome_on_ramdisk() -> bool:
    """True when the lane dirs live on the node tmpfs rather than /tmp."""
    return chrome_lane_root() != _DEFAULT_CHROME_LANE_ROOT


def lane_user_data_dir(lane_idx: int) -> Path:
    """This lane's Chrome user-data-dir."""
    return chrome_lane_root() / f"chrome-lane-{lane_idx}"


def lane_backup_dir(lane_idx: int) -> Path:
    """Where use_profile() parks the lane's own profile during a swap."""
    return chrome_lane_root() / f"chrome-lane-{lane_idx}.lane-default"


def lane_tmp_dir(lane_idx: int) -> Path:
    """This lane's TMPDIR -- where Chrome puts its *scratch*, as opposed to
    its profile.

    Separate from the user-data-dir on purpose: this one is disposable and
    gets wiped on every spawn, while the profile carries login state.
    """
    return chrome_lane_root() / f"chrome-lane-{lane_idx}.tmp"


def chrome_lane_tmp_roots() -> list[Path]:
    """Every lane TMPDIR this worker owns, for the periodic sweeper.

    Empty unless the lane root is a real ramdisk, because that is the only
    case where Chrome's TMPDIR gets redirected at all. The sweeper needs
    these because wiping at spawn only bounds the leak per Chrome lifetime,
    and a lane that runs for days without a respawn would otherwise
    accumulate on the ramdisk -- where it is *worse* than on disk: the mount
    is shared by every CT on the node, has no per-directory quota, and a
    full mount takes out every lane on it at once.
    """
    if not chrome_on_ramdisk():
        return []
    try:
        return [
            p for p in chrome_lane_root().glob("chrome-lane-*.tmp")
            if p.is_dir()
        ]
    except OSError:
        return []


#: Touched by the sweeper every pass so the OTHER CTs on this node can tell a
#: live owner directory from one whose worker no longer exists.
_CHROME_OWNER_MARKER = ".paprika-owner"

#: Lane-root names are worker ids, and a worker that could not reach the hub at
#: startup derives a container-hash fallback id (``<hash>-<suffix>``) instead of
#: the LAN-IP form -- see server/worker/agent/workerid.py. Those are the ones
#: most likely to be abandoned (the container gets its real id on the next
#: start and never returns to this directory): 4.1 GB of them on one node's
#: ramdisk when this was written. Hence the dash.
_CHROME_OWNER_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")


def touch_chrome_owner() -> None:
    """Stamp our own lane root as live. Cheap, and the only positive proof a
    neighbouring CT gets: the profile trees are mutually visible on the shared
    ramdisk but the processes using them are not."""
    if not chrome_on_ramdisk():
        return
    try:
        (chrome_lane_root() / _CHROME_OWNER_MARKER).touch()
    except OSError:
        pass


def _chrome_owner_last_touch(d: Path) -> float:
    """Newest sign of life under a lane-root directory.

    The directory's OWN mtime is not enough: it only moves when a lane dir is
    created or removed, so a worker that has been running the same two lanes
    for a week looks a week idle. Chrome writes inside the lane dirs
    continuously, so the newest depth-1 mtime is the real signal -- plus the
    owner marker for workers new enough to leave one.
    """
    newest = 0.0
    try:
        newest = d.stat().st_mtime
    except OSError:
        return 0.0
    try:
        for child in d.iterdir():
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return newest


def sweep_chrome_orphans(worker_id: str, min_age_s: float) -> tuple[int, int]:
    """Remove lane roots belonging to workers that no longer exist.

    ``/ram/chrome`` is one tmpfs per NODE, subdivided by worker id
    (docs/ramdisk-chrome-lane.md), so a decommissioned or renamed worker
    leaves a multi-GB Chrome profile tree that no surviving process is allowed
    to touch -- eight such trees were sitting across the fleet on 2026-08-16,
    the oldest 76h. This is the pool's :func:`scratch_pool.sweep_orphans` for
    the Chrome half.

    Conservative by construction: only sibling directories whose name looks
    like a worker id, only when NOTHING under them has been written for
    ``min_age_s`` (12h by default -- an idle Chrome still writes its profile
    every few minutes), and never our own. Losing this race would cost a
    neighbour its logged-in profile, which the hub can re-push but the
    operator should never have to notice.

    Returns ``(removed, freed_bytes)``. ``min_age_s <= 0`` disables it.
    """
    if min_age_s <= 0 or not chrome_on_ramdisk():
        return (0, 0)
    root = chrome_lane_root()
    mount = root.parent
    now = time.time()
    removed = 0
    freed = 0
    try:
        entries = list(mount.iterdir())
    except OSError:
        return (0, 0)
    for d in entries:
        if d.name == root.name or d.name.startswith("."):
            continue
        if not _CHROME_OWNER_RE.match(d.name):
            continue
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        last = _chrome_owner_last_touch(d)
        if not last or now - last < min_age_s:
            continue
        size = 0
        try:
            for dirpath, _dirs, files in os.walk(str(d)):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
        except OSError:
            pass
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
        freed += size
        log.info(
            "[pool] reclaimed orphan chrome lane root %s (idle %.1fh, %d MB)",
            d, (now - last) / 3600.0, size // (1024 * 1024),
        )
    return (removed, freed)


def chrome_live_socket_dirs() -> set[str]:
    """Temp directories a LIVE Chrome is currently using, as realpaths.

    Chrome puts its singleton socket in a temp dir of its own
    (``com.google.Chrome.XXXXXX/`` holding ``SingletonSocket`` +
    ``SingletonCookie``) and symlinks the profile's ``SingletonSocket`` at
    it. That symlink is the only reliable way to tell a live one from the
    thousands of leaked ones: the directory's mtime is its *creation* time,
    observed a full day stale on a running browser, so an age heuristic
    would happily delete the socket dir out from under a working lane.

    Scoped to this worker by construction -- chrome_lane_root() already ends
    in our worker_id, and the neighbouring CTs' lane dirs (visible on the
    shared ramdisk) point at *their* container's /tmp, which does not exist
    in ours.

    Realpaths rather than Paths so callers can compare against a realpath of
    their own: readlink alone returns whatever spelling the link carries.
    """
    out: set[str] = set()
    try:
        lane_dirs = list(chrome_lane_root().glob("chrome-lane-*"))
    except OSError:
        return out
    for d in lane_dirs:
        # SingletonSocket is the only one of the three that points at a
        # path; SingletonLock is "<host>-<pid>" and SingletonCookie is a
        # bare number.
        p = d / "SingletonSocket"
        try:
            if not p.is_symlink():
                continue
            target = os.path.dirname(os.path.realpath(str(p)))
        except OSError:
            continue
        if target and target != os.sep:
            out.add(target)
    return out


def chrome_lane_status_line() -> str:
    """One-line human summary for the startup log (mirrors scratch_pool)."""
    root = chrome_lane_root()
    if root == _DEFAULT_CHROME_LANE_ROOT:
        return (
            "chrome lane root: /tmp (no ramdisk) -- chrome profiles stay on "
            "the CT disk"
        )
    try:
        st = os.statvfs(str(root))
        total_mb = (st.f_blocks * st.f_frsize) // (1024 * 1024)
        free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
    except Exception:
        total_mb = free_mb = 0
    return (
        f"chrome lane root: {root} tmpfs {total_mb} MB ({free_mb} MB free)"
    )


def _migrate_user_data_dirs(n_lanes: int) -> None:
    """One-time rename of chrome-slot-{i} -> chrome-lane-{i}.

    Carries cookies / login state across the Slot -> Lane rename so users
    don't lose their saved sessions. Idempotent and safe to run on every
    worker boot -- a no-op once the rename has happened. Drop this helper
    one release after the rename ships.
    """
    # Legacy dirs only ever existed in /tmp. On a tmpfs root there is nothing
    # to migrate (and renaming across the two would be a cross-device copy of
    # a profile nobody has used since the Slot->Lane rename shipped).
    if chrome_lane_root() != _DEFAULT_CHROME_LANE_ROOT:
        return
    for i in range(n_lanes):
        old = Path(f"/tmp/chrome-slot-{i}")
        new = lane_user_data_dir(i)
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

    def _prepare_lane_tmpdir(self) -> None:
        """Point Chrome's TMPDIR at a lane-private dir on the ramdisk and
        empty it. Called from _spawn_chrome, i.e. always while this lane's
        Chrome is dead.

        Chrome's scratch -- ScopedTempDirs (``scoped_dir*``) and, because of
        --disable-dev-shm-usage below, its shm segments
        (``.com.google.Chrome.*``) -- goes to TMPDIR. Left at the default
        that is the container's /tmp on the CT's LVM-thin rootfs: exactly
        the write path docs/ramdisk-chrome-lane.md moved the profiles off,
        with the scratch left behind.

        It also never gets cleaned up, because Chrome only tidies these on a
        graceful exit and in this pipeline the parent SIGKILLs it on every
        lane swap, Xvfb restart and container SIGTERM. Measured across 11
        fleet CTs on 2026-08-05: 6.6-10.9 GB of container /tmp at 0-1d
        uptime on the six without the (hand-installed, daily) CT-side timer.

        A lane-private TMPDIR fixes both halves. The writes land on the node
        tmpfs, and ownership stops being ambiguous: the global /tmp mixes
        live and dead scratch from every lane with no way to tell them
        apart, whereas here Chrome is dead at this instant, so the whole
        directory is garbage by construction and can go without an age
        heuristic. That bound is what makes it safe to put on RAM at all.

        Inert unless the lane root is a real ramdisk -- on the default /tmp
        this would change behaviour for no gain, and the periodic sweep
        (_TMP_SWEEP_CHROME_PREFIXES) already covers that case.
        """
        if not chrome_on_ramdisk():
            return
        # Free-space gate, re-evaluated on every spawn. The root check in
        # _resolve_chrome_lane_root() runs once at startup and cannot see a
        # mount that filled up hours later -- and unlike the profile, this
        # scratch has a graceful fallback available (the CT disk), so there
        # is no reason to gamble the lane on it. This restores for Chrome
        # the property /ram/pdl has by design and /ram/chrome lacks: when
        # the pool is full, degrade to disk instead of dying. Matters most
        # right after deployment, when the mount is still sized for
        # profiles alone (1G/CT) and the operator has not re-capped it.
        min_free_mb = _env_int("PAPRIKA_CHROME_TMP_MIN_FREE_MB", 1024)
        if min_free_mb > 0:
            try:
                st = os.statvfs(str(chrome_lane_root()))
                free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
            except Exception:
                # Same breadth as _resolve_chrome_lane_root's probe: if we
                # cannot measure the mount we must not claim space on it.
                free_mb = 0
            if free_mb < min_free_mb:
                _log(
                    self.lane_idx,
                    f"lane tmpdir skipped: ramdisk has {free_mb} MB free "
                    f"(< {min_free_mb}); chrome scratch stays on the CT disk",
                )
                self._env.pop("TMPDIR", None)
                return
        tmpdir = lane_tmp_dir(self.lane_idx)
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
            tmpdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Never fatal: falling back to the inherited TMPDIR is the
            # pre-existing behaviour, and a lane that cannot start is a much
            # worse outcome than a lane that writes its scratch to disk.
            _log(self.lane_idx, f"lane tmpdir unavailable ({e}); using default TMPDIR")
            self._env.pop("TMPDIR", None)
            return
        self._env["TMPDIR"] = str(tmpdir)

    async def _spawn_chrome(self) -> None:
        """Start (or restart) Chrome on this lane's display."""
        # Suppress the "Chrome didn't shut down correctly -- restore tabs?"
        # bubble that pops up after an unclean exit. Chrome decides whether
        # to show it by reading <user-data-dir>/Default/Preferences:
        # exit_type == "Crashed" triggers the prompt. Flip it back to
        # "Normal" before launching so the new instance starts clean.
        self._mark_prefs_clean()
        self._clear_foreign_singleton_lock()
        self._prepare_lane_tmpdir()
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
            f"--user-data-dir={lane_user_data_dir(self.lane_idx)}",
            "--window-size=1920,1080",
            "--start-maximized",
        ]
        if chrome_on_ramdisk():
            # Chrome sizes its HTTP cache from the FREE SPACE it sees, so a
            # multi-GB ramdisk invites a multi-GB cache. On the CT disk that
            # was self-limiting (small rootfs); on a shared node ramdisk one
            # lane could crowd out every other CT's browser. Pin it -- there
            # is no per-directory quota on a tmpfs to fall back on.
            cache_mb = max(16, _env_int("PAPRIKA_CHROME_DISK_CACHE_MB", 128))
            chrome_args.append(f"--disk-cache-size={cache_mb * 1024 * 1024}")
        # Worker egress proxy (target-site plane). When PAPRIKA_WORKER_PROXY
        # is set, this lane's Chrome routes all its outbound traffic through
        # that proxy so sites see the proxy box's IP (e.g. a per-拠点 box) --
        # the worker-side half of IP-block avoidance. Loopback/LAN bypass so
        # noVNC/devtools/hub stay direct; WebRTC locked to proxied UDP only so
        # it can't leak the real egress IP. Unset = no-op (prod unchanged).
        # Shared with the fetch/yt-dlp paths so every egress surface on this
        # worker uses the SAME exit IP (one pick per process; see
        # core.fetcher._worker_egress_proxy for the consistency rationale).
        from core.fetcher import _worker_egress_proxy, _worker_proxy_bypass
        _egress_proxy = _worker_egress_proxy()
        if _egress_proxy:
            chrome_args += [
                f"--proxy-server={_egress_proxy}",
                f"--proxy-bypass-list={_worker_proxy_bypass()}",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ]
            _log(self.lane_idx, f"  --proxy-server={_egress_proxy} (egress proxy)")
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
            # Own session/process group so _kill_chrome_proc can SIGKILL
            # the whole tree. Without this, .kill() drops only the main
            # process and Chrome's children (renderers/zygotes/gpu) get
            # reparented to the container's PID 1 (the worker python,
            # which doesn't reap them) and accumulate as zombies.
            start_new_session=True,
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
        # NOTE: the built-in Paprika Agent extension is intentionally NOT
        # loaded here. Chrome 148 ignores --load-extension for unpacked
        # extensions, and the CDP Extensions.loadUnpacked command is only
        # available over --remote-debugging-pipe (we use
        # --remote-debugging-port for noVNC/nodriver), so it returns
        # "Method not available". No automation path loads it on this
        # Chrome build. The agent framework (extension + worker bridge +
        # /sessions/{id}/zoom) stays in the tree; page zoom currently
        # falls back to CDP Emulation.setPageScaleFactor (zoom-in works,
        # zoom-out doesn't). Revisit if a port-compatible load path
        # appears (Chrome change) or we invest in a CRX/policy install.

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

               <chrome-lane-root>/chrome-lane-N/Default/Extensions/<id>/<version>/

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
        # NOTE: the built-in Paprika Agent extension is NOT loaded here.
        # Chrome 148 ignores --load-extension for unpacked extensions, so
        # we load the agent over CDP (Extensions.loadUnpacked) after
        # Chrome starts instead -- see _load_agent_extension().
        ext_root = lane_user_data_dir(self.lane_idx) / "Default" / "Extensions"
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

    def _clear_foreign_singleton_lock(self) -> None:
        """Drop a ``SingletonLock`` left by a DIFFERENT container.

        Chrome refuses to open a profile whose ``SingletonLock`` symlink names
        a host other than the current one -- "The profile appears to be in use
        by another Google Chrome process (N) on another computer (HOST)" -- and
        it never expires. On the CT disk that could not happen: the lane dir
        died with the container that wrote it. On the node-shared ramdisk the
        dir OUTLIVES the container, and a container's hostname is its docker id,
        so **every ``docker compose up -d`` (recreate) leaves a lock Chrome will
        reject forever**. That wedged 9 workers on 2026-08-05 even after their
        ids were fixed, at ~40s per crash-restart cycle.

        Safe because we are the dir's owner: worker_id is derived from this
        CT's LAN IP (server/worker/agent/workerid.py), so no other live
        container shares this path. We only clear a FOREIGN host's lock -- our
        own (a crashed previous process in this same container) is left for
        Chrome, which handles that case itself.
        """
        lane_dir = lane_user_data_dir(self.lane_idx)
        try:
            host = socket.gethostname()
        except Exception:
            return
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = lane_dir / name
            try:
                if not p.is_symlink():
                    continue
                target = os.readlink(str(p))
            except OSError:
                continue
            # Format: "<hostname>-<pid>" (Socket points at a /tmp path).
            owner = target.rsplit("-", 1)[0] if name == "SingletonLock" else ""
            if name == "SingletonLock" and (not owner or owner == host):
                return  # ours (or unparseable) -- let Chrome decide
            try:
                p.unlink()
                if name == "SingletonLock":
                    _log(
                        self.lane_idx,
                        f"cleared stale Chrome profile lock from a previous "
                        f"container ({target}; we are {host})",
                    )
            except OSError:
                pass

    def _mark_prefs_clean(self) -> None:
        prefs = lane_user_data_dir(self.lane_idx) / "Default" / "Preferences"
        try:
            if prefs.exists():
                data = json.loads(prefs.read_text())
            else:
                # Seed a minimal Preferences before the FIRST launch so
                # developer_mode (below) is on from the very first
                # Chrome start -- otherwise the built-in Paprika Agent
                # (an unpacked --load-extension) loads disabled.
                prefs.parent.mkdir(parents=True, exist_ok=True)
                data = {}
            if not isinstance(data, dict):
                data = {}
            profile = data.setdefault("profile", {})
            if isinstance(profile, dict):
                profile["exit_type"] = "Normal"
                profile["exited_cleanly"] = True
            # Enable the extensions "Developer mode" toggle. Chrome 137+
            # disables unpacked extensions loaded via --load-extension
            # unless developer mode is on -- which is exactly how the
            # built-in Paprika Agent extension is loaded. developer_mode
            # lives in the regular (non-MAC-protected) Preferences, so
            # we can set it here and Chrome honours it on launch.
            ext = data.setdefault("extensions", {})
            if isinstance(ext, dict):
                ui = ext.setdefault("ui", {})
                if isinstance(ui, dict):
                    ui["developer_mode"] = True
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
                if proc is not None and proc.poll() is None:
                    continue  # alive
                # proc is None means the lane was left with NO Chrome at
                # all -- e.g. a profile-swap restore that got cancelled
                # after killing Chrome but before respawning. The old
                # check treated that as "nothing to watch" and skipped,
                # stranding the lane (dead Chrome, no watchdog action)
                # until some later job happened to trigger a swap. Treat
                # it the same as an exited Chrome and respawn.
                code = None if proc is None else proc.returncode
                _log(
                    self.lane_idx,
                    f"Chrome :{self.chrome_port} down (code={code}); "
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

    def _kill_chrome_proc(self) -> None:
        """SIGKILL the lane's Chrome AND its whole process group so the
        child processes (renderers, zygotes, gpu, utility) die with it.

        Chrome is launched with ``start_new_session=True`` so it leads its
        own process group; ``os.killpg`` then takes the whole tree down at
        once. The old ``self._chrome_proc.kill()`` dropped only the main
        process, leaving the children to be reparented to the container's
        PID 1 -- the worker python, which doesn't reap them -- so they
        piled up as zombies (hundreds observed in production). Safe to call
        when no Chrome is running. Blocks briefly to reap the main proc."""
        proc = self._chrome_proc
        self._chrome_proc = None
        if proc is None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    async def _akill_chrome_proc(self) -> None:
        """``_kill_chrome_proc`` off the event loop.

        It blocks up to 5s in ``proc.wait`` reaping the Chrome it just killed.
        On the loop that is 5s in which this worker answers nothing -- not the
        hub's keepalive ping, not a job result. The loop-stall watchdog caught
        it as the top frame in 2 of 3 post-fix stalls on 2026-08-15.

        The sync form stays for ``stop()``, which runs during shutdown when
        there is no loop left to protect.
        """
        await asyncio.to_thread(self._kill_chrome_proc)

    def stop(self) -> None:
        self._stopping = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        self._kill_chrome_proc()
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
    # its current ``chrome-lane-N`` dir aside, swaps the extracted
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
        lane_dir = lane_user_data_dir(self.lane_idx)
        backup_dir = lane_backup_dir(self.lane_idx)
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
        await self._akill_chrome_proc()
        # Move the lane's current profile aside, then move the operator's in.
        #
        # Off the event loop. A Chrome profile is tens of thousands of small
        # files and the rename is only cheap while both ends are on one
        # filesystem -- with the profile cache in /tmp and the lane on
        # /ram/chrome it is cross-device, so it falls through to copytree and
        # copies the lot. The loop-stall watchdog caught this at 1.0-1.5s on
        # three sampled workers on 2026-08-15, with copytree the single most
        # common frame. A worker that cannot answer for 1.5s cannot answer the
        # hub's keepalive either, and that is how a stall becomes a WS close
        # and a batch of jobs failed as "disconnected".
        def _swap() -> None:
            if backup_dir.exists():
                # A previous swap crashed mid-way and left a stale
                # .lane-default; the running lane_dir is authoritative.
                shutil.rmtree(backup_dir, ignore_errors=True)
            if lane_dir.exists():
                try:
                    lane_dir.rename(backup_dir)
                except OSError:
                    # Cross-device or race. Copy then remove as fallback.
                    shutil.copytree(lane_dir, backup_dir, dirs_exist_ok=True)
                    shutil.rmtree(lane_dir, ignore_errors=True)
            try:
                profile_dir.rename(lane_dir)
            except OSError:
                shutil.copytree(profile_dir, lane_dir, dirs_exist_ok=True)
                shutil.rmtree(profile_dir, ignore_errors=True)

        await asyncio.to_thread(_swap)
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
        finally:
            # See restore_default_profile: restart the watchdog even on
            # spawn failure or cancellation so the lane can't be stranded
            # with no Chrome and no watchdog.
            if self._watchdog_task is None or self._watchdog_task.done():
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
        lane_dir = lane_user_data_dir(self.lane_idx)
        _log(self.lane_idx, f"ambient profile install: {profile_name!r} -> {lane_dir}")
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        await self._akill_chrome_proc()
        # Replace lane_dir's content. Unlike use_profile() we do NOT
        # back up the previous content -- the operator explicitly
        # asked for this profile to be the default; the previous
        # ambient (or empty lane state) is discarded. clear_ambient_-
        # profile() resets to empty.
        # Off the loop, same reason as use_profile: a Chrome profile is tens
        # of thousands of small files, and the watchdog named this exact
        # copytree once use_profile's was fixed.
        def _install() -> None:
            if lane_dir.exists():
                shutil.rmtree(lane_dir, ignore_errors=True)
            shutil.copytree(profile_dir, lane_dir)

        try:
            await asyncio.to_thread(_install)
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
        lane_dir = lane_user_data_dir(self.lane_idx)
        _log(self.lane_idx, "ambient profile clear: reverting to lane stock")
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        await self._akill_chrome_proc()
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
        lane_dir = lane_user_data_dir(self.lane_idx)
        backup_dir = lane_backup_dir(self.lane_idx)
        _log(self.lane_idx, "profile swap: restoring lane default")
        # Pause watchdog + stop Chrome (same dance as use_profile).
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        await self._akill_chrome_proc()
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
        finally:
            # Restart the watchdog even if _spawn_chrome raised OR this
            # coroutine is cancelled (job timeout / worker churn) between
            # the Chrome-kill above and here. Without the finally, a
            # cancel in that window leaves the lane with NO Chrome and NO
            # watchdog; acquire() would still hand it to the next job
            # ("Failed to connect to browser"). The watchdog's
            # proc-is-None recovery then brings Chrome back.
            if self._watchdog_task is None or self._watchdog_task.done():
                self._watchdog_task = asyncio.create_task(self._chrome_watchdog())
            self._profile_swap_active = False
        _log(self.lane_idx, "profile swap: lane default restored")

    async def ensure_chrome_alive(self) -> None:
        """Guarantee Chrome is up and answering on the debug port before a
        job attaches to this lane. The pool calls this at acquire time.

        Recovers a lane whose Chrome died without the watchdog catching it
        yet: a crash mid-cycle, a profile-swap restore that was cancelled
        before respawning, or a Chrome that is process-alive but whose
        debug port never came up (zombie under heavy host load). Cheap on
        the happy path -- a single local HTTP probe.
        """
        if self._stopping:
            return
        # The debug port answering is the only thing the attach actually
        # needs, so probe it directly -- this covers both "no Chrome
        # process" and "process alive but port dead".
        if await _wait_http(
            f"http://localhost:{self.chrome_port}/json/version",
            timeout=3.0,
        ):
            return
        # Unresponsive: kill any stale/zombie process group, then respawn.
        await self._akill_chrome_proc()
        _log(
            self.lane_idx,
            f"acquire: Chrome :{self.chrome_port} not answering; "
            f"respawning before handing lane to job",
        )
        await self._spawn_chrome()
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._chrome_watchdog())

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

        Before returning, the lane's Chrome liveness is verified (and
        Chrome respawned if dead) so a job never attaches to a lane whose
        Chrome died -- the cause of fleet-wide "Failed to connect to
        browser" failures on lane 0, where per-job profile swaps churn
        Chrome and a cancelled/slow restore could leave it down. The
        liveness gate runs OUTSIDE the pool lock so a slow respawn on one
        lane doesn't block acquires on the others.
        """
        lane: Lane | None = None
        if lane_hint is not None:
            if not (0 <= lane_hint < len(self.lanes)):
                return None
            target = self.lanes[lane_hint]
            while lane is None:
                async with self._lock:
                    if not target.busy:
                        target.busy = True
                        lane = target
                if lane is None:
                    await asyncio.sleep(0.5)
        else:
            async with self._lock:
                for s in self.lanes:
                    if not s.busy:
                        s.busy = True
                        lane = s
                        break
            if lane is None:
                return None
        # Liveness gate (outside the lock). Best-effort: if Chrome can't
        # be brought up at all (host wedged), still return the lane so the
        # job surfaces a clear error rather than acquire hanging the
        # queue -- but the common case (Chrome was simply dead) is now
        # repaired before the job ever attaches.
        try:
            await lane.ensure_chrome_alive()
        except Exception as e:
            log.warning(
                "[pool] lane %d Chrome respawn at acquire failed: %r; "
                "handing lane over anyway",
                lane.lane_idx,
                e,
            )
        return lane

    def release(self, lane: Lane) -> None:
        lane.busy = False

    def stats(self) -> dict:
        return {
            "total": len(self.lanes),
            "busy": sum(1 for s in self.lanes if s.busy),
        }
