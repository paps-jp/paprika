"""MariaDB connection pool + schema + data migration helpers.

When the operator configures MariaDB connection settings and clicks
"テーブル作成" / "Jobs を移行" etc., this module handles:

  1. **Pool management**: lazy ``aiomysql.Pool`` creation from saved
     settings, with automatic health checks and teardown.
  2. **Schema creation**: idempotent ``CREATE TABLE IF NOT EXISTS`` for
     every table the hub uses.
  3. **Migration functions**: read data from the current backends
     (Redis ``JobStore``, file-backed registries) and batch-insert
     into MariaDB with ``INSERT IGNORE`` so re-runs are safe.

The pool instance lives on ``state.mariadb_pool`` (see ``_state.py``).
It is *not* created at hub startup -- only when the operator actually
triggers a migration or the schema endpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema DDL (idempotent)
# ---------------------------------------------------------------------------

_TABLES: list[tuple[str, str]] = [
    (
        "jobs",
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id        VARCHAR(64)   PRIMARY KEY,
            status        VARCHAR(20)   NOT NULL,
            url           TEXT          NOT NULL,
            mode          VARCHAR(20)   DEFAULT 'fetch',
            goal          TEXT,
            options       JSON,
            worker_id     VARCHAR(128),
            lane_idx      INT,
            session_id    VARCHAR(128),
            created_at    DATETIME(3),
            started_at    DATETIME(3),
            completed_at  DATETIME(3),
            error         TEXT,
            progress      JSON,
            INDEX idx_status     (status),
            INDEX idx_created_at (created_at),
            INDEX idx_worker_id  (worker_id),
            INDEX idx_url_prefix (url(255))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "job_results",
        """
        CREATE TABLE IF NOT EXISTS job_results (
            job_id          VARCHAR(64)   PRIMARY KEY,
            status          VARCHAR(20),
            html_href       TEXT,
            log_href        TEXT,
            assets          JSON,
            assets_failed   INT           DEFAULT 0,
            video_detection JSON,
            video_urls_seen JSON,
            iframe_srcs     JSON,
            ytdlp_results   JSON,
            visited_urls    JSON,
            error           TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "job_logs",
        """
        CREATE TABLE IF NOT EXISTS job_logs (
            id       BIGINT        AUTO_INCREMENT PRIMARY KEY,
            job_id   VARCHAR(64)   NOT NULL,
            line_num INT           NOT NULL,
            line     TEXT          NOT NULL,
            INDEX idx_job_id (job_id),
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "hosts",
        """
        CREATE TABLE IF NOT EXISTS hosts (
            host                VARCHAR(255) PRIMARY KEY,
            cookies             JSON,
            notes               TEXT,
            recrawl_patterns    JSON,
            popup_policy        VARCHAR(20)  DEFAULT 'kill',
            login_url           TEXT,
            login_goal          TEXT,
            login_check         VARCHAR(255),
            login_refresh_ttl_s INT          DEFAULT 900,
            last_login_at       DATETIME(3),
            fetch_recipes       JSON,
            created_at          DATETIME(3),
            updated_at          DATETIME(3),
            last_used_at        DATETIME(3),
            INDEX idx_updated (updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "visited_urls",
        """
        CREATE TABLE IF NOT EXISTS visited_urls (
            id         BIGINT        AUTO_INCREMENT PRIMARY KEY,
            host       VARCHAR(255)  NOT NULL,
            url        TEXT          NOT NULL,
            url_hash   VARCHAR(40)   NOT NULL,
            visited_at DATETIME(3)   DEFAULT CURRENT_TIMESTAMP(3),
            INDEX idx_host (host),
            UNIQUE KEY uk_host_url (host, url_hash)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    # ---- Additional registries ----
    (
        "skills",
        """
        CREATE TABLE IF NOT EXISTS skills (
            slug             VARCHAR(255)  PRIMARY KEY,
            tier             VARCHAR(20)   NOT NULL DEFAULT 'auto',
            name             VARCHAR(255)  NOT NULL DEFAULT '',
            description      TEXT,
            code_template    MEDIUMTEXT,
            llm_instructions MEDIUMTEXT,
            applicable_when  JSON,
            tags             JSON,
            auto_extracted   TINYINT(1)    DEFAULT 1,
            extracted_from   JSON,
            use_count        INT           DEFAULT 0,
            created_at       DATETIME(3),
            updated_at       DATETIME(3),
            last_used_at     DATETIME(3),
            INDEX idx_tier (tier)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "conventions",
        """
        CREATE TABLE IF NOT EXISTS conventions (
            slug             VARCHAR(255)  PRIMARY KEY,
            tier             VARCHAR(20)   NOT NULL DEFAULT 'auto',
            name             VARCHAR(255)  NOT NULL DEFAULT '',
            advice           TEXT,
            rationale        TEXT,
            bad_example      TEXT,
            good_example     TEXT,
            applicable_when  JSON,
            tags             JSON,
            extracted_from   JSON,
            use_count        INT           DEFAULT 0,
            created_at       DATETIME(3),
            updated_at       DATETIME(3),
            last_used_at     DATETIME(3),
            INDEX idx_tier (tier)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "engines",
        """
        CREATE TABLE IF NOT EXISTS engines (
            slug                VARCHAR(128)  PRIMARY KEY,
            name                VARCHAR(255)  NOT NULL DEFAULT '',
            kind                VARCHAR(30)   DEFAULT 'chat',
            protocol            VARCHAR(30)   DEFAULT 'openai',
            endpoint            TEXT,
            model               VARCHAR(255)  DEFAULT '',
            api_key_env         VARCHAR(128)  DEFAULT '',
            api_key             TEXT          DEFAULT '',
            headers             JSON,
            timeout_s           INT           DEFAULT 120,
            promoted            TINYINT(1)    DEFAULT 0,
            supports_tools      TINYINT(1)    DEFAULT 1,
            use_for_codegen     TINYINT(1)    DEFAULT 0,
            daily_token_budget  INT           DEFAULT 0,
            daily_request_budget INT          DEFAULT 0,
            notes               TEXT,
            builtin             TINYINT(1)    DEFAULT 0,
            created_at          DATETIME(3),
            updated_at          DATETIME(3)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "presets",
        """
        CREATE TABLE IF NOT EXISTS presets (
            name                    VARCHAR(255)  PRIMARY KEY,
            category                VARCHAR(128)  DEFAULT '',
            description             TEXT,
            ui_mode                 VARCHAR(20)   DEFAULT 'fetch',
            ai_engine               VARCHAR(30)   DEFAULT 'codegen',
            url                     TEXT,
            goal                    MEDIUMTEXT,
            simple_rows             JSON,
            code_script             MEDIUMTEXT,
            max_attempts            INT           DEFAULT 3,
            attempt_timeout_s       INT           DEFAULT 86400,
            attempt_timeout_simple_s INT          DEFAULT 600,
            host_dedup              TINYINT(1)    DEFAULT 1,
            options                 JSON,
            created_at              DATETIME(3),
            updated_at              DATETIME(3),
            last_used_at            DATETIME(3),
            INDEX idx_category (category)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
]


# ---------------------------------------------------------------------------
# Pool helpers
# ---------------------------------------------------------------------------

async def create_pool(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
) -> Any:
    """Create an ``aiomysql.Pool``.  Returns the pool object."""
    import aiomysql

    return await aiomysql.create_pool(
        host=host,
        port=port,
        db=database,
        user=username,
        password=password,
        minsize=1,
        maxsize=5,
        autocommit=True,
        charset="utf8mb4",
    )


async def close_pool(pool: Any) -> None:
    """Gracefully close an aiomysql pool."""
    if pool is None:
        return
    try:
        pool.close()
        await pool.wait_closed()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

async def ensure_schema(pool: Any) -> list[str]:
    """Run all CREATE TABLE IF NOT EXISTS statements.

    Returns the list of table names that were ensured.
    """
    created: list[str] = []
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for name, ddl in _TABLES:
                await cur.execute(ddl)
                created.append(name)
    return created


async def table_counts(pool: Any) -> dict[str, int]:
    """Return row counts for each known table (0 if table doesn't exist)."""
    counts: dict[str, int] = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for name, _ in _TABLES:
                try:
                    await cur.execute(f"SELECT COUNT(*) FROM `{name}`")
                    row = await cur.fetchone()
                    counts[name] = row[0] if row else 0
                except Exception:
                    counts[name] = -1  # table missing
    return counts


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------

def _parse_dt(v: Any) -> datetime | None:
    """Parse an ISO datetime string, a datetime object, or None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # Strip trailing Z and handle timezone-aware strings
        s = s.rstrip("Z")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _json_dumps(v: Any) -> str | None:
    """Serialise a value to a JSON string, or None if empty/None."""
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Migration: Jobs  (Redis → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_jobs(
    store: Any,
    pool: Any,
    *,
    progress: Callable[[int, int], None] | None = None,
    purge: bool = True,
) -> dict:
    """Migrate all jobs from the current JobStore to MariaDB.

    Returns ``{"migrated": N, "skipped": M, "errors": [...]}``
    where *skipped* counts rows that already existed (INSERT IGNORE).
    """
    job_ids = await store.list_job_ids(offset=0, limit=0)
    total = len(job_ids)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    BATCH = 50
    for i in range(0, total, BATCH):
        batch_ids = job_ids[i : i + BATCH]
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for jid in batch_ids:
                    try:
                        info = await store.get_job_info(jid)
                        if info is None:
                            skipped += 1
                            continue

                        # ---- jobs table ----
                        opts = info.options
                        await cur.execute(
                            """INSERT IGNORE INTO jobs
                               (job_id, status, url, mode, goal, options,
                                worker_id, lane_idx, session_id,
                                created_at, started_at, completed_at,
                                error, progress)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                       %s,%s,%s,%s,%s)""",
                            (
                                info.job_id,
                                info.status.value if hasattr(info.status, "value") else str(info.status),
                                info.url,
                                opts.mode if opts else "fetch",
                                opts.goal if opts else None,
                                _json_dumps(opts.model_dump() if opts else None),
                                info.worker_id,
                                info.lane_idx,
                                info.session_id,
                                _parse_dt(info.created_at),
                                _parse_dt(info.started_at),
                                _parse_dt(info.completed_at),
                                info.error,
                                _json_dumps(info.progress.model_dump() if info.progress else None),
                            ),
                        )
                        affected = cur.rowcount
                        if affected == 0:
                            skipped += 1
                            # Still existing row -- skip result+logs too
                            continue

                        # ---- job_results table ----
                        try:
                            result = await store.get_job_result(jid)
                            if result is not None:
                                await cur.execute(
                                    """INSERT IGNORE INTO job_results
                                       (job_id, status, html_href, log_href,
                                        assets, assets_failed,
                                        video_detection, video_urls_seen,
                                        iframe_srcs, ytdlp_results,
                                        visited_urls, error)
                                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                    (
                                        result.job_id,
                                        result.status.value if hasattr(result.status, "value") else str(result.status),
                                        result.html_href,
                                        result.log_href,
                                        _json_dumps([a.model_dump() for a in result.assets] if result.assets else []),
                                        result.assets_failed,
                                        _json_dumps(result.video_detection),
                                        _json_dumps(result.video_urls_seen),
                                        _json_dumps(result.iframe_srcs),
                                        _json_dumps([y.model_dump() for y in result.ytdlp_results] if result.ytdlp_results else []),
                                        _json_dumps(result.visited_urls),
                                        result.error,
                                    ),
                                )
                        except Exception as e:
                            log.debug("job_result for %s: %s", jid, e)

                        # ---- job_logs table ----
                        try:
                            lines = await store.get_log_lines(jid)
                            if lines:
                                LOG_BATCH = 200
                                for li in range(0, len(lines), LOG_BATCH):
                                    batch_lines = lines[li : li + LOG_BATCH]
                                    values = [
                                        (jid, li + idx, line)
                                        for idx, line in enumerate(batch_lines)
                                    ]
                                    await cur.executemany(
                                        "INSERT IGNORE INTO job_logs (job_id, line_num, line) VALUES (%s,%s,%s)",
                                        values,
                                    )
                        except Exception as e:
                            log.debug("job_logs for %s: %s", jid, e)

                        migrated += 1
                    except Exception as e:
                        errors.append({"job_id": jid, "error": str(e)})
                        log.warning("migrate job %s failed: %s", jid, e)

        if progress:
            progress(min(i + BATCH, total), total)

    # ---- Purge source data (Redis) after successful migration ----
    purged = 0
    if purge and not errors:
        for jid in job_ids:
            try:
                await store.delete_job(jid)
                purged += 1
            except Exception as e:
                log.warning("purge job %s from source failed: %s", jid, e)

    return {
        "ok": True,
        "category": "jobs",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "purged": purged,
        "errors": errors[:20],  # cap for response size
    }


# ---------------------------------------------------------------------------
# Migration: Hosts  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_hosts(
    host_registry: Any,
    pool: Any,
    *,
    progress: Callable[[int, int], None] | None = None,
    purge: bool = True,
) -> dict:
    """Migrate HostRegistry files to MariaDB."""
    from dataclasses import asdict

    all_hosts = host_registry.list_all()
    total = len(all_hosts)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for idx, rec in enumerate(all_hosts):
                try:
                    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
                    recipes = d.get("fetch_recipes", [])
                    # Normalise recipes to plain dicts
                    recipe_dicts = []
                    for r in (recipes or []):
                        if hasattr(r, "to_json"):
                            recipe_dicts.append(r.to_json())
                        elif isinstance(r, dict):
                            recipe_dicts.append(r)
                        else:
                            recipe_dicts.append(asdict(r))

                    await cur.execute(
                        """INSERT IGNORE INTO hosts
                           (host, cookies, notes, recrawl_patterns,
                            popup_policy, login_url, login_goal,
                            login_check, login_refresh_ttl_s,
                            last_login_at, fetch_recipes,
                            created_at, updated_at, last_used_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            d.get("host", ""),
                            _json_dumps(d.get("cookies", [])),
                            d.get("notes"),
                            _json_dumps(d.get("recrawl_patterns", [])),
                            d.get("popup_policy", "kill"),
                            d.get("login_url"),
                            d.get("login_goal"),
                            d.get("login_check"),
                            d.get("login_refresh_ttl_s", 900),
                            _parse_dt(d.get("last_login_at")),
                            _json_dumps(recipe_dicts),
                            _parse_dt(d.get("created_at")),
                            _parse_dt(d.get("updated_at")),
                            _parse_dt(d.get("last_used_at")),
                        ),
                    )
                    if cur.rowcount > 0:
                        migrated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    host_name = getattr(rec, "host", "?")
                    errors.append({"host": host_name, "error": str(e)})
                    log.warning("migrate host %s failed: %s", host_name, e)

                if progress and (idx + 1) % 50 == 0:
                    progress(idx + 1, total)

    if progress:
        progress(total, total)

    # ---- Purge source data (JSON files) after successful migration ----
    purged = 0
    if purge and not errors:
        for rec in all_hosts:
            host = getattr(rec, "host", None)
            if host:
                try:
                    host_registry.delete(host)
                    purged += 1
                except Exception as e:
                    log.warning("purge host %s from source failed: %s", host, e)

    return {
        "ok": True,
        "category": "hosts",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "purged": purged,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Migration: Visited URLs  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

def _url_hash(url: str) -> str:
    """Short SHA-1 hash for dedup key (matches host_visited.py logic)."""
    return hashlib.sha1(url.encode("utf-8", errors="replace")).hexdigest()[:16]


async def migrate_visited_urls(
    host_registry: Any,
    visited_registry: Any,
    pool: Any,
    *,
    progress: Callable[[int, int], None] | None = None,
    purge: bool = True,
) -> dict:
    """Migrate per-host visited URL sets to MariaDB."""
    all_hosts = host_registry.list_all()
    total_hosts = len(all_hosts)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    for idx, rec in enumerate(all_hosts):
        host = getattr(rec, "host", None)
        if not host:
            continue
        try:
            urls = visited_registry.all_urls(host)
            if not urls:
                continue

            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    BATCH = 200
                    url_list = list(urls) if not isinstance(urls, list) else urls
                    for bi in range(0, len(url_list), BATCH):
                        batch = url_list[bi : bi + BATCH]
                        values = [
                            (host, u, _url_hash(u))
                            for u in batch
                        ]
                        await cur.executemany(
                            "INSERT IGNORE INTO visited_urls (host, url, url_hash) VALUES (%s,%s,%s)",
                            values,
                        )
                        migrated += cur.rowcount
                        skipped += len(batch) - cur.rowcount
        except Exception as e:
            errors.append({"host": host, "error": str(e)})
            log.warning("migrate visited_urls for %s failed: %s", host, e)

        if progress and (idx + 1) % 20 == 0:
            progress(idx + 1, total_hosts)

    if progress:
        progress(total_hosts, total_hosts)

    # ---- Purge source data (JSON files) after successful migration ----
    purged = 0
    if purge and not errors:
        for rec in all_hosts:
            host = getattr(rec, "host", None)
            if host:
                try:
                    visited_registry.delete_host(host)
                    purged += 1
                except Exception as e:
                    log.warning("purge visited_urls for %s from source failed: %s", host, e)

    return {
        "ok": True,
        "category": "visited_urls",
        "migrated": migrated,
        "skipped": skipped,
        "total_hosts": total_hosts,
        "purged": purged,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Migration: Skills  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_skills(
    skill_registry: Any,
    pool: Any,
    *,
    purge: bool = True,
) -> dict:
    """Migrate SkillRegistry files to MariaDB."""
    from dataclasses import asdict

    all_skills = skill_registry.list_all()
    total = len(all_skills)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for rec in all_skills:
                try:
                    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
                    await cur.execute(
                        """INSERT IGNORE INTO skills
                           (slug, tier, name, description,
                            code_template, llm_instructions,
                            applicable_when, tags, auto_extracted,
                            extracted_from, use_count,
                            created_at, updated_at, last_used_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            d.get("slug", ""),
                            d.get("tier", "auto"),
                            d.get("name", ""),
                            d.get("description"),
                            d.get("code_template"),
                            d.get("llm_instructions"),
                            _json_dumps(d.get("applicable_when", [])),
                            _json_dumps(d.get("tags", [])),
                            1 if d.get("auto_extracted", True) else 0,
                            _json_dumps(d.get("extracted_from", [])),
                            d.get("use_count", 0),
                            _parse_dt(d.get("created_at")),
                            _parse_dt(d.get("updated_at")),
                            _parse_dt(d.get("last_used_at")),
                        ),
                    )
                    if cur.rowcount > 0:
                        migrated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    slug = d.get("slug", "?") if isinstance(d, dict) else getattr(rec, "slug", "?")
                    errors.append({"slug": slug, "error": str(e)})
                    log.warning("migrate skill %s failed: %s", slug, e)

    # ---- Purge source data (JSON files) after successful migration ----
    purged = 0
    if purge and not errors:
        for rec in all_skills:
            slug = getattr(rec, "slug", None)
            if slug:
                try:
                    skill_registry.delete(slug)
                    purged += 1
                except Exception as e:
                    log.warning("purge skill %s from source failed: %s", slug, e)

    return {
        "ok": True,
        "category": "skills",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "purged": purged,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Migration: Conventions  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_conventions(
    convention_registry: Any,
    pool: Any,
    *,
    purge: bool = True,
) -> dict:
    """Migrate ConventionRegistry files to MariaDB."""
    from dataclasses import asdict

    all_convs = convention_registry.list_all()
    total = len(all_convs)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for rec in all_convs:
                try:
                    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
                    await cur.execute(
                        """INSERT IGNORE INTO conventions
                           (slug, tier, name, advice, rationale,
                            bad_example, good_example,
                            applicable_when, tags, extracted_from,
                            use_count, created_at, updated_at,
                            last_used_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            d.get("slug", ""),
                            d.get("tier", "auto"),
                            d.get("name", ""),
                            d.get("advice"),
                            d.get("rationale"),
                            d.get("bad_example"),
                            d.get("good_example"),
                            _json_dumps(d.get("applicable_when", [])),
                            _json_dumps(d.get("tags", [])),
                            _json_dumps(d.get("extracted_from", [])),
                            d.get("use_count", 0),
                            _parse_dt(d.get("created_at")),
                            _parse_dt(d.get("updated_at")),
                            _parse_dt(d.get("last_used_at")),
                        ),
                    )
                    if cur.rowcount > 0:
                        migrated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    slug = d.get("slug", "?") if isinstance(d, dict) else getattr(rec, "slug", "?")
                    errors.append({"slug": slug, "error": str(e)})
                    log.warning("migrate convention %s failed: %s", slug, e)

    # ---- Purge source data (JSON files) after successful migration ----
    purged = 0
    if purge and not errors:
        for rec in all_convs:
            slug = getattr(rec, "slug", None)
            if slug:
                try:
                    convention_registry.delete(slug)
                    purged += 1
                except Exception as e:
                    log.warning("purge convention %s from source failed: %s", slug, e)

    return {
        "ok": True,
        "category": "conventions",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "purged": purged,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Migration: Engines  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_engines(
    engine_registry: Any,
    pool: Any,
    *,
    purge: bool = True,
) -> dict:
    """Migrate EngineRegistry files to MariaDB."""
    from dataclasses import asdict

    all_engines = engine_registry.list_all()
    total = len(all_engines)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for rec in all_engines:
                try:
                    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
                    await cur.execute(
                        """INSERT IGNORE INTO engines
                           (slug, name, kind, protocol, endpoint,
                            model, api_key_env, api_key, headers,
                            timeout_s, promoted, supports_tools,
                            use_for_codegen, daily_token_budget,
                            daily_request_budget, notes, builtin,
                            created_at, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                   %s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            d.get("slug", ""),
                            d.get("name", ""),
                            d.get("kind", "chat"),
                            d.get("protocol", "openai"),
                            d.get("endpoint", ""),
                            d.get("model", ""),
                            d.get("api_key_env", ""),
                            d.get("api_key", ""),
                            _json_dumps(d.get("headers", {})),
                            d.get("timeout_s", 120),
                            1 if d.get("promoted") else 0,
                            1 if d.get("supports_tools", True) else 0,
                            1 if d.get("use_for_codegen") else 0,
                            d.get("daily_token_budget", 0),
                            d.get("daily_request_budget", 0),
                            d.get("notes", ""),
                            1 if d.get("builtin") else 0,
                            _parse_dt(d.get("created_at")),
                            _parse_dt(d.get("updated_at")),
                        ),
                    )
                    if cur.rowcount > 0:
                        migrated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    slug = d.get("slug", "?") if isinstance(d, dict) else getattr(rec, "slug", "?")
                    errors.append({"slug": slug, "error": str(e)})
                    log.warning("migrate engine %s failed: %s", slug, e)

    # ---- Purge source data (JSON files) after successful migration ----
    purged = 0
    if purge and not errors:
        for rec in all_engines:
            slug = getattr(rec, "slug", None)
            if slug:
                try:
                    engine_registry.delete(slug)
                    purged += 1
                except Exception as e:
                    log.warning("purge engine %s from source failed: %s", slug, e)

    return {
        "ok": True,
        "category": "engines",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "purged": purged,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Migration: Presets  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_presets(
    preset_registry: Any,
    pool: Any,
    *,
    purge: bool = True,
) -> dict:
    """Migrate PresetRegistry files to MariaDB."""
    from dataclasses import asdict

    all_presets = preset_registry.list_all()
    total = len(all_presets)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for rec in all_presets:
                try:
                    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
                    await cur.execute(
                        """INSERT IGNORE INTO presets
                           (name, category, description, ui_mode,
                            ai_engine, url, goal, simple_rows,
                            code_script, max_attempts,
                            attempt_timeout_s, attempt_timeout_simple_s,
                            host_dedup, options,
                            created_at, updated_at, last_used_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            d.get("name", ""),
                            d.get("category", ""),
                            d.get("description", ""),
                            d.get("ui_mode", "fetch"),
                            d.get("ai_engine", "codegen"),
                            d.get("url", ""),
                            d.get("goal", ""),
                            _json_dumps(d.get("simple_rows", [])),
                            d.get("code_script", ""),
                            d.get("max_attempts", 3),
                            d.get("attempt_timeout_s", 86400),
                            d.get("attempt_timeout_simple_s", 600),
                            1 if d.get("host_dedup", True) else 0,
                            _json_dumps(d.get("options", {})),
                            _parse_dt(d.get("created_at")),
                            _parse_dt(d.get("updated_at")),
                            _parse_dt(d.get("last_used_at")),
                        ),
                    )
                    if cur.rowcount > 0:
                        migrated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    name = d.get("name", "?") if isinstance(d, dict) else getattr(rec, "name", "?")
                    errors.append({"name": name, "error": str(e)})
                    log.warning("migrate preset %s failed: %s", name, e)

    # ---- Purge source data (JSON files) after successful migration ----
    purged = 0
    if purge and not errors:
        for rec in all_presets:
            n = getattr(rec, "name", None)
            if n:
                try:
                    preset_registry.delete(n)
                    purged += 1
                except Exception as e:
                    log.warning("purge preset %s from source failed: %s", n, e)

    return {
        "ok": True,
        "category": "presets",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "purged": purged,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Restore: MariaDB → file-backed registries (startup auto-populate)
# ---------------------------------------------------------------------------
# After migration + purge, the file-backed registries are empty. These
# functions read from MariaDB and re-populate the registries so the hub
# serves data from MariaDB even though registries are still file-backed.
# Only called at startup when a registry is empty but MariaDB has rows.
# ---------------------------------------------------------------------------

async def restore_hosts(pool: Any, host_registry: Any) -> int:
    """Mirror MariaDB ``hosts`` table to the file-backed HostRegistry.
    Returns the number of records restored.

    MariaDB is the source of truth: any host present on disk but
    missing from MariaDB is removed from disk. See ``restore_engines``
    for the original rationale (phpMyAdmin / direct-SQL deletions
    need to survive a hub restart instead of being undone by the
    file mirror)."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT host, cookies, notes, recrawl_patterns, "
                "popup_policy, login_url, login_goal, login_check, "
                "login_refresh_ttl_s, last_login_at, fetch_recipes, "
                "created_at, updated_at, last_used_at "
                "FROM hosts"
            )
            rows = await cur.fetchall()

    # Deletion reconciliation (runs BEFORE upsert pass so a row
    # dropped from MariaDB doesn't survive on disk). Normalise both
    # sides via the same ``_normalise_host`` the registry applies on
    # write, so a "FOO.com" row in MariaDB doesn't false-positive
    # against a "foo.com" file (delete-then-recreate cycle).
    from server.hub.hosts import _normalise_host as _norm_host
    mariadb_hosts = {_norm_host(row[0]) for row in rows if row[0]}
    for existing in list(host_registry.list_all()):
        if _norm_host(existing.host) not in mariadb_hosts:
            try:
                if host_registry.delete(existing.host):
                    log.info(
                        "restore: removed host %s (no longer in MariaDB)",
                        existing.host,
                    )
            except Exception as e:
                log.warning(
                    "restore: removing host %s failed: %s", existing.host, e,
                )

    restored = 0
    for row in rows:
        try:
            host_registry.upsert(
                host=row[0],
                cookies=json.loads(row[1]) if row[1] else [],
                notes=row[2],
                recrawl_patterns=json.loads(row[3]) if row[3] else [],
                popup_policy=row[4] or "kill",
                login_url=row[5],
                login_goal=row[6],
                login_check=row[7],
                login_refresh_ttl_s=row[8],
                last_login_at=str(row[9]) if row[9] else None,
                fetch_recipes=json.loads(row[10]) if row[10] else [],
            )
            restored += 1
        except Exception as e:
            log.warning("restore host %s failed: %s", row[0], e)
    return restored


async def restore_visited_urls(pool: Any, visited_registry: Any) -> int:
    """Mirror MariaDB ``visited_urls`` table to HostVisitedRegistry.

    MariaDB is source of truth. Per-host reconciliation:
      * a host present on disk but missing from MariaDB     -> file deleted
      * a host present in both                              -> on-disk URLs
        that aren't in MariaDB are removed; missing URLs are added
    Empty MariaDB (rows == []) still triggers reconciliation -- which
    means hosts with zero visited URLs will be wiped from disk. That's
    only an issue if MariaDB hasn't been populated yet; the caller
    (``restore_all_registries``) guards this with ``_mdb_count > 0``.
    """
    from pathlib import Path as _Path

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT host, url FROM visited_urls ORDER BY host"
            )
            rows = await cur.fetchall()

    # Group MariaDB rows by normalised host -> set(urls). The
    # visited registry normalises hosts on write (lowercase, strip
    # www.) so we apply the same on read; otherwise a "FOO.com"
    # MariaDB row vs a "foo.com" file would false-positive.
    from collections import defaultdict
    from server.hub.host_visited import _normalise_host as _norm_vh
    mariadb_by_host: dict[str, set[str]] = defaultdict(set)
    for host, url in rows:
        if host:
            mariadb_by_host[_norm_vh(host)].add(url)

    # Enumerate hosts that have a file on disk. HostVisitedRegistry
    # doesn't expose a list_hosts() helper, so we glob its data dir
    # and read each file's ``host`` field (already normalised at
    # write-time). Tolerate unreadable files rather than failing.
    file_hosts: set[str] = set()
    try:
        for p in _Path(visited_registry.dir).glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                h = d.get("host")
                if h:
                    file_hosts.add(_norm_vh(h))
            except Exception:
                continue
    except Exception as e:
        log.warning("restore: could not enumerate visited dir: %s", e)

    # Drop hosts that no longer appear in MariaDB.
    for h in file_hosts:
        if h not in mariadb_by_host:
            try:
                if visited_registry.delete_host(h):
                    log.info(
                        "restore: removed visited %s (no longer in MariaDB)",
                        h,
                    )
            except Exception as e:
                log.warning(
                    "restore: removing visited %s failed: %s", h, e,
                )

    # For each MariaDB host, remove file-only URLs then add the rest.
    restored = 0
    for host, mdb_urls in mariadb_by_host.items():
        try:
            file_urls = set(visited_registry.all_urls(host))
            for stale in file_urls - mdb_urls:
                try:
                    visited_registry.remove(host, stale)
                except Exception as e:
                    log.warning(
                        "restore: drop stale url %s for %s failed: %s",
                        stale, host, e,
                    )
            restored += visited_registry.add_many(host, list(mdb_urls))
        except Exception as e:
            log.warning("restore visited_urls for %s failed: %s", host, e)
    return restored


async def restore_skills(pool: Any, skill_registry: Any) -> int:
    """Mirror MariaDB ``skills`` table to the file-backed SkillRegistry.
    Skills are tiered (curated / auto) so the reconciliation key is
    (tier, slug), not just slug -- a curated entry must NOT shadow-
    delete an auto entry with the same slug."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT slug, tier, name, description, "
                "code_template, llm_instructions, "
                "applicable_when, tags, auto_extracted, "
                "extracted_from, use_count, "
                "created_at, updated_at, last_used_at "
                "FROM skills"
            )
            rows = await cur.fetchall()

    # Deletion reconciliation: drop any file-side (tier, slug) pair
    # missing from MariaDB. Slugs are normalised on both sides
    # (SkillRegistry._slug applies ``normalise_slug`` on write) so
    # case-mismatched MariaDB keys don't false-positive.
    from server.hub.skills import normalise_slug as _norm_sk_slug
    mariadb_keys = {
        (row[1] or "auto", _norm_sk_slug(row[0])) for row in rows if row[0]
    }
    for existing in list(skill_registry.list_all()):
        key = (existing.tier, _norm_sk_slug(existing.slug))
        if key not in mariadb_keys:
            try:
                if skill_registry.delete(existing.slug, tier=existing.tier):
                    log.info(
                        "restore: removed skill %s/%s (no longer in MariaDB)",
                        existing.tier, existing.slug,
                    )
            except Exception as e:
                log.warning(
                    "restore: removing skill %s/%s failed: %s",
                    existing.tier, existing.slug, e,
                )

    restored = 0
    for row in rows:
        try:
            skill_registry.upsert(
                row[0],  # slug
                name=row[2] or "",
                description=row[3] or "",
                code_template=row[4] or "",
                llm_instructions=row[5] or "",
                applicable_when=json.loads(row[6]) if row[6] else [],
                tags=json.loads(row[7]) if row[7] else [],
                auto_extracted=bool(row[8]),
                extracted_from=json.loads(row[9]) if row[9] else [],
                tier=row[1] or "auto",
            )
            restored += 1
        except Exception as e:
            log.warning("restore skill %s failed: %s", row[0], e)
    return restored


async def restore_conventions(pool: Any, convention_registry: Any) -> int:
    """Mirror MariaDB ``conventions`` table. Same (tier, slug) keying
    as ``restore_skills`` -- conventions live in the same TieredJson
    layout (curated / auto)."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT slug, tier, name, advice, rationale, "
                "bad_example, good_example, "
                "applicable_when, tags, extracted_from, "
                "use_count, created_at, updated_at, last_used_at "
                "FROM conventions"
            )
            rows = await cur.fetchall()

    from server.hub.conventions import normalise_slug as _norm_cv_slug
    mariadb_keys = {
        (row[1] or "auto", _norm_cv_slug(row[0])) for row in rows if row[0]
    }
    for existing in list(convention_registry.list_all()):
        key = (existing.tier, _norm_cv_slug(existing.slug))
        if key not in mariadb_keys:
            try:
                if convention_registry.delete(existing.slug, tier=existing.tier):
                    log.info(
                        "restore: removed convention %s/%s (no longer in MariaDB)",
                        existing.tier, existing.slug,
                    )
            except Exception as e:
                log.warning(
                    "restore: removing convention %s/%s failed: %s",
                    existing.tier, existing.slug, e,
                )

    restored = 0
    for row in rows:
        try:
            convention_registry.upsert(
                row[0],  # slug
                name=row[2] or "",
                advice=row[3] or "",
                rationale=row[4] or "",
                bad_example=row[5] or "",
                good_example=row[6] or "",
                applicable_when=json.loads(row[7]) if row[7] else [],
                tags=json.loads(row[8]) if row[8] else [],
                extracted_from=json.loads(row[9]) if row[9] else [],
                tier=row[1] or "auto",
            )
            restored += 1
        except Exception as e:
            log.warning("restore convention %s failed: %s", row[0], e)
    return restored


async def restore_engines(pool: Any, engine_registry: Any) -> int:
    """Mirror MariaDB ``engines`` table to the file-backed EngineRegistry.

    MariaDB is the source of truth: any engine that exists on the
    filesystem but NOT in MariaDB is removed from the filesystem. This
    propagates phpMyAdmin / direct-SQL deletions to the file mirror.
    Previously this method only INSERTed; stale files were left
    behind, causing UI count > MariaDB count drift.
    """
    from server.hub.engines import EngineRecord

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT slug, name, kind, protocol, endpoint, "
                "model, api_key_env, api_key, headers, "
                "timeout_s, promoted, supports_tools, "
                "use_for_codegen, daily_token_budget, "
                "daily_request_budget, notes, builtin, "
                "created_at, updated_at "
                "FROM engines"
            )
            rows = await cur.fetchall()

    # Deletion reconciliation: any slug present on disk but missing in
    # MariaDB is dropped. Run BEFORE the upsert pass so a row that
    # disappeared from MariaDB doesn't survive on disk. Both sides are
    # passed through ``normalise_slug`` -- otherwise a "Claude" row in
    # MariaDB compares unequal to a "claude" file and triggers a
    # spurious delete-then-recreate cycle on every restart.
    from server.hub.engines import normalise_slug as _norm_eng_slug
    mariadb_slugs = {_norm_eng_slug(row[0]) for row in rows if row[0]}
    for existing in list(engine_registry.list_all()):
        if _norm_eng_slug(existing.slug) not in mariadb_slugs:
            try:
                if engine_registry.delete(existing.slug):
                    log.info(
                        "restore: removed %s.json (no longer in MariaDB)",
                        existing.slug,
                    )
            except Exception as e:
                log.warning(
                    "restore: removing %s failed: %s", existing.slug, e,
                )

    restored = 0
    for row in rows:
        try:
            rec = EngineRecord(
                slug=row[0],
                name=row[1] or "",
                kind=row[2] or "chat",
                protocol=row[3] or "openai",
                endpoint=row[4] or "",
                model=row[5] or "",
                api_key_env=row[6] or "",
                api_key=row[7] or "",
                headers=json.loads(row[8]) if row[8] else {},
                timeout_s=row[9] or 120,
                promoted=bool(row[10]),
                supports_tools=bool(row[11]),
                use_for_codegen=bool(row[12]),
                daily_token_budget=row[13] or 0,
                daily_request_budget=row[14] or 0,
                notes=row[15] or "",
                builtin=bool(row[16]),
            )
            engine_registry.upsert(rec)
            restored += 1
        except Exception as e:
            log.warning("restore engine %s failed: %s", row[0], e)
    return restored


# ---------------------------------------------------------------------------
# Write-through helpers (per-record CRUD against MariaDB)
# ---------------------------------------------------------------------------
#
# When MariaDB is configured, route handlers call these AFTER updating
# the file-backed registry so MariaDB stays in sync without waiting
# for the next ``auto_migrate_all`` cycle. The "MariaDB is the source
# of truth when connected" contract is preserved by:
#   1. ``restore_engines`` (startup) -- file becomes a mirror of MariaDB
#   2. ``upsert_engine_row`` (write) -- in-flight updates flow to MariaDB
#   3. ``delete_engine_row`` (delete) -- in-flight deletes propagate
# Failures are caught at the call site so a transient MariaDB outage
# doesn't break the admin UI; the next ``auto_migrate_all`` will heal
# the divergence.


async def upsert_engine_row(pool: Any, rec: Any) -> None:
    """INSERT-or-UPDATE one engine record in MariaDB."""
    from dataclasses import asdict
    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO engines
                   (slug, name, kind, protocol, endpoint, model,
                    api_key_env, api_key, headers, timeout_s, promoted,
                    supports_tools, use_for_codegen, daily_token_budget,
                    daily_request_budget, notes, builtin,
                    created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     name=VALUES(name), kind=VALUES(kind),
                     protocol=VALUES(protocol), endpoint=VALUES(endpoint),
                     model=VALUES(model), api_key_env=VALUES(api_key_env),
                     api_key=VALUES(api_key), headers=VALUES(headers),
                     timeout_s=VALUES(timeout_s), promoted=VALUES(promoted),
                     supports_tools=VALUES(supports_tools),
                     use_for_codegen=VALUES(use_for_codegen),
                     daily_token_budget=VALUES(daily_token_budget),
                     daily_request_budget=VALUES(daily_request_budget),
                     notes=VALUES(notes), builtin=VALUES(builtin),
                     updated_at=VALUES(updated_at)""",
                (
                    d.get("slug", ""),
                    d.get("name", ""),
                    d.get("kind", "chat"),
                    d.get("protocol", "openai"),
                    d.get("endpoint", ""),
                    d.get("model", ""),
                    d.get("api_key_env", ""),
                    d.get("api_key", ""),
                    _json_dumps(d.get("headers", {})),
                    d.get("timeout_s", 120),
                    1 if d.get("promoted") else 0,
                    1 if d.get("supports_tools", True) else 0,
                    1 if d.get("use_for_codegen") else 0,
                    d.get("daily_token_budget", 0),
                    d.get("daily_request_budget", 0),
                    d.get("notes", ""),
                    1 if d.get("builtin") else 0,
                    _parse_dt(d.get("created_at")),
                    _parse_dt(d.get("updated_at")),
                ),
            )


async def delete_engine_row(pool: Any, slug: str) -> None:
    """DELETE one engine row from MariaDB. No-op when the row doesn't
    exist (no error -- aligns with the file registry's delete()
    semantics which also tolerate missing records)."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM engines WHERE slug=%s", (slug,))


async def restore_presets(pool: Any, preset_registry: Any) -> int:
    """Mirror MariaDB ``presets`` table to the file-backed
    PresetRegistry. Presets are keyed by ``name`` only (no tier)."""
    from server.hub.presets import PresetRecord

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, category, description, ui_mode, "
                "ai_engine, url, goal, simple_rows, "
                "code_script, max_attempts, "
                "attempt_timeout_s, attempt_timeout_simple_s, "
                "host_dedup, options, "
                "created_at, updated_at, last_used_at "
                "FROM presets"
            )
            rows = await cur.fetchall()

    # Normalise both sides via PresetRegistry's filename-safe key so
    # "Foo Bar" and "foo bar" (which both map to "foo-bar.json")
    # don't cause spurious delete-then-recreate cycles.
    from server.hub.presets import _safe_filename as _norm_preset
    mariadb_names = {_norm_preset(row[0]) for row in rows if row[0]}
    for existing in list(preset_registry.list_all()):
        if _norm_preset(existing.name) not in mariadb_names:
            try:
                if preset_registry.delete(existing.name):
                    log.info(
                        "restore: removed preset %s (no longer in MariaDB)",
                        existing.name,
                    )
            except Exception as e:
                log.warning(
                    "restore: removing preset %s failed: %s",
                    existing.name, e,
                )

    restored = 0
    for row in rows:
        try:
            rec = PresetRecord(
                name=row[0],
                category=row[1] or "",
                description=row[2] or "",
                ui_mode=row[3] or "fetch",
                ai_engine=row[4] or "codegen",
                url=row[5] or "",
                goal=row[6] or "",
                simple_rows=json.loads(row[7]) if row[7] else [],
                code_script=row[8] or "",
                max_attempts=row[9] or 3,
                attempt_timeout_s=row[10] or 86400,
                attempt_timeout_simple_s=row[11] or 600,
                host_dedup=bool(row[12]),
                options=json.loads(row[13]) if row[13] else {},
            )
            preset_registry.upsert(rec)
            restored += 1
        except Exception as e:
            log.warning("restore preset %s failed: %s", row[0], e)
    return restored


async def _mdb_count(pool: Any, table: str) -> int:
    """Quick row count for a MariaDB table (0 if table missing)."""
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                row = await cur.fetchone()
                return row[0] if row else 0
    except Exception:
        return 0


async def restore_all_registries(
    pool: Any,
    *,
    host_registry: Any = None,
    visited_registry: Any = None,
    skill_registry: Any = None,
    convention_registry: Any = None,
    engine_registry: Any = None,
    preset_registry: Any = None,
) -> dict[str, int]:
    """Restore all registries from MariaDB.

    MariaDB is the source of truth after migration.  For each
    category, if the MariaDB table has rows, upsert them into the
    file-backed registry (overwrites local data with MariaDB data).
    Returns ``{category: count_restored}``.
    """
    results: dict[str, int] = {}

    # Hosts
    if host_registry is not None:
        try:
            if await _mdb_count(pool, "hosts") > 0:
                n = await restore_hosts(pool, host_registry)
                if n:
                    results["hosts"] = n
                    log.info("restore: %d hosts from MariaDB", n)
        except Exception as e:
            log.warning("restore hosts failed: %s", e)

    # Visited URLs
    if visited_registry is not None:
        try:
            if await _mdb_count(pool, "visited_urls") > 0:
                n = await restore_visited_urls(pool, visited_registry)
                if n:
                    results["visited_urls"] = n
                    log.info("restore: %d visited URLs from MariaDB", n)
        except Exception as e:
            log.warning("restore visited_urls failed: %s", e)

    # Skills
    if skill_registry is not None:
        try:
            if await _mdb_count(pool, "skills") > 0:
                n = await restore_skills(pool, skill_registry)
                if n:
                    results["skills"] = n
                    log.info("restore: %d skills from MariaDB", n)
        except Exception as e:
            log.warning("restore skills failed: %s", e)

    # Conventions
    if convention_registry is not None:
        try:
            if await _mdb_count(pool, "conventions") > 0:
                n = await restore_conventions(pool, convention_registry)
                if n:
                    results["conventions"] = n
                    log.info("restore: %d conventions from MariaDB", n)
        except Exception as e:
            log.warning("restore conventions failed: %s", e)

    # Engines
    if engine_registry is not None:
        try:
            if await _mdb_count(pool, "engines") > 0:
                n = await restore_engines(pool, engine_registry)
                if n:
                    results["engines"] = n
                    log.info("restore: %d engines from MariaDB", n)
        except Exception as e:
            log.warning("restore engines failed: %s", e)

    # Presets
    if preset_registry is not None:
        try:
            if await _mdb_count(pool, "presets") > 0:
                n = await restore_presets(pool, preset_registry)
                if n:
                    results["presets"] = n
                    log.info("restore: %d presets from MariaDB", n)
        except Exception as e:
            log.warning("restore presets failed: %s", e)

    return results


# ---------------------------------------------------------------------------
# Auto-migrate: startup sync (Redis / files → MariaDB, no purge)
# ---------------------------------------------------------------------------

async def auto_migrate_all(
    pool: Any,
    *,
    redis_url: str | None = None,
    host_registry: Any = None,
    visited_registry: Any = None,
    skill_registry: Any = None,
    convention_registry: Any = None,
    engine_registry: Any = None,
    preset_registry: Any = None,
) -> dict[str, int]:
    """Sync all current data sources into MariaDB at startup.

    Called automatically when MariaDB settings are configured.
    Uses ``INSERT IGNORE`` (idempotent) and **never purges** source
    data, so re-runs are safe and the file/Redis backends stay intact
    as a fallback.

    Returns ``{category: migrated_count}`` for categories that had
    new rows inserted.
    """
    # 1. Ensure schema (CREATE TABLE IF NOT EXISTS)
    try:
        await ensure_schema(pool)
    except Exception as e:
        log.warning("auto-migrate: schema creation failed: %s", e)
        return {}

    results: dict[str, int] = {}

    # 2. Jobs from Redis → MariaDB
    if redis_url:
        try:
            from server.store import RedisJobStore

            tmp_store = RedisJobStore(redis_url)
            try:
                await tmp_store.initialize()
                job_count = await tmp_store.count_jobs()
                if job_count > 0:
                    r = await migrate_jobs(tmp_store, pool, purge=False)
                    if r.get("migrated", 0) > 0:
                        results["jobs"] = r["migrated"]
                        log.info(
                            "auto-migrate: %d jobs → MariaDB (skipped %d)",
                            r["migrated"], r.get("skipped", 0),
                        )
            finally:
                try:
                    await tmp_store.close()
                except Exception:
                    pass
        except Exception as e:
            log.warning("auto-migrate jobs failed: %s", e)

    # 3. Hosts
    if host_registry is not None:
        try:
            hosts = host_registry.list_all()
            if hosts:
                r = await migrate_hosts(host_registry, pool, purge=False)
                if r.get("migrated", 0) > 0:
                    results["hosts"] = r["migrated"]
                    log.info("auto-migrate: %d hosts → MariaDB", r["migrated"])
        except Exception as e:
            log.warning("auto-migrate hosts failed: %s", e)

    # 4. Visited URLs
    if visited_registry is not None and host_registry is not None:
        try:
            r = await migrate_visited_urls(
                host_registry, visited_registry, pool, purge=False,
            )
            if r.get("migrated", 0) > 0:
                results["visited_urls"] = r["migrated"]
                log.info("auto-migrate: %d visited URLs → MariaDB", r["migrated"])
        except Exception as e:
            log.warning("auto-migrate visited_urls failed: %s", e)

    # 5. Skills
    if skill_registry is not None:
        try:
            if skill_registry.list_all():
                r = await migrate_skills(skill_registry, pool, purge=False)
                if r.get("migrated", 0) > 0:
                    results["skills"] = r["migrated"]
                    log.info("auto-migrate: %d skills → MariaDB", r["migrated"])
        except Exception as e:
            log.warning("auto-migrate skills failed: %s", e)

    # 6. Conventions
    if convention_registry is not None:
        try:
            if convention_registry.list_all():
                r = await migrate_conventions(convention_registry, pool, purge=False)
                if r.get("migrated", 0) > 0:
                    results["conventions"] = r["migrated"]
                    log.info("auto-migrate: %d conventions → MariaDB", r["migrated"])
        except Exception as e:
            log.warning("auto-migrate conventions failed: %s", e)

    # 7. Engines
    if engine_registry is not None:
        try:
            if engine_registry.list_all():
                r = await migrate_engines(engine_registry, pool, purge=False)
                if r.get("migrated", 0) > 0:
                    results["engines"] = r["migrated"]
                    log.info("auto-migrate: %d engines → MariaDB", r["migrated"])
        except Exception as e:
            log.warning("auto-migrate engines failed: %s", e)

    # 8. Presets
    if preset_registry is not None:
        try:
            if preset_registry.list_all():
                r = await migrate_presets(preset_registry, pool, purge=False)
                if r.get("migrated", 0) > 0:
                    results["presets"] = r["migrated"]
                    log.info("auto-migrate: %d presets → MariaDB", r["migrated"])
        except Exception as e:
            log.warning("auto-migrate presets failed: %s", e)

    return results
