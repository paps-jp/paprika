"""Auto-escalate FAILED worker ``fetch`` jobs into the AI codegen-loop.

WHY
===
Almost all production traffic is plain ``mode=fetch`` (single-shot
capture). When a fetch fails on a *recoverable* barrier -- a video that
won't download, a login / age gate -- the job is a dead end: the
operator restarts the worker/hub (which does NOT help; the page still
gates) or gives up. Meanwhile the AI learning loop (codegen-loop +
HostKnowledge / skills / conventions distillation, surfaced in the #ai
tab) sits idle, because learning only runs on codegen-loop jobs and
almost nobody submits those.

This module closes the gap. On a genuine fetch FAILURE -- the
worker-reported ``WorkerJobFailed`` path, NOT the reaper's restart-orphan
path (which carries no learning signal) -- the hub may auto-spawn a
``codegen-loop`` retry. The AI agent then tries to get past the barrier,
and every such run feeds the distillers. Failures become the fuel that
finally exercises the learning loop. This split is exactly the operator's
ask: escalate real failures, NOT worker/hub-restart noise.

SCOPE / GATES (deliberately conservative -- the shared RTX 6000 is the
fleet bottleneck; see _gpu_gate.py)
  * OFF by default. Operator opts in via Settings ``auto_escalate_enabled``.
  * Only ``mode=fetch`` failures escalate; codegen-loop / rerun never do
    (no self-escalation loops). A job already carrying ``escalated_to``
    is never re-escalated.
  * Only recoverable CATEGORIES escalate -- ``video_dl`` (video-download
    failure) and ``auth_gate`` (login / age / paywall) -- classified from
    the error string + log tail + HostKnowledge barriers. Subset chosen
    via Settings ``auto_escalate_categories``.
  * GPU-idle gate: skip while THIS hub is running any perception
    inference or already has an escalation-budget's worth of codegen-loop
    jobs in flight. (The operator chose "auto only when the GPU is idle".)
  * Burst control: a per-host cooldown + a rolling-hour cap.
  * Kill-switch env ``PAPRIKA_ESCALATE_DISABLE=1``.

Everything here is best-effort and backgrounded: a failure in this module
never affects the job's own failure handling. Schedule
``maybe_escalate_failed_fetch`` with ``asyncio.create_task`` so it never
blocks the WS handler.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

from server.hub._state import state
from server.protocol import JobInfo, JobOptions, JobProgress, JobStatus

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (env). Settings (auto_escalate_*) gate the feature on/off +
# categories; these env knobs bound the cost.
# ---------------------------------------------------------------------------
def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


_DISABLE = _flag("PAPRIKA_ESCALATE_DISABLE", False)
# Max codegen-loop jobs allowed in flight on THIS hub before the escalator
# backs off (the "GPU idle" budget). 1 = only escalate when the hub is
# running no codegen-loop work of its own.
_MAX_INFLIGHT = int(_num("PAPRIKA_ESCALATE_MAX_INFLIGHT", 1))
# Per-host cooldown: don't escalate the same host more than once per N s.
_HOST_COOLDOWN_S = _num("PAPRIKA_ESCALATE_HOST_COOLDOWN_S", 1800.0)
# Global cap: at most N escalations per rolling hour (per hub).
_MAX_PER_HOUR = int(_num("PAPRIKA_ESCALATE_MAX_PER_HOUR", 12))
# How many generate->execute attempts the escalated codegen-loop job gets.
_RETRY_ATTEMPTS = max(1, min(10, int(_num("PAPRIKA_ESCALATE_ATTEMPTS", 3))))

# In-memory rate-limit state (per hub; reset on restart -- acceptable, the
# hourly cap is a courtesy throttle, not a correctness invariant).
_last_host_escalate: dict[str, float] = {}
_recent_escalates: list[float] = []  # unix ts of recent escalations


def _host_of(url: str | None) -> str | None:
    """Normalised host (lowercase, ``www.`` stripped) from a URL."""
    try:
        h = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return None
    if h.startswith("www."):
        h = h[4:]
    return h or None


# ---------------------------------------------------------------------------
# Classification -- what kind of recoverable barrier (if any) failed?
# ---------------------------------------------------------------------------
_AUTH_RE = re.compile(
    r"(log[\s_-]?in|sign[\s_-]?in|signin|authenticat|unauthor|http[\s_]?error[\s_]?403"
    r"|\b403\b|forbidden|age[\s_-]?gate|verify your age|are you (over )?18|18\+"
    r"|adult content|paywall|subscri|membership"
    r"|ログイン|サインイン|年齢|認証|会員|有料)",
    re.I,
)
_VIDEO_FAIL_RE = re.compile(
    r"(yt[\s_-]?dlp|unable to (download|extract)|requested format (is )?not available"
    r"|no video|sign in to confirm|drm|fragment|video unavailable|私的録画)",
    re.I,
)
# Hard dead-ends an AI retry can't fix (DNS / TLS / connection / 404 / 410).
# Used to keep the generic ``under_delivered`` supply path from spending GPU
# on pages that simply don't exist or won't connect.
_HARD_DEAD_RE = re.compile(
    r"(name or service not known|nodename nor servname|temporary failure in name"
    r" resolution|no address associated|getaddrinfo|connection refused|connection"
    r" reset|connection timed out|ssl|certificate|err_cert|\b404\b|not found"
    r"|\b410\b|\bgone\b|\b50[0235]\b)",
    re.I,
)


def _read_log_tail(job_id: str, max_bytes: int = 8000) -> str:
    """Best-effort tail of the job's log.txt (hub-local cache). Empty when
    the log isn't on this hub's disk (e.g. lost a race with the upload)."""
    try:
        from server.hub._state import get_storage_dir

        p = get_storage_dir() / job_id / "log.txt"
        if not p.is_file():
            return ""
        data = p.read_bytes()
        return data[-max_bytes:].decode("utf-8", "replace")
    except Exception:
        return ""


def _host_barriers(host: str | None) -> set[str]:
    """BarrierKinds HostKnowledge has marked ``present`` for ``host``.

    A bonus signal layered on top of the regex classifier -- if it can't
    be read, classification still works from the error/log text.
    """
    out: set[str] = set()
    if not host:
        return out
    try:
        import json as _json

        from server.hub._state import get_storage_dir

        p = get_storage_dir() / "host_knowledge" / f"{host}.json"
        if not p.is_file():
            return out
        k = _json.loads(p.read_text("utf-8"))
        barriers = ((k.get("per_page") or {}).get("barriers") or {})
        for kind, v in barriers.items():
            if isinstance(v, dict) and v.get("present"):
                out.add(kind)
    except Exception:
        pass
    return out


def _host_excluded(host: str | None) -> bool:
    """True iff the operator registered this host as having NO video
    (``HostRecord.excluded``). Read from the live host registry. We never
    spend AI hunting a video on a host the operator confirmed has none."""
    if not host:
        return False
    try:
        reg = getattr(state, "hosts", None)
        if reg is None:
            return False
        rec = reg.get(host)
        return bool(rec is not None and getattr(rec, "excluded", False))
    except Exception:
        return False


def classify_failure(info: JobInfo, error: str) -> str | None:
    """Return an escalation category for a failed fetch job, or None.

    Categories:
      ``video_dl``  -- the job wanted a video but captured none.
      ``auth_gate`` -- a login / age / paywall barrier blocked it.
    None means "not a recoverable barrier we'd spend GPU on".
    """
    opts = info.options
    blob = f"{error or ''}\n{_read_log_tail(info.job_id)}"
    host = _host_of(info.url)
    barriers = _host_barriers(host)

    # ---- video-download failure --------------------------------------
    # Strongest signal: the job explicitly wanted a video (download_video=
    # True) but captured nothing, OR the log shows a yt-dlp / extractor
    # failure. download_video is the operator's "this page has a video"
    # declaration, so a 0-asset failure there is almost always recoverable.
    if opts is not None and getattr(opts, "download_video", False):
        saved = (info.progress.assets_saved if info.progress else 0) or 0
        if saved == 0 or _VIDEO_FAIL_RE.search(blob):
            return "video_dl"

    # ---- auth / age / paywall gate -----------------------------------
    if barriers & {"login_wall", "age_gate", "paywall"}:
        return "auth_gate"
    if _AUTH_RE.search(blob):
        return "auth_gate"

    return None


_VIDEO_ASSET_EXTS = {"mp4", "webm", "mov", "m4v", "mkv"}


def _has_video_asset(result) -> bool:
    """True iff the completed job actually saved a video file."""
    for a in (getattr(result, "assets", None) or []):
        mime = (getattr(a, "mime", None) or "")
        if mime.startswith("video/"):
            return True
        name = (getattr(a, "name", "") or "")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in _VIDEO_ASSET_EXTS:
            return True
    return False


def classify_completed(info: JobInfo, result) -> str | None:
    """Escalation category for a fetch that COMPLETED but didn't deliver.

    Most "auth screen" / "video won't download" outcomes are NOT hard
    failures -- the worker returns a FetchResult (login page captured /
    yt-dlp errored) and the job ends ``completed``. This classifier finds
    the recoverable ones from the JobResult:

      ``video_dl``       -- download_video was requested, a video was
                            DETECTED (or yt-dlp attempted) yet none saved.
      ``auth_gate``      -- a login / age / consent / paywall wall ate the
                            content. Signals, strongest first:
                              1. the structural occlusion probe behind the
                                 課題/review feature flagged a full-screen
                                 blocking overlay (server/hub/_review.py),
                              2. HostKnowledge already marks the host gated,
                              3. a gate keyword in the captured log/page.
      ``under_delivered`` -- (opt-in) completed but saved ZERO assets and
                            isn't an obvious hard dead-end. The broad supply
                            path that lets the AI loop actually run at the
                            target cadence; the rate limiter -- not this
                            classifier -- caps how many we spend GPU on.
    """
    opts = info.options
    host = _host_of(info.url)

    # ---- video requested, evidence existed, nothing saved ----
    if opts is not None and getattr(opts, "download_video", False):
        if not _has_video_asset(result):
            detected = bool(getattr(result, "video_detection", None)) or bool(
                getattr(result, "video_urls_seen", None)
            )
            yt = list(getattr(result, "ytdlp_results", None) or [])
            yt_all_failed = bool(yt) and all(not getattr(r, "ok", False) for r in yt)
            if detected or yt_all_failed:
                return "video_dl"

    asset_count = len(getattr(result, "assets", None) or [])

    # ---- full-screen overlay wall (login / age / consent / paywall) ----
    # Reuse the structural occlusion probe that powers the 課題/review
    # bucket: if it flagged this completed fetch as content-blocked, that IS
    # a recoverable gate the codegen-loop should drive past. Far higher
    # precision than a keyword scan and no per-site rules. Best-effort: any
    # import/exec error falls through to the heuristics below.
    try:
        from server.hub._review import classify_review

        if classify_review(info, result):
            return "auth_gate"
    except Exception:
        pass

    # ---- known-gated host returned nothing ----
    barriers = _host_barriers(host)
    if barriers & {"login_wall", "age_gate", "paywall"} and asset_count == 0:
        return "auth_gate"

    # ---- gate / under-delivery from the captured log+page ----
    if asset_count == 0:
        blob = _read_log_tail(info.job_id)
        # A gate keyword even before HostKnowledge has learned the host.
        if blob and _AUTH_RE.search(blob):
            return "auth_gate"
        # Generic under-delivery (opt-in via the ``under_delivered``
        # category): completed but captured nothing, and not an obvious hard
        # dead-end (DNS / 404 / TLS / refused). Bounded by the per-host
        # cooldown + hourly cap, so a noisy long tail is fine.
        if "under_delivered" in _enabled_categories() and not (
            blob and _HARD_DEAD_RE.search(blob)
        ):
            return "under_delivered"

    return None


# ---------------------------------------------------------------------------
# Gates -- GPU idle + burst control
# ---------------------------------------------------------------------------
def _gpu_idle() -> bool:
    """True when THIS hub is doing little/no AI work right now -- the
    "only escalate when the GPU is idle" policy.

    The RTX 6000 is shared by every hub + worker, so a single hub's view
    is a proxy, not global truth; the per-host cooldown + hourly cap are
    the real backstops. We refuse when either:
      * this hub has >= _MAX_INFLIGHT codegen-loop jobs running, or
      * this hub has an in-flight vision (perception) inference.
    """
    try:
        from server.hub._gpu_gate import codegen_loop_in_flight

        if codegen_loop_in_flight() >= _MAX_INFLIGHT:
            return False
    except Exception:
        pass
    try:
        from server.hub.perception_llm import get_vision_inference_stats

        if (get_vision_inference_stats() or {}).get("active", 0) > 0:
            return False
    except Exception:
        pass
    return True


async def _thermal_ok() -> bool:
    """Preemptive thermal gate: True when the CODER engine's local GPU is
    accepting, so we don't spawn a codegen-loop onto a hot GPU. Delegates to
    the per-engine thermal config (server/hub/thermal.py) of the engine that
    serves the default codegen model -- the call sites enforce per call, this
    just avoids wasting a lane. Open when no coder engine / thermal window is
    configured; never blocks on the thermal layer's own error."""
    try:
        from server.hub.codegen import _env_default_target, _slug_for_model
        from server.hub import thermal

        reg = getattr(state, "engines", None)
        if reg is None:
            return True
        slug = _slug_for_model(getattr(_env_default_target(), "model", "") or "")
        if not slug:
            return True
        rec = reg.get(slug)
        if rec is None:
            return True
        return await thermal.engine_thermal_ok(rec)
    except Exception:
        return True


def _rate_ok(host: str | None) -> bool:
    now = time.time()
    cutoff = now - 3600.0
    # prune the rolling-hour window in place
    _recent_escalates[:] = [t for t in _recent_escalates if t >= cutoff]
    if len(_recent_escalates) >= _MAX_PER_HOUR:
        return False
    last = _last_host_escalate.get(host or "")
    if last is not None and (now - last) < _HOST_COOLDOWN_S:
        return False
    return True


def _record_escalate(host: str | None) -> None:
    now = time.time()
    _recent_escalates.append(now)
    if host:
        _last_host_escalate[host] = now


# ---------------------------------------------------------------------------
# Goal synthesis -- codegen-loop requires a natural-language goal; a fetch
# job only has a URL + options, so we template one from the category.
# ---------------------------------------------------------------------------
_GOALS = {
    "video_dl": (
        "このページから動画を取得して保存してください。"
        "通常の自動取得(fetch)では動画ファイルが1本も取れませんでした"
        "（download_video は有効でした）。"
        "再生ボタンのクリック、iframe や HLS(m3u8)/DASH の追跡、"
        "ログインや年齢確認の突破などが必要なら行い、"
        "動画本体をダウンロードしてください。\n対象URL: {url}"
    ),
    "auth_gate": (
        "このページを開き、ログイン・年齢確認(18歳以上の確認)・Cookie同意"
        "などの障壁を突破してから、ページの主要なメディア(画像・動画)と"
        "本文を取得・保存してください。"
        "通常の自動取得(fetch)は認証/年齢ゲートで失敗しました。"
        "保存済みの Cookie やプロフィールがあれば活用し、無ければ"
        "確認ダイアログをクリック操作で通過してください。\n対象URL: {url}"
    ),
    "under_delivered": (
        "このページを開き、ページの主要なコンテンツ(画像・動画・本文)を"
        "取得・保存してください。通常の自動取得(fetch)ではアセットを1件も"
        "保存できませんでした。原因として、スクロールやクリックで遅延ロード"
        "される画像、iframe 内の本体、lazy-load、年齢確認や Cookie 同意など"
        "の軽い障壁が考えられます。必要ならスクロール・クリック・JS での展開"
        "を行い、本来取得すべきメディアと本文を保存してください。\n対象URL: {url}"
    ),
}


def _synth_goal(category: str, url: str) -> str:
    return (_GOALS.get(category) or _GOALS["auth_gate"]).format(url=url)


# ---------------------------------------------------------------------------
# Feature toggles (Settings)
# ---------------------------------------------------------------------------
def _feature_on() -> bool:
    if _DISABLE:
        return False
    try:
        if state.settings is not None:
            return bool(state.settings.get("auto_escalate_enabled", False))
    except Exception:
        pass
    return False


def _enabled_categories() -> set[str]:
    raw = ""
    try:
        if state.settings is not None:
            raw = (state.settings.get("auto_escalate_categories", "") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        return {"video_dl", "auth_gate"}
    return {c.strip() for c in raw.split(",") if c.strip()}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _precheck(info: JobInfo) -> bool:
    """Common gate shared by both entry points: feature on, mode=fetch,
    not already escalated."""
    if info is None or state.store is None:
        return False
    opts = info.options
    if ((opts.mode if opts else None) or "fetch") != "fetch":
        return False  # only fetch escalates; no self-escalation loops
    # Operator marked this host 対象外 (HostRecord.excluded = "do nothing"):
    # no escalation of any category.
    if _host_excluded(_host_of(info.url)):
        return False
    if not _feature_on():
        return False
    if opts is not None and getattr(opts, "escalated_to", None):
        return False  # already escalated once -- dedup
    return True


async def _escalate_if_eligible(info: JobInfo, category: str | None) -> str | None:
    """Apply category + rate + GPU-idle gates, then escalate."""
    if category is None or category not in _enabled_categories():
        return None
    host = _host_of(info.url)
    if not _rate_ok(host):
        log.info(
            "escalate: rate/cooldown gate skipped %s (host=%s, cat=%s)",
            info.job_id, host, category,
        )
        return None
    if not await _thermal_ok():
        log.info(
            "escalate: GPU-thermal gate closed -- not escalating %s (host=%s, cat=%s)",
            info.job_id, host, category,
        )
        return None
    if not _gpu_idle():
        # Policy is "only when idle" -> drop rather than queue. The GPU is
        # normally idle here, so this is the uncommon case; logged so it's
        # visible if escalations mysteriously don't fire.
        log.info(
            "escalate: GPU busy -- not escalating %s (host=%s, cat=%s)",
            info.job_id, host, category,
        )
        return None
    return await _do_escalate(info, category, host)


async def maybe_escalate_failed_fetch(info: JobInfo, error: str) -> str | None:
    """A worker `fetch` job HARD-FAILED (WorkerJobFailed = exception). If
    it's a recoverable barrier, retry it via the AI codegen-loop.
    Best-effort: returns the new job_id on escalation, else None.
    """
    try:
        if not _precheck(info):
            return None
        # classify_failure does small synchronous disk reads (log tail +
        # HostKnowledge json); keep them off the event loop, matching the
        # hub's "no sync IO on the loop" direction.
        category = await asyncio.to_thread(classify_failure, info, error)
        return await _escalate_if_eligible(info, category)
    except Exception:
        log.debug(
            "escalate: failed-path crashed for %s",
            getattr(info, "job_id", "?"), exc_info=True,
        )
        return None


async def maybe_escalate_completed_fetch(info: JobInfo, result) -> str | None:
    """A worker `fetch` job COMPLETED but may not have delivered (login
    page captured / video detected-but-not-downloaded). Most real "auth
    screen" / "video DL failed" cases land here, NOT in the failed path,
    because the worker returns a FetchResult rather than raising. If the
    completion is recoverable, retry it via the AI codegen-loop.
    """
    try:
        if not _precheck(info):
            return None
        category = await asyncio.to_thread(classify_completed, info, result)
        return await _escalate_if_eligible(info, category)
    except Exception:
        log.debug(
            "escalate: completed-path crashed for %s",
            getattr(info, "job_id", "?"), exc_info=True,
        )
        return None


async def _do_escalate(info: JobInfo, category: str, host: str | None) -> str | None:
    from server.hub._jobrunner import _run_codegen_loop_job

    orig = info.options
    new_id = uuid.uuid4().hex[:12]
    goal = _synth_goal(category, info.url)

    # Copy the original options forward (preserving download_video,
    # use_profile, cookies_from, referer, codegen_engine, …) and override
    # only what turns it into an AI retry. model_copy avoids re-listing
    # every field + dodges any field name drift.
    overrides = {
        "mode": "codegen-loop",
        "goal": goal,
        "max_codegen_attempts": _RETRY_ATTEMPTS,
        "attempt_timeout_s": max(int(getattr(orig, "attempt_timeout_s", 180) or 180), 300),
        "capture_assets": True,
        "escalated_from": info.job_id,
        "escalated_to": None,
        # clear rerun-only / hub-stamped fields so they don't leak across
        "rerun_from": None,
        "code": None,
        "fetch_recipe": None,
    }
    try:
        new_opts = orig.model_copy(update=overrides) if orig is not None else JobOptions(**overrides)
    except Exception:
        # Defensive: if model_copy chokes on an unexpected field, build a
        # minimal valid codegen-loop options object.
        new_opts = JobOptions(
            mode="codegen-loop",
            goal=goal,
            max_codegen_attempts=_RETRY_ATTEMPTS,
            escalated_from=info.job_id,
        )

    now = datetime.utcnow()
    new_info = JobInfo(
        job_id=new_id,
        status=JobStatus.queued,
        url=info.url,
        options=new_opts,
        created_at=now,
        progress=JobProgress(
            phase="queued",
            last_log=f"auto-escalated from fetch {info.job_id} ({category})",
        ),
        owner_id=getattr(info, "owner_id", "default"),
    )
    await state.store.save_job_info(new_info)

    # Spawn the hub-side codegen-loop orchestrator. request=None mirrors
    # the orphan-redispatch path (_jobrunner.redispatch_orphan_job); the
    # orchestrator self-registers in the GPU gate.
    task = asyncio.create_task(_run_codegen_loop_job(None, new_info))
    try:
        state.local_tasks[new_id] = task
    except Exception:
        pass

    # Stamp the original so we never escalate it twice + the UI can link
    # the pair. Re-save (the failure handler already saved it once).
    try:
        if info.options is not None:
            info.options.escalated_to = new_id
            await state.store.save_job_info(info)
    except Exception:
        log.debug("escalate: failed to stamp escalated_to on %s", info.job_id, exc_info=True)

    _record_escalate(host)
    log.info(
        "escalate: fetch %s -> codegen-loop %s (cat=%s, host=%s, attempts=%d)",
        info.job_id, new_id, category, host, _RETRY_ATTEMPTS,
    )
    return new_id
