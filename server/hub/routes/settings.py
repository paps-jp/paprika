"""Hub-wide settings: GET (admin UI Settings tab payload) + PUT
(partial update). Backed by ``server.hub.settings.SettingsRegistry``,
instantiated by app.py's lifespan and stashed on
``server.hub._state.state.settings``.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException

from server.hub._state import config, get_storage_dir, state
from server.hub.codegen import CODEGEN_LLM_URL, CODEGEN_MODEL_NAME
from server.hub.settings import SettingsRegistry

log = logging.getLogger(__name__)

router = APIRouter(tags=["Settings"])

# Setting keys whose VALUE must never be returned by GET /settings (the
# endpoint is unauthenticated on the LAN). Redacted to "" in the GET
# payload; the UI uses the companion ``secrets_set`` map to show whether
# one is stored. PUT still accepts the real value to (re)set it.
_SECRET_KEYS = frozenset({
    "mariadb_password",
    "s3_secret_key",
    "s3_nonvideo_secret_key",
    "s3_spill_secret_key",
    "s3_nonvideo_spill_secret_key",
    "worker_ssh_key_pem",
    # Reaches the hypervisor (pct reboot), so if anything is a secret, it is.
    "proxmox_ssh_key_pem",
})


def _require_settings() -> SettingsRegistry:
    if state.settings is None:
        raise HTTPException(503, "settings registry not initialised")
    return state.settings


# -------------------------------------------------------------------
# Egress proxy pool (Settings.proxy_pool) -> worker broadcast
#
# Settings stores proxy_pool as a free-form string (one proxy per line /
# comma / space). We normalise it to a URL list and push it to workers
# via HubProxyPoolSync -- both live (after a PUT that changes it) and on
# worker connect (catch-up). Mirrors profiles.py's _broadcast_profile_sync
# / _sync_all_profiles_to_worker.
# -------------------------------------------------------------------
def _split_proxy_pool(raw: str) -> list[str]:
    return [p for p in re.split(r"[\s,]+", raw or "") if p]


def _current_proxy_pool() -> list[str]:
    if state.settings is None:
        return []
    return _split_proxy_pool(str(state.settings.get("proxy_pool", "") or ""))


async def send_proxy_pool_to_worker(worker) -> None:
    """Send the current egress proxy pool to ONE worker (connect-time
    catch-up). Best-effort."""
    from server.protocol import HubProxyPoolSync
    try:
        await worker.send(HubProxyPoolSync(pool=_current_proxy_pool()))
    except Exception:
        log.warning(
            "proxy_pool sync to %s failed",
            getattr(worker, "worker_id", "?"),
            exc_info=True,
        )


async def _broadcast_proxy_pool() -> None:
    """Push the current pool to EVERY connected worker (after a Settings
    edit). Best-effort per worker."""
    from server.protocol import HubProxyPoolSync
    if state.registry is None:
        return
    pool = _current_proxy_pool()
    for w in list(state.registry.connections.values()):
        try:
            await w.send(HubProxyPoolSync(pool=pool))
        except Exception:
            log.warning(
                "proxy_pool broadcast to %s failed",
                getattr(w, "worker_id", "?"),
                exc_info=True,
            )


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
    # unauthenticated on the LAN, so returning mariadb_password /
    # s3_secret_key in cleartext (as reg.all() does) would leak the
    # DB + object-store credentials to anyone who can curl the hub. Redact the
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
    # Phase B: write the changed values through to MariaDB + broadcast to peer
    # hubs so a settings edit on any hub propagates with no restart (excludes
    # the mariadb_* DSN keys, kept per-hub). Best-effort.
    try:
        from server.hub._invalidate import share_settings
        _effective = reg.all()
        _changed = {k: _effective[k] for k in body if k in _effective}
        await share_settings(_changed)
    except Exception:
        log.debug("settings write-through/publish failed", exc_info=True)
    # Egress proxy pool changed -> push it to every connected worker now so
    # the edit is adopted on their next Chrome / yt-dlp spawn (no restart).
    if "proxy_pool" in body:
        try:
            await _broadcast_proxy_pool()
        except Exception:
            log.debug("proxy_pool broadcast failed", exc_info=True)
    return await get_settings()


# -------------------------------------------------------------------
# MariaDB connection test
# -------------------------------------------------------------------

@router.post("/settings/s3/test")
async def s3_test(body: dict | None = None) -> dict:
    """Test S3 / MinIO connectivity. Uses body values (endpoint, bucket,
    access_key, secret_key, region) when provided -- so the operator can
    verify before saving -- else the saved settings. A blank secret_key in
    the body falls back to the stored one. Does head_bucket + a 1-key list.

    ``tier`` selects which stored keys the blank-field fallback reads:
    ``"main"`` (default) -> ``s3_*``; ``"nonvideo"`` -> ``s3_nonvideo_*`` so
    a blank secret reuses the SAVED non-video secret (not the main tier's).
    The non-video bucket falls back to the main bucket when blank, matching
    the objstore routing (see server/hub/objstore.py + settings s3_nonvideo_*).
    """
    import asyncio

    _require_settings()  # ensure state.settings is bound (raises 503 if not)
    from server.hub import objstore as _obj
    b = body or {}
    tier = (b.get("tier") or "main").strip().lower()
    # Resolve blank body fields against the SAME env-aware path the live client
    # uses (objstore._s3cfg: empty setting -> PAPRIKA_S3_* env). Reading reg.get()
    # directly returned a stored empty string WITHOUT the env fallback, which
    # surfaced as a spurious "PartialCredentialsError: missing aws_access_key_id"
    # whenever the access key is env-provided (its Setting left blank).
    if tier in ("nonvideo", "nv", "non_video"):
        endpoint = (b.get("endpoint") or _obj._s3cfg("s3_nonvideo_endpoint", "PAPRIKA_S3_NONVIDEO_ENDPOINT")).strip()
        bucket = (b.get("bucket") or _obj._s3cfg("s3_nonvideo_bucket", "PAPRIKA_S3_NONVIDEO_BUCKET")
                  or _obj._s3cfg("s3_bucket", "PAPRIKA_S3_BUCKET", "paprika")).strip()
        region = (b.get("region") or _obj._s3cfg("s3_nonvideo_region", "PAPRIKA_S3_NONVIDEO_REGION", "us-east-1")).strip() or "us-east-1"
        access_key = (b.get("access_key") or _obj._s3cfg("s3_nonvideo_access_key", "PAPRIKA_S3_NONVIDEO_ACCESS_KEY")).strip()
        secret_key = b.get("secret_key") or _obj._s3cfg("s3_nonvideo_secret_key", "PAPRIKA_S3_NONVIDEO_SECRET_KEY")
        if not endpoint:
            return {"ok": False, "message": "非動画 tier のエンドポイントが未設定です (= 分離OFF)"}
    else:
        endpoint = (b.get("endpoint") or _obj._s3cfg("s3_endpoint", "PAPRIKA_S3_ENDPOINT")).strip()
        bucket = (b.get("bucket") or _obj._s3cfg("s3_bucket", "PAPRIKA_S3_BUCKET", "paprika")).strip()
        region = (b.get("region") or _obj._s3cfg("s3_region", "PAPRIKA_S3_REGION", "us-east-1")).strip() or "us-east-1"
        access_key = (b.get("access_key") or _obj._s3cfg("s3_access_key", "PAPRIKA_S3_ACCESS_KEY")).strip()
        secret_key = b.get("secret_key") or _obj._s3cfg("s3_secret_key", "PAPRIKA_S3_SECRET_KEY")

    if not bucket:
        return {"ok": False, "message": "バケット名が未設定です"}

    def _test() -> dict:
        try:
            import boto3
            from botocore.config import Config as _BotoConfig
        except ImportError:
            return {"ok": False, "message": "boto3 がインストールされていません"}
        # Per-call client -> must be closed. A boto3 client owns a urllib3
        # pool; dropping the reference leaves its sockets to the GC, where they
        # sit in CLOSE-WAIT after MinIO drops the keep-alive. The operator can
        # press "テスト" many times while tuning endpoints, and each press used
        # to cost the hub a permanent fd (2026-07-24 EMFILE outage).
        client = None
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
            # head_bucket is the connectivity gate: endpoint + credentials +
            # bucket all good (sub-second even on a huge bucket).
            client.head_bucket(Bucket=bucket)
        except Exception as e:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}
        # The follow-up list is a BONUS (list permission / object presence). A
        # whole-bucket list -- even MaxKeys=1 -- can take tens of seconds on a
        # large bucket over slow storage (.8 paprika measured ~40s), so a list
        # failure AFTER a good head_bucket must NOT fail the test.
        try:
            r = client.list_objects_v2(Bucket=bucket, MaxKeys=1)
            return {
                "ok": True,
                "message": f"接続成功 (bucket={bucket}, objects≧{r.get('KeyCount', 0)})",
            }
        except Exception as le:
            return {
                "ok": True,
                "message": f"接続成功 (bucket={bucket}; head OK / list 応答遅延: {type(le).__name__})",
            }
        finally:
            try:
                client.close()
            except Exception:
                pass

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
    that data to flat files under the storage dir where it's served
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
