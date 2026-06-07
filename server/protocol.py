"""Pydantic schemas shared between client API and the hub↔worker WebSocket
protocol (Phase 3)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, model_validator

# ----------------------------------------------------------------------------
# Job input / output
# ----------------------------------------------------------------------------


class JobOptions(BaseModel):
    """One-to-one mapping with core.fetcher.FetchOptions (minus log/path types).

    Path-shaped options (assets_dir, user_data_dir) are intentionally omitted —
    those are decided server-side (per-job working directory), not by the
    client.
    """

    wait_seconds: int = 20
    settle_seconds: float = 0.0
    idle_seconds: float = 3.0
    max_wait_seconds: float = 60.0
    scroll: bool = False
    scroll_step: int = 50
    scroll_max: int = 3000
    scroll_early_after: float = 5.0
    post_click_seconds: float = 5.0
    # Enable video-download logic. When True:
    #   * codegen-loop の system prompt に「download_video を使え」セクション
    #     を含める。AI 調査 (mode='codegen-loop') では UI 側で自動的に True が
    #     セットされる。
    #   * worker は session 開始時から iframe + ネスト iframe の network
    #     トレースを ON にして HLS/DASH の m3u8/mpd 由来の URL を漏らさず
    #     収集する。
    #   * page.download_video() が後付けで呼ばれた場合は、その時点で
    #     iframe トレースを ON にしてから yt-dlp 実行 (False のとき)。
    # False (default) では一切の動画 DL ロジックが休眠する。
    download_video: bool = False
    cookies_from: str | None = None
    referer: str | None = None
    headless: bool = False
    # Browser session reuse — exactly one of these may be specified.
    attach: str | None = Field(
        None,
        description="Attach to a running Chrome. Format: [HOST:]PORT.",
    )
    clone_chrome_profile: str | None = Field(
        None,
        description="Clone this Chrome profile name to a temp dir. "
        "LOCAL-ONLY: only meaningful when the hub runs on "
        "the same machine as the operator's Chrome. Use "
        "``use_profile`` for the worker-fleet equivalent.",
    )
    use_profile: str | None = Field(
        None,
        description="Name of a Chrome profile previously uploaded to "
        "the hub via ``POST /profiles/{name}`` (paprika-"
        "client CLI: ``upload-profile``). The worker "
        "fetches the tarball from the hub, extracts to a "
        "scratch dir, and launches Chrome with "
        "``--user-data-dir=<scratch>`` so the session "
        "starts with the operator's cookies / logins / "
        "localStorage already in place. Each job extracts "
        "its own copy, so multiple jobs can use the same "
        "uploaded profile concurrently without Chrome's "
        "single-instance lock fighting them. The scratch "
        "dir is removed when the job finishes. "
        "When this field is omitted, the hub checks the "
        "operator-set default profile (POST "
        "/profiles/{name}/default) and applies it if set; "
        "no default + no use_profile = lane's stock "
        "profile, same as before this feature existed.",
    )
    codegen_engine: str | None = Field(
        None,
        description="Slug of the engine (from /engines) to use for "
        "the codegen-loop's LLM calls: code generation, "
        "planner (goal decomposition), and judge (goal "
        "verification). The engine must be kind=chat or "
        "vision-chat AND protocol=openai. Omit to fall "
        "back to the env-var defaults (CODEGEN_LLM_URL "
        "+ CODEGEN_MODEL_NAME). For the vision judge to "
        "actually use the final screenshot, pick a "
        "vision-capable model; non-vision engines just "
        "treat the screenshot as a no-op (text-only "
        "verdict).",
    )
    # Whether to save captured assets to disk (server-managed directory).
    capture_assets: bool = True
    # Phase 4: attach to a previous job's browser lane (same Chrome,
    # same user-data-dir → cookies/session preserved). Hub looks up the
    # previous job's lane_idx and routes this job to the same lane.
    attach_to_job: str | None = Field(
        None,
        description="Previous job_id whose browser lane should be reused. "
        "Cookies/login state are preserved across jobs.",
    )
    # Mode = "fetch" -- single-shot HTML + assets capture (worker path)
    # Mode = "codegen-loop" -- LLM generates paprika-client script,
    # hub runs it in a sandboxed paprika-runner container, retries
    # on failure. (PR-14)
    # Mode = "rerun" -- skip the LLM entirely and run a known script
    # in the sandbox once. Sources: ``rerun_from`` (reference to an
    # existing job/attempt's script.py) or ``code`` (inline string).
    # The legacy "agent" mode (per-step LLM driving the worker loop)
    # was removed in PR-14a; goal now belongs to codegen-loop.
    # The "vision-agent" mode (CogAgent-driven pixel-space agent loop)
    # was removed in the v2 cleanup: replaced by codegen-loop + the
    # eye/brain split (Qwen-VL perception + R1 judge + plugin auto-
    # invocation). Recent traffic showed 0 vision-agent jobs, so the
    # mode was retired alongside the CogAgent service.
    mode: Literal["fetch", "codegen-loop", "rerun"] = "fetch"
    goal: str | None = Field(
        None,
        description="Natural-language task for codegen-loop. "
        "Required. Used by the codegen LLM as the script-"
        "generation prompt.",
    )
    max_codegen_attempts: int = Field(
        3,
        ge=1,
        le=10,
        description="How many times the hub retries the generate -> "
        "execute loop before giving up. Ignored in fetch mode.",
    )
    attempt_timeout_s: int = Field(
        180,
        ge=30,
        le=864000,  # cap at 10 days; long-running crawls need this
        description="Per-attempt sandbox execution timeout (seconds). "
        "Max 10 days (864000s). Bigger values let scripts that "
        "download large videos / do long crawls finish without "
        "SIGKILL.",
    )
    # rerun-mode source. Exactly one of these must be set when
    # mode='rerun'. ``rerun_from`` wins if both are given.
    rerun_from: str | None = Field(
        None,
        description="rerun mode: reference to an existing attempt's "
        "script. Formats accepted: '{job_id}' (final/winning "
        "script.py at the job root) or '{job_id}/attempts/N' "
        "(specific attempt). Hub reads the file from disk.",
    )
    code: str | None = Field(
        None,
        description="rerun mode: inline Python source to execute "
        "directly. Max 200KB. Useful for hand-edited "
        "variants of an LLM-generated script.",
        max_length=200_000,
    )
    min_asset_size_bytes: int = Field(
        0,
        ge=0,
        description="Drop any captured asset smaller than this many "
        "bytes. 0 = no filter. Applies to the passive "
        "CDP listener in both Fetch mode and session "
        "mode (Code / LLM via paprika-runner). The hub "
        "fills this in from Settings when the client "
        "leaves it at 0.",
    )
    # Fetch sub-mode (Phase 2a). 3-way knob inside Fetch UI:
    #   * "recipe"  (default) = if HostRegistry has a matching recipe
    #                           for this URL, the hub injects it via
    #                           fetch_recipe below and the worker runs
    #                           it right after navigation. This is the
    #                           Phase 1 behavior.
    #   * "normal"            = skip the recipe lookup even if one
    #                           matches. Used to verify "what does
    #                           plain Fetch do?" vs "what does the
    #                           recipe add?".
    # AI調査 (paid LLM) is NOT a fetch_strategy value -- it's a UI-only
    # shortcut that submits mode="codegen-loop" instead of mode="fetch".
    fetch_strategy: Literal["normal", "recipe"] = Field(
        default="recipe",
        description=(
            "Fetch sub-mode. 'recipe' = honour HostRegistry."
            "fetch_recipes (default); 'normal' = ignore them. "
            "AI調査 mode submits as mode='codegen-loop' instead."
        ),
    )
    # Hub-injected pre-baked per-host recipe (HostRecord.pick_recipe).
    # Operators do NOT set this directly via API; the hub looks up the
    # URL's host in HostRegistry and stamps the matching recipe here on
    # Fetch dispatch. Worker reads it and runs the action list right
    # after navigation (before scroll / asset capture). See
    # server/hub/hosts.py:HostRecipe.
    fetch_recipe: dict | None = Field(
        default=None,
        description=(
            "Hub-injected per-host fetch playbook (see HostRecord."
            "fetch_recipes). API callers should NOT set this; the "
            "hub stamps it from HostRegistry on dispatch. Shape: "
            '{"pattern": "...", "actions": [...], "description": "..."}.'
        ),
    )
    keep_session: bool = Field(
        False,
        description="Fetch mode only. When true, the browser and "
        "session are kept alive after the crawl finishes "
        "instead of being torn down. The job transitions "
        "to status=completed (the fetch crawl IS done) "
        "but JobInfo.session_id keeps resolving so the "
        "operator can interact via noVNC. New assets "
        "captured during interaction (e.g. videos played "
        "manually) can be flushed to the job's assets "
        "directory + the page's links re-extracted via "
        "POST /jobs/{id}/refresh. End the session "
        "explicitly via DELETE /sessions/{sid} when done.",
    )
    # ---- Auto-escalation lineage (failed fetch → AI codegen-loop) -------
    # Set by the hub's escalation hook (server/hub/_escalate.py), NOT by
    # API callers. When a worker ``fetch`` job FAILS on a recoverable
    # barrier (video-download failure / auth / age gate), the hub may
    # auto-spawn a ``codegen-loop`` retry that the AI agent drives. These
    # two fields record the lineage so the admin UI can link the pair and
    # the hook never escalates the same job twice:
    #   * escalated_from -- on the NEW codegen-loop job: the failed fetch
    #                       job_id that triggered it.
    #   * escalated_to   -- on the ORIGINAL fetch job: the codegen-loop
    #                       job_id it was escalated into (also the dedup
    #                       "already handled" marker).
    escalated_from: str | None = Field(
        None,
        description="Hub-set. On an auto-escalated codegen-loop job: the "
        "failed fetch job_id that triggered the escalation.",
    )
    escalated_to: str | None = Field(
        None,
        description="Hub-set. On a failed fetch job: the codegen-loop "
        "job_id it was auto-escalated into (dedup marker).",
    )

    @model_validator(mode="after")
    def _force_capture_assets_when_download_video(self) -> "JobOptions":
        """download_video=True で capture_assets=False の組合せは
        意味を成さない: core/fetcher の Auto yt-dlp / 通常アセット
        保存ループ / iframe deep-trace すべてが ``if assets_dir is
        not None`` でガードされており、capture_assets=False の状態で
        worker 側に渡すと「3 秒で何もせず完了」という無音失敗になる
        (job 6e851e7985d5)。

        UI 側 (admin.js syncFetchDlGuard) でも同じ矛盾を防いでいるが、
        API 直叩き / preset 復元 / CLI ルート 経由のリクエストにも
        効かせるため、ここで強制 True に矯正する (= 設定値を
        validation 段で書き換える)。
        """
        if self.download_video and not self.capture_assets:
            self.capture_assets = True
        return self


class JobRequest(BaseModel):
    url: str
    options: JobOptions = Field(default_factory=JobOptions)


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class AssetInfo(BaseModel):
    name: str
    size: int
    mime: str | None = None
    url: str | None = Field(
        None,
        description=(
            "Asset's original source URL on the captured page. Kept as "
            "``url`` (not ``source_url``) for backward compatibility with "
            "existing client integrations; ``/jobs/{id}/assets.json`` uses "
            "the more explicit name."
        ),
    )
    page_url: str | None = Field(
        None,
        description=(
            "URL of the page that initiated this resource request "
            "(CDP's Network.RequestWillBeSent.documentURL). Lets a "
            "caller answer 'which page did this image come from'. "
            "Same value the gallery / assets.json endpoints expose."
        ),
    )
    href: str = Field(..., description="Public path to fetch this asset from the API")


class YtdlpResult(BaseModel):
    url: str
    label: str
    referer: str | None = None
    ok: bool
    message: str


class JobProgress(BaseModel):
    """Lightweight, frequently-updated counters."""

    phase: str | None = None
    assets_saved: int = 0
    assets_failed: int = 0
    last_log: str | None = None


class JobInfo(BaseModel):
    """Server-side state of a job. Returned by GET /jobs/{id}."""

    job_id: str
    status: JobStatus
    url: str
    options: JobOptions
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    progress: JobProgress = Field(default_factory=JobProgress)
    # Phase 3: which worker is executing this job + how to watch its browser
    worker_id: str | None = None
    novnc_url: str | None = Field(
        None,
        description="Direct URL to the noVNC viewer for this job's Chrome "
        "(includes autoconnect/scale params).",
    )
    # Phase 4: lane pool index (which dedicated browser this job used).
    # Subsequent jobs can attach to this lane via JobOptions.attach_to_job.
    lane_idx: int | None = None
    # Hub-allocated session that the job runs against. For fetch jobs
    # this is the read-only inspection session the worker registers
    # for the duration of the run (so the admin UI can save cookies /
    # screenshot / inspect outline mid-fetch). For codegen-loop /
    # rerun, the runner can spin up several sessions over its lifetime
    # -- this field tracks the most recently opened one; full history
    # is available via GET /jobs/{id}/sessions. None for jobs that
    # never reserved a session (failed dispatch, legacy local-fallback).
    session_id: str | None = Field(
        None,
        description="Hub-allocated session that the job runs against. "
        "Empty until dispatch creates one. Survives job "
        "completion as a historical reference (the session "
        "itself may already be closed).",
    )
    # Phase 2 (tenancy): owner that submitted this job. "default" = the shared
    # tenant (pre-tenancy rows + anything created while auth is off/optional).
    # Used to scope GET /jobs* to the caller under enforce; admins see all.
    owner_id: str = Field(
        "default",
        description="Owner (tenant) that submitted the job; 'default' = shared.",
    )


class JobResult(BaseModel):
    """Full result returned by GET /jobs/{id}/result once status is completed."""

    job_id: str
    status: JobStatus
    html_href: str | None = None
    log_href: str | None = None
    assets: list[AssetInfo] = Field(default_factory=list)
    assets_failed: int = 0
    video_detection: dict[str, Any] = Field(default_factory=dict)
    video_urls_seen: list[str] = Field(default_factory=list)
    iframe_srcs: list[str] = Field(default_factory=list)
    ytdlp_results: list[YtdlpResult] = Field(default_factory=list)
    # Canonicalised URLs the agent visited during this job (only populated
    # for jobs that ran in agent mode, i.e. JobOptions.goal was set).
    # In arrival order. Powers the visited=true marker in the page outline and lets
    # callers reconstruct the agent's navigation path.
    visited_urls: list[str] = Field(default_factory=list)
    error: str | None = None


# ----------------------------------------------------------------------------
# WebSocket event envelope (used by /jobs/{id}/events and future hub<->worker)
# ----------------------------------------------------------------------------


class Event(BaseModel):
    """Single event over the client-facing WS /jobs/{id}/events stream."""

    type: str
    job_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------------------------
# Hub ↔ Worker WebSocket protocol (Phase 3)
#
# Each direction has a discriminated union of message types so both ends can
# parse safely. Every message has a `type` literal that selects the model.
# ----------------------------------------------------------------------------


class WorkerCapabilities(BaseModel):
    """What a worker tells the hub about itself on register."""

    max_concurrent: int = 1
    labels: dict[str, str] = Field(default_factory=dict)
    chrome_attach_host: str | None = None
    chrome_attach_port: int | None = None
    chrome_version: str | None = None
    has_yt_dlp: bool = False
    version: str | None = None
    novnc_url: str | None = Field(
        None,
        description="URL of the noVNC viewer for this worker's Chrome. "
        "Stored on JobInfo so clients can watch the job live.",
    )
    lane_novnc_urls: list[str] = Field(
        default_factory=list,
        description="Per-lane noVNC URLs, indexed by lane_idx. Empty for "
        "workers that don't use a lane pool. The admin UI uses "
        "this so the live-screenshot tiles can link directly "
        "to the matching VNC viewer.",
    )
    supports_preview_push: bool = Field(
        False,
        description="Worker runs the self-capture preview loop: it captures "
        "watched lanes on a timer and PUSHES WorkerPreviewFrame to "
        "the hub (cached in Redis), instead of the hub pulling a live "
        "screenshot per admin poll. The hub only sends "
        "HubPreviewSubscribe to workers that advertise this; older "
        "workers keep the legacy pull path. Set by new worker builds.",
    )


# --- Worker → Hub ----------------------------------------------------------


class WorkerRegister(BaseModel):
    type: Literal["register"] = "register"
    worker_id: str
    capabilities: WorkerCapabilities
    secret: str | None = None


class ProfileCacheEntry(BaseModel):
    """One entry in WorkerHeartbeat.profiles_cached -- the worker's
    advertised view of "which operator-uploaded Chrome profiles do I
    have prefetched right now".

    Used by the admin UI Workers tab to surface "this worker has the
    'mydefault' profile ready" without an extra round trip. The
    etag matches HubProfileSync.etag the worker last saw + accepted,
    so a stale entry (operator re-uploaded since prefetch finished)
    is visible to the operator at a glance.
    """

    name: str
    etag: str
    size_bytes: int = 0


class WorkerHeartbeat(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    in_flight: int = 0
    capacity: int = 1
    profiles_cached: list[ProfileCacheEntry] = Field(
        default_factory=list,
        description="Names + etags of operator-uploaded Chrome "
        "profiles this worker currently has prefetched. "
        "Empty list means nothing cached (= every job "
        "using a profile will pay the on-demand download "
        "cost). Hub aggregates these into the /workers "
        "and /profiles responses so the admin UI can show "
        "which workers are ready for which profile.",
    )
    # Host/CT resource snapshot taken at heartbeat time. All optional
    # (default 0.0) so an older worker that doesn't send them still
    # deserialises -- the admin UI just shows "—" for that row.
    # cpu_pct + load1 reflect the LXC HOST (Proxmox node), since the CT
    # shares its kernel and /proc/stat. mem_pct + disk_* reflect the CT
    # itself (cgroup-limited mem + overlayfs root). The split is what an
    # operator needs to distinguish "this CT is full" from "the underlying
    # node is overloaded across all its CTs".
    cpu_pct: float = 0.0
    mem_pct: float = 0.0
    disk_pct: float = 0.0
    disk_free_gb: float = 0.0
    load1: float = 0.0


class WorkerJobAccepted(BaseModel):
    type: Literal["job_accepted"] = "job_accepted"
    job_id: str
    novnc_url: str | None = Field(
        None,
        description="Per-job noVNC URL when the worker has allocated a "
        "dedicated browser lane for this job (lane-pool mode).",
    )
    lane_idx: int | None = Field(
        None,
        description="Which lane index this job was assigned to. Stored on "
        "JobInfo so future jobs can attach via attach_to_job.",
    )


# Sentinel prefix for EPHEMERAL per-download progress markers that ride
# the WorkerJobLog channel.  The worker emits "JOB_PROGRESS_MARKER + json"
# lines for live yt-dlp/ffmpeg download progress; the hub recognises the
# prefix and BROADCASTS them to /events viewers WITHOUT persisting them to
# the job log (otherwise per-second progress would flood log.txt).  The
# admin Live panel intercepts these lines and renders per-download
# progress bars instead of appending them as text.  See:
#   * server/worker/agent.py  _ytdlp_log          (emit)
#   * server/hub/routes/workers.py  WorkerJobLog   (ephemeral branch)
#   * server/hub/static/admin.js  ljpAppendLine    (render)
# Distinct from WorkerJobProgress below, which carries COARSE job-level
# phase / asset counts and IS persisted.
JOB_PROGRESS_MARKER = "[[paprika:progress]] "

# Sentinel prefix for EPHEMERAL network-capture deltas that ride the same
# WorkerJobLog channel (identical mechanism to JOB_PROGRESS_MARKER). The
# fetch engine emits "NET_CAPTURE_MARKER + json" with the batch of newly-
# captured network URLs once per poll cycle; the hub BROADCASTS them to
# /events viewers WITHOUT persisting (per-poll deltas would flood the log),
# and the admin Live panel intercepts them to populate the Network tab in
# real time -- replacing the page.network() pull that 504s on streaming
# pages. Everything streams over the ONE /events pipe, demuxed by prefix.
#   * core/fetcher.py  _fetch_url_capture_poller   (emit; literal must match)
#   * server/hub/routes/workers.py  WorkerJobLog    (ephemeral branch)
#   * server/hub/static/admin.js  ljpAppendLine     (render)
NET_CAPTURE_MARKER = "[[paprika:netcap]] "

# Sentinel prefixes for EPHEMERAL "a thing landed, refresh it" signals that
# ride the same WorkerJobLog channel. Like the markers above, the hub
# broadcasts them to /events viewers WITHOUT persisting; the admin Live panel
# treats them as event-driven refresh triggers (and drops the periodic poll
# timers), so assets/links update on change instead of on a 2.5s clock.
#   * ASSET_CAPTURE_MARKER -> a new asset was uploaded -> refresh the gallery
#   * LINKS_CAPTURE_MARKER -> page links were captured  -> refresh the links tab
# Emitted by server/worker/agent.py (_upload_asset / _upload_one_session_asset
# / the links_snapshot POST); handled in server/hub/routes/workers.py
# (ephemeral branch) + server/hub/static/admin.js (ljpAppendLine).
ASSET_CAPTURE_MARKER = "[[paprika:asset]] "
LINKS_CAPTURE_MARKER = "[[paprika:links]] "


class WorkerJobProgress(BaseModel):
    type: Literal["progress"] = "progress"
    job_id: str
    phase: str | None = None
    assets_saved: int = 0
    assets_failed: int = 0


class WorkerJobLog(BaseModel):
    type: Literal["log"] = "log"
    job_id: str
    line: str


class WorkerJobComplete(BaseModel):
    type: Literal["complete"] = "complete"
    job_id: str
    result: JobResult


class WorkerJobFailed(BaseModel):
    type: Literal["failed"] = "failed"
    job_id: str
    error: str


class WorkerScreenshotReply(BaseModel):
    """Reply to a HubScreenshotRequest. The hub correlates by req_id."""

    type: Literal["screenshot_reply"] = "screenshot_reply"
    req_id: str
    lane_idx: int
    # JPEG bytes, base64-encoded. Empty when error is set.
    jpeg_b64: str = ""
    # If set, the worker failed to capture the lane (e.g. ffmpeg crashed,
    # lane index out of range). hub turns this into a 5xx for the admin UI.
    error: str | None = None


class WorkerPreviewFrame(BaseModel):
    """Worker-PUSHED lane preview frame (push-based previews).

    A worker advertising ``supports_preview_push`` captures each watched
    lane on its own ~10s timer (see HubPreviewSubscribe) and sends this
    unsolicited. The hub writes it to a Redis frame cache
    (``preview:frame:{worker_id}:{lane_idx}``) that ANY hub serves to the
    admin #screens grid -- decoupling capture rate from admin poll rate so
    a full-grid poll never triggers a live cross-hub screenshot storm.
    Unlike WorkerScreenshotReply there is no req_id: it's not a reply."""

    type: Literal["preview_frame"] = "preview_frame"
    lane_idx: int
    # JPEG bytes, base64-encoded.
    jpeg_b64: str = ""
    # Actual encoded width in px (after downscale); admin may show it.
    width: int = 0
    # Worker capture wall-clock (epoch secs) so the admin can show frame age
    # / "N s ago" and tell a static screen apart from a stalled worker.
    ts: float = 0.0


# --- Session protocol (RFC-001 §7) ----------------------------------------
#
# Session = a long-lived reservation of a Lane that the client drives
# action-by-action over HTTP. The hub talks to the worker via the same
# WS as for jobs; messages are just additive.


class WorkerSessionStartAck(BaseModel):
    """Reply to HubSessionStart. ``error`` set when the lane could not
    be acquired or the initial navigation failed."""

    type: Literal["session_start_ack"] = "session_start_ack"
    session_id: str
    lane_idx: int | None = None
    novnc_url: str | None = None
    error: str | None = None


class WorkerSessionActionResult(BaseModel):
    """Reply to HubSessionAction. The hub correlates by ``request_id``."""

    type: Literal["session_action_result"] = "session_action_result"
    session_id: str
    request_id: str
    status: str = Field(
        ...,
        description="Short outcome -- 'OK', 'NO_MATCH', or 'ERR: ...'. "
        "Matches the strings produced by browser_ops.*.",
    )
    elapsed_ms: int = 0
    # Action-specific payload: outline string for ``outline``, JPEG/PNG
    # base64 for ``screenshot``, dict for ``state``, None for actions
    # that don't return data.
    result: Any | None = None


class WorkerSessionEndAck(BaseModel):
    type: Literal["session_end_ack"] = "session_end_ack"
    session_id: str
    error: str | None = None


class WorkerSessionAgentResult(BaseModel):
    """Reply to a HubSessionAgent (page.agent() in the SDK).

    The worker ran a localised agent loop (observe -> /act -> execute)
    against the session's tab for up to ``max_steps`` iterations, and
    returns the outcome here.
    """

    type: Literal["session_agent_result"] = "session_agent_result"
    session_id: str
    request_id: str
    completed: bool = False
    steps_taken: int = 0
    summary: str | None = None
    last_action: dict[str, Any] | None = None
    error: str | None = None
    # Per-step trace of what the agent actually did inside the
    # observe/act/execute loop. Each entry is a flat dict like:
    #   {"n": 1, "engine": "cogagent", "kind": "click",
    #    "outcome": "clicked element at (150, 80)",
    #    "summary": null}
    # The SDK prints these continuation lines after the high-level
    # ``[paprika] page.agent(...) -> OK`` so the job log shows what
    # the agent actually did (previously these lines were only
    # written to worker stderr via _slog and never reached the hub).
    # Empty list when the agent took no steps (= early error).
    steps: list[dict[str, Any]] = Field(default_factory=list)


class SessionStateSnapshot(BaseModel):
    """One session as the worker currently sees it -- enough info for
    the hub to reconstruct (or confirm) its SessionInfo on a WS
    reconnect. Used inside ``WorkerSessionAnnounce``.
    """

    session_id: str
    lane_idx: int
    novnc_url: str | None = None
    initial_url: str | None = None
    # parent_job_id, when applicable (Fetch keep_session, codegen-loop
    # rerun, etc.). Lets the hub look up JobInfo on the disk store
    # to rebuild richer SessionInfo state (idle_ttl_s, detached flag).
    job_id: str | None = None
    # ``True`` once the worker has flipped is_fetch_owned=False on a
    # keep_session Fetch (= operator-managed). Hub mirrors this onto
    # SessionInfo.detached so the reaper / cascade do the right thing.
    detached: bool = False
    # ``True`` while a Fetch crawl is actively running on the lane
    # (= worker's SessionState.is_fetch_owned). Hub maps this to
    # SessionInfo.state="fetch_running" so the reaper skips it.
    is_fetch_owned: bool = False


class WorkerDraining(BaseModel):
    """Worker signals that it has detected a version mismatch and is
    entering drain mode prior to a self-update + exit(42).

    The hub records this in registry as ``draining=True`` so the
    scheduler stops handing it new jobs / sessions. The worker then
    waits for in-flight work to complete (up to ``DRAIN_DEADLINE_S``)
    and, gated by the hub's response (HubUpdateGate), pulls the source
    tarball and exits to let docker restart pick up the new code.

    Replaces the previous "every worker self-updates as soon as it
    sees the new expected version" thundering-herd pattern -- the hub
    now controls update concurrency so the fleet rolls forward instead
    of going dark simultaneously.
    """

    type: Literal["draining"] = "draining"
    to_version: str = Field(
        description="The hub-advertised version this worker is moving to."
    )
    reason: str = "version_mismatch"


class WorkerSessionAnnounce(BaseModel):
    """Sent by the worker right after the WS handshake. Lists every
    session the worker is currently holding so the hub can:

      * confirm matching SessionInfo entries
      * rebuild missing ones (= hub was restarted, JobInfo persisted
        but SessionInfo is in-memory only)
      * drop stale registry entries for this worker that the worker
        doesn't actually have (= worker restarted, sessions are gone)
      * tell the worker to end true orphans (= worker has session X
        but no JobInfo references it -> can't be re-claimed)

    Reconciliation runs on every WS connect, so a worker restart or
    a hub restart naturally re-syncs the SessionRegistry without
    needing a separate persistence layer.
    """

    type: Literal["session_announce"] = "session_announce"
    sessions: list[SessionStateSnapshot] = Field(default_factory=list)


WorkerToHubMsg = Annotated[
    Union[
        WorkerRegister,
        WorkerHeartbeat,
        WorkerJobAccepted,
        WorkerJobProgress,
        WorkerJobLog,
        WorkerJobComplete,
        WorkerJobFailed,
        WorkerScreenshotReply,
        WorkerPreviewFrame,
        WorkerSessionStartAck,
        WorkerSessionActionResult,
        WorkerSessionEndAck,
        WorkerSessionAgentResult,
        WorkerSessionAnnounce,
        WorkerDraining,
    ],
    Field(discriminator="type"),
]
worker_to_hub_adapter: TypeAdapter[WorkerToHubMsg] = TypeAdapter(WorkerToHubMsg)


# --- Hub → Worker ----------------------------------------------------------


class HubAssignJob(BaseModel):
    type: Literal["assign_job"] = "assign_job"
    job_id: str
    url: str
    options: JobOptions
    asset_upload_base: str = Field(
        ...,
        description="Base URL the worker must POST captured assets to "
        "(e.g. http://hub:8000/jobs/{id}/assets). The worker "
        "appends the filename when uploading.",
    )
    lane_hint: int | None = Field(
        None,
        description="When set, worker must use this specific lane index "
        "(waits if currently busy). Used by attach_to_job.",
    )
    cookies: list[dict[str, Any]] | None = Field(
        None,
        description="Optional CDP CookieParam-shaped dicts to install "
        "via Network.setCookies BEFORE navigating. Provided "
        "by the hub when the host of ``url`` has a record "
        "in the host registry. Mirrors the session protocol.",
    )
    save_cookies_host: str | None = Field(
        None,
        description="When set, the worker dumps the browser's cookie jar "
        "right before fetch returns and PUTs it to "
        "/hosts/{save_cookies_host} so the registry "
        "captures any session cookies the page set. Always "
        "the normalised host of ``url`` (set by the hub).",
    )
    session_id: str | None = Field(
        None,
        description="Hub-allocated session_id that this job should "
        "register itself against on the worker (so the "
        "admin UI can inspect via /sessions/{session_id}/* "
        "while the fetch is running). The session is "
        "read-only -- write actions like click/fill are "
        "rejected to avoid racing the fetch loop.",
    )
    popup_policy: str = Field(
        "kill",
        description="Per-host popup containment policy, looked up by "
        "the hub from the HostRegistry for the host of "
        "``url``. 'kill' (default) closes popups + only "
        "redirects the main tab when the popup is same-"
        "netloc; 'follow' redirects across netlocs too. "
        "Used by vision-agent jobs so the tab-killer "
        "follows the host's configured behaviour instead "
        "of always killing.",
    )
    profile_url: str | None = Field(
        None,
        description="HTTP URL the worker GETs to fetch the profile "
        "tarball when ``options.use_profile`` is set. "
        "Hub fills this in from its own base URL + the "
        "profile name. Kept as a URL (not just a name) so "
        "the worker doesn't need to know hub-side path "
        "conventions. Tarball is gzipped, contains a "
        "single 'User Data' subtree.",
    )
    profile_name: str | None = Field(
        None,
        description="Plain profile name (mirrors options.use_profile). "
        "Combined with profile_etag lets the worker hit "
        "its prefetched cache instead of refetching the "
        "tarball on every job.",
    )
    profile_etag: str | None = Field(
        None,
        description="Cache key for the profile_url tarball. Equal to "
        "the HubProfileSync.etag value last broadcast for "
        "this name. Worker uses (name, etag) as the cache "
        "lookup key; matching cached extraction is reused, "
        "stale -> refetch.",
    )
    asset_url_blacklist: list[str] = Field(
        default_factory=list,
        description="Operator-managed deny list of substring patterns. "
        "Any asset URL containing one of these substrings is "
        "NOT saved to assets/ and is NOT passed to yt-dlp "
        "(even when an .m3u8/.mpd is observed). Pulled from "
        "Settings.asset_url_blacklist at dispatch time and "
        "stamped onto the job so a Settings edit mid-fleet "
        "takes effect on the next job. Match is plain "
        "case-insensitive substring (e.g. "
        "'media-hls.saawsedge.com').",
    )


class HubCancelJob(BaseModel):
    type: Literal["cancel_job"] = "cancel_job"
    job_id: str


class HubPing(BaseModel):
    type: Literal["ping"] = "ping"


class HubRegistered(BaseModel):
    """Hub's ack of a WorkerRegister, with negotiated values.

    ``assigned_worker_id`` is set when the hub detected that the
    requested ``worker_id`` is already held by an active connection
    from a DIFFERENT client IP -- typically because the worker host
    was cloned (LXC / Proxmox / VMware / plain dd-copy) and the
    persisted ``/root/.paprika/worker_id`` came along for the ride.
    Hub mints a fresh unique ID and sends it back here; the worker
    is expected to:

      1. persist the new ID over its old one,
      2. drop the current WS,
      3. reconnect using the new ID in the URL.

    No assigned_worker_id == registration accepted as-is.

    ``expected_worker_version`` is the hub's own VERSION string -- the
    version the hub thinks the fleet should be running. The worker
    compares this against its local VERSION on every successful
    registration and emits a prominent warning on mismatch. When the
    operator opts in via ``PAPRIKA_WORKER_AUTO_EXIT_ON_VERSION_MISMATCH``
    (default on) the worker process exits with code 42 so Docker's
    restart policy can pick up the new image. ``None`` from an older
    hub means "no expectation"; the worker silently skips the check.
    """

    type: Literal["registered"] = "registered"
    worker_id: str
    server_time: datetime = Field(default_factory=datetime.utcnow)
    assigned_worker_id: str | None = None
    expected_worker_version: str | None = None


class HubScreenshotRequest(BaseModel):
    """Ask the worker to capture the Xvfb display of a lane and reply with
    a JPEG. The worker MUST echo `req_id` back so the hub can match the
    pending HTTP request that started this RPC."""

    type: Literal["screenshot_request"] = "screenshot_request"
    req_id: str
    lane_idx: int
    # Max width in pixels; the worker scales the Xvfb screen down to fit.
    # Height is computed to preserve the aspect ratio. None = native size.
    max_width: int | None = 480
    # JPEG quality 2..31 (lower is better; ffmpeg's -q:v scale).
    quality: int = 5


class HubPreviewSubscribe(BaseModel):
    """Tell a worker which lanes are CURRENTLY being watched in the admin
    #screens grid, so it self-captures + pushes WorkerPreviewFrame for them.

    Interest-gated + self-expiring: the hub (re)sends this while at least one
    admin is watching the worker (a ``preview:watch:{worker_id}`` key is live
    in Redis). The worker keeps capturing for ``ttl_s`` after the last one it
    received, then STOPS -- so if every admin closes #screens (or the hub
    dies), capture quiesces on its own and idle workers pay nothing. Only sent
    to workers that advertised ``supports_preview_push`` (older workers can't
    parse this type)."""

    type: Literal["preview_subscribe"] = "preview_subscribe"
    # Lane indices to capture; None = all of the worker's active lanes.
    lanes: list[int] | None = None
    # Seconds between captures of each watched lane.
    interval_s: float = 10.0
    # Keep capturing for this long after THIS message; the hub refreshes it
    # well within the window while a viewer remains.
    ttl_s: float = 30.0
    max_width: int = 320
    # ffmpeg mjpeg q (2..31, lower=better).
    quality: int = 5


class HubSessionStart(BaseModel):
    """Reserve a Lane on the worker for the named session.

    The worker acquires a lane, attaches nodriver to its Chrome,
    navigates to ``initial_url`` (if set), installs the tab-killer, and
    replies with WorkerSessionStartAck.
    """

    type: Literal["session_start"] = "session_start"
    session_id: str
    lane_hint: int | None = None
    initial_url: str | None = None
    asset_upload_base: str | None = Field(
        None,
        description="Reserved for future per-action asset upload. "
        "V1 sessions keep assets on the worker until end.",
    )
    cookies: list[dict[str, Any]] | None = Field(
        None,
        description="Optional CDP CookieParam-shaped dicts to install "
        "via Network.setCookies BEFORE navigating to "
        "``initial_url``. Provided by the hub when the host "
        "of ``initial_url`` has a record in the host "
        "registry. The worker silently skips this step if "
        "the list is empty or None.",
    )
    min_asset_size_bytes: int = Field(
        0,
        ge=0,
        description="Drop captured assets smaller than this many "
        "bytes. 0 = no filter. Filled in by the hub from "
        "Settings; the worker plumbs it into the session "
        "asset capture handler.",
    )
    asset_url_blacklist: list[str] = Field(
        default_factory=list,
        description="Substring deny list. Any asset URL containing one "
        "of these is dropped at capture time and excluded "
        "from yt-dlp triggers. Mirrors HubAssignJob field "
        "of the same name; hub fills both from "
        "Settings.asset_url_blacklist.",
    )
    popup_policy: str = Field(
        "kill",
        description="How the worker's tab-killer treats new tabs "
        "opened by this session's pages. "
        "'kill' = close popup, redirect main tab only "
        "on same netloc. "
        "'follow' = close popup, redirect main tab to "
        "popup URL regardless of netloc (use for sites "
        "that fan video pages out across subdomains).",
    )
    profile_url: str | None = Field(
        None,
        description="HTTP URL the worker GETs to fetch a Chrome "
        "profile tarball before launching Chrome for "
        "this session. Mirrors HubAssignJob.profile_url. "
        "Hub sets this when /sessions was opened with "
        "``use_profile`` (or by a /jobs call that flowed "
        "into a session). Worker extracts to a per-job "
        "scratch dir and launches Chrome with "
        "--user-data-dir; the original tarball stays "
        "read-only so the same upload can back many "
        "concurrent sessions.",
    )
    profile_name: str | None = Field(
        None,
        description="Profile name (mirrors HubAssignJob.profile_name).",
    )
    profile_etag: str | None = Field(
        None,
        description="Profile cache key (mirrors HubAssignJob.profile_etag).",
    )
    download_video: bool = Field(
        False,
        description="When True the worker sets up iframe + nested-"
        "iframe network deep-trace (Target.setAutoAttach + per-"
        "child Network.enable) at session start so HLS/DASH "
        "manifest URLs from cross-origin video players are "
        "captured. When False the deep-trace is SKIPPED to save "
        "resources; it can be enabled retroactively the first "
        "time page.download_video() is called. Plumbed from "
        "JobOptions.download_video (mode='fetch' / 'codegen-loop') "
        "or from /sessions request body.",
    )


class HubSessionAction(BaseModel):
    """Run one browser_ops primitive against a bound session."""

    type: Literal["session_action"] = "session_action"
    session_id: str
    request_id: str
    action: dict[str, Any] = Field(
        ...,
        description="ParsedAction-shaped dict: at minimum has 'kind' "
        "(click/fill/scroll/navigate/back/press_key/outline/"
        "screenshot/state/capture). Other fields per action.",
    )


class HubSessionEnd(BaseModel):
    """Release the lane bound to ``session_id``. Worker resets tabs but
    leaves Chrome running so the lane is reusable for the next session."""

    type: Literal["session_end"] = "session_end"
    session_id: str


class HubSessionAgent(BaseModel):
    """Run a localised agent loop on a bound session.

    The worker observes the page outline, asks the configured engine
    for the next action, executes it via browser_ops, and repeats up
    to ``max_steps`` iterations or until the model emits ``done``.
    Used to handle small unknown situations (age gates, login dialogs,
    "find and click the play button") from inside an otherwise
    deterministic paprika-client script via ``page.agent(goal, ...)``.

    ``engine`` selects which model drives each step:

      - ``"qwen"``: Qwen-VL via the agent_service /act endpoint.
                    Emits CSS selectors against the page outline.
                    The only supported engine after the v2 cleanup
                    (CogAgent / gui-agent was retired).
      - ``"auto"``: Alias for "qwen". Kept for backward compatibility
                    with paprika_client callers that pass engine="auto".

    Japanese / Chinese goals are auto-translated to English on the
    worker side before being shown to the engine.
    """

    type: Literal["session_agent"] = "session_agent"
    session_id: str
    request_id: str
    goal: str
    max_steps: int = 5
    engine: Literal["auto", "qwen", "cogagent"] = "auto"


class HubProfileSync(BaseModel):
    """Notify a worker about an operator-uploaded Chrome profile so
    it can prefetch the tarball into its local cache.

    Sent by the hub right after the operator finishes uploading a
    profile (POST /profiles/{name}) and also re-sent for every
    existing profile when a worker reconnects (so workers that were
    offline at upload time catch up).

    Workers MAY ignore this -- the on-demand fetch path
    (HubAssignJob.profile_url) still works without a primed cache.
    The cache is purely a bandwidth optimisation for the
    same-profile-used-by-many-jobs pattern.
    """

    type: Literal["profile_sync"] = "profile_sync"
    name: str = Field(..., description="Profile name (registry key).")
    url: str = Field(
        ...,
        description="HTTP URL the worker GETs to fetch the tarball. "
        "Same shape as HubAssignJob.profile_url so the "
        "worker can reuse a single download helper.",
    )
    etag: str = Field(
        ...,
        description="Opaque cache key. Workers compare this against "
        "their cached entry's etag; equal -> reuse cached "
        "extraction, different -> refetch + re-extract. "
        "Hub derives it from (size_bytes, updated_at) so "
        "every re-upload of the same name produces a "
        "fresh etag.",
    )
    size_bytes: int = Field(
        0,
        ge=0,
        description="Compressed tarball size in bytes (informational, "
        "for the worker's pre-flight disk-space check).",
    )
    is_default: bool = Field(
        False,
        description="True when this profile is the operator-set "
        "default. Workers install the default into every "
        "idle lane right after the prefetch completes so "
        "noVNC viewers see the operator's logged-in "
        "Chrome even on lanes that haven't run a job "
        "yet. Re-broadcast (with is_default flipped) "
        "whenever the default changes -- see "
        "POST /profiles/{name}/default + DELETE "
        "/profiles/default.",
    )


class HubProfileDelete(BaseModel):
    """Tell workers to drop their cached copy of a profile.

    Sent after DELETE /profiles/{name}. Workers should rmtree the
    cache entry; in-flight jobs that have already extracted into a
    lane keep their copy until the job ends (cache delete races
    job teardown by design).
    """

    type: Literal["profile_delete"] = "profile_delete"
    name: str


class HubUpdateGate(BaseModel):
    """Hub's response to a WorkerDraining: either grant a "you may
    proceed with the source fetch + exit now" green light, or hold
    the worker in drain mode a little longer because the fleet's
    concurrent-update budget is currently full.

    The hub limits concurrent source-fetch + restart cycles to
    ``PAPRIKA_ROLLING_UPDATE_MAX_PARALLEL`` (default 3) so the fleet
    rolls forward in batches rather than all going dark at once.
    Workers that get ``allow_now=False`` keep draining (no new work
    accepted) and the hub sends another HubUpdateGate when a slot
    opens up. ``jitter_s`` (when allow_now=True) is an additional
    randomised delay the hub picks so even within one batch the
    actual fetch + restart times spread out.
    """

    type: Literal["update_gate"] = "update_gate"
    allow_now: bool
    why: str = ""
    jitter_s: float = 0.0


class HubExpectedVersion(BaseModel):
    """Hub re-advertises the worker source version it expects, OUTSIDE the
    register handshake, so a connected worker self-updates without waiting for a
    reconnect. ``HubRegistered.expected_worker_version`` only fires on connect;
    the hub now also sends this on heartbeat to any worker whose reported version
    differs from the hub's current source hash. The worker runs the SAME
    version-mismatch check + rolling drain/self-update (HubUpdateGate still gates
    concurrency) it does at handshake -- so a worker-code deploy rolls out with
    NO hub restart / WS-drop (no session loss, no reconnect storm)."""

    type: Literal["expected_version"] = "expected_version"
    expected_worker_version: str | None = None


class HubSessionInteraction(BaseModel):
    """Notify the worker that an operator is actively driving a session
    via noVNC (RFB KeyEvent / PointerEvent / ClientCutText detected
    on the hub's noVNC WS bridge).

    Used by the worker's yt-dlp stall-detection gates (inline /
    adapter Popen loops + parent watchdog task) to DEFER the kill
    while a human is interacting with the lane's Chrome -- the
    operator's wishes outrank the automatic "too slow" verdict
    (evidence preservation > throughput). Protection lifts naturally
    ~60 seconds after the last interaction since the hub throttles
    these pushes to once per 10s.

    ``ts`` is the hub-side time.time() at which the RFB activity was
    observed. Workers compare against their own clock; small skew is
    fine because the grace window is 60s.
    """

    type: Literal["session_interaction"] = "session_interaction"
    session_id: str
    ts: float


HubToWorkerMsg = Annotated[
    Union[
        HubAssignJob,
        HubCancelJob,
        HubPing,
        HubRegistered,
        HubScreenshotRequest,
        HubPreviewSubscribe,
        HubSessionStart,
        HubSessionAction,
        HubSessionEnd,
        HubSessionAgent,
        HubProfileSync,
        HubProfileDelete,
        HubSessionInteraction,
        HubUpdateGate,
        HubExpectedVersion,
    ],
    Field(discriminator="type"),
]
hub_to_worker_adapter: TypeAdapter[HubToWorkerMsg] = TypeAdapter(HubToWorkerMsg)


# --- Helpers ----------------------------------------------------------------


def encode_msg(msg: BaseModel) -> str:
    """Serialize a message for the wire."""
    return msg.model_dump_json()


def decode_worker_msg(raw: str) -> WorkerToHubMsg:
    return worker_to_hub_adapter.validate_json(raw)


def decode_hub_msg(raw: str) -> HubToWorkerMsg:
    return hub_to_worker_adapter.validate_json(raw)
