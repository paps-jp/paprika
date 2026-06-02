"""FastAPI hub.

Phase 3:
- Workers connect via WebSocket /workers/{worker_id}/link and pull jobs.
- POST /jobs prefers an alive worker; falls back to local in-process
  execution when no worker is connected.
- Workers upload captured assets to POST /jobs/{id}/assets.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    FastAPI,
)

from server._logging import setup_logging

# Belt-and-braces: ``python -m server`` already calls setup_logging() in
# main(), but uvicorn workers spun up directly against ``server.hub.app:app``
# (gunicorn, ``--reload``, test fixtures) skip __main__.py entirely. The
# call is idempotent.
setup_logging()
log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Server config + runtime state. Both live in server/hub/_state.py so
# route-group modules and out-of-tree helpers can import them without
# the lazy-import dance that used to be the only way to dodge the
# app.py <-> module circular. Re-exported here for backwards compat.
# ----------------------------------------------------------------------------
from server.hub._state import AppState, HubConfig, config, get_storage_dir, state  # noqa: F401
from server.hub.conventions import (
    ConventionRegistry,
)
from server.hub.engines import (
    EngineRegistry,
    EngineUsageRegistry,
)
from server.hub.extensions import (
    ExtensionRegistry,
)
from server.hub.host_visited import HostVisitedRegistry
from server.hub.hosts import HostRegistry
from server.hub.presets import PresetRegistry
from server.hub.profiles import ProfileRegistry
from server.hub.sessions import SessionRegistry
from server.hub.settings import SettingsRegistry
from server.hub.skills import SkillRegistry
from server.scheduler import WorkerRegistry
from server.store import make_store

# ----------------------------------------------------------------------------
# Lifespan
# ----------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    state.sessions = SessionRegistry()
    # Per-host cookie store lives next to the per-job state files, under
    # data/jobs/hosts/. (We reuse data_dir even though "jobs" is in the
    # path -- the registry just appends /hosts and never collides with
    # job IDs.)
    state.hosts = HostRegistry(config.data_dir)
    state.profiles = ProfileRegistry(config.data_dir)
    state.profiles_lock = asyncio.Lock()
    state.extensions = ExtensionRegistry(config.data_dir)
    state.extensions_lock = asyncio.Lock()
    state.host_visited = HostVisitedRegistry(config.data_dir)
    state.skills = SkillRegistry(config.data_dir)
    state.conventions = ConventionRegistry(config.data_dir)
    state.settings = SettingsRegistry(config.data_dir)
    # Named Submit-form presets, file-per-preset under
    # data/jobs/presets/. Used by the dropdown above the Submit form
    # + the POST /presets/{name}/run endpoint for cron/external
    # triggers.
    state.presets = PresetRegistry(config.data_dir)
    # AI engine registry. Seeds qwen / qwen-chat / cogagent on first
    # start so the existing engine names keep resolving.
    state.engines = EngineRegistry(config.data_dir)
    state.engine_usage = EngineUsageRegistry(config.data_dir)
    # NOTE: ¥/1M default pricing seed runs AFTER MariaDB restore (further
    # down in this lifespan), otherwise MariaDB pulls zero values back
    # and overwrites whatever we seeded here.
    # Ensure the effective storage directory (SMB mount or data_dir)
    # exists. get_storage_dir() reads settings.storage_dir which was
    # just initialised above.
    _sdir = get_storage_dir()
    if _sdir != config.data_dir:
        _sdir.mkdir(parents=True, exist_ok=True)

    # ---- MariaDB: auto-connect, auto-migrate, use as primary store ----
    # When MariaDB settings are configured, the hub:
    #   1. Creates a connection pool
    #   2. Ensures the schema (CREATE TABLE IF NOT EXISTS)
    #   3. Auto-migrates data from Redis/files → MariaDB (INSERT IGNORE)
    #   4. Uses MariaDBJobStore as the primary job store
    #   5. Restores MariaDB data → file registries (so they stay in sync)
    # If MariaDB is unreachable, falls back to Redis / in-memory.
    _mdb_pool = None
    if state.settings is not None:
        _mdb_host = state.settings.get("mariadb_host", "")
        _mdb_user = state.settings.get("mariadb_username", "")
        if _mdb_host and _mdb_user:
            try:
                from server.hub.mariadb import create_pool as _mdb_create_pool

                _mdb_pool = await _mdb_create_pool(
                    host=_mdb_host,
                    port=int(state.settings.get("mariadb_port", 3306)),
                    database=state.settings.get("mariadb_database", "paprika"),
                    username=_mdb_user,
                    password=state.settings.get("mariadb_password", ""),
                )
                state.mariadb_pool = _mdb_pool
                log.info("MariaDB pool created (%s@%s)", _mdb_user, _mdb_host)

                # ORDER MATTERS: restore BEFORE migrate.
                # The "MariaDB is source-of-truth when connected" contract
                # requires deletions made directly in MariaDB (phpMyAdmin,
                # ad-hoc SQL) to propagate to files on the next restart.
                # If we migrated first, ``INSERT IGNORE`` would resurrect
                # any file row whose slug had been deleted from MariaDB
                # -- the subsequent restore would then never wipe it
                # because MariaDB just "got it back". Doing restore
                # first means the file mirror is brought into line with
                # MariaDB (orphans deleted, present rows refreshed)
                # BEFORE migrate runs, so migrate now only pushes
                # genuinely new file-only entries (= engines created
                # while MariaDB was disconnected).
                #
                # First-time-connecting safety: restore_all_registries
                # internally gates each table on ``_mdb_count > 0``, so
                # an empty MariaDB at first connection doesn't trigger
                # the wipe and migrate gets to seed the table from files.
                from server.hub.mariadb import (
                    auto_migrate_all,
                    restore_all_registries,
                )

                # 1. Restore: MariaDB → files (deletion-reconciling).
                try:
                    restored = await restore_all_registries(
                        _mdb_pool,
                        host_registry=state.hosts,
                        visited_registry=state.host_visited,
                        skill_registry=state.skills,
                        convention_registry=state.conventions,
                        engine_registry=state.engines,
                        preset_registry=state.presets,
                    )
                    if restored:
                        log.info("MariaDB restore: %s", restored)
                except Exception as e:
                    log.warning("MariaDB registry restore failed: %s", e)

                # 2. Migrate: files → MariaDB (INSERT IGNORE).
                #    Catches first-time-connect and engines created while
                #    MariaDB was unreachable. After step 1, files are
                #    already a subset (or exact mirror) of MariaDB, so
                #    most rows here are no-ops.
                migrated = await auto_migrate_all(
                    _mdb_pool,
                    redis_url=config.redis_url,
                    host_registry=state.hosts,
                    visited_registry=state.host_visited,
                    skill_registry=state.skills,
                    convention_registry=state.conventions,
                    engine_registry=state.engines,
                    preset_registry=state.presets,
                )
                if migrated:
                    log.info("MariaDB auto-migrate: %s", migrated)

            except Exception as e:
                log.warning(
                    "MariaDB connection/migration failed (%s); "
                    "falling back to Redis/in-memory",
                    e,
                )
                _mdb_pool = None
                state.mariadb_pool = None

    # ¥/1M default pricing seed (U). Runs AFTER MariaDB restore so the
    # auto-priced values land in BOTH the file mirror and MariaDB
    # (via the upsert which routes through the upsert_engine_row write-
    # through). Operator-edited prices are preserved because the seeder
    # only touches engines whose cost fields are still 0.0. Idempotent.
    try:
        from server.hub.engines import seed_default_pricing as _seed_pricing
        _n_priced = _seed_pricing(state.engines)
        if _n_priced:
            log.info("engines: auto-priced %d engine(s) with default ¥/1M rates", _n_priced)
            # Push the freshly-priced records to MariaDB so the next
            # restart's restore returns the new values instead of 0.
            if state.mariadb_pool is not None:
                try:
                    from server.hub.mariadb import upsert_engine_row as _mdb_upsert_eng
                    for _rec in state.engines.list_all():
                        if _rec.cost_input_per_1m_jpy or _rec.cost_output_per_1m_jpy:
                            try:
                                await _mdb_upsert_eng(state.mariadb_pool, _rec)
                            except Exception:
                                pass
                except Exception:
                    pass
    except Exception as _e:
        log.warning("engines: default pricing seed crashed: %s: %s", type(_e).__name__, _e)

    state.store, state.store_kind = await make_store(
        config.redis_url, mariadb_pool=_mdb_pool,
    )
    state._local_sem = asyncio.Semaphore(config.max_concurrent_jobs)

    # WorkerJobLog batcher: when using Redis, buffer log lines and
    # flush in pipeline batches (50 lines or 100ms). Cuts Redis ops
    # from ~10 000/sec to ~200/sec at 200 workers. No-op for
    # InMemoryJobStore (which is already zero-cost).
    if state.store_kind == "redis":
        from server.hub._log_batcher import LogBatcher

        state.log_batcher = LogBatcher(state.store)

    # Worker registry — pass the redis client if we have one. hub_id
    # records WS ownership in Redis (multi-hub foundation; dormant for
    # single hub).
    redis_client = getattr(state.store, "_r", None)
    state.registry = WorkerRegistry(redis_client=redis_client, hub_id=config.hub_id)
    # Mirror the Session Map (sid -> worker/hub) to the same Redis so a
    # future Hub→Hub forwarding layer can route session actions across
    # replicas. Writes only; nothing reads it back yet.
    state.sessions.bind_redis(redis_client, config.hub_id)

    # Background reaper that evicts idle / aged sessions (RFC-001 §11).
    # Otherwise a client that forgets to DELETE leaks a Lane forever.
    reaper_task = asyncio.create_task(_session_reaper_loop())

    # Selection-loop retire phase: hourly, drop auto-tier skills/conventions
    # that the fitness signal (success_count/use_count) shows are duds or
    # zombies. Curated is never auto-touched; auto deletion is gated by the
    # ``auto_retire_enabled`` setting (default off -> dry-run logs only).
    retire_task = asyncio.create_task(_skill_convention_reaper_loop())

    # Dead-worker reaper: drop Redis registrations for workers that
    # haven't heartbeated in > 7 days. Stops the Workers tab from
    # silting up with stale entries every time the fleet churns
    # (clone-collision burst, version-mismatch loop, redeploy).
    from server.hub._reaper import _dead_worker_reaper_loop
    dead_worker_task = asyncio.create_task(_dead_worker_reaper_loop())

    # Job-lease loop (multi-hub control-plane phase 4: dead-hub recovery).
    # Refreshes leases for this hub's in-flight codegen-loop/rerun jobs and
    # re-dispatches jobs orphaned by a crashed peer. Gated by
    # PAPRIKA_JOB_LEASE_ENABLED (default OFF) -- the loop returns immediately
    # when off, so single-hub behaviour is unchanged.
    from server.hub._reaper import _job_lease_loop
    job_lease_task = asyncio.create_task(_job_lease_loop())

    # SMB storage: a cifs mount does not survive a restart, so re-mount
    # the configured share NOW (before the first job needs storage_dir)
    # and spawn a watchdog that re-mounts it if it ever drops (NAS
    # reboot / network blip). No-op when SMB isn't configured or
    # smb_auto_mount is off. Needs CAP_SYS_ADMIN (same as the manual
    # /settings/smb/mount endpoint).
    smb_watchdog_task = None
    try:
        from server.hub.smb_mount import (
            ensure_smb_mounted,
            smb_is_configured,
            smb_watchdog_loop,
        )

        if state.settings is not None and smb_is_configured(state.settings):
            ok, msg = await asyncio.to_thread(ensure_smb_mounted, state.settings)
            if ok:
                log.info("SMB: share mounted at startup (%s)", msg)
            else:
                log.warning(
                    "SMB: startup mount skipped/failed (%s); watchdog will retry",
                    msg,
                )
        smb_watchdog_task = asyncio.create_task(smb_watchdog_loop())
    except Exception:
        log.exception("SMB startup mount / watchdog launch failed")

    # Recover from previous hub crash / deploy: any job persisted as
    # `status=running` but no longer driven by a local task is an
    # orphan from a killed orchestrator. Mark it failed so it doesn't
    # stay "running" forever in the admin UI.
    try:
        recovered = await _recover_orphan_running_jobs()
        if recovered:
            log.info(
                "recovery: marked %d orphan running job(s) as failed "
                "(orchestrator killed by previous hub restart)",
                recovered,
            )
    except Exception:
        log.exception("recovery scan failed")

    # Sweep orphan paprika-runner containers from the previous hub
    # incarnation. Without this they keep polling /sessions/.../state
    # forever (the session they were bound to was reaped during the
    # restart), wedging the hub log and starving worker WS keepalives.
    # See server/hub/runner.py:sweep_orphan_runners for the gory story.
    try:
        from server.hub.runner import sweep_orphan_runners

        n_runners = await sweep_orphan_runners()
        if n_runners:
            log.info(
                "recovery: killed %d orphan paprika-runner container(s) "
                "from previous hub incarnation",
                n_runners,
            )
    except Exception:
        log.exception("runner sweep failed")

    log.info(
        "store=%s  data_dir=%s  max_local=%d  public_base=%s",
        state.store_kind,
        config.data_dir.resolve(),
        config.max_concurrent_jobs,
        config.public_base_url or "(auto from request)",
    )

    yield

    reaper_task.cancel()
    retire_task.cancel()
    dead_worker_task.cancel()
    job_lease_task.cancel()
    if smb_watchdog_task is not None:
        smb_watchdog_task.cancel()
    for t in list(state.local_tasks.values()):
        if not t.done():
            t.cancel()
    # Drain any buffered log lines before closing the store.
    if state.log_batcher is not None:
        try:
            await state.log_batcher.flush_all()
        except Exception:
            pass
    # Close MariaDB pool if it was lazily initialised.
    if state.mariadb_pool is not None:
        try:
            from server.hub.mariadb import close_pool
            await close_pool(state.mariadb_pool)
            state.mariadb_pool = None
        except Exception:
            pass
    if state.store is not None:
        try:
            await state.store.close()
        except Exception:
            pass


# ---- Session reaper + orphan-job recovery -- moved to _reaper.py (#2B-H) ---
from server.hub._reaper import (  # noqa: F401
    _REAPER_INTERVAL_S,
    _recover_orphan_running_jobs,
    _session_reaper_loop,
    _skill_convention_reaper_loop,
)

# Tag taxonomy: groups every endpoint into a logical section in the
# Swagger UI / ReDoc viewer. Order here = order of appearance in
# /docs. Each tag has a one-line description so the section header is
# self-explanatory; the actual route -> tag mapping is done by
# ``_apply_route_tags()`` at module-load tail (a single regex sweep
# over ``app.routes``, easier than threading tags through 200+
# decorators by hand).
#
# Descriptions are kept in English so the rendered Swagger UI reads
# the same regardless of the operator's locale -- the source of truth
# for users is the GitHub Pages manual; Swagger is for developers
# integrating against the API.
_OPENAPI_TAGS = [
    {
        "name": "Jobs",
        "description": "Submit jobs, poll status, fetch results / page.html / log / assets.",
    },
    {
        "name": "Sessions",
        "description": "Drive a live browser session: page.* primitives (goto / click / fill / scroll / agent / ask / capture), cookies, outline, links, visited URLs.",
    },
    {
        "name": "Workers",
        "description": "Inspect the worker fleet, change per-worker status (active / drain / standby), serve the self-update source tarball.",
    },
    {
        "name": "Preview",
        "description": "Live preview thumbnails of each worker lane. Lightweight, polled, never persisted -- for 'what's on screen right now?' use.",
    },
    {
        "name": "Screenshots",
        "description": "Take a high-quality screenshot of a running job's lane, save as a JPEG asset, browse the saved set per job.",
    },
    {
        "name": "noVNC",
        "description": "Hub-proxied noVNC viewer (HTML + assets + WebSocket bridge) so external clients never need to reach a worker LAN IP directly.",
    },
    {
        "name": "AI Engines",
        "description": "Registry of LLM / VLM / VLA backends. Used by page.agent / page.ask and codegen to pick a model at request time.",
    },
    {
        "name": "Skills",
        "description": "Reusable script snippets the LLM can pull in as context (manual + auto-distilled).",
    },
    {
        "name": "Conventions",
        "description": "Codegen guardrails: project-specific 'always do X / never do Y' rules injected into every prompt.",
    },
    {
        "name": "Presets",
        "description": "Named job templates: save once, replay with POST /presets/{name}/run from cron or external schedulers.",
    },
    {
        "name": "Hosts",
        "description": "Per-host cookies, default options, and other site-specific configuration that hitches a ride on every job that touches the host.",
    },
    {
        "name": "Settings",
        "description": "Hub-wide configuration: toggle codegen / skill-distillation / convention-distillation, default timeouts, etc.",
    },
    {
        "name": "Codegen",
        "description": "LLM-driven script generation (one-shot + iterative). Service discovery + manual trigger; the main entry is via Jobs.",
    },
    {
        "name": "System",
        "description": "Health probe, static assets, OpenAPI / docs / redoc, admin cleanup, and the catch-all quick-fetch shortcut.",
    },
]


app = FastAPI(
    title="Paprika",
    description=(
        "Distributed browser fleet hub.\n\n"
        "User-facing manual: <https://paps-jp.github.io/paprika/>\n\n"
        "Endpoints are grouped by feature -- see the tag sections below."
    ),
    lifespan=lifespan,
    openapi_tags=_OPENAPI_TAGS,
)

# Admin-UI static assets. Extracted from the inline _ADMIN_HTML blob in
# Nov 2026 (admin.css 761 lines, admin.js 8963 lines, net -9726 in
# app.py). The HTML references them with a ``?v={hub_version}`` query
# string baked in at request time, so a deploy that ships new JS gets
# picked up by every connected browser without an explicit no-cache
# header (StaticFiles' default ETag handling is fine for fingerprinted
# URLs).
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ---- Codegen-loop + rerun orchestrators -- moved to _jobrunner.py ------
# Re-export for routes/jobs.py lazy-bridge wrappers and any in-app.py
# code (currently none directly) that still references these names.
from server.hub._jobrunner import (  # noqa: F401
    _copy_session_state_dir,
    _distill_convention_background,
    _distill_skill_background,
    _final_attempt_judge_ok,
    _run_codegen_loop_job,
    _run_rerun_loop_job,
)

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


# These leaf helpers moved to server/hub/_helpers.py so route modules can
# import them directly instead of lazy-importing through app.py (the old
# cycle workaround). Re-exported here for backwards compatibility.
from server.hub._helpers import (  # noqa: F401,E402
    _asset_upload_url,
    _ffmpeg_q_from_quality_pct,
    _hub_base_url,
    _safe_job_file,
)


# ---- Hub version resolver -- moved to _version.py (#2B-H) ---------------
from server.hub._version import (  # noqa: F401
    _HUB_VERSION_FILE,
    _compute_hub_source_version,
    _hub_version,
)


def _mint_unique_worker_id(registry: WorkerRegistry, hint: str) -> str:
    """Generate a fresh worker_id that isn't currently held.

    Called when the hub detects a clone collision (same worker_id from a
    different client IP, original still alive). The hint is the colliding
    ID -- we strip any trailing ``-<rand4>`` suffix so repeated clones
    don't grow unboundedly long, then attach a new 4-char suffix and
    keep trying until we land on something unused.
    """
    import random
    import string
    import uuid

    # Strip an existing "-rand4" tail (e.g. "host-aB3z" -> "host") so we
    # never produce IDs like "host-aB3z-Xq91-7vR2" after a few clone
    # generations. If the hint has no dash, use it whole.
    if "-" in hint:
        base, tail = hint.rsplit("-", 1)
        if len(tail) <= 8 and tail.isalnum():
            pass  # base is already the trimmed form
        else:
            base = hint
    else:
        base = hint
    base = base or "worker"

    in_use = set(registry.connections.keys())
    alphabet = string.ascii_lowercase + string.digits
    for _ in range(50):
        suffix = "".join(random.choices(alphabet, k=4))
        cand = f"{base}-{suffix}"
        if cand not in in_use:
            return cand
    # Pathological fallback: use a full uuid4 hex tail.
    return f"{base}-{uuid.uuid4().hex[:8]}"


# ---- POST /jobs (create_job) -- moved to routes/jobs.py (#2B-G3) -------

# ---- /jobs lifecycle (list/get/result/cancel/delete/cleanup) ----
# ---- moved to routes/jobs.py (#2B-G2). Re-imports from there      ----
# ---- are not needed: the only outside callers of the moved        ----
# ---- functions are the codegen-loop background tasks (which use  ----
# ---- state.local_tasks directly) and there are none.              ----


# Grace period (seconds) for job dispatch when no worker currently has
# free capacity. pick_worker() returning None is usually transient --
# right after a hub restart the workers take a few seconds to reconnect
# their WebSockets, and a job submitted in that window would otherwise
# instantly 503 ("fleet at capacity"). We poll pick_worker() for up to
# this many seconds before giving up. Set to 0 to restore the old
# instant-reject behaviour. Default 8s comfortably covers a hub-restart
# reconnect (workers reconnect within ~1-3s of the WS drop).
JOB_DISPATCH_GRACE_S = float(os.environ.get("PAPRIKA_JOB_DISPATCH_GRACE_S") or 8.0)
# How often to re-poll pick_worker() during the grace window.
_JOB_DISPATCH_POLL_S = 0.5


# ---- /profiles -- moved to routes/profiles.py (#2B-D-profiles) ---------
# _sync_all_profiles_to_worker is called from worker-connect code still
# in app.py; re-export so the existing call site stays unchanged.
from server.hub.routes.profiles import _sync_all_profiles_to_worker  # noqa: F401
from server.hub.routes.profiles import router as _profiles_router

app.include_router(_profiles_router)


# ---- Chrome extension registry -- moved to routes/extensions.py (#2B-D) --
from server.hub.routes.extensions import router as _extensions_router

app.include_router(_extensions_router)


# ---- Per-host cookies + auto-login + visited -- moved to routes/hosts.py (#2B-D) --
# (The _ensure_host_login helper stays here -- the job runner pre-fetch
#  hook calls it too. routes/hosts.py imports it lazily.)
# Re-export the host helpers so session-route code still in app.py
# can keep using ``_require_hosts()`` unchanged until #2B-F migrates
# /sessions and lets those callers import directly.
from server.hub.routes.hosts import (  # noqa: F401
    _host_to_dict,
    _require_host_visited,
    _require_hosts,
)
from server.hub.routes.hosts import router as _hosts_router

app.include_router(_hosts_router)

# ---- auto re-login -- moved to routes/hosts.py (#2B-H) ------------------
# Re-export so create_job pre-fetch hook in routes/jobs.py keeps working.
from server.hub.routes.hosts import _ensure_host_login, _session_state_dict  # noqa: F401

# ---- Submit-form presets -- moved to routes/presets.py (#2B-D) ------------
from server.hub.routes.presets import router as _presets_router

app.include_router(_presets_router)


# ----------------------------------------------------------------------------
# Engine registry (AI Engines tab) -- moved to server/hub/routes/engines.py (#2B-B)
# Skill registry (LLM-distilled patterns) -- moved to routes/skills.py (#2B-C)
# Convention registry (atomic codegen rules) -- moved to routes/conventions.py (#2B-C)
# ----------------------------------------------------------------------------
from server.hub.routes.conventions import router as _conventions_router
from server.hub.routes.engines import router as _engines_router
from server.hub.routes.skills import router as _skills_router

app.include_router(_engines_router)
app.include_router(_skills_router)
app.include_router(_conventions_router)


# ----------------------------------------------------------------------------
# Hub-wide settings -- moved to server/hub/routes/settings.py (#2B-B)
# ----------------------------------------------------------------------------
from server.hub.routes.settings import router as _settings_router

app.include_router(_settings_router)


# ---- LLM availability surface (Win-1: graceful degrade) --------------------
# Read-only ``GET /llm/status`` so the admin UI can grey-out LLM
# controls / show a banner when no engine is configured. Kept separate
# from /settings to avoid bloating the hot page-load payload and so the
# UI can poll it cheaply.
from server.hub.routes.forensics import router as _forensics_router
app.include_router(_forensics_router)

from server.hub.routes.oprec import router as _oprec_router
app.include_router(_oprec_router)

from server.hub.routes.llm import router as _llm_router

app.include_router(_llm_router)


# ---- CDP-Screencast live viewer (Windows portable noVNC replacement) -------
# Worker_id with chrome_attach_port set (= Windows portable's single
# Chromium) gets a viewer at /sessions/{sid}/screencast/ that opens a
# WebSocket to /sessions/{sid}/screencast/ws and streams JPEG frames
# from Chrome's Page.startScreencast. Works in headless mode (no
# physical screen capture needed).
from server.hub.routes.screencast import router as _screencast_router

app.include_router(_screencast_router)


# ---- /jobs/{id} file-serve + asset gallery -- moved to routes/jobs.py (#2B-G) --
# Re-export helpers + mime constants that now live in routes/jobs.py
# so the /jobs routes still in app.py (create_job / list / get /
# result / cancel / delete / cleanup / screenshot / assets POST /
# files / WS events) resolve unchanged until #2B-G2 finishes the
# migration.
from server.hub.routes.jobs import (  # noqa: F401
    _AUDIO_EXTS,
    _IMG_EXTS,
    _VIDEO_EXTS,
    _asset_href,
    _extract_links_from_html,
    _human_size,
    _require_job_info,
    _soft_resolve_job,
)
from server.hub.routes.jobs import router as _jobs_router

app.include_router(_jobs_router)


# ---- Live log WebSocket -- moved to routes/jobs.py (#2B-G2) -------------

# ----------------------------------------------------------------------------
# /workers — worker-facing WebSocket + listing
# ----------------------------------------------------------------------------

# ---- worker_link WS + worker-protocol helpers -- moved to routes/workers.py (#2B-G3-partial)

# ---- Worker registry HTTP routes -- moved to routes/workers.py (#2B-E) ----
# (WS /workers/{id}/link + /workers/{id}/lanes/{idx}/preview still here.)
from server.hub.routes.workers import router as _workers_router

app.include_router(_workers_router)


# ----------------------------------------------------------------------------
# Session API (RFC-001)
# ----------------------------------------------------------------------------
#
# A Session is a long-lived reservation of a Lane that the client drives
# action by action over HTTP. POST /sessions opens one (the hub picks a
# free Lane on some Worker); the client then hits
# /sessions/{id}/click, /sessions/{id}/fill, etc.; DELETE /sessions/{id}
# releases the lane.
#
# Behaviourally the same browser_ops primitives are used as the agent
# loop -- this surface just exposes them over HTTP for clients that
# want deterministic, script-driven control instead of LLM-driven.


# ---- Session API (RFC-001) -- moved to routes/sessions.py (#2B-F) --------
# All 37 HTTP routes + the 4 core helpers (_require_session_infra,
# _get_session_or_404, _route_to_page, _send_session_action) plus the
# public handler functions (create_session, close_session, session_agent,
# session_save_cookies_to_host) live there now. Re-exported below so the
# auto re-login chain (L3069+), lifespan cleanup (L332/389), and pre-fetch
# hook (L3784/3878) keep working unchanged.
#
# Still in app.py until #2B-F-novnc: the noVNC HTTP proxy routes
# (/sessions/{id}/novnc/, /novnc/{subpath}) and the WS websockify proxy.
from server.hub.routes.sessions import (  # noqa: F401
    _get_session_or_404,
    _novnc_autoconnect,
    _require_session_infra,
    _route_to_page,
    _send_session_action,
    close_session,
    create_session,
    session_agent,
    session_save_cookies_to_host,
)
from server.hub.routes.sessions import router as _sessions_router

app.include_router(_sessions_router)


# ----------------------------------------------------------------------------
# Two distinct screenshot concepts
# --------------------------------
# Paprika exposes two screenshot endpoints with different intents.
# The split matters because the cost profile is wildly different and
# the URLs were previously confused (both ended up taking the same
# heavy default).
#
#   GET  /workers/{wid}/lanes/{idx}/preview      = PREVIEW
#       "what's on screen RIGHT NOW?" -- ephemeral, light, polled.
#       Used by the admin UI's Live Preview tile grid + each Live
#       panel's preview tab. Cheap defaults (small width, aggressive
#       JPEG compression) so 25 lanes × 5s polling stays under
#       ~1 MB/s steady state.
#       Note: the legacy URL ``/lanes/{idx}/screenshot`` is kept as
#       an alias for one release cycle; the canonical name is
#       ``/preview`` to make the conceptual separation from the
#       Capture endpoint explicit at the URL level.
#
#   POST /jobs/{job_id}/screenshot                = SCREENSHOT
#       "save this exact moment as an asset" -- persisted JPEG file
#       under data/jobs/<id>/assets/ + sidecar metadata. Used by the
#       "Screenshot" button in the Live panel + future per-step
#       capture hooks. Heavy defaults (readable resolution, high
#       JPEG quality) so the saved file is useful for human review
#       or LLM-vision analysis later.
#       Note: legacy URL /jobs/{id}/screenshot/capture is kept as a
#       hidden alias for one release cycle.
#
# Both endpoints take a ``quality`` parameter on a 0-100 perceptual
# scale (100 = best). The hub translates to ffmpeg's q:v (2-31, lower
# is better) before dispatching to the worker, so the API stays
# intuitive while the worker code stays unchanged.
# ----------------------------------------------------------------------------


# _ffmpeg_q_from_quality_pct moved to server/hub/_helpers.py (re-exported above).


# ---- worker_lane_preview -- moved to routes/workers.py (#2B-G3-partial) -


# ---- noVNC HTTP + WS proxy -- moved to routes/novnc.py (#2B-F-novnc) -----
# Re-export URL-builder + cleanup helpers so /jobs handlers (still in
# app.py) and the routes/sessions.py lazy-import chain keep working:
#  * _proxy_info: rewrites JobInfo.novnc_url on /jobs responses
#  * _hub_proxied_novnc_url / *_for_session: building blocks
#  * _proxy_session_dict: rewrites SessionInfo.to_json() dicts
#  * _disconnect_session_novnc_clients: invoked by close_session
from server.hub.routes.novnc import (  # noqa: F401
    _disconnect_session_novnc_clients,
    _find_active_session_id,
    _hub_proxied_novnc_url,
    _hub_proxied_novnc_url_for_session,
    _proxy_info,
    _proxy_session_dict,
    _resolve_session_novnc_target,
)
from server.hub.routes.novnc import router as _novnc_router

app.include_router(_novnc_router)


# ---- /jobs upload + screenshot -- moved to routes/jobs.py (#2B-G2) ------

# ----------------------------------------------------------------------------
# Index / health
# ----------------------------------------------------------------------------

# Inline SVG for the paprika logo. Served from /icon.svg so every
# HTML surface (admin dashboard, /screenshots, /jobs/*/log,
# per-job galleries) can reference one URL instead of duplicating
# markup. Also used as the favicon via <link rel="icon"
# type="image/svg+xml" href="/icon.svg"> in each <head>.
# ---- System probes + logo -- moved to routes/system.py (#2B-E) -----------
from server.hub.routes.system import router as _system_router

app.include_router(_system_router)


# ---- Admin UI shell (/) -- moved to routes/system.py (#2B-G3-partial)

# ---- /screenshots HTML viewer -- moved to routes/system.py (#2B-G3-partial)


# ----------------------------------------------------------------------------
# /ui/log/{id} — live tail-f viewer (HTML page). /jobs/{id}/log is a
# legacy alias of the same handler.
#
# The page connects to the existing /jobs/{id}/events WebSocket and renders
# log lines in a dark terminal-style pane that auto-scrolls. Unlike the raw
# log.txt download (which only ever shows what's already on disk), this
# updates in real time while the job is running.
# ----------------------------------------------------------------------------

# ---- /ui/log live-log HTML viewer -- moved to routes/jobs.py (#2B-G3-partial)


# /info + /health moved to routes/system.py alongside /icon.svg (#2B-E)

# ----------------------------------------------------------------------------
# URL pass-through:  GET /https://example.com   ==>   POST /jobs
#
# Quick one-line job submission from a browser address bar or curl.
# Options can be passed as query params:
#   GET /https://example.com?scroll=1&play_videos=1&max_wait=120
#
# NOTE: if the target URL itself has a query string, URL-encode it (e.g.
# /https://x.com/y%3Ffoo%3Dbar) or use POST /jobs with a JSON body.
#
# This route must be registered LAST so it doesn't shadow /jobs, /*,
# /docs, /openapi.json, etc.
# ----------------------------------------------------------------------------

# ---- URL pass-through -- moved to routes/passthrough.py (#2B-G3-partial)


# ----------------------------------------------------------------------------
# Tag every route so /docs (Swagger) and /redoc group endpoints by
# feature instead of dumping them in a flat alphabetical pile.
#
# Approach: regex-match each route's path against an ordered rule list,
# assign the first matching tag. This is way less invasive than
# threading ``tags=[...]`` through 200+ decorators by hand, and the
# rules below double as a quick map "URL prefix -> functional area".
#
# Rules run in declaration order so more-specific patterns (e.g.
# ``/sessions/{sid}/novnc/...`` -> noVNC) win over the broader
# ``/sessions`` -> Sessions catch-all. Anything that doesn't match is
# bucketed under "System" so it's still discoverable.
# ----------------------------------------------------------------------------

import re as _re_tag

_ROUTE_TAG_RULES: list[tuple[_re_tag.Pattern[str], str]] = [
    # More-specific routes first.
    (_re_tag.compile(r"^/sessions/[^/]+/novnc(/|$)"), "noVNC"),
    (_re_tag.compile(r"^/ui/assets(/|$)"), "Screenshots"),
    (_re_tag.compile(r"^/ui/attempts(/|$)"), "Screenshots"),  # legacy alias
    (_re_tag.compile(r"^/ui/log(/|$)"), "Jobs"),
    (_re_tag.compile(r"^/jobs/[^/]+/screenshots?(/|$)"), "Screenshots"),
    (_re_tag.compile(r"^/jobs/[^/]+/gallery"), "Screenshots"),
    (_re_tag.compile(r"^/jobs/[^/]+/assets"), "Screenshots"),
    (_re_tag.compile(r"^/workers/[^/]+/lanes/[^/]+/preview"), "Preview"),
    (_re_tag.compile(r"^/workers/[^/]+/lanes/[^/]+/screenshot"), "Preview"),
    (_re_tag.compile(r"^/workers/[^/]+/link"), "Workers"),
    (_re_tag.compile(r"^/screenshots"), "Preview"),
    (_re_tag.compile(r"^/worker-source"), "Workers"),
    # Broader catch-alls.
    (_re_tag.compile(r"^/jobs"), "Jobs"),
    (_re_tag.compile(r"^/sessions"), "Sessions"),
    (_re_tag.compile(r"^/workers"), "Workers"),
    (_re_tag.compile(r"^/engines"), "AI Engines"),
    (_re_tag.compile(r"^/skills"), "Skills"),
    (_re_tag.compile(r"^/conventions"), "Conventions"),
    (_re_tag.compile(r"^/presets"), "Presets"),
    (_re_tag.compile(r"^/hosts"), "Hosts"),
    (_re_tag.compile(r"^/settings"), "Settings"),
    (_re_tag.compile(r"^/codegen"), "Codegen"),
    # Tiny system surface: /health, /icon.svg, /openapi.json (auto),
    # /docs, /redoc, catch-all quick-fetch ``/{full_url:path}``.
    (_re_tag.compile(r"^/health"), "System"),
    (_re_tag.compile(r"^/icon"), "System"),
    (_re_tag.compile(r"^/$"), "System"),
    (_re_tag.compile(r"^/\{full_url"), "System"),
]


def _apply_route_tags() -> None:
    """Walk every registered route, apply a single feature tag based
    on the first regex match in ``_ROUTE_TAG_RULES``. Routes already
    carrying an explicit tag (= someone wrote ``tags=[...]`` on the
    decorator) are left alone.

    Called once at module import tail, after every ``@app.{verb}``
    decorator has registered its route. The mutation has to land
    before FastAPI builds the OpenAPI schema (which is lazy on first
    /openapi.json hit), so this position is fine.
    """
    from fastapi.routing import APIRoute, APIWebSocketRoute

    for r in app.routes:
        # Skip FastAPI's built-in routes (/docs, /redoc, /openapi.json):
        # they're WSGI/HTML servers, not user-facing API.
        path = getattr(r, "path", "")
        if not path or path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
            continue
        if not isinstance(r, (APIRoute, APIWebSocketRoute)):
            continue
        if getattr(r, "tags", None):
            continue  # respect explicit tags=[...] if any
        for pat, tag in _ROUTE_TAG_RULES:
            if pat.search(path):
                r.tags = [tag]
                break
        else:
            r.tags = ["System"]


# ---- URL pass-through catch-all (MUST be the last include_router) ------
# /{full_url:path} matches everything, so its router has to be mounted
# AFTER every other route is registered or it shadows them.
from server.hub.routes.passthrough import router as _passthrough_router

app.include_router(_passthrough_router)

_apply_route_tags()
