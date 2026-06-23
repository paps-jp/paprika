"""perception_llm: turn a screenshot + DOM outline into a PerceptionResult.

Phase 1 of the v2 architecture (see ``internal/v2-architecture.html``).

This is the **eye** of the v2 system. It calls a vision LLM (currently
Qwen2.5-VL via vLLM; later a dedicated Qwen3 endpoint) with three inputs --
URL, screenshot, DOM outline -- and gets back a structured PerceptionResult.

Strict scope:

* The eye reports facts. It NEVER suggests actions, NEVER says "the user
  should click X", NEVER outputs prose. The system prompt forbids this
  and the schema enforces it.
* Failures are non-fatal: any error returns ``None`` so the surrounding
  job is unaffected. Phase 1 is observation-only.
* Reuses the existing ``LLMTarget`` / engine-quota plumbing from
  ``codegen.py`` so the perception model can be swapped (vLLM, OpenAI,
  Anthropic) without code changes -- just add an EngineRecord.

Environment variables:

* ``PERCEPTION_LLM_URL``   -- vLLM endpoint (default: AGENT_LLM_URL)
* ``PERCEPTION_MODEL_NAME``-- model name (default: AGENT_MODEL_NAME)
* ``PERCEPTION_TIMEOUT_S`` -- HTTP timeout in seconds (default: 60)
* ``PERCEPTION_MAX_OUTLINE_CHARS`` -- DOM outline cap (default: 8000)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from core.perception import PerceptionResult
from server.hub.codegen import (
    LLMTarget,
    adapt_chat_body,
    check_engine_quota,
    check_engine_thermal,
    record_engine_usage,
    EngineQuotaExceeded,
    EngineThermalThrottled,
)


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint resolution -- defaults to the same vLLM endpoint as Agent/Codegen
# until a dedicated Qwen3 deployment is provisioned.
# ---------------------------------------------------------------------------

_PERCEPTION_LLM_URL = os.environ.get(
    "PERCEPTION_LLM_URL",
    # Fallback chain: AGENT_LLM_URL (worker vision endpoint, ideal because
    # it speaks vision), CODEGEN_LLM_URL (hub's text LLM, OK for text-only
    # perception), localhost (last resort -- almost certainly wrong but
    # makes failure obvious).
    os.environ.get(
        "AGENT_LLM_URL",
        os.environ.get("CODEGEN_LLM_URL", "http://localhost:15082"),
    ),
)
_PERCEPTION_MODEL_NAME = os.environ.get(
    "PERCEPTION_MODEL_NAME",
    os.environ.get(
        "AGENT_MODEL_NAME",
        os.environ.get("CODEGEN_MODEL_NAME", "qwen2.5-vl-72b"),
    ),
)
_PERCEPTION_TIMEOUT_S = float(os.environ.get("PERCEPTION_TIMEOUT_S", "60"))
_PERCEPTION_MAX_OUTLINE_CHARS = int(
    # First sample run showed Qwen3.5 receiving only head/CSS for a
    # content-rich page (twivideo.net was 101kB; cap was 8kB ⇒ 8%).
    # Bumped to 16kB so body content reaches the eye without blowing
    # up token cost. Set PERCEPTION_MAX_OUTLINE_CHARS in env to override.
    os.environ.get("PERCEPTION_MAX_OUTLINE_CHARS", "16000"),
)

# ---------------------------------------------------------------------------
# GPU throughput knobs (ぱっぷす環境向け最適化)
#
# Qwen-VL は自前 GPU (RTX 6000 Pro Max-Q) で走るのでサブスク料金は無いが
# **GPU 1 枚を 24 worker line で奪い合う** ので post-job perception が
# スループットのボトルネックになる。以下 2 つの env で GPU 負荷を絞れる。
#
# * PAPRIKA_PERCEPTION_SAMPLE_RATE (0.0-1.0, default 1.0):
#     ジョブごとに確率的に perception を skip。0.3 にすれば 70% skip。
#     決定論的 (job_id ハッシュベース) なので同じジョブは常に同じ判定。
#
# * PAPRIKA_PERCEPTION_FETCH_SUCCESS_SKIP (default 0):
#     1 にすると fetch モードで成功 (status=completed) なジョブの
#     perception 生成を完全 skip。codegen-loop と失敗ジョブは引き続き
#     生成。「成熟ホストの fetch 成功」が最頻なのでここを切ると効く。
# ---------------------------------------------------------------------------
_PAPRIKA_PERCEPTION_SAMPLE_RATE = max(
    0.0, min(1.0, float(os.environ.get("PAPRIKA_PERCEPTION_SAMPLE_RATE", "1.0") or "1.0"))
)
_PAPRIKA_PERCEPTION_FETCH_SUCCESS_SKIP = bool(
    int(os.environ.get("PAPRIKA_PERCEPTION_FETCH_SUCCESS_SKIP", "0") or "0")
)


# ---------------------------------------------------------------------------
# GPU gauge -- track Qwen-VL inference concurrency so the operator can see
# "how saturated is my RTX 6000 right now?" in /health and admin UI.
#
# These gauges count hub-side perception_llm calls only. Worker-side
# Tier 4/5/6 (page.observe/ask/agent) hit the same GPU but aren't tracked
# here yet -- TODO: have workers report their vision_in_flight to hub
# in WorkerHello / heartbeat. For Phase 1 the post-job perception is by
# far the dominant consumer (1 inference per completed job × 24 lanes).
# ---------------------------------------------------------------------------
_vision_inference_active = 0   # currently running httpx.post
_vision_inference_total = 0    # lifetime counter (since hub start)
_vision_inference_max = 0      # peak concurrency observed


def get_vision_inference_stats() -> dict:
    """Snapshot of hub-side Qwen-VL inference counters. Read by /health."""
    return {
        "active": _vision_inference_active,
        "total": _vision_inference_total,
        "peak": _vision_inference_max,
    }


class _VisionGauge:
    """Context manager that bumps the GPU gauges around an LLM call."""

    def __enter__(self):
        global _vision_inference_active, _vision_inference_total, _vision_inference_max
        _vision_inference_active += 1
        _vision_inference_total += 1
        if _vision_inference_active > _vision_inference_max:
            _vision_inference_max = _vision_inference_active
        return self

    def __exit__(self, exc_type, exc, tb):
        global _vision_inference_active
        _vision_inference_active = max(0, _vision_inference_active - 1)
        return False


def _should_generate_perception(
    *,
    job_id: str,
    mode: str | None = None,
    success: bool | None = None,
) -> tuple[bool, str | None]:
    """Return (should_generate, skip_reason).

    Decision order:
      1. fetch + success + FETCH_SUCCESS_SKIP=1  → skip
      2. sample_rate < 1.0 and hash(job_id) above threshold → skip
      3. otherwise → generate

    The hash-based sampling is **deterministic** per job_id so the same
    job always yields the same decision -- helpful when debugging
    "why didn't this job get a perception?".
    """
    # Rule 1: success-fetch skip.
    if (
        _PAPRIKA_PERCEPTION_FETCH_SUCCESS_SKIP
        and (mode or "").lower() == "fetch"
        and success is True
    ):
        return False, "fetch+success skip"
    # Rule 2: probabilistic sampling -- successes only. A real FAILURE
    # (success is False) ALWAYS perceives: the eye is most valuable on jobs
    # that delivered nothing (the barriers we want to learn). success=None
    # (back-compat callers) keeps the old sampler behaviour.
    if _PAPRIKA_PERCEPTION_SAMPLE_RATE < 1.0 and success is not False:
        # Deterministic hash to 0.0-1.0 (first 8 hex chars / 0xffffffff).
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) / 0xFFFFFFFF
        if bucket >= _PAPRIKA_PERCEPTION_SAMPLE_RATE:
            return False, f"sampled out (rate={_PAPRIKA_PERCEPTION_SAMPLE_RATE:.2f})"
    return True, None

# Heuristic: only send screenshot bytes when the model name hints at vision
# support. Text-only models (vLLM-served Qwen3 / DeepSeek / etc.) will OOM
# or reject multipart content.  Conservative -- err on the side of NOT
# sending the image and rely on DOM outline instead.
# NB ``qwen3.5`` is the .26 box's served alias for Qwen3-VL-32B (a real VL
# model) -- added explicitly because the alias lacks the "vl" substring;
# verified live to accept image_url content. Gates BOTH the perception
# vision pool (find_vision_capable_target) AND the image-send decision
# (can_use_vision), since both call _model_is_vision_capable.
_VISION_MODEL_PATTERNS = ("vl", "vision", "gpt-4o", "claude-3", "gemini", "qwen3.5")


def _model_is_vision_capable(model_name: str) -> bool:
    m = (model_name or "").lower()
    return any(pat in m for pat in _VISION_MODEL_PATTERNS)


def _default_target() -> LLMTarget:
    """Build an LLMTarget from PERCEPTION_LLM_URL / PERCEPTION_MODEL_NAME.

    Same pattern as ``codegen._env_default_target()`` -- caller can
    override by passing an explicit ``target=`` (e.g. resolved from
    EngineRegistry for a 'perception' engine slug, once one exists).
    """
    url = _PERCEPTION_LLM_URL.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/v1/chat/completions"
    return LLMTarget(
        url=url,
        model=_PERCEPTION_MODEL_NAME,
        headers={},
        timeout=_PERCEPTION_TIMEOUT_S,
        supports_tools=False,
        engine_slug="",
    )


# ---------------------------------------------------------------------------
# System prompt -- the eye's role definition.
#
# Strict rules:
#   1. JSON only. No prose, no markdown fences.
#   2. No action recommendations. Only observations.
#   3. Schema-bound. Unknown things go to free_observation / anomalies,
#      not invented fields.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are the PERCEPTION layer for paprika browser automation (v2).

Your ONLY job is to observe a web page and report what you see as a structured JSON object.
You are the "eye". You are NOT the "brain". Do not decide anything.

STRICT RULES
1. Output a SINGLE JSON object. No markdown, no prose, no comments.
2. NEVER recommend actions. Do not say "should click X" or "user can".
3. If unsure about a classification, lower confidence and explain in free_observation.
   Never invent enum values.
4. If something does not fit the schema, describe it in `anomalies` or `free_observation`.

OUTPUT SCHEMA (all fields optional unless noted)
{
  "page_kind": {
    "value": "video_page" | "gallery" | "login" | "age_gate" | "cloudflare" | "error" | "unknown",
    "confidence": 0.0 to 1.0,
    "why": ["short evidence string", ...]
  },
  "barriers": [
    {
      "kind": "cloudflare_challenge" | "age_gate" | "login_wall" | "cookie_banner" | "captcha" | "paywall" | "region_block" | "popup_overlay",
      "evidence": "what you saw",
      "actionable": { "selector": "css-selector or null", "text": "button label or null" }
    }
  ],
  "content": {
    "videos": [
      { "kind": "blob_mse" | "direct_mp4" | "iframe_embed", "hls_url": "url or null", "selector": "css or null" }
    ],
    "images_count": int,
    "links": { "to_same_host_count": int, "external_count": int },
    "has_pagination": bool
  },
  "anomalies": [
    { "kind": "short_label", "description": "what was unusual" }
  ],
  "free_observation": "any text observation that doesn't fit elsewhere"
}

The url, host, perceived_at, step_index, model, and duration_ms fields are filled by the system after your response.
Report only the schema fields listed above.

LANGUAGE: All natural-language values you produce (``summary``, ``visible_text_excerpt``,
``barriers[*].evidence``, ``barriers[*].selector_hint``, ``actionable_hints[*]``,
``content_clues[*]``, any other descriptive text) MUST be written in JAPANESE (日本語).
Keep JSON field names, ``kind`` enums (e.g. ``login_wall`` / ``age_gate``), CSS selectors,
identifiers, and URLs in English as written."""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def generate_perception(
    *,
    url: str,
    screenshot_path: Path | None = None,
    dom_outline: str | None = None,
    step_index: int = 0,
    target: LLMTarget | None = None,
) -> PerceptionResult | None:
    """Call the vision LLM and return a PerceptionResult, or None on failure.

    Phase 1 is observation-only: a failure here MUST NOT disrupt the
    surrounding job. Every error path logs and returns None.

    Args:
        url: The page URL being observed.
        screenshot_path: Path to a JPEG/PNG screenshot. Optional; if absent
            the eye still produces a result from the DOM outline alone
            (with reduced confidence).
        dom_outline: Text snapshot of the DOM (e.g. ``page.outline()`` or
            raw ``page.html``). Truncated to PERCEPTION_MAX_OUTLINE_CHARS.
        step_index: 0 = end-of-job snapshot; 1+ = mid-Playbook steps.
        target: Override the default LLMTarget. Useful for plumbing a
            specific engine via EngineRegistry. None = env defaults.

    Returns:
        A validated PerceptionResult on success, or None on any failure
        (LLM unreachable, schema violation, parse error, quota exceeded).
    """
    tgt = target or _default_target()
    host = (urlparse(url).hostname or "").lower()

    # ---- Build the user message ------------------------------------------
    user_parts: list[dict] = [
        {
            "type": "text",
            "text": f"URL: {url}\nHost: {host}\n",
        },
    ]

    if dom_outline:
        truncated = dom_outline[:_PERCEPTION_MAX_OUTLINE_CHARS]
        if len(dom_outline) > _PERCEPTION_MAX_OUTLINE_CHARS:
            truncated += "\n... [truncated]"
        # The dom_summarizer outputs ## SECTION headers (TITLE, HEADINGS,
        # LINKS, IMAGES, BUTTONS, INPUTS, FORMS, MEDIA, TEXT); telling the
        # LLM that helps it interpret each section correctly.
        user_parts.append(
            {
                "type": "text",
                "text": (
                    "\nPage content summary (structured extract of the body, "
                    "head/CSS/scripts removed):\n" + truncated
                ),
            },
        )

    img_bytes_len = 0
    can_use_vision = _model_is_vision_capable(tgt.model)
    if (
        screenshot_path is not None
        and screenshot_path.exists()
        and can_use_vision
    ):
        try:
            img_bytes = screenshot_path.read_bytes()
            img_bytes_len = len(img_bytes)
            b64 = base64.b64encode(img_bytes).decode("ascii")
            mime = (
                "image/jpeg"
                if screenshot_path.suffix.lower() in (".jpg", ".jpeg")
                else "image/png"
            )
            user_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
            )
        except Exception as e:
            _log.info(
                "[perception] screenshot read failed (%s: %s); proceeding without",
                type(e).__name__,
                e,
            )

    user_parts.append(
        {
            "type": "text",
            "text": "\nProduce the PerceptionResult JSON now.",
        },
    )

    # Text-only endpoints (vLLM-served Qwen3 / gpt-oss) sometimes reject
    # the multipart content array form even when all parts are type=text.
    # Collapse to a single string when we did NOT attach an image.
    user_content: object
    if img_bytes_len == 0:
        user_content = "".join(p.get("text", "") for p in user_parts)
    else:
        user_content = user_parts

    body = {
        "model": tgt.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
        # vLLM honours this for structured outputs; legacy endpoints
        # that ignore it still work because we parse defensively below.
        "response_format": {"type": "json_object"},
    }
    body = adapt_chat_body(tgt, body)

    # ---- Call the LLM ----------------------------------------------------
    t0 = time.time()
    try:
        check_engine_quota(tgt)
        await check_engine_thermal(tgt)
    except EngineQuotaExceeded as e:
        _log.info("[perception] quota gate refused: %s", e)
        return None
    except EngineThermalThrottled as e:
        _log.info("[perception] thermal gate refused: %s", e)
        return None

    try:
        # Bump GPU gauge for the duration of the httpx.post -- visible
        # in /health.vision_inference. RTX 6000 Pro Max-Q is single-batch
        # for Qwen-VL-72B at int8, so this counter approximates the GPU
        # busy state when its value is >= 1.
        from server.hub._ai_activity import track as _track
        with _VisionGauge(), _track("vision", slug=getattr(tgt, "engine_slug", "")):
            async with httpx.AsyncClient(timeout=tgt.timeout) as client:
                r = await client.post(tgt.url, json=body, headers=tgt.headers)
                if r.status_code >= 400:
                    _log.info(
                        "[perception] LLM %d from %s model=%s: %s",
                        r.status_code,
                        tgt.url,
                        tgt.model,
                        r.text[:600],
                    )
                    return None
                payload = r.json()
                record_engine_usage(tgt, payload.get("usage") or {})
    except Exception as e:
        _log.info(
            "[perception] LLM call failed: %s: %s",
            type(e).__name__,
            e,
        )
        try:
            from server.hub._ai_io_log import record_ai_io
            record_ai_io(purpose="perception",
                         engine_slug=getattr(tgt, "engine_slug", "") or tgt.model,
                         job_id=job_id, prompt="(perception brief)",
                         response=None,
                         latency_ms=int((time.time()-t0)*1000),
                         error=f"{type(e).__name__}: {e}")
        except Exception: pass
        return None
    elapsed_ms = int((time.time() - t0) * 1000)

    # ---- Extract raw content --------------------------------------------
    choices = payload.get("choices") or []
    if not choices:
        _log.info("[perception] LLM returned no choices: %s", payload)
        return None
    msg = choices[0].get("message") or {}
    raw = msg.get("content") or ""
    try:
        from server.hub._ai_io_log import record_ai_io
        _u = payload.get("usage") or {}
        record_ai_io(purpose="perception",
                     engine_slug=getattr(tgt, "engine_slug", "") or tgt.model,
                     job_id=job_id, prompt="(perception brief)",
                     response=raw, latency_ms=elapsed_ms,
                     tokens_in=_u.get("prompt_tokens"),
                     tokens_out=_u.get("completion_tokens"))
    except Exception: pass
    if not raw.strip():
        _log.info("[perception] LLM returned empty content")
        return None

    parsed = _parse_json_lenient(raw)
    if parsed is None:
        _log.info(
            "[perception] could not parse JSON from response (raw[:200]=%r)",
            raw[:200],
        )
        return None

    # ---- Inject system-controlled fields & validate ---------------------
    parsed["url"] = url
    parsed["host"] = host
    parsed["step_index"] = step_index
    parsed["model"] = payload.get("model") or tgt.model
    parsed["duration_ms"] = elapsed_ms

    try:
        result = PerceptionResult.model_validate(parsed)
    except Exception as e:
        _log.info(
            "[perception] schema validation failed (%s: %s); raw=%r",
            type(e).__name__,
            e,
            raw[:400],
        )
        return None

    _log.info(
        "[perception] %s page_kind=%s confidence=%.2f barriers=%d images=%d videos=%d (img_bytes=%d, dom_chars=%d, %dms)",
        host,
        result.page_kind.value,
        result.page_kind.confidence,
        len(result.barriers),
        result.content.images_count,
        len(result.content.videos),
        img_bytes_len,
        len(dom_outline or ""),
        elapsed_ms,
    )
    return result


# ---------------------------------------------------------------------------
# JSON parsing -- defensive against ```json fences and extra prose.
# ---------------------------------------------------------------------------


async def save_perception_for_job(
    *,
    job_id: str,
    url: str,
    data_dir: Path,
    log: object = None,
    mode: str | None = None,
    success: bool | None = None,
) -> PerceptionResult | None:
    """Generate end-of-job PerceptionResult and save to ``data/jobs/{id}/perception.json``.

    Phase 1 integration helper: call this from job completion paths
    (both codegen-loop and fetch). Looks for whatever artifacts the job
    already produced -- ``page.html`` (always), ``screenshots/*`` (codegen-
    loop only) -- and feeds them to ``generate_perception()``.

    Safe to fail: any error is logged at info level and None is returned.
    The surrounding job is unaffected.

    Args:
        job_id: The job identifier.
        url: The page URL the job operated on.
        data_dir: Hub's ``config.data_dir`` (typically ``/data/jobs``).
        log: Optional callable ``(line: str) -> None`` for streaming job
            logs. Defaults to module logger if None.
        mode: The job's mode ("fetch" / "codegen-loop" / "rerun"). Used by
            the sampling gate (PAPRIKA_PERCEPTION_FETCH_SUCCESS_SKIP) to
            decide whether to skip generation. Optional for back-compat;
            when omitted, only the rate-based sampler can skip.
        success: Whether the job succeeded (status==completed). Used by
            the success-skip gate above. Optional.
    """
    # GPU throughput gate -- ぱっぷす環境では Qwen-VL 1 枚を 24 ライン
    # で奪い合うので、ここで一部 skip して GPU を空けることが効く。
    # 設定は env (PAPRIKA_PERCEPTION_SAMPLE_RATE /
    # PAPRIKA_PERCEPTION_FETCH_SUCCESS_SKIP) で operator が回せる。
    should_run, skip_reason = _should_generate_perception(
        job_id=job_id, mode=mode, success=success
    )
    if not should_run:
        if callable(log):
            try:
                log(f"  📸 perception: skipped ({skip_reason})")
            except Exception:
                pass
        _log.debug(
            "[perception] skip job=%s reason=%s mode=%s success=%s",
            job_id, skip_reason, mode, success,
        )
        return None

    workdir = data_dir / job_id

    # Find latest screenshot (codegen-loop saves per-attempt screenshots;
    # fetch mode usually has none -- that's OK, the eye copes with text-only).
    screenshot_path: Path | None = None
    shots_dir = workdir / "screenshots"
    if shots_dir.is_dir():
        # Sort by name (timestamp / attempt number); take the last one.
        candidates: list[Path] = []
        for ext in ("*.jpg", "*.jpeg", "*.png"):
            candidates.extend(shots_dir.glob(ext))
        if candidates:
            screenshot_path = sorted(candidates)[-1]

    # Fetch jobs don't take screenshots but might leave attempt_*.jpg at
    # the workdir root; check there too.
    if screenshot_path is None:
        roots: list[Path] = []
        for ext in ("attempt_*.jpg", "attempt_*.png", "final.jpg", "final.png"):
            roots.extend(workdir.glob(ext))
        if roots:
            screenshot_path = sorted(roots)[-1]

    # DOM outline: read page.html and run it through the dom_summarizer so
    # we feed the eye a body-focused text view instead of head+CSS noise.
    # Phase 1 testing on 20 hosts showed 11/20 had "truncated_dom" anomalies
    # because the raw HTML's head+CSS ate the entire 16k cap. The summarizer
    # produces ~4-10x denser, body-focused content.
    dom_outline: str | None = None
    page_html = workdir / "page.html"
    if page_html.is_file():
        try:
            raw_html = page_html.read_text(encoding="utf-8", errors="replace")
            from core.dom_summarizer import summarize
            dom_outline = summarize(raw_html, max_chars=_PERCEPTION_MAX_OUTLINE_CHARS)
            if not dom_outline:
                # Summarizer produced nothing (parser failure / empty body).
                # Fall back to raw HTML so the eye still has *something*.
                dom_outline = raw_html
        except Exception as e:
            _log.info(
                "[perception] could not read/summarize page.html for %s: %s",
                job_id,
                e,
            )

    if screenshot_path is None and not dom_outline:
        # Nothing to perceive. Quietly skip.
        return None

    try:
        # Prefer a registered vision engine (the Qwen3-VL boxes) with thermal
        # failover; None falls back to the env-default endpoint inside
        # generate_perception.
        _vt = await find_vision_capable_target()
        result = await generate_perception(
            url=url,
            screenshot_path=screenshot_path,
            dom_outline=dom_outline,
            step_index=0,  # 0 = end-of-job snapshot
            target=_vt,
        )
    except Exception as e:
        _log.info(
            "[perception] generate_perception crashed for %s: %s: %s",
            job_id,
            type(e).__name__,
            e,
        )
        return None

    if result is None:
        return None

    # Persist alongside other job artifacts.
    out_path = workdir / "perception.json"
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            result.model_dump_json(indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        _log.info(
            "[perception] could not write perception.json for %s: %s",
            job_id,
            e,
        )
        # Result is still useful as a return value even if persistence failed.

    try:
        from server.hub._ai_activity import record_event
        from server.hub.distiller_light import host_from_url as _hfu
        record_event(
            "perceive",
            f"{result.page_kind.value} · barriers={len(result.barriers)}",
            host=_hfu(url) or "",
            job_id=job_id,
        )
    except Exception:
        pass

    # C: Vision -> URL-role feedback. When the vision LLM confidently says
    # this is a video page (which the fetcher's video_detection / yt-dlp
    # signals may have missed -- blob/MSE players, lazy iframes, paywalled
    # previews), record it as video evidence into the per-host URL-role
    # table. Next time the same URL template is fetched, role_for_url()
    # returns ``detail`` at 0.95 conf even without re-running perception,
    # so the escalation gate + future routing decisions get smarter. Pure
    # in-process / fire-and-forget; env kill-switch.
    try:
        import os as _os
        if (_os.environ.get("PAPRIKA_PERCEPTION_TO_URL_FEEDBACK", "1") or "1").strip().lower() not in ("0", "false", "no", "off"):
            pk = getattr(result, "page_kind", None)
            if pk is not None and getattr(pk, "value", "") == "video_page" and float(getattr(pk, "confidence", 0.0) or 0.0) >= 0.7:
                from server.hub._page_role import record_url as _record_url
                _record_url(url, has_video_evidence=True)
    except Exception:
        pass

    # Surface a one-line summary in the job log if a logger was provided.
    if callable(log):
        try:
            log(
                f"  📸 perception: page_kind={result.page_kind.value} "
                f"(confidence {result.page_kind.confidence:.2f}) "
                f"barriers={len(result.barriers)} "
                f"images={result.content.images_count} "
                f"videos={len(result.content.videos)} "
                f"({result.duration_ms or 0}ms)",
            )
        except Exception:
            pass

    return result


async def generate_perception_for_attempt(
    *,
    job_id: str,
    attempt_n: int,
    url: str,
    data_dir: Path,
    target: LLMTarget | None = None,
) -> PerceptionResult | None:
    """Generate a PerceptionResult for a single codegen-loop attempt.

    Phase 4 helper. Reads the attempt's final screenshot from
    ``data/jobs/{job_id}/attempts/{n}/final_screenshot.jpg`` and feeds
    it to ``generate_perception()``.  Returns None if no screenshot
    exists for that attempt (no fallback to DOM -- codegen-loop attempts
    don't save page.html).

    Pass ``target=`` to force a specific (vision-capable) engine; without
    it falls back to ``_default_target()`` whose model may be text-only,
    in which case the eye reasons from URL/host alone -- low signal.

    Unlike ``save_perception_for_job()``, this DOES NOT write to disk;
    the caller decides what to do with the result (typically: pass to
    ``judge_via_r1()`` and save next to ``judge.json``).
    """
    workdir = data_dir / job_id
    att_dir = workdir / "attempts" / str(attempt_n)
    screenshot_path: Path | None = None
    for cand in (att_dir / "final_screenshot.jpg", att_dir / "final_screenshot.png"):
        if cand.is_file():
            screenshot_path = cand
            break
    if screenshot_path is None:
        _log.info(
            "[perception:attempt] no screenshot for job=%s attempt=%s",
            job_id,
            attempt_n,
        )
        return None

    try:
        result = await generate_perception(
            url=url,
            screenshot_path=screenshot_path,
            dom_outline=None,
            step_index=attempt_n,
            target=target,
        )
    except Exception as e:
        _log.info(
            "[perception:attempt] generate_perception crashed for %s/%s: %s: %s",
            job_id,
            attempt_n,
            type(e).__name__,
            e,
        )
        return None
    return result


async def find_vision_capable_target() -> LLMTarget | None:
    """Resolve a vision-capable engine target for perception, preferring one
    that is thermally ACCEPTING.

    Two-stage selection:

      1. **Operator tiers** (Settings ``vision_engine_order``, parsed by
         :func:`server.hub._roles.role_order_tiers` -- ``|`` = same-tier
         load-balanced, ``,`` = ranked fallback). For each tier in order:
         round-robin the engines (per-hub counter, so two co-equal Qwen3-VL
         boxes alternate instead of always hitting the alphabetically-first
         one), then thermally fail over within the rotation.

      2. **Legacy fallback** (no setting / no tier accepts): any unlisted
         vision-capable engine, ordered local-GPU-first / cloud-last /
         promoted-first / slug. Single-engine tier == no rotation; the
         operator opts into load-balancing by grouping engines with ``|``.

    Returns ``None`` when EVERY vision engine is throttled or disabled --
    the caller falls back to text-only perception (env default).
    """
    try:
        from server.hub._state import state as _st
        from server.hub.codegen import resolve_engine_target
        from server.hub import thermal
        from server.hub._roles import role_order_tiers, rr_rotate
    except Exception:
        return None
    if _st.engines is None:
        return None
    try:
        recs = _st.engines.list_all()
    except Exception:
        return None
    vis = [r for r in recs if _model_is_vision_capable(getattr(r, "model", "") or "")]
    if not vis:
        return None

    by_slug = {getattr(r, "slug", "") or "": r for r in vis}

    # Stage 1: operator tiers (round-robin within each tier).
    tiers = role_order_tiers("vision")
    listed_slugs: set[str] = set()
    for tier_idx, tier_slugs in enumerate(tiers):
        listed_slugs.update(tier_slugs)
        rr_key = f"vision#{tier_idx}#{','.join(sorted(tier_slugs))}"
        rotated = rr_rotate(tier_slugs, rr_key)
        tier_recs = [by_slug[s] for s in rotated if s in by_slug]
        if not tier_recs:
            continue
        chosen = await thermal.first_accepting(tier_recs)
        if chosen is not None:
            try:
                return resolve_engine_target(getattr(chosen, "slug", "") or "", _st.engines)
            except Exception:
                return None

    # Stage 2: legacy fallback for unlisted vision engines (local-first,
    # cloud-last, promoted-first, slug). Not round-robined -- operator
    # opts into load-balancing by listing engines in the role panel.
    def _vis_is_cloud(r):
        return not (
            str(getattr(r, "gpu_temp_url", "") or "").strip()
            or float(getattr(r, "gpu_temp_stop_c", 0) or 0) > 0
        )
    unlisted = [r for r in vis if (getattr(r, "slug", "") or "") not in listed_slugs]
    unlisted.sort(key=lambda r: (
        _vis_is_cloud(r),
        not getattr(r, "promoted", False),
        getattr(r, "slug", ""),
    ))
    if not unlisted:
        return None
    chosen = await thermal.first_accepting(unlisted)
    if chosen is None:
        return None
    try:
        return resolve_engine_target(getattr(chosen, "slug", "") or "", _st.engines)
    except Exception:
        return None


def _parse_json_lenient(text: str) -> dict | None:
    """Try to extract a JSON object from a model response.

    Handles three common patterns:
      1. Plain JSON: ``{"foo": ...}``
      2. Fenced JSON: ````json\\n{...}\\n````
      3. JSON with leading/trailing prose: ``Here is the result: {...}``
    """
    s = text.strip()

    # Strip Markdown fences if present.
    if s.startswith("```"):
        # Find the first newline (drops ``` or ```json line) and the last fence.
        first_nl = s.find("\n")
        last_fence = s.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            s = s[first_nl + 1:last_fence].strip()

    # Fast path: clean JSON.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Slow path: find the first { ... last } pair.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = s[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None
