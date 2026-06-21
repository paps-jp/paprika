"""Hub-wide runtime settings.

A small JSON file at ``{data_dir}/settings.json`` stores the handful
of knobs that benefit from being mutable at runtime via the admin UI
(skill / convention auto-extract toggles, skill retrieval top-K).

Things that aren't here:
  * LLM URLs / model names -- env-controlled, require deploy to swap.
  * Per-Submit-form defaults -- those live in the operator's
    browser localStorage (one operator = one preference set).
  * Per-host things -- HostRegistry has them.

The registry exposes a dict-like API with a typed schema and
sensible env-derived defaults so an unset key falls back to the
deploy-time configuration.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from server.hub._jsonstore import atomic_write_json

# Default schema. Keys map to (default, "type"). The type label is
# advisory -- it shapes how the UI renders the field.
_SCHEMA: dict[str, tuple[Any, str]] = {
    # Auto-extraction toggles -- whether codegen-loop SUCCESS triggers
    # the skill / convention distillation LLM calls.
    "skill_auto_extract_enabled": (True, "bool"),
    "convention_auto_extract_enabled": (True, "bool"),
    # Grooming reaper: when on, the hourly reaper auto-deletes auto-tier
    # duds/zombies (retire) / merges near-duplicates (dedup). Off by
    # default -> the reaper only dry-run-logs candidates for review.
    "auto_retire_enabled": (False, "bool"),
    "auto_dedup_enabled": (False, "bool"),
    # Auto-escalation: when a worker `fetch` job FAILS on a recoverable
    # barrier (video-download failure / auth / age gate), auto-spawn an AI
    # `codegen-loop` retry that drives past the barrier -- which also feeds
    # the distillers, so the #ai learning loop finally gets exercised by
    # the failures it should be learning from. OFF by default. Only fires
    # while the GPU is idle (see server/hub/_escalate.py). The categories
    # knob is a CSV subset of {video_dl, auth_gate}; empty = both.
    "auto_escalate_enabled": (False, "bool"),
    "auto_escalate_categories": ("video_dl,auth_gate", "str"),
    # When ON, the reasoning distiller translates a newly-learned, replayable
    # barrier strategy (kind=click/sequence) into a per-host fetch_recipe so
    # plain mode=fetch jobs replay it and get past the barrier (age gate /
    # consent / login click). Conservative: present + confidence>=0.7 +
    # scoped pattern + deduped + created_by="ai" (operator-visible/deletable).
    # NB: auto recipes are permanent + have no self-heal yet (no failure
    # feedback / retire), so the guards above matter. env kill-switch
    # PAPRIKA_AUTO_RECIPE_FROM_BARRIER. See server/hub/distiller_r1.py.
    "auto_recipe_from_barrier": (True, "bool"),
    # Comma-separated engine slugs the operator has manually STOPPED (停止中).
    # A stopped engine is skipped by every AI call (codegen / judge /
    # distiller / perception / page.agent) -- enforced in
    # codegen.check_engine_thermal + thermal.first_accepting, same as a
    # thermal throttle, so callers fail over to another engine of the kind.
    # Cross-hub + restart-safe via the settings table (no engines DB column).
    # Toggled by POST /engines/{slug}/stop|resume.
    "engines_disabled": ("", "str"),
    # POST /translate kill-switch. When False, the UI 翻訳 button gets 503
    # so it can fall back gracefully. Default ON. env override:
    # PAPRIKA_TRANSLATE_ENABLE (0/1).
    "translate_enabled": (True, "bool"),
    # 役割(Roles) パネル: 各 AI の仕事に「優先順のエンジン列」を割り当てる
    # (csv, 上から試し、過熱/停止なら次へ = thermal.first_accepting)。空なら
    # 従来の既定にフォールバック: チャット=promoted(kind=chat) / コード生成=
    # env(CODEGEN_LLM_URL) / page.agent=worker_agent_engine_slug / 判定=
    # reasoning_judge_engine / 蒸留=reasoning_distiller_engine。解決は
    # server/hub/_roles.py (vision は perception_llm が同じキーを読む)。
    "chat_engine_order": ("", "str"),
    "codegen_engine_order": ("", "str"),
    "page_agent_engine_order": ("", "str"),
    "vision_engine_order": ("", "str"),
    "judge_engine_order": ("", "str"),
    "distiller_engine_order": ("", "str"),
    # 翻訳役割 (#ai 作法モーダルの翻訳ボタン用)。空なら chat 役割の
    # Promoted へフォールバック。deepseek-r1 などのバイリンガル
    # モデルが目的言語に他言語を混入させるのを避けたい時に、ここで
    # 翻訳に強いエンジンを優先指定する。
    "translate_engine_order": ("", "str"),
    # 課題(review) auto-classification: when a `fetch` job COMPLETES but its
    # content was blocked by a full-screen login / age / consent / paywall
    # overlay (detected structurally by the worker's live-DOM occlusion probe),
    # re-bucket the job into the distinct "課題" terminal status instead of
    # letting it hide among the clean successes. The result is kept. ON by
    # default with a conservative threshold (see server/hub/_review.py). Env
    # kill-switch: PAPRIKA_REVIEW_DISABLE=1.
    "review_flag_enabled": (True, "bool"),
    # Which engine the worker's page.agent (vision-agent /act loop) uses as
    # its backend AI. Set by the Engines tab "page.agent でこの engine を使う"
    # checkbox. Empty = NO engine selected = page.agent is DISABLED. Shared
    # cross-hub via the settings table so every hub + worker resolves the
    # same backend; the worker resolves it via POST /engines/worker-agent/
    # resolve. agent-service-protocol engines are the realistic targets
    # (page.agent drives them via /act).
    "worker_agent_engine_slug": ("", "str"),
    # How many skills the retriever picks per new job before injection.
    "skill_retrieval_top_k": (3, "int"),
    # Minimum byte size for a captured asset. Anything smaller than
    # this is dropped on the floor instead of written to the gallery.
    # Set to 0 (default) to disable -- save everything. Useful values:
    #   1024  -- skip 1KB-and-under "decorative" icons / 1px trackers
    #   4096  -- skip small SVG icons + favicons
    #   10240 -- skip thumbnails too
    # Applied by both core.fetcher (Fetch mode) and the worker session
    # asset capture (Code / LLM modes via paprika-runner sessions).
    "min_asset_size_bytes": (0, "int"),
    # Asset URL blacklist (V). Newline-separated list of case-insensitive
    # substrings; any matching URL is dropped at the capture layer and
    # is also blocked from triggering yt-dlp (so HLS playlists served
    # from these CDNs don't spawn downloads either). Use for:
    #   - 広告 CDN (媒体無関係の素材)
    #   - 計測ピクセル / トラッカー
    #   - 動画プレーヤーの preview thumbnail などノイズアセット
    # Pulled into HubAssignJob.asset_url_blacklist at dispatch so a
    # Settings edit takes effect on the next job. Example values:
    #   media-hls.saawsedge.com
    #   /tracker.gif
    #   .cloudfront.net/ads/
    "asset_url_blacklist": ("", "str"),
    # Egress proxy pool. Newline / comma / whitespace separated list of
    # proxy URLs (full scheme), e.g.
    #   http://10.20.0.5:3128
    #   socks5://10.20.0.6:1080
    # Broadcast to every worker (HubProxyPoolSync); each worker random-picks
    # ONE entry for its target-site egress (browser + yt-dlp) so sites see
    # that proxy's IP instead of the fleet's. Empty = direct (default).
    # Per-worker env PAPRIKA_WORKER_PROXY* is the fallback before the hub
    # has pushed a pool. See core.fetcher._worker_egress_proxy.
    "proxy_pool": ("", "str"),
    # ---- Fetch defaults --------------------------------------------------
    # Mirrors of FetchOptions / JobOptions knobs. The hub overlays these
    # onto JobOptions on dispatch for any field the client didn't set
    # explicitly (Pydantic model_fields_set). Applies primarily to
    # Fetch mode -- Code / LLM modes don't go through core.fetcher.
    #
    # Pydantic JobOptions defaults are reproduced here so a fresh
    # SettingsRegistry matches existing behaviour byte-for-byte.
    "fetch_wait_seconds": (20, "int"),
    "fetch_settle_seconds": (0.0, "float"),
    "fetch_idle_seconds": (3.0, "float"),
    "fetch_max_wait_seconds": (60.0, "float"),
    "fetch_scroll": (False, "bool"),
    "fetch_scroll_step": (50, "int"),
    "fetch_scroll_max": (3000, "int"),
    "fetch_scroll_early_after": (5.0, "float"),
    "fetch_post_click_seconds": (5.0, "float"),
    # ---- Fleet capacity recommendation (GET /workers/capacity) ----------
    # Three knobs feeding the per-worker-health formula in
    # server/hub/routes/workers.py:_compute_capacity. Cross-hub via the
    # settings registry so the operator can tune live without a deploy
    # (consumers like .23 poll /workers/capacity for recommended_concurrency
    # and adjust their parallel POST /jobs rate).
    #
    # Each falls through to its env var equivalent if the settings value
    # is left at default 0 — see _env_default below for the mapping.
    #
    # fetch_load_factor: global headroom multiplier on healthy_lanes.
    #   0.7 = recommend 70% of healthy capacity (was a fixed 0.8 in the
    #   old single-input formula).
    # fetch_load_ref: load1 above which a worker contributes less than its
    #   full capacity. LXC host load1 propagates into the container per
    #   [[paprika-fleet-lxc-on-proxmox]]; ~24 is a typical 2-lane-on-busy-host
    #   threshold. health = clamp(1 - (load1 - REF)/REF, 0.3, 1.0).
    # fetch_mem_ref: mem_pct above which a worker contributes less. 75 keeps
    #   safe margin before Chrome/yt-dlp OOM territory (~85+).
    "fetch_load_factor": (0.7, "float"),
    "fetch_load_ref":    (24.0, "float"),
    "fetch_mem_ref":     (75.0, "float"),
    # ---- Codegen web_search tool (SearXNG-backed) ------------------------
    # When ``searxng_url`` is non-empty AND the Coder's engine has
    # supports_tools=True, the hub attaches a ``web_search`` OpenAI tool
    # to the request so the LLM can look up external facts (third-party
    # API shapes, unfamiliar site selectors). Empty URL -> feature off.
    # Both knobs fall back to SEARXNG_URL / SEARXNG_TIMEOUT_S env vars
    # via _env_default; see server/hub/web_search.py.
    "searxng_url": ("", "str"),
    "searxng_timeout_s": (15.0, "float"),
    # Per-attempt cap on how many web_search calls the LLM may make
    # inside one generate_script. The Coder usually needs 0-2; the cap
    # bounds token cost / latency when a confused model keeps re-
    # searching. 0 -> tool effectively off (no calls allowed) even when
    # SearXNG is reachable.
    "web_search_max_calls": (5, "int"),
    # ---- Storage: alternative data directory ----------------------------
    # When non-empty, job artifact directories ({job_id}/, assets/,
    # page.html, log.txt, …) are written to this path instead of the
    # default ``data_dir``.  Intended as a local cache-dir override (e.g.
    # a dedicated disk): the durable copy lives in the object store
    # (MinIO/S3) and reads fall back to it, so this dir is a bounded
    # write-through cache. Hub metadata (skills, conventions, hosts,
    # engines, settings.json) always stays in ``data_dir``.
    # Empty string (default) = use ``data_dir`` as before.
    "storage_dir": ("", "str"),
    # ---- Reasoning Judge ------------------------------------------------
    # A second, higher-quality LLM judge that runs alongside (shadow) or
    # instead of (primary) the default judge. Originally "R1 judge"
    # because DeepSeek-R1 was the first model used here, but the slot
    # accepts any engine -- Claude, GPT, Qwen-thinking, etc.
    #   off     -- never call the reasoning judge (default).
    #   shadow  -- call it, log both verdicts for comparison, keep using
    #              the default judge's verdict.
    #   primary -- use the reasoning judge's verdict; fall back to
    #              default when it's unreachable / unparseable.
    "reasoning_judge_mode": ("off", "str"),
    # Engine slug registered in the Engines tab.  When empty, falls back
    # to env PAPRIKA_R1_DISTILLER_ENGINE (legacy compat) → "deepseek-r1".
    "reasoning_judge_engine": ("", "str"),
    # Blind-judge: when ON (default), the codegen-loop judge prompt OMITS
    # the agent script, stdout tail, and stderr tail — it must rule on
    # asset counts + exit_code + screenshot + (for reasoning judge) the
    # perception facts alone. Goal: stop the judge being persuaded by
    # the maker's narrative. Aligns with the "evaluator-optimizer"
    # pattern and the 0xCodez 14-step "the gate must be objective —
    # never 'a second agent with an opinion'" principle.
    "judge_blind_mode": (True, "bool"),
    # Objective gates short-circuit: when ON (default), unambiguous
    # objective evidence settles the verdict BEFORE the LLM judge is
    # called. Currently implemented (server/hub/iterative_codegen.py
    # _objective_pregate):
    #   * video-intent goals + ≥1 video file in assets   → satisfied=True
    #   * video-intent goals + 0 video files in assets   → satisfied=False
    # Other intents fall through to judge. Saves an LLM round trip on
    # the easy cases and removes the judge as the FINAL gate on them
    # (it stays advisory for ambiguous cases).
    "judge_objective_gates_first": (True, "bool"),
    # Reasoning DISTILLER (deep HostKnowledge updates from job outcomes, incl.
    # 課題/blocked pages via the eye's perception). Abstracted from the
    # DeepSeek-specific "R1" name -- any reasoning engine. mode: off/on/new
    # (new = run on a new-barrier or failed job; recommended steady state).
    # engine: slug in the Engines tab; empty -> env -> "deepseek-r1".
    "reasoning_distiller_mode": ("off", "str"),
    "reasoning_distiller_engine": ("", "str"),
    # Per-host URL-template page-role gate: skip escalation when the URL is a
    # high-confidence listing / error / top page (nothing for codegen to
    # recover). See server/hub/_page_role.py. Default OFF so operators can opt
    # in after the role tables warm up. Detected-video fetches always bypass.
    "escalate_page_role_gate": (False, "bool"),
    # AI I/O log: per-LLM-call (purpose, engine, prompt, response, latency)
    # capture for observing the whole loop end-to-end. Persisted to MariaDB
    # ai_io_log + per-day JSONL + MinIO offload for long content. Default ON
    # so the operator can see the loop without flipping a switch first; flip
    # OFF if cost / privacy becomes a concern. See server/hub/_ai_io_log.py.
    "ai_io_log_enabled": (True, "bool"),
    # Success Audit: periodically sample completed video-download jobs and
    # ask a VisionAI whether the saved video is plausibly the page's main
    # content (not a preview / ad / mismatched). See _success_audit.py.
    # Default OFF -- flip ON when the operator wants the audit signal.
    "success_audit_enabled": (False, "bool"),
    "success_audit_sample_pct": (0.10, "float"),   # 10% of recent completed
    "success_audit_max_per_run": (12, "int"),       # cap audits per pass
    "success_audit_interval_min": (30, "int"),      # minutes between passes
    # Nightly review subagent: runs once per day at the configured UTC hour,
    # picks hosts with notable failure/review activity in the last 24h, and
    # writes a fresh per-host strategy digest into host_strategy via the
    # reasoning engine. Cross-hub safe (Redis lease — only ONE hub per day).
    # Read-only: never mutates skills / conventions / fetch_recipes /
    # HostKnowledge — only host_strategy gets updated. Operator-edited
    # digests (updated_by='operator') are preserved. See
    # server/hub/_nightly_review.py.
    "nightly_review_enabled": (False, "bool"),
    "nightly_review_hour_utc": (16, "int"),     # 16 UTC = 01 JST
    "nightly_review_max_hosts": (30, "int"),
    # Per-job token kill-switch (codegen-loop / rerun). Cumulative
    # prompt+completion tokens across ALL LLM calls inside one job
    # (codegen, judge, perception, reasoning judge). When the running
    # total crosses this, the orchestrator aborts with a clean failure
    # ("token budget exceeded: X / Y tokens") instead of letting the
    # iteration loop burn through max_attempts. 0 = unlimited (legacy).
    # See server/hub/codegen.py:check_job_token_budget (called at the
    # start of each codegen-loop attempt). 500_000 covers normal 3-
    # attempt runs with vision perception + reasoning judge comfortably.
    "job_max_tokens": (500_000, "int"),
    # Skill audit warn threshold: when an `auto`-tier skill or
    # convention crosses this use_count without being promoted
    # (= operator-reviewed), the admin UI surfaces a 監査要 warning
    # badge. Reflects the 0xCodez 14-step Tier 3 guidance that skills
    # are prompt-injection vectors and need operator review before
    # heavy reliance. 0 disables the warning.
    "skill_audit_warn_threshold": (10, "int"),
    # ---- Database: MariaDB -----------------------------------------------
    # External MariaDB / MySQL connection for persistent structured data.
    # When configured and reachable, the hub can migrate job state,
    # worker registry, and eventually file-backed registries into tables.
    # Empty host (default) = feature off, keep using Redis / file storage.
    "mariadb_host": ("", "str"),
    "mariadb_port": (3306, "int"),
    "mariadb_database": ("paprika", "str"),
    "mariadb_username": ("", "str"),
    "mariadb_password": ("", "str"),
    # ---- Object storage: S3 / MinIO -------------------------------------
    # S3-compatible object store (MinIO etc.) used as the durable mirror +
    # read source for job artifacts. When enabled, the gallery and every
    # /jobs/{id}/* read fall back to the bucket if the local/NAS copy is
    # gone, and completed jobs mirror their whole dir here. Disabled
    # (default) = local disk only. Each key falls back to the matching
    # PAPRIKA_S3_* env var via _env_default. The secret key is stored in
    # settings.json and redacted from GET /settings.
    "s3_enabled": (False, "bool"),
    "s3_endpoint": ("", "str"),            # e.g. http://10.10.50.16:9100
    "s3_bucket": ("paprika", "str"),
    "s3_prefix": ("jobs", "str"),
    "s3_access_key": ("", "str"),
    "s3_secret_key": ("", "str"),
    "s3_region": ("us-east-1", "str"),
    # ---- Windows portable: Chrome headless ------------------------------
    # When True, the bundled Chromium starts with ``--headless=new`` so
    # the operator's physical desktop isn't taken over by paprika's job
    # browser. Lane preview thumbnails (CDP screenshot) still work; the
    # live noVNC viewer doesn't render anything (= no physical pixels to
    # capture) and is hidden by the platform=windows label gate in
    # routes/novnc.py.
    #
    # Takes effect on the NEXT paprika.exe start. (Chrome is launched
    # once at boot; toggling this setting at runtime doesn't migrate
    # the running Chromium.) fleet 版の Linux worker は ``--headless``
    # を使わない設計 (Xvfb 仮想 display + lane VNC で見るので) なの
    # で、この knob は実質 Windows portable 専用。
    "worker_chrome_headless": (False, "bool"),
    # ---- Worker salvage: SSH fallback -----------------------------------
    # Per-hub auto-salvage (server/hub/_salvage.py) restarts a ghost worker
    # via its HTTP self-restart endpoint, falling back to SSH + `docker
    # restart`. These define the SSH login used for that fallback
    # (key_path is a path inside the hub container, e.g. /run/secrets/...).
    # Empty key_path = SSH fallback off (HTTP self-restart only). Each
    # falls back to the matching PAPRIKA_WORKER_SSH_* env var.
    "worker_ssh_user": ("root", "str"),
    "worker_ssh_port": (22, "int"),
    "worker_ssh_key_path": ("", "str"),
    # SSH private key PEM uploaded via the admin UI (secret; redacted from GET).
    # Stored in settings -> MariaDB write-through -> shared to every hub, so the
    # operator uploads ONCE and all hubs can SSH-salvage. Each hub materialises
    # it to a local 0600 file on use; worker_ssh_key_path (above) takes
    # precedence when set.
    "worker_ssh_key_pem": ("", "str"),
    # Master ON/OFF for the salvage loop, alongside the PAPRIKA_SALVAGE_ENABLE
    # env. Lets the operator arm/disarm from the Settings UI with no hub restart
    # (re-evaluated every pass, cross-hub via settings).
    "salvage_enabled": (False, "bool"),
    # ---- Storage capacity monitor (MinIO) -------------------------------
    # The background sampler in server/hub/_storage_metrics.py snapshots
    # MinIO's /minio/v2/metrics/cluster every `storage_sample_interval_s`
    # seconds and writes one row to storage_capacity_samples for the admin-
    # UI trend chart. `warn_percent` / `crit_percent` flip the admin
    # banner from blue→amber→red. `keep_days` bounds the table (a 5-min
    # sample for 60 days is ~17k rows = tiny).
    "storage_sample_interval_s": (300, "int"),
    "storage_sample_keep_days": (60, "int"),
    "storage_capacity_warn_percent": (85, "int"),
    "storage_capacity_crit_percent": (95, "int"),
}


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _env_default(key: str, fallback: Any) -> Any:
    """Resolve a default from env first, then fallback to the static
    schema. Lets a deploy-time env continue to seed initial values
    even after the settings file exists."""
    # Map a few key env names that pre-existed.
    env_map = {
        "skill_auto_extract_enabled": ("SKILL_AUTO_EXTRACT_ENABLED", "bool"),
        "convention_auto_extract_enabled": ("CONVENTION_AUTO_EXTRACT_ENABLED", "bool"),
        "skill_retrieval_top_k": ("SKILL_RETRIEVAL_TOP_K", "int"),
        # Storage: alternative data directory for job artifacts.
        "storage_dir": ("STORAGE_DIR", "str"),
        # Codegen web_search: settings.json -> env vars -> static default.
        "searxng_url": ("SEARXNG_URL", "str"),
        "searxng_timeout_s": ("SEARXNG_TIMEOUT_S", "float"),
        "web_search_max_calls": ("WEB_SEARCH_MAX_CALLS", "int"),
        # Capacity recommendation knobs: settings.json -> env vars -> default.
        "fetch_load_factor": ("PAPRIKA_FETCH_LOAD_FACTOR", "float"),
        "fetch_load_ref":    ("PAPRIKA_FETCH_LOAD_REF",    "float"),
        "fetch_mem_ref":     ("PAPRIKA_FETCH_MEM_REF",     "float"),
        # Reasoning judge: settings.json -> env vars -> static default.
        "reasoning_judge_mode": ("PAPRIKA_R1_JUDGE_MODE", "str"),
        "reasoning_judge_engine": ("PAPRIKA_R1_DISTILLER_ENGINE", "str"),
        "reasoning_distiller_mode": ("PAPRIKA_REASONING_DISTILLER_MODE", "str"),
        "reasoning_distiller_engine": ("PAPRIKA_REASONING_DISTILLER_ENGINE", "str"),
        "escalate_page_role_gate": ("PAPRIKA_ESCALATE_PAGE_ROLE_GATE", "bool"),
        "ai_io_log_enabled": ("PAPRIKA_AI_IO_LOG_ENABLED", "bool"),
        "success_audit_enabled": ("PAPRIKA_SUCCESS_AUDIT_ENABLED", "bool"),
        "success_audit_sample_pct": ("PAPRIKA_SUCCESS_AUDIT_SAMPLE_PCT", "float"),
        "success_audit_max_per_run": ("PAPRIKA_SUCCESS_AUDIT_MAX_PER_RUN", "int"),
        "success_audit_interval_min": ("PAPRIKA_SUCCESS_AUDIT_INTERVAL_MIN", "int"),
        # MariaDB: settings.json -> env vars -> static default.
        "mariadb_host": ("PAPRIKA_MARIADB_HOST", "str"),
        "mariadb_port": ("PAPRIKA_MARIADB_PORT", "int"),
        "mariadb_database": ("PAPRIKA_MARIADB_DATABASE", "str"),
        "mariadb_username": ("PAPRIKA_MARIADB_USERNAME", "str"),
        "mariadb_password": ("PAPRIKA_MARIADB_PASSWORD", "str"),
        # S3 / MinIO: settings.json -> env vars -> static default.
        "s3_enabled": ("PAPRIKA_S3_ENABLED", "bool"),
        "s3_endpoint": ("PAPRIKA_S3_ENDPOINT", "str"),
        "s3_bucket": ("PAPRIKA_S3_BUCKET", "str"),
        "s3_prefix": ("PAPRIKA_S3_PREFIX", "str"),
        "s3_access_key": ("PAPRIKA_S3_ACCESS_KEY", "str"),
        "s3_secret_key": ("PAPRIKA_S3_SECRET_KEY", "str"),
        "s3_region": ("PAPRIKA_S3_REGION", "str"),
        # Worker salvage SSH: settings.json -> env vars -> static default.
        "worker_ssh_user": ("PAPRIKA_WORKER_SSH_USER", "str"),
        "worker_ssh_port": ("PAPRIKA_WORKER_SSH_PORT", "int"),
        "worker_ssh_key_path": ("PAPRIKA_WORKER_SSH_KEY", "str"),
        "salvage_enabled": ("PAPRIKA_SALVAGE_ENABLE", "bool"),
    }
    info = env_map.get(key)
    if not info:
        return fallback
    env_name, kind = info
    raw = os.environ.get(env_name)
    if raw is None:
        return fallback
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            return fallback
    if kind == "float":
        try:
            return float(raw)
        except ValueError:
            return fallback
    if kind == "str":
        # No coercion needed -- env values are already strings. Strip
        # to drop trailing whitespace on .env edits.
        return raw.strip()
    return raw


class SettingsRegistry:
    """File-backed dict of hub-wide settings."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "settings.json"
        # Cached state. Lazily loaded.
        self._cache: dict | None = None
        # Guards the read-modify-write in update(). Without it two
        # concurrent updates (e.g. a PUT /settings on the event loop
        # racing another update() running in a worker thread via
        # asyncio.to_thread) both load the same base dict, both write,
        # and the second silently drops the first's change.
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        data: dict = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        self._cache = data
        return data

    def _write(self, data: dict) -> None:
        data["_updated_at"] = _utcnow_iso()
        # Atomic so a crash mid-save can't truncate settings.json (which
        # would wipe every operator-configured value on next start).
        atomic_write_json(self.path, data)
        self._cache = data

    def schema(self) -> dict:
        """The merged default-set: env > static fallback. Used by the
        UI to know what keys exist + their default values."""
        out = {}
        for k, (fb, kind) in _SCHEMA.items():
            out[k] = {
                "default": _env_default(k, fb),
                "type": kind,
            }
        return out

    def all(self) -> dict:
        """Return the full effective settings (file value or
        env-default-or-schema fallback per key)."""
        stored = self._load()
        out = {}
        for k, (fb, kind) in _SCHEMA.items():
            if k in stored:
                out[k] = stored[k]
            else:
                out[k] = _env_default(k, fb)
        # Include metadata fields too (e.g. _updated_at).
        for k, v in stored.items():
            if k.startswith("_"):
                out[k] = v
        return out

    def is_set(self, key: str) -> bool:
        """True iff ``key`` was explicitly written to settings.json
        (NOT counting the env-fallback default). Used by first-run
        dialogs to detect "operator hasn't been asked yet"."""
        return key in self._load()

    def get(self, key: str, default: Any = None) -> Any:
        """Single-key getter. Falls back to env / schema default
        when the key isn't in the persisted file."""
        if key.startswith("_"):
            return self._load().get(key, default)
        stored = self._load()
        if key in stored:
            return stored[key]
        if key in _SCHEMA:
            fb, _ = _SCHEMA[key]
            return _env_default(key, fb)
        return default

    def _coerce(self, kind: str, v: Any, fallback: Any) -> Any:
        """Best-effort coerce ``v`` to the schema's declared type."""
        if kind == "bool":
            return bool(v)
        if kind == "int":
            try:
                return int(v)
            except (TypeError, ValueError):
                return fallback
        if kind == "float":
            try:
                return float(v)
            except (TypeError, ValueError):
                return fallback
        if kind == "str":
            # Coerce + strip. None / missing -> empty string so the
            # admin UI sees a stable value (and the env-fallback path
            # gets a chance via _env_default when this is later read).
            if v is None:
                return ""
            return str(v).strip()
        return v

    def replace_all(self, new_values: dict) -> dict:
        """Replace the entire persisted settings dict (operator did a
        full save from the admin UI). Returns the effective view.
        Validates each key against the schema; unknown keys are
        dropped silently."""
        cleaned: dict = {}
        for k, (fb, kind) in _SCHEMA.items():
            if k not in new_values:
                continue
            cleaned[k] = self._coerce(kind, new_values[k], fb)
        self._write(cleaned)
        return self.all()

    def update(self, partial: dict) -> dict:
        """Partial update -- merge ``partial`` into the persisted
        dict, leaving other keys alone. The read-modify-write is held
        under a lock so concurrent updates don't clobber each other."""
        with self._lock:
            merged = dict(self._load())
            for k, (fb, kind) in _SCHEMA.items():
                if k not in partial:
                    continue
                merged[k] = self._coerce(kind, partial[k], fb)
            # Drop the metadata key before writing -- _write re-stamps it.
            merged.pop("_updated_at", None)
            self._write(merged)
        return self.all()
