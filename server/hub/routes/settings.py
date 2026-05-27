"""Hub-wide settings: GET (admin UI Settings tab payload) + PUT
(partial update). Backed by ``server.hub.settings.SettingsRegistry``,
instantiated by app.py's lifespan and stashed on
``server.hub._state.state.settings``.

SMB mount/unmount endpoints live here too — they read the SMB
connection fields from SettingsRegistry, shell out to
``mount -t cifs`` / ``umount``, and update ``storage_dir``
accordingly.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException

from server.hub._state import config, get_storage_dir, state
from server.hub.codegen import CODEGEN_LLM_URL, CODEGEN_MODEL_NAME
from server.hub.settings import SettingsRegistry

log = logging.getLogger(__name__)

router = APIRouter(tags=["Settings"])


def _require_settings() -> SettingsRegistry:
    if state.settings is None:
        raise HTTPException(503, "settings registry not initialised")
    return state.settings


# -------------------------------------------------------------------
# SMB mount helpers
# -------------------------------------------------------------------

def _smb_is_mounted(mount_point: str) -> bool:
    """Check if *mount_point* is currently a mount point."""
    if not mount_point:
        return False
    try:
        return os.path.ismount(mount_point)
    except Exception:
        return False


def _smb_mount(server: str, share: str, username: str, password: str,
               mount_point: str, extra_opts: str) -> str:
    """Mount an SMB share.  Returns "" on success, error string on failure."""
    mp = Path(mount_point)
    mp.mkdir(parents=True, exist_ok=True)

    # Build the mount command
    opts_parts = [f"username={username}"] if username else ["guest"]
    if password:
        opts_parts.append(f"password={password}")
    else:
        if not username:
            opts_parts.append("password=")
    # iocharset + file_mode so job files are world-readable
    opts_parts.extend(["iocharset=utf8", "file_mode=0666", "dir_mode=0777"])
    if extra_opts:
        opts_parts.append(extra_opts)
    opts_str = ",".join(opts_parts)

    unc = f"//{server}/{share}"
    cmd = ["mount", "-t", "cifs", unc, str(mp), "-o", opts_str]
    log.info("SMB mount: %s", " ".join(cmd).replace(password, "***") if password else " ".join(cmd))

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


def _smb_unmount(mount_point: str) -> str:
    """Unmount.  Returns "" on success, error string on failure."""
    try:
        r = subprocess.run(["umount", mount_point],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return (r.stderr or r.stdout or "unknown error").strip()
        return ""
    except Exception as e:
        return str(e)


# -------------------------------------------------------------------
# Settings CRUD
# -------------------------------------------------------------------

@router.get("/settings")
async def get_settings() -> dict:
    """Return the current effective settings + schema info + system
    info (read-only, env-derived). The UI uses this single payload
    to render the Settings tab without extra round trips.
    """
    reg = _require_settings()
    # Lazy imports so this endpoint stays cheap even if the LLM
    # modules haven't been touched yet.
    from server.hub.convention_llm import (
        CONVENTION_DISTILL_LLM_URL,
        CONVENTION_DISTILL_MODEL_NAME,
    )
    from server.hub.skill_llm import (
        SKILL_DISTILL_LLM_URL,
        SKILL_DISTILL_MODEL_NAME,
        SKILL_RETRIEVAL_LLM_URL,
        SKILL_RETRIEVAL_MODEL_NAME,
    )

    smb_mp = reg.get("smb_mount_point", "/mnt/paprika")
    return {
        "values": reg.all(),
        "schema": reg.schema(),
        "system": {
            "codegen_llm_url": CODEGEN_LLM_URL,
            "codegen_model": CODEGEN_MODEL_NAME,
            "skill_distill_llm_url": SKILL_DISTILL_LLM_URL,
            "skill_distill_model": SKILL_DISTILL_MODEL_NAME,
            "skill_retrieval_llm_url": SKILL_RETRIEVAL_LLM_URL,
            "skill_retrieval_model": SKILL_RETRIEVAL_MODEL_NAME,
            "convention_distill_llm_url": CONVENTION_DISTILL_LLM_URL,
            "convention_distill_model": CONVENTION_DISTILL_MODEL_NAME,
            "data_dir": str(config.data_dir.resolve()),
            "storage_dir": str(get_storage_dir().resolve()),
            "store": state.store_kind,
        },
        "smb_status": {
            "mounted": _smb_is_mounted(smb_mp),
            "mount_point": smb_mp,
        },
    }


@router.put("/settings")
async def put_settings(body: dict) -> dict:
    """Partial update of the settings. Unknown keys are silently
    ignored; known keys are coerced to their declared type."""
    reg = _require_settings()
    body = body or {}
    reg.update(body)
    return await get_settings()


# -------------------------------------------------------------------
# SMB mount / unmount / status endpoints
# -------------------------------------------------------------------

@router.post("/settings/smb/mount")
async def smb_mount() -> dict:
    """Mount the SMB share using the saved connection settings.
    On success, ``storage_dir`` is automatically set to the mount point."""
    reg = _require_settings()
    server = reg.get("smb_server", "")
    share = reg.get("smb_share", "")
    username = reg.get("smb_username", "")
    password = reg.get("smb_password", "")
    mount_point = reg.get("smb_mount_point", "/mnt/paprika")
    extra_opts = reg.get("smb_mount_options", "")

    if not server or not share:
        raise HTTPException(400, "smb_server and smb_share are required")

    # Already mounted?
    if _smb_is_mounted(mount_point):
        # Still set storage_dir in case it drifted
        reg.update({"storage_dir": mount_point})
        return {"ok": True, "message": "already mounted", "mount_point": mount_point}

    err = _smb_mount(server, share, username, password, mount_point, extra_opts)
    if err:
        raise HTTPException(500, f"mount failed: {err}")

    # Point storage_dir at the mount
    reg.update({"storage_dir": mount_point})
    return {"ok": True, "message": "mounted", "mount_point": mount_point}


@router.post("/settings/smb/unmount")
async def smb_unmount() -> dict:
    """Unmount the SMB share and revert ``storage_dir`` to default."""
    reg = _require_settings()
    mount_point = reg.get("smb_mount_point", "/mnt/paprika")

    if not _smb_is_mounted(mount_point):
        # Clear storage_dir anyway
        reg.update({"storage_dir": ""})
        return {"ok": True, "message": "not mounted", "mount_point": mount_point}

    err = _smb_unmount(mount_point)
    if err:
        raise HTTPException(500, f"unmount failed: {err}")

    reg.update({"storage_dir": ""})
    return {"ok": True, "message": "unmounted", "mount_point": mount_point}


@router.get("/settings/smb/status")
async def smb_status() -> dict:
    """Quick check: is the SMB mount point currently mounted?"""
    reg = _require_settings()
    mp = reg.get("smb_mount_point", "/mnt/paprika")
    mounted = _smb_is_mounted(mp)

    # Disk usage when mounted
    usage = None
    if mounted:
        try:
            st = shutil.disk_usage(mp)
            usage = {
                "total_gb": round(st.total / (1024**3), 1),
                "used_gb": round(st.used / (1024**3), 1),
                "free_gb": round(st.free / (1024**3), 1),
            }
        except Exception:
            pass

    return {
        "mounted": mounted,
        "mount_point": mp,
        "server": reg.get("smb_server", ""),
        "share": reg.get("smb_share", ""),
        "usage": usage,
    }
