"""Hub process state + config — extracted from server/hub/app.py.

Lives in its own module so route-group modules under
``server/hub/routes/`` and out-of-tree helpers (``web_search``,
``_url_utils``, future test fixtures) can ``from server.hub._state
import state, config`` directly, instead of doing the lazy-import
dance to dodge the app.py <-> module circular that used to be the
only option.

app.py re-exports the same names for backwards compatibility, so old
in-tree callers (``from server.hub.app import state``) keep working
until they're migrated module by module.
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

from server.hub.conventions import ConventionRegistry
from server.hub.engines import EngineRegistry, EngineUsageRegistry
from server.hub.extensions import ExtensionRegistry
from server.hub.host_visited import HostVisitedRegistry
from server.hub.hosts import HostRegistry
from server.hub.presets import PresetRegistry
from server.hub.profiles import ProfileRegistry
from server.hub.sessions import SessionRegistry
from server.hub.settings import SettingsRegistry
from server.hub.skills import SkillRegistry

# Registry types referenced by AppState. Each Registry lives in its
# own ``server.hub.<topic>`` module; none of them import back from
# ``server.hub.app``, so importing them here is safe (no cycle).
from server.scheduler import WorkerRegistry
from server.store import JobStore


class HubConfig:
    """Process-wide configuration set by the hub's CLI entrypoint.
    Routes / handlers / helpers read these as ``config.<field>``."""

    data_dir: Path = Path("./data/jobs")
    max_concurrent_jobs: int = 2
    redis_url: str | None = None
    public_base_url: str | None = None  # how workers reach this hub
    worker_secret: str | None = None  # shared secret for worker auth
    # Client/admin API auth mode: "off" | "optional" | "enforce".
    #   off      -- today's behaviour: every request is the SYSTEM principal.
    #   optional -- attribute a principal when a credential is present, else
    #               ANONYMOUS; never blocks (transition / observation mode).
    #   enforce  -- a valid principal is required; anonymous is rejected.
    # Ramped via PAPRIKA_AUTH_MODE so flipping it on doesn't break the live
    # fleet / existing clients until we deliberately move to enforce. See
    # server/hub/auth.py + the auth middleware in app.py.
    auth_mode: str = (os.environ.get("PAPRIKA_AUTH_MODE") or "off").strip().lower()
    # Stable identifier for THIS hub process / replica. Records which
    # hub owns a worker's control WebSocket and tags the Redis Session
    # Map — the foundation for multi-hub (nginx + Hub×N) routing.
    # Defaults to $HUB_ID, else the container / host name. Completely
    # dormant for single-hub deployments (nothing reads it back until a
    # Hub→Hub forwarding layer is added), so writing it is side-effect
    # free. In a compose scale-out, set ``HUB_ID`` per replica.
    hub_id: str = os.environ.get("HUB_ID") or socket.gethostname()
    # Process role. When PAPRIKA_ROLE=admin this is a read-only management
    # service (serves the admin UI + reads the shared stores) that runs NO
    # worker WS, NO job dispatch, NO reapers / leases / orphan-recovery. Read
    # once at import — the CLI entrypoint (_run_admin) sets the env before the
    # hub app is imported. Default ("" / "hub" / "all") = full hub, unchanged.
    admin_mode: bool = (os.environ.get("PAPRIKA_ROLE") or "").strip().lower() == "admin"


config = HubConfig()


class AppState:
    """Mutable runtime state populated by the lifespan hook.

    Every field starts None / empty so importing this module is cheap
    -- nothing here touches disk or Redis. The actual registries are
    instantiated in app.py's ``lifespan`` after CLI config is applied.
    """

    store: JobStore | None = None
    store_kind: str = "in-memory"
    local_tasks: dict[str, asyncio.Task] = {}
    _local_sem: asyncio.Semaphore | None = None
    registry: WorkerRegistry | None = None
    # Session API (RFC-001): tracks /sessions/{id} reservations. In-memory
    # only in V1; persistence is RFC-002.
    sessions: SessionRegistry | None = None
    # Per-host cookie registry. Cookies are auto-injected into
    # ``HubSessionStart`` whenever the host of the requested ``initial_url``
    # has a record. File-backed under ``{data_dir}/hosts/``.
    hosts: HostRegistry | None = None
    # Uploaded Chrome profile registry. Operators upload their local
    # Chrome User Data via ``paprika-client upload-profile``; jobs
    # opt in with ``options.use_profile = "<name>"``. File-backed
    # under ``{data_dir}/profiles/``.
    profiles: ProfileRegistry | None = None
    # Serialises concurrent uploads / deletes on the same profile
    # name so a re-upload mid-job can't race the atomic replace.
    profiles_lock: asyncio.Lock | None = None
    # Operator-managed Chrome extension registry. Distinct from
    # profiles because extensions are app-shaped (an ad blocker should
    # run on every lane) whereas profiles are operator-identity-shaped
    # (cookies / login state, opt-in per job). File-backed under
    # ``{data_dir}/extensions/``; workers prefetch on connect and
    # launch Chrome with ``--load-extension`` pointing at each cache.
    extensions: ExtensionRegistry | None = None
    extensions_lock: asyncio.Lock | None = None
    # Per-host visited-URL set (one big list per host). pap.walk
    # consults this at start to skip already-crawled pages across job
    # boundaries. File-backed under ``{data_dir}/hosts/visited/``.
    host_visited: HostVisitedRegistry | None = None
    # Skill registry: LLM-distilled reusable patterns. Codegen-loop
    # retrieves relevant skills before each job and distils new ones
    # after every SUCCESS. File-backed under ``{data_dir}/skills/``.
    skills: SkillRegistry | None = None
    # Convention registry: LLM-distilled atomic rules from
    # failure→success diffs. Curated conventions are always injected
    # into the codegen system prompt. File-backed under
    # ``{data_dir}/conventions/``.
    conventions: ConventionRegistry | None = None
    # Runtime-mutable hub settings (skill / convention auto-extract
    # toggles, retrieval top-K). Operator edits via /settings
    # from the Settings tab.
    settings: SettingsRegistry | None = None
    # Named snapshots of the Submit form (URL + mode + engine + macro
    # rows + options). Loaded via dropdown above Submit; fired off
    # without UI via POST /presets/{name}/run.
    presets: PresetRegistry | None = None
    # AI engine registry: pluggable LLM / VLM / VLA backends. Each
    # record maps a ``engine="<slug>"`` argument (passed to
    # page.agent / page.ask) to a concrete endpoint + protocol + API
    # key env var. The three seed entries (qwen, qwen-chat, cogagent)
    # keep the existing engine names working. File-backed under
    # ``{data_dir}/engines/``.
    engines: EngineRegistry | None = None
    # Per-engine daily usage counter + quota checker. Counts prompt /
    # completion tokens and request counts per engine slug per UTC
    # day; codegen LLM calls consult it before dispatching and
    # increment it after each successful response. Limits live on
    # the EngineRecord itself (daily_token_budget /
    # daily_request_budget); the registry just persists the counts.
    engine_usage: EngineUsageRegistry | None = None
    # WorkerJobLog batcher: buffers incoming log lines and flushes
    # to Redis in pipeline batches (50 lines or 100ms, whichever
    # comes first). Only active when Redis store is in use; None
    # when running with InMemoryJobStore. See _log_batcher.py.
    log_batcher: object | None = None  # LogBatcher | None (lazy import)
    # MariaDB connection pool (optional). Initialised lazily on first
    # migration endpoint call when MariaDB settings are configured.
    # None when MariaDB is not configured or the pool hasn't been
    # created yet. See server/hub/mariadb.py.
    mariadb_pool: object | None = None  # aiomysql.Pool | None (lazy)
    # Hub-presence registry: each hub heartbeats itself into Redis
    # (TTL 90 s) so multi-hub deploys can enumerate live peers from
    # the admin UI without explicit per-hub configuration. None when
    # Redis isn't wired (single-host in-memory deploys); list_all()
    # then returns a one-element synthetic local view. See _hubs.py.
    hubs: object | None = None  # HubRegistry | None (lazy)
    # Auth store: users + API keys, MariaDB-or-file backed with an
    # in-memory cache. Initialised in the lifespan AFTER the MariaDB pool
    # so it picks the durable shared backend in prod (file fallback in
    # dev / single-hub). None until then. See server/hub/auth.py.
    auth: object | None = None  # AuthStore | None


state = AppState()


def get_storage_dir() -> Path:
    """Return the effective directory for per-job artifacts.

    When the hub operator has configured ``storage_dir`` in Settings
    (a local cache-dir override -- the durable copy lives in the object
    store), that path is used.  Otherwise the default ``config.data_dir``
    is returned.

    Hub-internal metadata (skills, conventions, hosts, engines,
    settings.json, …) always lives in ``config.data_dir`` regardless
    of this setting.
    """
    if state.settings is not None:
        sd = state.settings.get("storage_dir", "")
        if sd:
            return Path(sd)
    return config.data_dir


__all__ = ["HubConfig", "AppState", "config", "state", "get_storage_dir"]
