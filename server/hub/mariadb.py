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
            owner_id      VARCHAR(64)   NOT NULL DEFAULT 'default',
            created_at    DATETIME(3),
            started_at    DATETIME(3),
            completed_at  DATETIME(3),
            error         TEXT,
            progress      JSON,
            -- Persisted page-role classification (value/confidence/reason).
            -- Computed once from the URL (role_for_url) and read back on the
            -- /jobs list so it isn't recomputed for every job on every request.
            page_role     JSON,
            INDEX idx_status         (status),
            INDEX idx_created_at     (created_at),
            INDEX idx_worker_id      (worker_id),
            INDEX idx_url_prefix     (url(255)),
            -- Composite indexes for "WHERE status=X ORDER BY created_at DESC
            -- LIMIT N" / "WHERE mode=X ORDER BY created_at DESC LIMIT N"
            -- which the admin UI hits on every status sub-tab switch. Without
            -- these, MariaDB falls back to filesort over the full result of
            -- the single-column status/mode index; at 2,000+ rows the
            -- difference is ~500ms vs <10ms per query.
            INDEX idx_status_created (status, created_at),
            INDEX idx_mode_created   (mode, created_at),
            INDEX idx_owner_created  (owner_id, created_at)
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
            owner_id            VARCHAR(64)  NOT NULL DEFAULT 'default',
            shared              TINYINT(1)   NOT NULL DEFAULT 1,
            excluded            TINYINT(1)   NOT NULL DEFAULT 0,
            download_video      TINYINT(1)   NOT NULL DEFAULT 0,
            created_at          DATETIME(3),
            updated_at          DATETIME(3),
            last_used_at        DATETIME(3),
            INDEX idx_updated (updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    # ---- Per-host URL history (durable learning store) ----
    # Fetched/escalated URLs accumulate here so the per-host page-role
    # predictor (server/hub/_page_role.py) can learn each site's structure
    # from URLs that long outlive the jobs table (which gets purged on a
    # rolling window). Distinct from ``visited_urls`` (walker-only dedup):
    # this is written by EVERY job completion, not just pap.walk(), so it
    # captures the full URL set the fleet has seen on each host.
    (
        "host_url_history",
        """
        CREATE TABLE IF NOT EXISTS host_url_history (
            host           VARCHAR(255) NOT NULL,
            url_hash       CHAR(40)     NOT NULL,
            url            TEXT         NOT NULL,
            template       VARCHAR(512),
            video_evidence TINYINT(1)   NOT NULL DEFAULT 0,
            hit_count      INT          NOT NULL DEFAULT 1,
            first_seen_at  DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
            last_seen_at   DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
            PRIMARY KEY (host, url_hash),
            INDEX idx_host_recent (host, last_seen_at),
            INDEX idx_host_template (host, template(128))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    # Operator-set per-host-template page-role overrides. When the URL-based
    # heuristic (server/hub/_page_role.py) misclassifies a template, the
    # operator can pin the correct role here from the Live job panel or the
    # host edit modal. ``role_for_url`` consults this first; subsequent jobs
    # whose URL templates to the same value automatically get the corrected
    # role -- per-host learning that survives the jobs purge.
    (
        "host_url_role_overrides",
        """
        CREATE TABLE IF NOT EXISTS host_url_role_overrides (
            host           VARCHAR(255) NOT NULL,
            url_template   VARCHAR(512) NOT NULL,
            role           VARCHAR(32)  NOT NULL,
            set_by         VARCHAR(64)  DEFAULT '',
            set_at         DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
            PRIMARY KEY (host, url_template)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    # Every LLM call's (purpose, engine, prompt, response, latency, ...)
    # so the operator can reconstruct "for THIS job, what did each LLM see
    # and answer?" -- the AI loop end-to-end. Long prompts/responses are
    # offloaded to MinIO (ai_io/<sha1>.bin) and the row keeps only the sha1
    # ref + first 32KB preview. See server/hub/_ai_io_log.py.
    (
        "ai_io_log",
        """
        CREATE TABLE IF NOT EXISTS ai_io_log (
            id            BIGINT       AUTO_INCREMENT PRIMARY KEY,
            ts            DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
            job_id        VARCHAR(64),
            purpose       VARCHAR(32),
            engine_slug   VARCHAR(64),
            parent_call   BIGINT,
            prompt_len    INT,
            response_len  INT,
            tokens_in     INT,
            tokens_out    INT,
            latency_ms    INT,
            prompt_text   MEDIUMTEXT,
            response_text MEDIUMTEXT,
            prompt_ref    VARCHAR(64),
            response_ref  VARCHAR(64),
            error         TEXT,
            INDEX idx_job_ts (job_id, ts),
            INDEX idx_purpose_ts (purpose, ts),
            INDEX idx_engine_ts (engine_slug, ts),
            INDEX idx_ts (ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    # Per-job page-role overrides. Distinct from the host-template overrides
    # above: this lets the operator pin a single job's role without
    # affecting other jobs on the same template. Persisted so the
    # correction survives a hub restart.
    (
        "job_role_overrides",
        """
        CREATE TABLE IF NOT EXISTS job_role_overrides (
            job_id  VARCHAR(64)  PRIMARY KEY,
            role    VARCHAR(32)  NOT NULL,
            set_by  VARCHAR(64)  DEFAULT '',
            set_at  DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3)
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
            cost_input_per_1m_jpy  DOUBLE    DEFAULT 0,
            cost_output_per_1m_jpy DOUBLE    DEFAULT 0,
            gpu_temp_stop_c        DOUBLE    DEFAULT 0,
            gpu_temp_resume_c      DOUBLE    DEFAULT 0,
            gpu_temp_url           TEXT,
            notes               TEXT,
            builtin             TINYINT(1)    DEFAULT 0,
            created_at          DATETIME(3),
            updated_at          DATETIME(3)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "engine_usage",
        """
        CREATE TABLE IF NOT EXISTS engine_usage (
            usage_date          DATE          NOT NULL,
            slug                VARCHAR(128)  NOT NULL,
            prompt_tokens       BIGINT        NOT NULL DEFAULT 0,
            completion_tokens   BIGINT        NOT NULL DEFAULT 0,
            requests            BIGINT        NOT NULL DEFAULT 0,
            updated_at          DATETIME(3),
            PRIMARY KEY (usage_date, slug)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "storage_capacity_samples",
        """
        CREATE TABLE IF NOT EXISTS storage_capacity_samples (
            ts                  DATETIME(0)   NOT NULL,
            source              VARCHAR(64)   NOT NULL DEFAULT 'minio',
            total_bytes         BIGINT        NOT NULL DEFAULT 0,
            used_bytes          BIGINT        NOT NULL DEFAULT 0,
            free_bytes          BIGINT        NOT NULL DEFAULT 0,
            bucket_usage_bytes  BIGINT        NOT NULL DEFAULT 0,
            bucket_object_count BIGINT        NOT NULL DEFAULT 0,
            hub_id              VARCHAR(64)   NOT NULL DEFAULT '',
            healthy             TINYINT(1)    NOT NULL DEFAULT 1,
            note                VARCHAR(255)  NOT NULL DEFAULT '',
            PRIMARY KEY (ts, source),
            INDEX ix_storage_capacity_ts (ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "translations",
        """
        CREATE TABLE IF NOT EXISTS translations (
            text_hash       CHAR(64)      NOT NULL,
            target_lang     VARCHAR(8)    NOT NULL,
            translated      MEDIUMTEXT    NOT NULL,
            engine_slug     VARCHAR(128)  NOT NULL DEFAULT '',
            created_at      DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
            PRIMARY KEY (text_hash, target_lang)
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
    (
        "settings",
        """
        CREATE TABLE IF NOT EXISTS settings (
            k           VARCHAR(190)  PRIMARY KEY,
            v           TEXT,
            updated_at  DATETIME(3)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "profiles",
        """
        CREATE TABLE IF NOT EXISTS profiles (
            name                 VARCHAR(64)   PRIMARY KEY,
            size_bytes           BIGINT        DEFAULT 0,
            etag                 VARCHAR(128),
            s3_key               VARCHAR(255),
            chrome_profile_name  VARCHAR(128),
            source_machine       VARCHAR(190),
            note                 TEXT,
            is_default           TINYINT(1)    DEFAULT 0,
            owner_id             VARCHAR(64)   NOT NULL DEFAULT 'default',
            shared               TINYINT(1)    NOT NULL DEFAULT 1,
            uploaded_at          DATETIME(3),
            updated_at           DATETIME(3)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "extensions",
        """
        CREATE TABLE IF NOT EXISTS extensions (
            slug          VARCHAR(64)   PRIMARY KEY,
            name          VARCHAR(190),
            description   TEXT,
            version       VARCHAR(64),
            extension_id  VARCHAR(64),
            size_bytes    BIGINT        DEFAULT 0,
            enabled       TINYINT(1)    DEFAULT 1,
            note          TEXT,
            etag          VARCHAR(128),
            tarball       LONGBLOB,
            uploaded_at   DATETIME(3),
            updated_at    DATETIME(3)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "workers",
        """
        CREATE TABLE IF NOT EXISTS workers (
            worker_id            VARCHAR(64)  PRIMARY KEY,
            ip                   VARCHAR(45),
            ssh_user             VARCHAR(64),
            ssh_port             INT          DEFAULT 22,
            ssh_key_ref          VARCHAR(255),
            last_seen_at         DATETIME(3),
            last_status          VARCHAR(32),
            recovery_count       INT          NOT NULL DEFAULT 0,
            last_recovery_at     DATETIME(3),
            last_recovery_result VARCHAR(255),
            last_error           VARCHAR(255),
            updated_at           DATETIME(3)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        # 段階4 永続化: durable, fleet-wide salvage recovery history. The admin
        # recovery subtab reads this (cross-hub via the shared MariaDB) so the
        # operator sees "what was salvaged, when, how" across hub restarts --
        # the in-memory ring buffer (scheduler.log_event) only survives until a
        # hub restart. One row per salvage attempt (success OR failure).
        "recovery_events",
        """
        CREATE TABLE IF NOT EXISTS recovery_events (
            id          BIGINT       PRIMARY KEY AUTO_INCREMENT,
            worker_id   VARCHAR(64)  NOT NULL,
            hub_id      VARCHAR(64),
            ip          VARCHAR(45),
            method      VARCHAR(32),
            result      VARCHAR(32),
            detail      VARCHAR(255),
            created_at  DATETIME(3)  NOT NULL,
            INDEX idx_recovery_created (created_at),
            INDEX idx_recovery_worker (worker_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    # Per-host STATE digest (one-page "what we know, what worked, what
    # to try next"). Distinct from the structured HostKnowledge / skills
    # / conventions / recipes — this is the FREE-FORM synthesis the
    # operator can read at a glance and the distiller reads as VISION.md-
    # style context at the start of every escalation.
    #
    # Writers: operator (admin UI PUT), nightly_review (daily auto-roll-up).
    # Readers: distiller_r1 (start of distill), admin UI (host modal subtab).
    (
        "host_strategy",
        """
        CREATE TABLE IF NOT EXISTS host_strategy (
            host          VARCHAR(255) NOT NULL PRIMARY KEY,
            summary_md    TEXT         NOT NULL,
            updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
            updated_by    VARCHAR(64)  NOT NULL DEFAULT '',
            revision      INT          NOT NULL DEFAULT 1,
            INDEX idx_host_updated (updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
]


# ---------------------------------------------------------------------------
# Auth tables (users + api_keys)
# ---------------------------------------------------------------------------
#
# Added by the "LAN-trust → multi-user / public" hardening. Kept in their
# own list so AuthStore can ensure JUST these (``ensure_auth_tables``)
# on first use, independent of whether the operator has run the full
# ``ensure_schema``. Also appended to ``_TABLES`` so the normal schema
# create + ``table_counts`` cover them too.

_AUTH_TABLES: list[tuple[str, str]] = [
    (
        "users",
        """
        CREATE TABLE IF NOT EXISTS users (
            id          VARCHAR(64)   PRIMARY KEY,
            email       VARCHAR(255)  NOT NULL,
            pw_hash     VARCHAR(255)  NOT NULL,
            role        VARCHAR(20)   NOT NULL DEFAULT 'user',
            disabled    TINYINT(1)    DEFAULT 0,
            created_at  DATETIME(3),
            UNIQUE KEY uk_email (email)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "api_keys",
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id           VARCHAR(64)   PRIMARY KEY,
            prefix       VARCHAR(32)   NOT NULL,
            secret_hash  VARCHAR(128)  NOT NULL,
            user_id      VARCHAR(64)   NOT NULL,
            name         VARCHAR(255)  DEFAULT '',
            scopes       JSON,
            created_at   DATETIME(3),
            last_used_at DATETIME(3),
            expires_at   DATETIME(3),
            revoked      TINYINT(1)    DEFAULT 0,
            UNIQUE KEY uk_prefix (prefix),
            INDEX idx_user (user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
]
_TABLES.extend(_AUTH_TABLES)


async def ensure_auth_tables(pool: Any) -> None:
    """Idempotently create just the auth tables.

    Called by :class:`server.hub.auth.AuthStore` on first MariaDB use so
    login / API keys work even before the operator triggers the full
    ``ensure_schema``. Each statement is ``CREATE TABLE IF NOT EXISTS``."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for _name, ddl in _AUTH_TABLES:
                await cur.execute(ddl)


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
    """Run all CREATE TABLE IF NOT EXISTS statements + apply additive
    schema migrations (new indexes etc.) to existing tables.

    Returns the list of table names that were ensured.
    """
    created: list[str] = []
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for name, ddl in _TABLES:
                await cur.execute(ddl)
                created.append(name)
    # Apply additive migrations after CREATE TABLE so they target the
    # current schema. Run separately so a single failing migration
    # doesn't block the rest.
    # Columns BEFORE indexes so an index that references a freshly-added
    # column (e.g. jobs.owner_id) finds it.
    await _apply_column_migrations(pool)
    await _apply_index_migrations(pool)
    return created


# ---------------------------------------------------------------------------
# Additive index migrations (idempotent)
# ---------------------------------------------------------------------------
#
# When the DDL above grows a new INDEX, ``CREATE TABLE IF NOT EXISTS`` is
# a no-op on an already-existing table -- so newly-added indexes are never
# applied to legacy databases. This helper runs ``CREATE INDEX IF NOT
# EXISTS`` (MariaDB 10.5+) for each composite/secondary index the codebase
# now depends on. Each entry is (table, index_name, "(col, col, ...)")".

_REQUIRED_INDEXES: list[tuple[str, str, str]] = [
    # Speeds up "WHERE status=X ORDER BY created_at DESC LIMIT N" -- the
    # admin UI's per-status sub-tab queries (全部/成功/エラー/実行中).
    ("jobs", "idx_status_created", "(status, created_at)"),
    # Same shape for "WHERE mode=X ORDER BY created_at DESC LIMIT N".
    ("jobs", "idx_mode_created", "(mode, created_at)"),
    # Phase 2 tenancy: "WHERE owner_id=X ORDER BY created_at DESC".
    ("jobs", "idx_owner_created", "(owner_id, created_at)"),
]


async def _apply_index_migrations(pool: Any) -> None:
    """Idempotently add any indexes listed in ``_REQUIRED_INDEXES`` that
    don't already exist on the live schema. Safe to call on every
    startup -- each CREATE INDEX is wrapped in IF NOT EXISTS, and
    failures are logged but don't propagate."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for table, name, cols in _REQUIRED_INDEXES:
                try:
                    await cur.execute(
                        f"CREATE INDEX IF NOT EXISTS `{name}` "
                        f"ON `{table}` {cols}"
                    )
                except Exception as e:
                    log.warning(
                        "index migration %s.%s failed: %s", table, name, e,
                    )


# ---------------------------------------------------------------------------
# Additive column migrations (idempotent)
# ---------------------------------------------------------------------------
#
# Like the index migrations: ``CREATE TABLE IF NOT EXISTS`` never alters an
# existing table, so columns newly added to the DDL above are absent on legacy
# DBs. This adds them via ``ADD COLUMN IF NOT EXISTS`` (MariaDB 10.0.2+). Each
# entry is (table, column, "<type + default>"). Called BEFORE the index
# migrations so an index referencing a freshly-added column finds it.

_REQUIRED_COLUMNS: list[tuple[str, str, str]] = [
    # Phase 2 tenancy: owner that submitted the job. Existing rows backfill to
    # the shared 'default' tenant via the column DEFAULT.
    ("jobs", "owner_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
    # Phase 2b tenancy for profiles + hosts (Chrome login state — the top leak
    # surface). owner_id = uploading/pushing tenant; shared = visible to every
    # tenant (the pre-tenancy ambient default). Legacy rows backfill to
    # owner=default / shared=1 via the column DEFAULTs, so isolation only bites
    # under enforce for a non-admin user's private profile/host.
    ("profiles", "owner_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
    ("profiles", "shared", "TINYINT(1) NOT NULL DEFAULT 1"),
    ("hosts", "owner_id", "VARCHAR(64) NOT NULL DEFAULT 'default'"),
    ("hosts", "shared", "TINYINT(1) NOT NULL DEFAULT 1"),
    ("hosts", "excluded", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("hosts", "download_video", "TINYINT(1) NOT NULL DEFAULT 0"),
    # Persisted page-role (compute role_for_url once, read back on the /jobs
    # list instead of recomputing for every job on every request).
    ("jobs", "page_role", "JSON"),
]


async def _apply_column_migrations(pool: Any) -> None:
    """Idempotently add any columns in ``_REQUIRED_COLUMNS`` missing from the
    live schema. Safe on every startup (ADD COLUMN IF NOT EXISTS); failures are
    logged, not propagated."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for table, col, spec in _REQUIRED_COLUMNS:
                try:
                    await cur.execute(
                        f"ALTER TABLE `{table}` ADD COLUMN IF NOT EXISTS `{col}` {spec}"
                    )
                except Exception as e:
                    log.warning(
                        "column migration %s.%s failed: %s", table, col, e,
                    )


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
                "created_at, updated_at, last_used_at, owner_id, shared, "
                "excluded, download_video "
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
                # Phase 2b: MariaDB is SoT — mirror owner/shared onto the file
                # registry so a restart / peer hub keeps tenant attribution.
                owner_id=row[14] or "default",
                shared=bool(row[15]) if row[15] is not None else True,
                excluded=bool(row[16]) if len(row) > 16 and row[16] is not None else False,
                download_video=bool(row[17]) if len(row) > 17 and row[17] is not None else False,
            )
            restored += 1
        except Exception as e:
            log.warning("restore host %s failed: %s", row[0], e)
    return restored


async def record_host_url_row(
    pool: Any,
    *,
    host: str,
    url: str,
    template: str,
    has_video_evidence: bool,
) -> None:
    """UPSERT one (host, url) row into ``host_url_history``.

    Called fire-and-forget on every job completion so the per-host URL set
    survives the rolling purge of the jobs table. Repeated hits on the same
    URL bump ``hit_count`` and refresh ``last_seen_at``; ``video_evidence``
    is sticky (a single positive observation marks the template forever, so
    a later non-video hit can't un-flag a known detail).
    """
    import hashlib
    if not host or not url:
        return
    url_hash = hashlib.sha1(url.encode("utf-8", errors="replace")).hexdigest()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO host_url_history
                   (host, url_hash, url, template, video_evidence)
                   VALUES (%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     hit_count = hit_count + 1,
                     last_seen_at = CURRENT_TIMESTAMP(3),
                     video_evidence = video_evidence | VALUES(video_evidence),
                     template = COALESCE(VALUES(template), template)""",
                (host, url_hash, url, template or None,
                 1 if has_video_evidence else 0),
            )


async def fetch_host_url_history(
    pool: Any, host: str, limit: int = 2000
) -> list[tuple]:
    """Read recent URLs for ``host`` (most-recent-first). Returns rows of
    ``(url, template, video_evidence, hit_count)``."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT url, template, video_evidence, hit_count "
                "FROM host_url_history WHERE host=%s "
                "ORDER BY last_seen_at DESC LIMIT %s",
                (host, int(limit)),
            )
            return list(await cur.fetchall())


# ---------------------------------------------------------------------------
# host_strategy: per-host STATE digest (VISION.md-style synthesis).
# ---------------------------------------------------------------------------

async def host_strategy_get(pool: Any, host: str) -> dict | None:
    """Read the strategy digest for ``host``. Returns ``None`` when no
    digest exists yet. Caller uses an empty fallback when None."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT host, summary_md, updated_at, updated_by, revision "
                "FROM host_strategy WHERE host=%s",
                (host,),
            )
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "host": row[0],
        "summary_md": row[1] or "",
        "updated_at": row[2].isoformat() if row[2] else None,
        "updated_by": row[3] or "",
        "revision": int(row[4] or 1),
    }


async def host_strategy_upsert(
    pool: Any, host: str, summary_md: str, updated_by: str
) -> None:
    """Write the strategy digest for ``host``. ``updated_by`` is a free
    string used to track provenance: ``'operator'``, ``'nightly_review'``,
    a job_id, etc. Revision auto-increments on each write."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO host_strategy (host, summary_md, updated_by, revision)
                   VALUES (%s, %s, %s, 1)
                   ON DUPLICATE KEY UPDATE
                     summary_md = VALUES(summary_md),
                     updated_by = VALUES(updated_by),
                     revision   = revision + 1""",
                (host, summary_md or "", updated_by or ""),
            )


async def host_strategy_delete(pool: Any, host: str) -> int:
    """Drop the strategy digest for ``host``. Returns rowcount (0 or 1)."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM host_strategy WHERE host=%s", (host,)
            )
            return cur.rowcount or 0


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
            # AUTO skills are distilled locally per-hub. Historically they
            # were never written through to MariaDB, so a file-only auto
            # entry is NOT a MariaDB-side deletion to mirror -- it's a not-
            # yet-shared skill. Back it UP into MariaDB instead of deleting
            # it, so (a) it survives this very restart and (b) every hub
            # converges on the union of auto skills. The branch only runs
            # for keys absent from MariaDB, so this never clobbers a peer's
            # row -- whichever hub restarts first becomes the writer.
            # CURATED is operator-owned + always write-through, so a file-
            # only curated entry really was deleted elsewhere -> mirror it.
            if existing.tier == "auto":
                try:
                    await upsert_skill_row(pool, existing)
                    log.info(
                        "restore: backfilled local auto skill %s to MariaDB",
                        existing.slug,
                    )
                except Exception as e:
                    log.warning(
                        "restore: backfill skill %s to MariaDB failed: %s",
                        existing.slug, e,
                    )
                continue
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
            # AUTO conventions: back up file-only entries into MariaDB rather
            # than deleting them (same rationale as restore_skills above --
            # auto rules are distilled per-hub and must converge cross-hub,
            # not be wiped on restart). CURATED orphans are real deletions.
            if existing.tier == "auto":
                try:
                    await upsert_convention_row(pool, existing)
                    log.info(
                        "restore: backfilled local auto convention %s to MariaDB",
                        existing.slug,
                    )
                except Exception as e:
                    log.warning(
                        "restore: backfill convention %s to MariaDB failed: %s",
                        existing.slug, e,
                    )
                continue
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
            # SELECT with backward-compat for old MariaDB schemas that
            # predate the cost columns: try the new column list first,
            # fall back to the legacy SELECT when MariaDB returns
            # ER_BAD_FIELD_ERROR (1054).
            # Ensure optional columns exist, then SELECT the wide shape.
            # ``ADD COLUMN IF NOT EXISTS`` is idempotent on MariaDB 10.3+
            # (prod is 11.x), so this self-migrates old schemas in place --
            # covering the cost_*_jpy and the per-engine GPU-thermal columns.
            for _ddl in (
                "ALTER TABLE engines ADD COLUMN IF NOT EXISTS cost_input_per_1m_jpy DOUBLE DEFAULT 0",
                "ALTER TABLE engines ADD COLUMN IF NOT EXISTS cost_output_per_1m_jpy DOUBLE DEFAULT 0",
                "ALTER TABLE engines ADD COLUMN IF NOT EXISTS gpu_temp_stop_c DOUBLE DEFAULT 0",
                "ALTER TABLE engines ADD COLUMN IF NOT EXISTS gpu_temp_resume_c DOUBLE DEFAULT 0",
                "ALTER TABLE engines ADD COLUMN IF NOT EXISTS gpu_temp_url TEXT",
            ):
                try:
                    await cur.execute(_ddl)
                except Exception:
                    pass
            try:
                await cur.execute(
                    "SELECT slug, name, kind, protocol, endpoint, "
                    "model, api_key_env, api_key, headers, "
                    "timeout_s, promoted, supports_tools, "
                    "use_for_codegen, daily_token_budget, "
                    "daily_request_budget, notes, builtin, "
                    "created_at, updated_at, "
                    "cost_input_per_1m_jpy, cost_output_per_1m_jpy, "
                    "gpu_temp_stop_c, gpu_temp_resume_c, gpu_temp_url "
                    "FROM engines"
                )
                rows = await cur.fetchall()
                _wide = True
            except Exception as e:
                # Ancient MariaDB without IF-NOT-EXISTS -- legacy column set
                # (cost + thermal default to 0 / "").
                if "1054" in str(e) or "Unknown column" in str(e):
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
                    _wide = False
                else:
                    raise

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
            # cost_*_jpy (19,20) + gpu_temp_* (21,22,23) are appended after
            # the legacy 19 cols. On an ancient schema (_wide False) the row
            # has 19 cols and these fall back to 0.0 / "" → seed_default_
            # pricing() refills cost on next startup; thermal stays off until
            # set in the UI.
            ci = float(row[19]) if (_wide and len(row) > 19 and row[19] is not None) else 0.0
            co = float(row[20]) if (_wide and len(row) > 20 and row[20] is not None) else 0.0
            gstop = float(row[21]) if (_wide and len(row) > 21 and row[21] is not None) else 0.0
            gresume = float(row[22]) if (_wide and len(row) > 22 and row[22] is not None) else 0.0
            gurl = row[23] if (_wide and len(row) > 23 and row[23] is not None) else ""
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
                cost_input_per_1m_jpy=ci,
                cost_output_per_1m_jpy=co,
                gpu_temp_stop_c=gstop,
                gpu_temp_resume_c=gresume,
                gpu_temp_url=gurl or "",
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
            # Try the new shape with cost columns first; fall back to the
            # legacy SHAPE when old MariaDB schemas reject the columns.
            params_new = (
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
                float(d.get("cost_input_per_1m_jpy", 0) or 0),
                float(d.get("cost_output_per_1m_jpy", 0) or 0),
                float(d.get("gpu_temp_stop_c", 0) or 0),
                float(d.get("gpu_temp_resume_c", 0) or 0),
                d.get("gpu_temp_url", "") or "",
                d.get("notes", ""),
                1 if d.get("builtin") else 0,
                _parse_dt(d.get("created_at")),
                _parse_dt(d.get("updated_at")),
            )
            try:
                await cur.execute(
                    """INSERT INTO engines
                       (slug, name, kind, protocol, endpoint, model,
                        api_key_env, api_key, headers, timeout_s, promoted,
                        supports_tools, use_for_codegen, daily_token_budget,
                        daily_request_budget, cost_input_per_1m_jpy,
                        cost_output_per_1m_jpy, gpu_temp_stop_c,
                        gpu_temp_resume_c, gpu_temp_url, notes, builtin,
                        created_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                         cost_input_per_1m_jpy=VALUES(cost_input_per_1m_jpy),
                         cost_output_per_1m_jpy=VALUES(cost_output_per_1m_jpy),
                         gpu_temp_stop_c=VALUES(gpu_temp_stop_c),
                         gpu_temp_resume_c=VALUES(gpu_temp_resume_c),
                         gpu_temp_url=VALUES(gpu_temp_url),
                         notes=VALUES(notes), builtin=VALUES(builtin),
                         updated_at=VALUES(updated_at)""",
                    params_new,
                )
            except Exception as e:
                if "1054" in str(e) or "Unknown column" in str(e):
                    # Legacy MariaDB schema -- write without cost / thermal cols.
                    legacy_params = params_new[:15] + params_new[20:]
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
                        legacy_params,
                    )
                else:
                    raise


async def delete_engine_row(pool: Any, slug: str) -> None:
    """DELETE one engine row from MariaDB. No-op when the row doesn't
    exist (no error -- aligns with the file registry's delete()
    semantics which also tolerate missing records)."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM engines WHERE slug=%s", (slug,))


# ---------------------------------------------------------------------------
# Per-record write-through (cross-hub config sharing — Phase B)
#
# Mirror upsert_engine_row for the operator-managed registries so an edit on
# one hub is persisted to MariaDB *immediately*. The migrate_* paths above use
# INSERT IGNORE (bulk one-time import); these use ON DUPLICATE KEY UPDATE so an
# edit to an EXISTING row sticks. Column lists are kept verbatim-aligned with
# the matching migrate_* (so the table schema is the single source of truth);
# ``created_at`` is deliberately NOT in the UPDATE clause (preserve original).
# Paired with server/hub/_invalidate.py, which surgically replays each change
# on peer hubs. ``success_count`` / ``last_success_at`` are omitted for skills
# and conventions — they're on the dataclass but not the table.
# ---------------------------------------------------------------------------


async def upsert_skill_row(pool: Any, rec: Any) -> None:
    """INSERT-or-UPDATE one skill row (PK=slug)."""
    from dataclasses import asdict
    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
    params = (
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
    )
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO skills
                   (slug, tier, name, description,
                    code_template, llm_instructions,
                    applicable_when, tags, auto_extracted,
                    extracted_from, use_count,
                    created_at, updated_at, last_used_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     tier=VALUES(tier), name=VALUES(name),
                     description=VALUES(description),
                     code_template=VALUES(code_template),
                     llm_instructions=VALUES(llm_instructions),
                     applicable_when=VALUES(applicable_when),
                     tags=VALUES(tags), auto_extracted=VALUES(auto_extracted),
                     extracted_from=VALUES(extracted_from),
                     use_count=VALUES(use_count),
                     updated_at=VALUES(updated_at),
                     last_used_at=VALUES(last_used_at)""",
                params,
            )


async def delete_skill_row(pool: Any, slug: str) -> None:
    """DELETE one skill row (PK=slug). No-op when absent."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM skills WHERE slug=%s", (slug,))


async def upsert_convention_row(pool: Any, rec: Any) -> None:
    """INSERT-or-UPDATE one convention row (PK=slug)."""
    from dataclasses import asdict
    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
    params = (
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
    )
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO conventions
                   (slug, tier, name, advice, rationale,
                    bad_example, good_example,
                    applicable_when, tags, extracted_from,
                    use_count, created_at, updated_at,
                    last_used_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     tier=VALUES(tier), name=VALUES(name),
                     advice=VALUES(advice), rationale=VALUES(rationale),
                     bad_example=VALUES(bad_example),
                     good_example=VALUES(good_example),
                     applicable_when=VALUES(applicable_when),
                     tags=VALUES(tags), extracted_from=VALUES(extracted_from),
                     use_count=VALUES(use_count),
                     updated_at=VALUES(updated_at),
                     last_used_at=VALUES(last_used_at)""",
                params,
            )


async def delete_convention_row(pool: Any, slug: str) -> None:
    """DELETE one convention row (PK=slug). No-op when absent."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM conventions WHERE slug=%s", (slug,))


async def upsert_preset_row(pool: Any, rec: Any) -> None:
    """INSERT-or-UPDATE one preset row (PK=name)."""
    from dataclasses import asdict
    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
    params = (
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
    )
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO presets
                   (name, category, description, ui_mode,
                    ai_engine, url, goal, simple_rows,
                    code_script, max_attempts,
                    attempt_timeout_s, attempt_timeout_simple_s,
                    host_dedup, options,
                    created_at, updated_at, last_used_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     category=VALUES(category), description=VALUES(description),
                     ui_mode=VALUES(ui_mode), ai_engine=VALUES(ai_engine),
                     url=VALUES(url), goal=VALUES(goal),
                     simple_rows=VALUES(simple_rows),
                     code_script=VALUES(code_script),
                     max_attempts=VALUES(max_attempts),
                     attempt_timeout_s=VALUES(attempt_timeout_s),
                     attempt_timeout_simple_s=VALUES(attempt_timeout_simple_s),
                     host_dedup=VALUES(host_dedup), options=VALUES(options),
                     updated_at=VALUES(updated_at),
                     last_used_at=VALUES(last_used_at)""",
                params,
            )


async def delete_preset_row(pool: Any, name: str) -> None:
    """DELETE one preset row (PK=name). No-op when absent."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM presets WHERE name=%s", (name,))


async def upsert_host_row(pool: Any, rec: Any) -> None:
    """INSERT-or-UPDATE one host row (PK=host). Recipes normalised to plain
    dicts exactly as migrate_hosts does. The caller passes a record whose
    ``host`` is already normalised (the registry normalises on upsert)."""
    from dataclasses import asdict
    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
    recipe_dicts: list = []
    for r in (d.get("fetch_recipes", []) or []):
        if hasattr(r, "to_json"):
            recipe_dicts.append(r.to_json())
        elif isinstance(r, dict):
            recipe_dicts.append(r)
        else:
            recipe_dicts.append(asdict(r))
    params = (
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
        str(d.get("owner_id") or "default"),
        1 if d.get("shared", True) else 0,
        1 if d.get("excluded") else 0,
        1 if d.get("download_video") else 0,
        _parse_dt(d.get("created_at")),
        _parse_dt(d.get("updated_at")),
        _parse_dt(d.get("last_used_at")),
    )
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO hosts
                   (host, cookies, notes, recrawl_patterns,
                    popup_policy, login_url, login_goal,
                    login_check, login_refresh_ttl_s,
                    last_login_at, fetch_recipes, owner_id, shared,
                    excluded, download_video, created_at, updated_at, last_used_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     cookies=VALUES(cookies), notes=VALUES(notes),
                     recrawl_patterns=VALUES(recrawl_patterns),
                     popup_policy=VALUES(popup_policy),
                     login_url=VALUES(login_url), login_goal=VALUES(login_goal),
                     login_check=VALUES(login_check),
                     login_refresh_ttl_s=VALUES(login_refresh_ttl_s),
                     last_login_at=VALUES(last_login_at),
                     fetch_recipes=VALUES(fetch_recipes),
                     owner_id=VALUES(owner_id), shared=VALUES(shared),
                     excluded=VALUES(excluded),
                     download_video=VALUES(download_video),
                     updated_at=VALUES(updated_at),
                     last_used_at=VALUES(last_used_at)""",
                params,
            )


async def delete_host_row(pool: Any, host: str) -> None:
    """DELETE one host row (PK=host). No-op when absent."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM hosts WHERE host=%s", (host,))


# ---------------------------------------------------------------------------
# Hub settings write-through + restore (Phase B)
#
# The settings registry is a flat key/value dict (server/hub/settings.py). Each
# value is JSON-encoded so its type (bool/int/float/str) round-trips. The table
# is self-created (idempotent) so these don't depend on ensure_schema having run
# at boot. The mariadb_* DSN keys are EXCLUDED by the caller (_invalidate.py) --
# they are bootstrap config and must stay per-hub.
# ---------------------------------------------------------------------------

_SETTINGS_DDL = (
    "CREATE TABLE IF NOT EXISTS settings ("
    "k VARCHAR(190) PRIMARY KEY, v TEXT, updated_at DATETIME(3)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)


async def upsert_settings(pool: Any, mapping: dict) -> None:
    """Write a dict of hub settings through to MariaDB (JSON-encoded values)."""
    if not mapping:
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SETTINGS_DDL)
            for k, v in mapping.items():
                await cur.execute(
                    "INSERT INTO settings (k, v, updated_at) "
                    "VALUES (%s, %s, UTC_TIMESTAMP(3)) "
                    "ON DUPLICATE KEY UPDATE v=VALUES(v), updated_at=VALUES(updated_at)",
                    (str(k), _json_dumps(v)),
                )


async def load_settings(pool: Any) -> dict:
    """Load all hub settings from MariaDB (JSON-decoded). Returns {} when the
    table is empty or unreadable."""
    out: dict = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SETTINGS_DDL)
            await cur.execute("SELECT k, v FROM settings")
            for row in await cur.fetchall():
                k, v = row[0], row[1]
                try:
                    out[str(k)] = json.loads(v) if v is not None else None
                except Exception:
                    out[str(k)] = v
    return out


# ---------------------------------------------------------------------------
# Engine token-usage: cross-hub shared daily counters in MariaDB.
#
# The per-hub JSON file ({data_dir}/engines/_usage.json) is a single-writer
# local counter, so the #engines tab only ever saw the usage of whichever hub
# nginx happened to route to. This table is the SHARED source of truth: every
# hub increments the same (usage_date, slug) row via an atomic UPSERT, so a
# read is already the fleet-wide total -- no per-hub rows, no SUM, no
# aggregation race. Self-created (idempotent) so it works even before
# ensure_schema has run.
# ---------------------------------------------------------------------------

_ENGINE_USAGE_DDL = (
    "CREATE TABLE IF NOT EXISTS engine_usage ("
    "usage_date DATE NOT NULL, slug VARCHAR(128) NOT NULL, "
    "prompt_tokens BIGINT NOT NULL DEFAULT 0, "
    "completion_tokens BIGINT NOT NULL DEFAULT 0, "
    "requests BIGINT NOT NULL DEFAULT 0, "
    "updated_at DATETIME(3), "
    "PRIMARY KEY (usage_date, slug)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)


async def engine_usage_record(
    pool: Any, date_str: str, slug: str, prompt: int, completion: int
) -> None:
    """Atomically add (prompt, completion, +1 request) to the shared
    (date, slug) counter. Cross-hub safe: concurrent increments from any
    number of hubs accumulate into the one row, so the row IS the fleet
    total."""
    if not slug:
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_ENGINE_USAGE_DDL)
            await cur.execute(
                "INSERT INTO engine_usage "
                "(usage_date, slug, prompt_tokens, completion_tokens, requests, updated_at) "
                "VALUES (%s, %s, %s, %s, 1, UTC_TIMESTAMP(3)) "
                "ON DUPLICATE KEY UPDATE "
                "prompt_tokens = prompt_tokens + VALUES(prompt_tokens), "
                "completion_tokens = completion_tokens + VALUES(completion_tokens), "
                "requests = requests + 1, "
                "updated_at = VALUES(updated_at)",
                (str(date_str), str(slug), max(0, int(prompt or 0)), max(0, int(completion or 0))),
            )


async def load_engine_usage(pool: Any, days: int = 14) -> dict:
    """Return the shared per-day, per-slug counters for the last ``days``
    days as ``{date_str: {slug: {prompt, completion, requests}}}``.
    Empty dict when the table is empty / unreadable."""
    out: dict = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_ENGINE_USAGE_DDL)
            await cur.execute(
                "SELECT usage_date, slug, prompt_tokens, completion_tokens, requests "
                "FROM engine_usage "
                "WHERE usage_date >= (UTC_DATE() - INTERVAL %s DAY) "
                "ORDER BY usage_date",
                (max(1, int(days)),),
            )
            for row in await cur.fetchall():
                d, slug, p, c, r = row[0], row[1], row[2], row[3], row[4]
                ds = d.isoformat() if hasattr(d, "isoformat") else str(d)
                out.setdefault(ds, {})[str(slug)] = {
                    "prompt": int(p or 0),
                    "completion": int(c or 0),
                    "requests": int(r or 0),
                }
    return out


# ---------------------------------------------------------------------------
# storage_capacity_samples: periodic snapshots of the asset-store back-end
# (MinIO at .16). Sampled by ONE hub at a time (Redis SET NX EX gate) and
# read by the admin UI for the depletion-trend chart.

_STORAGE_CAPACITY_DDL = (
    "CREATE TABLE IF NOT EXISTS storage_capacity_samples ("
    "ts DATETIME(0) NOT NULL,"
    "source VARCHAR(64) NOT NULL DEFAULT 'minio',"
    "total_bytes BIGINT NOT NULL DEFAULT 0,"
    "used_bytes BIGINT NOT NULL DEFAULT 0,"
    "free_bytes BIGINT NOT NULL DEFAULT 0,"
    "bucket_usage_bytes BIGINT NOT NULL DEFAULT 0,"
    "bucket_object_count BIGINT NOT NULL DEFAULT 0,"
    "hub_id VARCHAR(64) NOT NULL DEFAULT '',"
    "healthy TINYINT(1) NOT NULL DEFAULT 1,"
    "note VARCHAR(255) NOT NULL DEFAULT '',"
    "PRIMARY KEY (ts, source),"
    "INDEX ix_storage_capacity_ts (ts)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)


async def storage_capacity_record(
    pool: Any,
    ts: str,
    source: str,
    total_bytes: int,
    used_bytes: int,
    free_bytes: int,
    bucket_usage_bytes: int,
    bucket_object_count: int,
    hub_id: str,
    healthy: bool,
    note: str = "",
) -> None:
    """Insert one capacity sample. INSERT IGNORE on PK collision so a rare
    multi-hub race at the same second silently keeps the first writer's row."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_STORAGE_CAPACITY_DDL)
            await cur.execute(
                "INSERT IGNORE INTO storage_capacity_samples "
                "(ts, source, total_bytes, used_bytes, free_bytes, "
                "bucket_usage_bytes, bucket_object_count, hub_id, healthy, note) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(ts),
                    str(source or "minio")[:64],
                    max(0, int(total_bytes or 0)),
                    max(0, int(used_bytes or 0)),
                    max(0, int(free_bytes or 0)),
                    max(0, int(bucket_usage_bytes or 0)),
                    max(0, int(bucket_object_count or 0)),
                    str(hub_id or "")[:64],
                    1 if healthy else 0,
                    str(note or "")[:255],
                ),
            )


async def load_storage_capacity(
    pool: Any, days: int = 7, source: str = "minio", limit: int = 4000
) -> list:
    """Return capacity samples for the last ``days`` days as a chronologically
    ordered list of dicts. Caller can downsample for display."""
    out: list = []
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_STORAGE_CAPACITY_DDL)
            await cur.execute(
                "SELECT ts, total_bytes, used_bytes, free_bytes, "
                "bucket_usage_bytes, bucket_object_count, hub_id, healthy, note "
                "FROM storage_capacity_samples "
                "WHERE source = %s AND ts >= (UTC_TIMESTAMP() - INTERVAL %s DAY) "
                "ORDER BY ts ASC "
                "LIMIT %s",
                (str(source or "minio"), max(1, int(days)), max(1, int(limit))),
            )
            for row in await cur.fetchall():
                ts = row[0]
                ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                out.append(
                    {
                        "ts": ts_iso,
                        "total_bytes": int(row[1] or 0),
                        "used_bytes": int(row[2] or 0),
                        "free_bytes": int(row[3] or 0),
                        "bucket_usage_bytes": int(row[4] or 0),
                        "bucket_object_count": int(row[5] or 0),
                        "hub_id": str(row[6] or ""),
                        "healthy": bool(row[7]),
                        "note": str(row[8] or ""),
                    }
                )
    return out


async def latest_storage_capacity(pool: Any, source: str = "minio") -> dict | None:
    """Most recent single sample, or None if table is empty."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_STORAGE_CAPACITY_DDL)
            await cur.execute(
                "SELECT ts, total_bytes, used_bytes, free_bytes, "
                "bucket_usage_bytes, bucket_object_count, hub_id, healthy, note "
                "FROM storage_capacity_samples "
                "WHERE source = %s "
                "ORDER BY ts DESC LIMIT 1",
                (str(source or "minio"),),
            )
            row = await cur.fetchone()
            if not row:
                return None
            ts = row[0]
            ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            return {
                "ts": ts_iso,
                "total_bytes": int(row[1] or 0),
                "used_bytes": int(row[2] or 0),
                "free_bytes": int(row[3] or 0),
                "bucket_usage_bytes": int(row[4] or 0),
                "bucket_object_count": int(row[5] or 0),
                "hub_id": str(row[6] or ""),
                "healthy": bool(row[7]),
                "note": str(row[8] or ""),
            }


async def prune_storage_capacity(pool: Any, keep_days: int = 60) -> int:
    """Drop samples older than ``keep_days`` to keep the table bounded."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_STORAGE_CAPACITY_DDL)
            await cur.execute(
                "DELETE FROM storage_capacity_samples "
                "WHERE ts < (UTC_TIMESTAMP() - INTERVAL %s DAY)",
                (max(1, int(keep_days)),),
            )
            return int(getattr(cur, "rowcount", 0) or 0)


# ---------------------------------------------------------------------------
# Translation cache: hash(text) + target_lang -> translated. Translations are
# generated by the chat Promoted engine on operator request (UI 翻訳 button on
# conventions) and persisted here so a re-open / a different operator / a
# different hub all hit the cache instantly. Convention advice is essentially
# immutable so the cache is effectively permanent.
# ---------------------------------------------------------------------------

_TRANSLATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS translations ("
    "text_hash CHAR(64) NOT NULL, target_lang VARCHAR(8) NOT NULL, "
    "translated MEDIUMTEXT NOT NULL, engine_slug VARCHAR(128) NOT NULL DEFAULT '', "
    "created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3), "
    "PRIMARY KEY (text_hash, target_lang)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)


async def translations_get_many(pool: Any, keys: list) -> dict:
    """Bulk-fetch cached translations. ``keys`` is a list of (text_hash,
    target_lang) tuples. Returns ``{(hash, lang): translated_str}`` for hits;
    misses are simply absent. Cross-hub safe — the row is shared MariaDB."""
    if not keys:
        return {}
    out: dict = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_TRANSLATIONS_DDL)
            placeholders = ",".join(["(%s, %s)"] * len(keys))
            params: list = []
            for h, lang in keys:
                params.append(str(h))
                params.append(str(lang))
            await cur.execute(
                "SELECT text_hash, target_lang, translated FROM translations "
                "WHERE (text_hash, target_lang) IN (" + placeholders + ")",
                params,
            )
            for h, lang, tr in await cur.fetchall():
                out[(str(h), str(lang))] = str(tr or "")
    return out


async def translations_upsert(
    pool: Any, text_hash: str, target_lang: str, translated: str, engine_slug: str
) -> None:
    """Persist one translation. Idempotent — same (hash, lang) overwrites."""
    if not text_hash or not target_lang or not translated:
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_TRANSLATIONS_DDL)
            await cur.execute(
                "INSERT INTO translations (text_hash, target_lang, translated, engine_slug, created_at) "
                "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(3)) "
                "ON DUPLICATE KEY UPDATE translated=VALUES(translated), "
                "engine_slug=VALUES(engine_slug), created_at=VALUES(created_at)",
                (str(text_hash), str(target_lang), str(translated), str(engine_slug or "")),
            )


# ---------------------------------------------------------------------------
# Extensions: shared store in MariaDB (Phase C — bytes as LONGBLOB + metadata)
#
# Extensions are small (KB–few MB) and operator-uploaded only, so per the
# operator principle ("configure MariaDB and everything works; MinIO holds only
# job media") the whole extension — tarball bytes AND metadata — lives in one
# MariaDB row. Any hub serves the shared set: list reads metadata here, download
# lazily pulls the BLOB and caches it locally. Table self-created (idempotent).
# ---------------------------------------------------------------------------

_EXTENSIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS extensions ("
    "slug VARCHAR(64) PRIMARY KEY, name VARCHAR(190), description TEXT, "
    "version VARCHAR(64), extension_id VARCHAR(64), size_bytes BIGINT DEFAULT 0, "
    "enabled TINYINT(1) DEFAULT 1, note TEXT, etag VARCHAR(128), tarball LONGBLOB, "
    "uploaded_at DATETIME(3), updated_at DATETIME(3)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)


def _dt_iso(v: Any) -> str:
    """A DB datetime → ISO-8601 string (with trailing Z) for API payloads."""
    if v is None:
        return ""
    try:
        return v.isoformat() + "Z"
    except Exception:
        return str(v)


async def upsert_extension_row(pool: Any, meta: Any, tarball_bytes: bytes, etag: str = "") -> None:
    """INSERT-or-UPDATE one extension row (metadata + the tar.gz BLOB)."""
    d = meta.to_json() if hasattr(meta, "to_json") else dict(meta)
    params = (
        d.get("slug", ""),
        d.get("name", ""),
        d.get("description", ""),
        d.get("version", ""),
        d.get("extension_id", ""),
        int(d.get("size_bytes", 0) or 0),
        1 if d.get("enabled", True) else 0,
        d.get("note", "") or "",
        etag or "",
        tarball_bytes,
        _parse_dt(d.get("uploaded_at")),
        _parse_dt(d.get("updated_at")),
    )
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_EXTENSIONS_DDL)
            await cur.execute(
                """INSERT INTO extensions
                   (slug, name, description, version, extension_id,
                    size_bytes, enabled, note, etag, tarball,
                    uploaded_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     name=VALUES(name), description=VALUES(description),
                     version=VALUES(version), extension_id=VALUES(extension_id),
                     size_bytes=VALUES(size_bytes), enabled=VALUES(enabled),
                     note=VALUES(note), etag=VALUES(etag),
                     tarball=VALUES(tarball), updated_at=VALUES(updated_at)""",
                params,
            )


async def load_extensions(pool: Any) -> list[dict]:
    """All extension metadata (NO BLOB) as API-ready dicts, newest first."""
    out: list[dict] = []
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_EXTENSIONS_DDL)
            await cur.execute(
                "SELECT slug, name, description, version, extension_id, "
                "size_bytes, enabled, note, etag, uploaded_at, updated_at "
                "FROM extensions ORDER BY updated_at DESC"
            )
            for r in await cur.fetchall():
                out.append({
                    "slug": r[0],
                    "name": r[1] or "",
                    "description": r[2] or "",
                    "version": r[3] or "",
                    "extension_id": r[4] or "",
                    "size_bytes": int(r[5] or 0),
                    "enabled": bool(r[6]),
                    "note": r[7] or "",
                    "etag": r[8] or "",
                    "uploaded_at": _dt_iso(r[9]),
                    "updated_at": _dt_iso(r[10]),
                    "builtin": False,
                })
    return out


async def fetch_extension_blob(pool: Any, slug: str) -> bytes | None:
    """The tar.gz bytes for one extension, or None when absent."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_EXTENSIONS_DDL)
            await cur.execute("SELECT tarball FROM extensions WHERE slug=%s", (slug,))
            row = await cur.fetchone()
    if row and row[0] is not None:
        return bytes(row[0])
    return None


async def set_extension_enabled_row(pool: Any, slug: str, enabled: bool) -> None:
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE extensions SET enabled=%s, updated_at=UTC_TIMESTAMP(3) "
                "WHERE slug=%s",
                (1 if enabled else 0, slug),
            )


async def delete_extension_row(pool: Any, slug: str) -> None:
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM extensions WHERE slug=%s", (slug,))


# ---------------------------------------------------------------------------
# Profiles: shared metadata in MariaDB; tarball BYTES in MinIO (Phase C-2)
#
# Profile tarballs are large (Chrome user-data-dirs), so unlike extensions the
# bytes go to MinIO (key ``profiles/<name>.tar.gz``) and only the metadata lives
# here. The ``is_default`` column is the shared default-profile pointer (replaces
# the per-hub ``_default.txt``). Every hub reads list/default/etag from here so
# any hub can resolve + serve any profile for a job. Table self-created.
# ---------------------------------------------------------------------------

_PROFILES_DDL = (
    "CREATE TABLE IF NOT EXISTS profiles ("
    "name VARCHAR(64) PRIMARY KEY, size_bytes BIGINT DEFAULT 0, etag VARCHAR(128), "
    "s3_key VARCHAR(255), chrome_profile_name VARCHAR(128), "
    "source_machine VARCHAR(190), note TEXT, is_default TINYINT(1) DEFAULT 0, "
    "owner_id VARCHAR(64) NOT NULL DEFAULT 'default', shared TINYINT(1) NOT NULL DEFAULT 1, "
    "uploaded_at DATETIME(3), updated_at DATETIME(3)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)


def _profile_row_to_dict(r: Any) -> dict:
    return {
        "name": r[0],
        "size_bytes": int(r[1] or 0),
        "etag": r[2] or "",
        "s3_key": r[3] or "",
        "chrome_profile_name": r[4],
        "source_machine": r[5],
        "note": r[6],
        "is_default": bool(r[7]),
        "owner_id": r[8] or "default",
        "shared": bool(r[9]),
        "uploaded_at": _dt_iso(r[10]),
        "updated_at": _dt_iso(r[11]),
    }


_PROFILE_COLS = (
    "name, size_bytes, etag, s3_key, chrome_profile_name, "
    "source_machine, note, is_default, owner_id, shared, uploaded_at, updated_at"
)


async def upsert_profile_row(pool: Any, meta: Any, etag: str = "", s3_key: str = "") -> None:
    """INSERT-or-UPDATE one profile's metadata. Does NOT touch ``is_default``
    (managed by set_default_profile), so a re-upload never changes the default.
    Phase 2b ``owner_id``/``shared`` are written on INSERT and refreshed on
    update from the (sticky-on-re-upload) ProfileMeta, so they propagate to
    every hub via the shared MariaDB profiles table."""
    d = meta.to_json() if hasattr(meta, "to_json") else dict(meta)
    params = (
        d.get("name", ""),
        int(d.get("size_bytes", 0) or 0),
        etag or "",
        s3_key or "",
        d.get("chrome_profile_name"),
        d.get("source_machine"),
        d.get("note"),
        str(d.get("owner_id") or "default"),
        1 if d.get("shared", True) else 0,
        _parse_dt(d.get("uploaded_at")),
        _parse_dt(d.get("updated_at")),
    )
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_PROFILES_DDL)
            await cur.execute(
                """INSERT INTO profiles
                   (name, size_bytes, etag, s3_key, chrome_profile_name,
                    source_machine, note, owner_id, shared, uploaded_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     size_bytes=VALUES(size_bytes), etag=VALUES(etag),
                     s3_key=VALUES(s3_key),
                     chrome_profile_name=VALUES(chrome_profile_name),
                     source_machine=VALUES(source_machine), note=VALUES(note),
                     owner_id=VALUES(owner_id), shared=VALUES(shared),
                     updated_at=VALUES(updated_at)""",
                params,
            )


async def load_profiles(pool: Any) -> list[dict]:
    """All profile metadata as dicts (no bytes), newest first."""
    out: list[dict] = []
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_PROFILES_DDL)
            await cur.execute(
                f"SELECT {_PROFILE_COLS} FROM profiles ORDER BY updated_at DESC"
            )
            for r in await cur.fetchall():
                out.append(_profile_row_to_dict(r))
    return out


async def get_profile_meta_row(pool: Any, name: str) -> dict | None:
    """One profile's metadata (incl. etag, s3_key, is_default) or None."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_PROFILES_DDL)
            await cur.execute(
                f"SELECT {_PROFILE_COLS} FROM profiles WHERE name=%s", (name,)
            )
            r = await cur.fetchone()
    return _profile_row_to_dict(r) if r else None


async def get_default_profile(pool: Any) -> str | None:
    """The shared default profile name, or None."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_PROFILES_DDL)
            await cur.execute("SELECT name FROM profiles WHERE is_default=1 LIMIT 1")
            r = await cur.fetchone()
    return r[0] if r else None


async def set_default_profile(pool: Any, name: str | None) -> None:
    """Mark ``name`` the shared default (clears all others). None clears it."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_PROFILES_DDL)
            await cur.execute("UPDATE profiles SET is_default=0 WHERE is_default=1")
            if name:
                await cur.execute(
                    "UPDATE profiles SET is_default=1 WHERE name=%s", (name,)
                )


async def delete_profile_row(pool: Any, name: str) -> None:
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM profiles WHERE name=%s", (name,))


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


# ---------------------------------------------------------------------------
# Page-role overrides (per-host-template + per-job)
# ---------------------------------------------------------------------------

_HOST_URL_ROLE_OVERRIDES_DDL = (
    "CREATE TABLE IF NOT EXISTS host_url_role_overrides ("
    "host VARCHAR(255) NOT NULL, url_template VARCHAR(512) NOT NULL, "
    "role VARCHAR(32) NOT NULL, set_by VARCHAR(64) DEFAULT '', "
    "set_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3), "
    "PRIMARY KEY (host, url_template)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)

_JOB_ROLE_OVERRIDES_DDL = (
    "CREATE TABLE IF NOT EXISTS job_role_overrides ("
    "job_id VARCHAR(64) PRIMARY KEY, role VARCHAR(32) NOT NULL, "
    "set_by VARCHAR(64) DEFAULT '', "
    "set_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
)


async def host_urls_by_template_get(
    pool: Any, host: str, template: str, limit: int = 3
) -> list:
    """Return up to ``limit`` real observed URLs whose templatize() result
    equals ``template`` for ``host``. Used by the host-edit modal to show
    "what concrete URLs got collapsed into this template" — makes the
    templatization rules tangible. Best-effort: errors return []."""
    if not host or not template:
        return []
    out: list = []
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT url FROM host_url_history "
                    "WHERE host=%s AND template=%s "
                    "ORDER BY last_seen_at DESC LIMIT %s",
                    (str(host), str(template), int(max(1, min(limit, 20)))),
                )
                out = [str(u) for (u,) in await cur.fetchall()]
    except Exception:
        out = []
    return out


async def host_url_role_overrides_get(pool: Any, host: str) -> dict:
    """Return ``{url_template: role}`` for ``host``. Empty when none set."""
    out: dict = {}
    if not host:
        return out
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_HOST_URL_ROLE_OVERRIDES_DDL)
            await cur.execute(
                "SELECT url_template, role FROM host_url_role_overrides "
                "WHERE host=%s", (str(host),),
            )
            for t, r in await cur.fetchall():
                out[str(t)] = str(r)
    return out


async def host_url_role_overrides_list(pool: Any, host: str) -> list:
    """Return [{url_template, role, set_by, set_at}, ...] for ``host``."""
    out: list = []
    if not host:
        return out
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_HOST_URL_ROLE_OVERRIDES_DDL)
            await cur.execute(
                "SELECT url_template, role, set_by, set_at FROM host_url_role_overrides "
                "WHERE host=%s ORDER BY url_template", (str(host),),
            )
            for t, r, b, sa in await cur.fetchall():
                out.append({
                    "url_template": str(t), "role": str(r),
                    "set_by": str(b or ""),
                    "set_at": sa.isoformat() + "Z" if sa else "",
                })
    return out


async def host_url_role_override_upsert(
    pool: Any, host: str, url_template: str, role: str, set_by: str = ""
) -> None:
    if not host or not url_template or not role:
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_HOST_URL_ROLE_OVERRIDES_DDL)
            await cur.execute(
                "INSERT INTO host_url_role_overrides "
                "(host, url_template, role, set_by, set_at) "
                "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(3)) "
                "ON DUPLICATE KEY UPDATE role=VALUES(role), "
                "set_by=VALUES(set_by), set_at=VALUES(set_at)",
                (str(host), str(url_template), str(role), str(set_by or "")),
            )


async def host_url_role_override_delete(pool: Any, host: str, url_template: str) -> int:
    if not host or not url_template:
        return 0
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_HOST_URL_ROLE_OVERRIDES_DDL)
            await cur.execute(
                "DELETE FROM host_url_role_overrides WHERE host=%s AND url_template=%s",
                (str(host), str(url_template)),
            )
            return int(cur.rowcount or 0)


async def job_role_override_get(pool: Any, job_id: str) -> str:
    if not job_id:
        return ""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_JOB_ROLE_OVERRIDES_DDL)
            await cur.execute(
                "SELECT role FROM job_role_overrides WHERE job_id=%s",
                (str(job_id),),
            )
            row = await cur.fetchone()
            return str(row[0]) if row else ""


async def job_role_overrides_get_many(pool: Any, job_ids: list) -> dict:
    """Bulk-fetch per-job overrides: ``{job_id: role}`` (only hits)."""
    if not job_ids:
        return {}
    out: dict = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_JOB_ROLE_OVERRIDES_DDL)
            placeholders = ",".join(["%s"] * len(job_ids))
            await cur.execute(
                "SELECT job_id, role FROM job_role_overrides "
                "WHERE job_id IN (" + placeholders + ")",
                [str(j) for j in job_ids],
            )
            for j, r in await cur.fetchall():
                out[str(j)] = str(r)
    return out


async def job_role_override_upsert(
    pool: Any, job_id: str, role: str, set_by: str = ""
) -> None:
    if not job_id or not role:
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_JOB_ROLE_OVERRIDES_DDL)
            await cur.execute(
                "INSERT INTO job_role_overrides (job_id, role, set_by, set_at) "
                "VALUES (%s, %s, %s, UTC_TIMESTAMP(3)) "
                "ON DUPLICATE KEY UPDATE role=VALUES(role), "
                "set_by=VALUES(set_by), set_at=VALUES(set_at)",
                (str(job_id), str(role), str(set_by or "")),
            )


async def job_role_override_delete(pool: Any, job_id: str) -> int:
    if not job_id:
        return 0
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_JOB_ROLE_OVERRIDES_DDL)
            await cur.execute(
                "DELETE FROM job_role_overrides WHERE job_id=%s",
                (str(job_id),),
            )
            return int(cur.rowcount or 0)
