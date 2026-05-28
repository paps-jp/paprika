"""SMB share auto-mount + watchdog.

The operator configures SMB connection params in the Settings tab; the
hub mounts ``//{server}/{share}`` at ``smb_mount_point`` (default
``/mnt/paprika``) and points ``storage_dir`` at it so large job
artifacts (page.html / screenshots / video) land on the NAS while the
hub metadata stays on fast local disk.

A ``cifs`` mount does NOT survive a host / container restart -- the
kernel mount table is empty on boot. Before this module the operator
had to re-click "Mount" in the Settings tab after every restart, and a
transient network blip that dropped the share left ``storage_dir``
pointing at an empty mountpoint until someone noticed.

This module fixes both:

  * :func:`ensure_smb_mounted` -- mount when configured and not already
    mounted (or remount when the existing mount went stale). Called once
    at lifespan-start so storage is ready before the first job, and on
    every watchdog tick.
  * :func:`smb_watchdog_loop` -- forever-loop that re-runs
    ``ensure_smb_mounted`` every :data:`SMB_WATCHDOG_INTERVAL_S` seconds,
    so a dropped mount is restored automatically within ~30s.

Both are gated by the ``smb_auto_mount`` setting (default ``True``) and
are no-ops when SMB isn't configured (no ``smb_server`` / ``smb_share``).

The actual mount needs ``CAP_SYS_ADMIN`` (the hub container already
runs the manual ``/settings/smb/mount`` endpoint via ``mount -t cifs``,
so this introduces no new privilege requirement).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# How often the watchdog re-checks the mount. 30s is a good balance:
# fast enough that a dropped share is restored before most jobs notice,
# slow enough that a genuinely-unreachable NAS doesn't spam ``mount``.
SMB_WATCHDOG_INTERVAL_S = 30


def _smb_is_mounted(mount_point: str) -> bool:
    """True if *mount_point* is a mount point (per the kernel table)."""
    if not mount_point:
        return False
    try:
        return os.path.ismount(mount_point)
    except Exception:
        return False


def _smb_is_healthy(mount_point: str) -> bool:
    """True if the mount is not just present but actually serving I/O.

    A CIFS mount can stay in the kernel mount table after the server
    disappears (network blip, NAS reboot); ``os.path.ismount`` keeps
    returning True but every read/write fails with ``ESTALE`` / ``EIO``.
    ``statvfs`` forces a round-trip to the server, so it raises on a
    dead mount -- which is exactly the "接続が切れた" state the operator
    wants auto-recovered.
    """
    if not _smb_is_mounted(mount_point):
        return False
    try:
        os.statvfs(mount_point)
        return True
    except OSError:
        return False


def _smb_mount(server: str, share: str, username: str, password: str,
               mount_point: str, extra_opts: str) -> str:
    """Mount an SMB share. Returns "" on success, error string on failure."""
    mp = Path(mount_point)
    try:
        mp.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"cannot create mount point {mount_point}: {e}"

    # Build the mount options.
    opts_parts = [f"username={username}"] if username else ["guest"]
    if password:
        opts_parts.append(f"password={password}")
    else:
        if not username:
            opts_parts.append("password=")
    # iocharset + file/dir modes so job files are world-readable.
    opts_parts.extend(["iocharset=utf8", "file_mode=0666", "dir_mode=0777"])
    if extra_opts:
        opts_parts.append(extra_opts)
    opts_str = ",".join(opts_parts)

    unc = f"//{server}/{share}"
    cmd = ["mount", "-t", "cifs", unc, str(mp), "-o", opts_str]
    # Never leak the password into the log.
    safe = " ".join(cmd).replace(password, "***") if password else " ".join(cmd)
    log.info("SMB mount: %s", safe)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "unknown error").strip()
            log.error("SMB mount failed: %s", err)
            return err
        return ""
    except subprocess.TimeoutExpired:
        return "mount timed out (30s)"
    except Exception as e:
        return str(e)


def _smb_unmount(mount_point: str, *, lazy: bool = False) -> str:
    """Unmount. Returns "" on success, error string on failure.

    ``lazy=True`` uses ``umount -l`` (detach now, clean up when no
    longer busy) which is the only thing that reliably frees a *stale*
    CIFS mount whose server has gone away -- a plain ``umount`` hangs /
    fails with "device is busy" on those.
    """
    cmd = ["umount"]
    if lazy:
        cmd.append("-l")
    cmd.append(mount_point)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return (r.stderr or r.stdout or "unknown error").strip()
        return ""
    except Exception as e:
        return str(e)


def smb_is_configured(reg) -> bool:
    """True if the operator has filled in at least server + share."""
    if reg is None:
        return False
    return bool(reg.get("smb_server", "") and reg.get("smb_share", ""))


def ensure_smb_mounted(reg) -> tuple[bool, str]:
    """Mount the configured SMB share if it isn't already healthily mounted.

    Idempotent and safe to call repeatedly (startup + every watchdog
    tick). Returns ``(ok, message)`` where ``ok`` means "storage is on
    the share now". Side effect: on success ``storage_dir`` is pointed
    at the mount point.

    No-op (returns ``(False, "...")``) when SMB isn't configured or
    ``smb_auto_mount`` is off.

    This is BLOCKING (shells out to ``mount``); call it via
    ``asyncio.to_thread`` from async code.
    """
    if not smb_is_configured(reg):
        return (False, "not configured")
    if not reg.get("smb_auto_mount", True):
        return (False, "auto-mount disabled")

    mount_point = reg.get("smb_mount_point", "/mnt/paprika")

    # Already mounted and serving I/O -> just make sure storage_dir tracks it.
    if _smb_is_healthy(mount_point):
        if reg.get("storage_dir", "") != mount_point:
            reg.update({"storage_dir": mount_point})
        return (True, "already mounted")

    # Stale mount (kernel thinks it's mounted but I/O fails) -> force
    # detach before remounting, otherwise the fresh mount fails with
    # "device or resource busy".
    if _smb_is_mounted(mount_point):
        log.warning("SMB mount %s is stale; force-unmounting before remount", mount_point)
        _smb_unmount(mount_point, lazy=True)

    err = _smb_mount(
        reg.get("smb_server", ""),
        reg.get("smb_share", ""),
        reg.get("smb_username", ""),
        reg.get("smb_password", ""),
        mount_point,
        reg.get("smb_mount_options", ""),
    )
    if err:
        return (False, err)

    reg.update({"storage_dir": mount_point})
    return (True, "mounted")


async def smb_watchdog_loop():
    """Forever-loop: keep the configured SMB share mounted.

    Re-runs :func:`ensure_smb_mounted` every
    :data:`SMB_WATCHDOG_INTERVAL_S` seconds. Logs only on state changes
    (mounted->dropped, dropped->recovered) so a healthy mount produces
    no log noise. Cancelled by the lifespan teardown.
    """
    # Lazy import to avoid a circular at module-load time
    # (smb_mount <- app.py lifespan, _state <- everything).
    from server.hub._state import state

    last_ok: bool | None = None
    while True:
        try:
            reg = state.settings
            if reg is not None and smb_is_configured(reg) and reg.get("smb_auto_mount", True):
                ok, msg = await asyncio.to_thread(ensure_smb_mounted, reg)
                if ok != last_ok:
                    if ok:
                        # First success, or recovered from a drop.
                        if last_ok is False:
                            log.info("SMB watchdog: share recovered (%s)", msg)
                        else:
                            log.info("SMB watchdog: share mounted (%s)", msg)
                    else:
                        log.warning("SMB watchdog: share NOT mounted (%s)", msg)
                    last_ok = ok
            else:
                # Not configured / disabled -> reset state so a later
                # enable logs the first mount.
                last_ok = None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("SMB watchdog tick failed")
        await asyncio.sleep(SMB_WATCHDOG_INTERVAL_S)
