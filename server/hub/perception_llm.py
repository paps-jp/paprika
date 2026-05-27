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
    record_engine_usage,
    EngineQuotaExceeded,
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

# Heuristic: only send screenshot bytes when the model name hints at vision
# support. Text-only models (vLLM-served Qwen3 / DeepSeek / etc.) will OOM
# or reject multipart content.  Conservative -- err on the side of NOT
# sending the image and rely on DOM outline instead.
_VISION_MODEL_PATTERNS = ("vl", "vision", "gpt-4o", "claude-3", "gemini")


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
Report only the schema fields listed above."""


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
    except EngineQuotaExceeded as e:
        _log.info("[perception] quota gate refused: %s", e)
        return None

    try:
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
        return None
    elapsed_ms = int((time.time() - t0) * 1000)

    # ---- Extract raw content --------------------------------------------
    choices = payload.get("choices") or []
    if not choices:
        _log.info("[perception] LLM returned no choices: %s", payload)
        return None
    msg = choices[0].get("message") or {}
    raw = msg.get("content") or ""
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
    """
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
        result = await generate_perception(
            url=url,
            screenshot_path=screenshot_path,
            dom_outline=dom_outline,
            step_index=0,  # 0 = end-of-job snapshot
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


def find_vision_capable_target() -> LLMTarget | None:
    """Pick the first vision-capable engine from the registry.

    Tries common slugs in priority order. Returns None if no engine
    with a vision-capable model name is registered.  Caller can fall
    back to text-only perception by passing target=None to
    ``generate_perception_*``.
    """
    try:
        from server.hub._state import state as _st
        from server.hub.codegen import resolve_engine_target
    except Exception:
        return None
    if _st.engines is None:
        return None

    # Priority: explicitly-named v2 engines, then common vendor names.
    candidates = (
        "perception",        # explicit v2 slot, if operator registered it
        "qwen3-vl-8b",
        "qwen-vl",
        "qwen2.5-vl",
        "qwen",
        "chatgpt51",         # GPT-5.1 (vision-capable)
        "claude-vision",
    )
    for slug in candidates:
        try:
            t = resolve_engine_target(slug, _st.engines)
        except Exception:
            continue
        if t and _model_is_vision_capable(t.model):
            return t
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
