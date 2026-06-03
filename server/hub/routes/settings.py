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
import shutil

from fastapi import APIRouter, HTTPException

from server.hub._state import config, get_storage_dir, state
from server.hub.codegen import CODEGEN_LLM_URL, CODEGEN_MODEL_NAME
from server.hub.settings import SettingsRegistry

# SMB mount logic lives in server.hub.smb_mount so the startup
# auto-mount + watchdog (app.py lifespan) and these endpoints share a
# single implementation. ``ensure_smb_mounted`` is the idempotent
# "mount if configured & not already healthily mounted" entrypoint.
from server.hub.smb_mount import (
    _smb_is_mounted,
    _smb_mount,
    _smb_unmount,
    ensure_smb_mounted,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["Settings"])

# Setting keys whose VALUE must never be returned by GET /settings (the
# endpoint is unauthenticated on the LAN). Redacted to "" in the GET
# payload; the UI uses the companion ``secrets_set`` map to show whether
# one is stored. PUT still accepts the real value to (re)set it.
_SECRET_KEYS = frozenset({"smb_password", "mariadb_password", "s3_secret_key"})


def _require_settings() -> SettingsRegistry:
    if state.settings is None:
        raise HTTPException(503, "settings registry not initialised")
    return state.settings


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

    # MariaDB status — show connected/disconnected in the UI
    mdb_status: dict = {"connected": False}
    if state.mariadb_pool is not None:
        try:
            async with state.mariadb_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT VERSION()")
                    ver_row = await cur.fetchone()
            mdb_status = {
                "connected": True,
                "version": ver_row[0] if ver_row else "",
                "host": reg.get("mariadb_host", ""),
                "port": int(reg.get("mariadb_port", 3306)),
                "database": reg.get("mariadb_database", "paprika"),
                "store_kind": state.store_kind,
            }
        except Exception:
            mdb_status = {"connected": False}

    # S3 / object storage status (config-only; reachability is on-demand
    # via POST /settings/s3/test to keep this GET cheap).
    from server.hub import objstore as _objstore
    _s3_enabled = _objstore.enabled()
    # Live reachability probe (head_bucket, short timeout) so the Settings
    # banner can show 接続中 / 未接続 -- same idea as mariadb_status above.
    _s3_connected, _s3_err = (await _objstore.reachable()) if _s3_enabled else (False, "")
    s3_status = {
        "enabled": _s3_enabled,
        "connected": _s3_connected,
        "endpoint": reg.get("s3_endpoint", ""),
        "bucket": reg.get("s3_bucket", "paprika"),
        "prefix": reg.get("s3_prefix", "jobs"),
        "error": _s3_err,
    }

    # Never ship secret values to the browser. GET /settings is
    # unauthenticated on the LAN, so returning smb_password /
    # mariadb_password in cleartext (as reg.all() does) would leak the
    # NAS + DB credentials to anyone who can curl the hub. Redact the
    # values and instead report whether each secret is set, so the UI
    # can render a "(設定済み — 変更時のみ入力)" placeholder. The PUT
    # path still accepts the real value when the operator types one.
    _values = reg.all()
    _secrets_set = {k: bool(_values.get(k)) for k in _SECRET_KEYS}
    for k in _SECRET_KEYS:
        _values[k] = ""

    return {
        "values": _values,
        "secrets_set": _secrets_set,
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
        "mariadb_status": mdb_status,
        "s3_status": s3_status,
    }


@router.put("/settings")
async def put_settings(body: dict) -> dict:
    """Partial update of the settings. Unknown keys are silently
    ignored; known keys are coerced to their declared type."""
    reg = _require_settings()
    body = body or {}
    reg.update(body)
    # S3 connection changed -> drop the cached boto3 client so the new
    # endpoint / credentials take effect on the next object-store call.
    if any(str(k).startswith("s3_") for k in body):
        try:
            from server.hub import objstore
            objstore.reset_client()
        except Exception:
            pass
    return await get_settings()


# -------------------------------------------------------------------
# SMB mount / unmount / status endpoints
# -------------------------------------------------------------------

@router.post("/settings/smb/mount")
async def smb_mount() -> dict:
    """Mount the SMB share using the saved connection settings.
    On success, ``storage_dir`` is automatically set to the mount point.

    Also (re-)enables ``smb_auto_mount`` so the startup auto-mount +
    watchdog keep the share mounted across restarts / network blips.
    A manual mount is the operator saying "I want this share up", which
    is exactly what auto-mount should track.
    """
    reg = _require_settings()
    if not reg.get("smb_server", "") or not reg.get("smb_share", ""):
        raise HTTPException(400, "smb_server and smb_share are required")

    # Re-enable auto-mount: a manual mount means the operator wants the
    # share kept up. (A prior manual unmount sets it False; mounting
    # again flips it back on.)
    reg.update({"smb_auto_mount": True})

    # ensure_smb_mounted is idempotent: mounts if needed, remounts a
    # stale mount, sets storage_dir on success. Run it off the event
    # loop since it shells out to `mount`.
    import asyncio

    ok, msg = await asyncio.to_thread(ensure_smb_mounted, reg)
    if not ok:
        raise HTTPException(500, f"mount failed: {msg}")
    mount_point = reg.get("smb_mount_point", "/mnt/paprika")
    return {"ok": True, "message": msg, "mount_point": mount_point}


@router.post("/settings/smb/unmount")
async def smb_unmount() -> dict:
    """Unmount the SMB share and revert ``storage_dir`` to default.

    Also disables ``smb_auto_mount`` so the watchdog respects the manual
    unmount and doesn't immediately re-mount the share on its next tick.
    Re-mounting via the Mount button flips auto-mount back on.
    """
    reg = _require_settings()
    mount_point = reg.get("smb_mount_point", "/mnt/paprika")

    # Stop the watchdog from fighting a deliberate unmount.
    reg.update({"smb_auto_mount": False})

    if not _smb_is_mounted(mount_point):
        # Clear storage_dir anyway
        reg.update({"storage_dir": ""})
        return {"ok": True, "message": "not mounted", "mount_point": mount_point}

    err = _smb_unmount(mount_point)
    if err:
        raise HTTPException(500, f"unmount failed: {err}")

    reg.update({"storage_dir": ""})
    return {"ok": True, "message": "unmounted", "mount_point": mount_point}


def _fmt_size(num_bytes: int) -> str:
    """Human-readable size that scales the unit to the magnitude.

    Operators with multi-TB NAS arrays complained that everything was
    reported in GB (e.g. "12345.6 GB"); pick the largest unit that keeps
    the number readable so a 12 TB array shows "12.1 TB", a 500 GB share
    "500.0 GB", and a tiny tmpfs "812.0 MB".
    """
    n = float(num_bytes)
    for unit, factor in (
        ("PB", 1024**5),
        ("TB", 1024**4),
        ("GB", 1024**3),
        ("MB", 1024**2),
        ("KB", 1024),
    ):
        if n >= factor:
            return f"{n / factor:.1f} {unit}"
    return f"{int(n)} B"


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
                # Raw GB kept for backwards-compat / programmatic use.
                "total_gb": round(st.total / (1024**3), 1),
                "used_gb": round(st.used / (1024**3), 1),
                "free_gb": round(st.free / (1024**3), 1),
                # Pre-formatted, unit-scaled strings (GB / TB / PB ...)
                # the admin UI renders directly.
                "total_h": _fmt_size(st.total),
                "used_h": _fmt_size(st.used),
                "free_h": _fmt_size(st.free),
            }
        except Exception:
            pass

    return {
        "mounted": mounted,
        "mount_point": mp,
        "server": reg.get("smb_server", ""),
        "share": reg.get("smb_share", ""),
        "auto_mount": bool(reg.get("smb_auto_mount", True)),
        "usage": usage,
    }


# -------------------------------------------------------------------
# MariaDB connection test
# -------------------------------------------------------------------

@router.post("/settings/s3/test")
async def s3_test(body: dict | None = None) -> dict:
    """Test S3 / MinIO connectivity. Uses body values (endpoint, bucket,
    access_key, secret_key, region) when provided -- so the operator can
    verify before saving -- else the saved settings. A blank secret_key in
    the body falls back to the stored one. Does head_bucket + a 1-key list."""
    import asyncio

    reg = _require_settings()
    b = body or {}
    endpoint = (b.get("endpoint") or reg.get("s3_endpoint", "")).strip()
    bucket = (b.get("bucket") or reg.get("s3_bucket", "paprika")).strip()
    region = (b.get("region") or reg.get("s3_region", "us-east-1")).strip() or "us-east-1"
    access_key = (b.get("access_key") or reg.get("s3_access_key", "")).strip()
    secret_key = b.get("secret_key") or reg.get("s3_secret_key", "")

    if not bucket:
        return {"ok": False, "message": "バケット名が未設定です"}

    def _test() -> dict:
        try:
            import boto3
            from botocore.config import Config as _BotoConfig
        except ImportError:
            return {"ok": False, "message": "boto3 がインストールされていません"}
        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint or None,
                aws_access_key_id=access_key or None,
                aws_secret_access_key=secret_key or None,
                region_name=region,
                config=_BotoConfig(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 1, "mode": "standard"},
                    connect_timeout=5,
                    read_timeout=5,
                ),
            )
            client.head_bucket(Bucket=bucket)
            r = client.list_objects_v2(Bucket=bucket, MaxKeys=1)
            return {
                "ok": True,
                "message": f"接続成功 (bucket={bucket}, objects≧{r.get('KeyCount', 0)})",
            }
        except Exception as e:
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}

    return await asyncio.to_thread(_test)


@router.post("/settings/mariadb/test")
async def mariadb_test(body: dict | None = None) -> dict:
    """Test MariaDB connectivity.

    If *body* contains host/port/database/username/password, those are
    used directly (for testing before saving).  Otherwise the saved
    settings are read.
    """
    import asyncio

    reg = _require_settings()
    b = body or {}
    host = b.get("host") or reg.get("mariadb_host", "")
    port = int(b.get("port") or reg.get("mariadb_port", 3306))
    database = b.get("database") or reg.get("mariadb_database", "paprika")
    username = b.get("username") or reg.get("mariadb_username", "")
    password = b.get("password") or reg.get("mariadb_password", "")

    if not host:
        return {"ok": False, "message": "ホストが未設定です"}
    if not username:
        return {"ok": False, "message": "ユーザー名が未設定です"}

    async def _test():
        try:
            import aiomysql  # type: ignore[import-untyped]
        except ImportError:
            # Fallback: try synchronous pymysql
            try:
                import pymysql  # type: ignore[import-untyped]
            except ImportError:
                return {"ok": False, "message": "aiomysql / pymysql がインストールされていません"}
            try:
                conn = pymysql.connect(
                    host=host, port=port, user=username,
                    password=password, database=database,
                    connect_timeout=5,
                )
                cur = conn.cursor()
                cur.execute("SELECT VERSION()")
                version = cur.fetchone()[0]
                cur.close()
                conn.close()
                return {"ok": True, "message": f"接続成功", "version": version}
            except Exception as e:
                return {"ok": False, "message": str(e)}

        try:
            conn = await aiomysql.connect(
                host=host, port=port, user=username,
                password=password, db=database,
                connect_timeout=5,
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT VERSION()")
                row = await cur.fetchone()
                version = row[0] if row else "unknown"
            conn.close()
            return {"ok": True, "message": "接続成功", "version": version}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    return await _test()


# -------------------------------------------------------------------
# MariaDB pool helper + migration endpoints
# -------------------------------------------------------------------

async def _get_or_create_pool():
    """Lazy-init the MariaDB connection pool from saved settings.

    The pool is cached on ``state.mariadb_pool`` so subsequent calls
    reuse the same connection pool.  If the pool is stale (ping fails),
    a new one is created.
    """
    from server.hub.mariadb import close_pool, create_pool

    # Re-use existing pool if healthy
    if state.mariadb_pool is not None:
        try:
            async with state.mariadb_pool.acquire() as conn:
                await conn.ping()
            return state.mariadb_pool
        except Exception:
            await close_pool(state.mariadb_pool)
            state.mariadb_pool = None

    reg = _require_settings()
    host = reg.get("mariadb_host", "")
    if not host:
        raise HTTPException(400, "MariaDB ホストが未設定です")
    username = reg.get("mariadb_username", "")
    if not username:
        raise HTTPException(400, "MariaDB ユーザー名が未設定です")

    try:
        pool = await create_pool(
            host=host,
            port=int(reg.get("mariadb_port", 3306)),
            database=reg.get("mariadb_database", "paprika"),
            username=username,
            password=reg.get("mariadb_password", ""),
        )
    except Exception as e:
        raise HTTPException(500, f"MariaDB 接続失敗: {e}")

    state.mariadb_pool = pool
    return pool


@router.post("/settings/mariadb/schema")
async def mariadb_create_schema() -> dict:
    """Create MariaDB tables (idempotent CREATE TABLE IF NOT EXISTS)."""
    from server.hub.mariadb import ensure_schema

    pool = await _get_or_create_pool()
    try:
        tables = await ensure_schema(pool)
        return {"ok": True, "tables": tables}
    except Exception as e:
        raise HTTPException(500, f"テーブル作成失敗: {e}")


@router.post("/settings/mariadb/migrate/{category}")
async def mariadb_migrate(category: str) -> dict:
    """Migrate one data category to MariaDB.

    *category*: ``jobs`` | ``hosts`` | ``visited_urls``
    """
    import asyncio

    from server.hub import mariadb

    pool = await _get_or_create_pool()

    # Ensure tables exist first
    try:
        await mariadb.ensure_schema(pool)
    except Exception as e:
        raise HTTPException(500, f"テーブル作成失敗: {e}")

    if category == "jobs":
        if state.store is None:
            raise HTTPException(503, "JobStore が未初期化です")
        # Guard: if the current JobStore IS MariaDB, migrating
        # MariaDB→MariaDB is a no-op that would INSERT IGNORE (all
        # skipped) then purge (DELETE) all rows — catastrophic data loss.
        if state.store_kind == "mariadb":
            return {
                "ok": True,
                "category": "jobs",
                "migrated": 0,
                "skipped": 0,
                "total": 0,
                "purged": 0,
                "errors": [],
                "message": "既に MariaDB を使用中のため移行不要です",
            }
        try:
            return await mariadb.migrate_jobs(state.store, pool)
        except Exception as e:
            raise HTTPException(500, f"Jobs 移行失敗: {e}")

    if category == "hosts":
        if state.hosts is None:
            raise HTTPException(503, "HostRegistry が未初期化です")
        try:
            return await mariadb.migrate_hosts(state.hosts, pool)
        except Exception as e:
            raise HTTPException(500, f"Hosts 移行失敗: {e}")

    if category == "visited_urls":
        if state.hosts is None or state.host_visited is None:
            raise HTTPException(503, "HostRegistry / VisitedRegistry が未初期化です")
        try:
            return await mariadb.migrate_visited_urls(
                state.hosts, state.host_visited, pool,
            )
        except Exception as e:
            raise HTTPException(500, f"Visited URLs 移行失敗: {e}")

    if category == "skills":
        if state.skills is None:
            raise HTTPException(503, "SkillRegistry が未初期化です")
        try:
            return await mariadb.migrate_skills(state.skills, pool)
        except Exception as e:
            raise HTTPException(500, f"Skills 移行失敗: {e}")

    if category == "conventions":
        if state.conventions is None:
            raise HTTPException(503, "ConventionRegistry が未初期化です")
        try:
            return await mariadb.migrate_conventions(state.conventions, pool)
        except Exception as e:
            raise HTTPException(500, f"Conventions 移行失敗: {e}")

    if category == "engines":
        if state.engines is None:
            raise HTTPException(503, "EngineRegistry が未初期化です")
        try:
            return await mariadb.migrate_engines(state.engines, pool)
        except Exception as e:
            raise HTTPException(500, f"Engines 移行失敗: {e}")

    if category == "presets":
        if state.presets is None:
            raise HTTPException(503, "PresetRegistry が未初期化です")
        try:
            return await mariadb.migrate_presets(state.presets, pool)
        except Exception as e:
            raise HTTPException(500, f"Presets 移行失敗: {e}")

    raise HTTPException(400, f"不明なカテゴリ: {category}")


@router.post("/settings/mariadb/recover-jobs")
async def mariadb_recover_jobs() -> dict:
    """Recover lost job records from on-disk output directories.

    Scans the storage directory for job output folders (log.txt,
    assets/, page.html) and reconstructs ``jobs`` rows in MariaDB
    using ``INSERT IGNORE`` (safe to re-run).
    """
    from server.hub.recover_jobs import recover_from_disk

    pool = await _get_or_create_pool()
    storage_dir = get_storage_dir()

    try:
        return await recover_from_disk(pool, storage_dir)
    except Exception as e:
        raise HTTPException(500, f"復旧失敗: {e}")


@router.post("/settings/mariadb/migrate-logs-to-disk")
async def mariadb_migrate_logs() -> dict:
    """Flush rows from the MariaDB ``job_logs`` table to disk
    (``{storage_dir}/{job_id}/log.txt``) so the table can be reclaimed.

    Idempotent: skips jobs whose log.txt already has content (the disk
    file is treated as authoritative because the codegen-loop has been
    double-writing for some time) and DELETEs the migrated rows so a
    re-run only sees what's left to do.

    At 3K jobs the table was 365 MB with ~2M rows; this endpoint moves
    that data to flat files under the SMB mount where it's served
    directly via /jobs/{id}/log.txt with no DB round-trip.
    """
    from server.hub.log_migrate import migrate_logs_to_disk

    pool = await _get_or_create_pool()
    try:
        return await migrate_logs_to_disk(pool, get_storage_dir)
    except Exception as e:
        raise HTTPException(500, f"ログ移行失敗: {e}")


@router.get("/settings/mariadb/tables")
async def mariadb_table_status() -> dict:
    """Return row counts for each MariaDB table."""
    from server.hub.mariadb import table_counts

    pool = await _get_or_create_pool()
    counts = await table_counts(pool)
    return {"ok": True, "tables": counts}
