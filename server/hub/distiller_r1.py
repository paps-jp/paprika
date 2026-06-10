"""R1 Distiller — deep HostKnowledge updates from job outcomes.

v2 Phase 6 of the architecture (see ``internal/v2-architecture.html``).

The light distiller (``distiller_light.py``) bumps numeric stats after
every job — total_jobs / success_rate / maturity tier — without an LLM.
This module is the heavier layer that runs on demand: it sends the job's
factual brief (PerceptionResult + judge verdict + script + stdout tail)
to DeepSeek-R1 and asks for **structured KnowledgeUpdates** to apply to
``HostKnowledge``.

Output is intentionally narrow: R1 may only propose
  * adding / updating ``per_page.barriers.<kind>``,
  * adding ``per_page.content_extraction[*]``,
  * adjusting ``per_page.navigation_hints.*``,
  * appending ``per_page.navigation_hints.common_observations``.

R1 must NOT rewrite stats, provenance, or schema_version.  Updates that
fall outside the allowed paths are filtered out before write so a
hallucinating model can't corrupt the registry.  Every applied update
appends an entry to the host's ``history.jsonl``.

Gating
======

PAPRIKA_R1_DISTILLER_MODE controls when R1 runs:

  ``off``    -- never (default; matches Phase 1-5 behaviour exactly).
  ``on``     -- after every completed job.  Most expensive setting.
  ``new``    -- only when the host has a new barrier in the PerceptionResult
                that isn't already in HostKnowledge, or when the job
                FAILED (a failure usually carries the most learning
                signal).  This is the recommended steady-state setting.

PAPRIKA_R1_DISTILLER_ENGINE chooses the engine slug (default "deepseek-r1").
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from server.hub.codegen import (
    LLMTarget,
    adapt_chat_body,
    check_engine_quota,
    record_engine_usage,
    EngineQuotaExceeded,
    resolve_engine_target,
)


_log = logging.getLogger(__name__)


_R1_DISTILLER_SYSTEM_PROMPT = """You are the DISTILLER for paprika browser automation (v2).

You receive a structured brief from one completed job: the GOAL, the
job's OUTCOME, the eye's PERCEPTION of the final page, and the CURRENT
HOSTKNOWLEDGE for the target host. Your job is to propose narrow,
evidence-supported updates to HostKnowledge so future jobs benefit.

ALLOWED UPDATE PATHS (everything else is silently dropped)
  per_page.barriers.<kind>                       e.g. per_page.barriers.age_gate
  per_page.content_extraction[append]            (append-only -- write a new entry)
  per_page.navigation_hints.lazy_load_trigger_needed
  per_page.navigation_hints.wait_after_load_ms
  per_page.navigation_hints.popup_policy
  per_page.navigation_hints.common_observations[append]

BarrierKnowledge fields you may set inside per_page.barriers.<kind>:
  present:        bool   (false = "we looked and didn't find it")
  strategy:       Strategy object (kind=click|tool|sequence|manual|passive_capture)
  confidence:     float in [0,1]
  notes:          short string
  subtype:        finer classification of the barrier. Conventional values:
                    - cloudflare_challenge: "js_challenge" | "turnstile" |
                                            "managed_challenge" | "ip_banned"
                    - age_gate / others: freeform short tag
  suggested_tool: which plugin to pre-flight before this host's jobs.
                    Currently installed tools: "paprika-flare" (real-Chrome CF bypass
                    via Worker session, IP-matched cookies; good for js_challenge
                    and turnstile), "paprika-proxy-fetch" (proxied fetch for
                    ip_banned hosts). Leave null if no plugin fits.
  tool_params:    dict passed verbatim to the plugin. For paprika-flare:
                    {"wait_s": 10, "use_vision_agent": true, "use_profile": "<slug>"}
                    For paprika-proxy-fetch: {"proxy": "<url>", "headers": {...}}.

OUTPUT SCHEMA (strict)
{
  "updates": [
    {
      "path":      "per_page.barriers.age_gate",
      "set":       { "present": true, "strategy": {"kind":"click","selector":"#age-yes"}, "confidence": 0.8 },
      "rationale": "PERCEPTION.barriers includes age_gate with actionable selector #age-yes"
    },
    {
      "path":      "per_page.barriers.cloudflare_challenge",
      "set":       { "present": true, "subtype": "turnstile",
                     "suggested_tool": "paprika-flare",
                     "tool_params": {"wait_s": 12, "use_vision_agent": true},
                     "confidence": 0.85,
                     "notes": "PaprikaFlare succeeded with vision agent click" },
      "rationale": "STDOUT: '☁ paprika-flare: fetched cf_clearance after vision click'"
    },
    {
      "path":      "per_page.content_extraction[append]",
      "set":       { "url_pattern": "/video/*", "page_kind": "video_page",
                     "strategy": {"kind":"tool","tool":"yt-dlp"}, "notes": "stdout shows mp4 download succeeded" },
      "rationale": "STDOUT: '[paprika] yt-dlp ok, 4.7 MB'"
    }
  ]
}

If the job was routine and CURRENT HOSTKNOWLEDGE already accurately
describes what was observed, output {"updates": []}.

STRICT RULES
1. Output a SINGLE JSON object. No markdown fences, no prose, no
   trailing commentary. You may include a leading <think>...</think>
   block; the system strips it.
2. Every update MUST cite evidence in the brief (PERCEPTION fact,
   STDOUT line, OUTCOME signal). Do not invent.
3. Do NOT touch stats / provenance / schema_version / created_at /
   updated_at -- those are managed elsewhere.
4. Be conservative. If unsure, omit the update.
"""


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def _strip_think_block(raw: str) -> str:
    if "<think>" not in raw:
        return raw
    return re.sub(r"<think>[\s\S]*?</think>\s*", "", raw, count=1)


def _parse_updates(raw: str) -> list[dict]:
    """Parse R1's response into a list of update proposals.

    Returns [] on any parse error (silently treat as "no updates").
    """
    if not raw:
        return []
    stripped = _strip_think_block(raw).strip()
    if stripped.startswith("```"):
        first = stripped.find("\n")
        last = stripped.rfind("```")
        if first != -1 and last > first:
            stripped = stripped[first + 1:last].strip()
    try:
        d = json.loads(stripped)
    except Exception:
        # Try first {...} block.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            return []
        try:
            d = json.loads(stripped[start:end + 1])
        except Exception:
            return []
    if not isinstance(d, dict):
        return []
    updates = d.get("updates")
    return updates if isinstance(updates, list) else []


_ALLOWED_PATHS = frozenset({
    # Barriers
    "per_page.barriers.cloudflare_challenge",
    "per_page.barriers.age_gate",
    "per_page.barriers.login_wall",
    "per_page.barriers.cookie_banner",
    "per_page.barriers.captcha",
    "per_page.barriers.paywall",
    "per_page.barriers.region_block",
    "per_page.barriers.popup_overlay",
    # Navigation hints
    "per_page.navigation_hints.lazy_load_trigger_needed",
    "per_page.navigation_hints.wait_after_load_ms",
    "per_page.navigation_hints.popup_policy",
    "per_page.navigation_hints.common_observations[append]",
    # Content extraction (append-only)
    "per_page.content_extraction[append]",
})


def _walk(obj: Any, path: list[str]) -> Any:
    """Navigate ``obj`` along ``path`` (list of keys). Returns None if any step fails."""
    for k in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _set_at(obj: dict, path: list[str], value: Any) -> bool:
    """Set ``value`` at ``path`` inside ``obj``, creating intermediate dicts.
    Returns True on success."""
    if not path:
        return False
    cur = obj
    for k in path[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[path[-1]] = value
    return True


def _append_at(obj: dict, path: list[str], value: Any) -> bool:
    """Append ``value`` to the list at ``path`` (creating it if missing)."""
    if not path:
        return False
    cur = obj
    for k in path[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    lst = cur.get(path[-1])
    if not isinstance(lst, list):
        lst = []
        cur[path[-1]] = lst
    lst.append(value)
    return True


def apply_updates_to_knowledge(
    *,
    knowledge: dict,
    updates: list[dict],
) -> tuple[dict, list[str]]:
    """Apply the validated subset of ``updates`` to ``knowledge`` in place.

    Returns ``(new_knowledge, applied_paths)`` -- a copy of knowledge with
    updates applied, plus the list of paths that survived validation.
    Updates targeting paths outside ``_ALLOWED_PATHS`` are silently
    dropped (logged at info level).
    """
    out = json.loads(json.dumps(knowledge))  # deep copy via JSON
    applied: list[str] = []
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        path = upd.get("path") or ""
        value = upd.get("set")
        if path not in _ALLOWED_PATHS:
            _log.info("[distiller-r1] dropping update for disallowed path: %s", path)
            continue
        if path.endswith("[append]"):
            real_path = path[:-len("[append]")].split(".")
            if _append_at(out, real_path, value):
                applied.append(path)
        else:
            real_path = path.split(".")
            if _set_at(out, real_path, value):
                applied.append(path)
    return out, applied


# ---------------------------------------------------------------------------
# Brief builder (compact human-readable summary for R1)
# ---------------------------------------------------------------------------


def _build_brief(
    *,
    host: str,
    goal: str,
    success: bool,
    error: str,
    perception: dict | None,
    stdout_tail: str,
    stderr_tail: str,
    script: str,
    current_knowledge: dict,
) -> str:
    parts: list[str] = []
    parts.append(f"HOST\n{host}\n")

    parts.append(
        f"OUTCOME\n  status:  {'success' if success else 'failure'}\n"
        + (f"  error:   {error[:240]}\n" if error else "")
    )

    if goal:
        parts.append(f"GOAL\n{goal[:600]}\n")

    if perception:
        # Re-use judge_llm's perception formatter for consistency.
        try:
            from server.hub.judge_llm import _format_perception_brief
            parts.append("PERCEPTION\n" + _format_perception_brief(perception) + "\n")
        except Exception:
            parts.append("PERCEPTION\n" + json.dumps(perception)[:1500] + "\n")
    else:
        parts.append("PERCEPTION\n(none -- no screenshot was captured for this attempt)\n")

    if stdout_tail:
        parts.append(f"STDOUT (last lines)\n{stdout_tail[-2000:]}\n")
    if stderr_tail:
        parts.append(f"STDERR (last lines)\n{stderr_tail[-1500:]}\n")
    if script and len(script) <= 4000:
        parts.append(f"SCRIPT\n{script}\n")

    # Trim current knowledge to the per_page section -- R1 doesn't need
    # stats / provenance to decide what to update.
    per_page_only = (current_knowledge.get("per_page") or {})
    parts.append(
        "CURRENT HOSTKNOWLEDGE.per_page\n"
        + json.dumps(per_page_only, ensure_ascii=False, indent=2)[:3000]
        + "\n"
    )

    parts.append("Produce the updates JSON now.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Gating logic
# ---------------------------------------------------------------------------


def _should_run(
    *,
    mode: str,
    success: bool,
    perception: dict | None,
    current_knowledge: dict,
) -> bool:
    """Decide whether to spend an R1 call on this job."""
    if mode == "on":
        return True
    if mode != "new":
        return False  # off / unknown -- skip
    # mode == "new": run when there's likely new information to capture.
    if not success:
        return True  # failures are high-signal
    if not perception:
        return False  # no observation, nothing to learn
    barriers_seen = (perception.get("barriers") or [])
    known_barriers = set(((current_knowledge.get("per_page") or {}).get("barriers") or {}).keys())
    for b in barriers_seen:
        kind = (b or {}).get("kind")
        if kind and kind not in known_barriers:
            return True  # new barrier kind observed
    return False  # routine successful job, knowledge already accurate


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def _distiller_mode() -> str:
    """Reasoning-distiller mode: ``off`` / ``on`` / ``new``. Abstracted from
    the DeepSeek-specific name -- resolved Settings (live, cross-hub) first,
    then the new ``PAPRIKA_REASONING_DISTILLER_MODE`` env, then the legacy
    ``PAPRIKA_R1_DISTILLER_MODE`` (back-compat), then ``off``."""
    try:
        from server.hub._state import state
        if state.settings is not None:
            m = (state.settings.get("reasoning_distiller_mode", "") or "").lower().strip()
            if m:
                return m
    except Exception:
        pass
    return (
        os.environ.get("PAPRIKA_REASONING_DISTILLER_MODE")
        or os.environ.get("PAPRIKA_R1_DISTILLER_MODE")
        or "off"
    ).lower().strip()


def _distiller_engine_slug() -> str:
    """Engine slug the reasoning distiller runs on (any reasoning engine, not
    only DeepSeek-R1): Settings ``reasoning_distiller_engine`` → new env →
    legacy ``PAPRIKA_R1_DISTILLER_ENGINE`` → ``deepseek-r1``."""
    try:
        from server.hub._state import state
        if state.settings is not None:
            s = (state.settings.get("reasoning_distiller_engine", "") or "").strip()
            if s:
                return s
    except Exception:
        pass
    return (
        os.environ.get("PAPRIKA_REASONING_DISTILLER_ENGINE")
        or os.environ.get("PAPRIKA_R1_DISTILLER_ENGINE")
        or "deepseek-r1"
    )


async def distill_for_job(
    *,
    host: str,
    job_id: str,
    goal: str,
    success: bool,
    error: str,
    perception: dict | None,
    stdout_tail: str,
    stderr_tail: str,
    script: str,
    data_dir: Path,
) -> dict | None:
    """Run R1 Distiller for one job. Returns the updated HostKnowledge dict
    on success (= R1 returned >=1 valid update), or None.

    Safe to call unconditionally: respects PAPRIKA_R1_DISTILLER_MODE
    (default ``off``), returns None when disabled.
    """
    mode = _distiller_mode()
    if mode not in ("on", "new"):
        return None

    if not host:
        return None

    # Load current knowledge (the light distiller will have just updated
    # stats, so this read sees the freshest version).
    knowledge_path = data_dir / "host_knowledge" / f"{host}.json"
    try:
        current = json.loads(knowledge_path.read_text(encoding="utf-8"))
    except Exception:
        return None  # no HostKnowledge yet; skip

    if not _should_run(
        mode=mode,
        success=success,
        perception=perception,
        current_knowledge=current,
    ):
        return None

    # Resolve R1 engine.
    try:
        from server.hub._state import state
        if state.engines is None:
            return None
        engine_slug = _distiller_engine_slug()
        target: LLMTarget = resolve_engine_target(engine_slug, state.engines)
    except Exception as e:
        _log.info("[distiller-r1] engine resolve failed: %s", e)
        return None

    brief = _build_brief(
        host=host,
        goal=goal,
        success=success,
        error=error,
        perception=perception,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        script=script,
        current_knowledge=current,
    )

    body = {
        "model": target.model,
        "messages": [
            {"role": "system", "content": _R1_DISTILLER_SYSTEM_PROMPT},
            {"role": "user",   "content": brief},
        ],
        "temperature": 0.6,
        "max_tokens": 8192,
    }
    body = adapt_chat_body(target, body)

    try:
        check_engine_quota(target)
    except EngineQuotaExceeded as e:
        _log.info("[distiller-r1] quota gate refused: %s", e)
        return None

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=target.timeout) as client:
            r = await client.post(target.url, json=body, headers=target.headers)
            if r.status_code >= 400:
                _log.info(
                    "[distiller-r1] LLM %d from %s: %s",
                    r.status_code,
                    target.url,
                    r.text[:400],
                )
                return None
            payload = r.json()
            record_engine_usage(target, payload.get("usage") or {})
    except Exception as e:
        _log.info("[distiller-r1] LLM call failed: %s: %s", type(e).__name__, e)
        return None
    elapsed_ms = int((time.time() - t0) * 1000)

    choices = payload.get("choices") or []
    raw = ""
    if choices:
        msg = choices[0].get("message") or {}
        raw = msg.get("content") or ""
    updates = _parse_updates(raw)
    if not updates:
        _log.info(
            "[distiller-r1] no updates for %s (job %s, %d ms)",
            host,
            job_id,
            elapsed_ms,
        )
        return None

    new_knowledge, applied = apply_updates_to_knowledge(
        knowledge=current,
        updates=updates,
    )
    if not applied:
        _log.info(
            "[distiller-r1] R1 proposed %d updates but none passed validation (job %s)",
            len(updates),
            job_id,
        )
        return None

    # Persist updated knowledge and append history entry.
    now_iso = datetime.utcnow().isoformat()
    new_knowledge["updated_at"] = now_iso
    new_knowledge["provenance"] = {
        "last_updated_by": "distiller-r1",
        "last_updated_at": now_iso,
    }
    try:
        knowledge_path.write_text(
            json.dumps(new_knowledge, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        _log.info("[distiller-r1] write failed for %s: %s", host, e)
        return None

    # history.jsonl: one entry per applied update for traceability.
    try:
        hist_path = data_dir / "host_knowledge" / host / "history.jsonl"
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        with hist_path.open("a", encoding="utf-8") as f:
            for u in updates:
                if (u.get("path") or "") not in applied:
                    continue
                entry = {
                    "at":           now_iso,
                    "by":           "distiller-r1",
                    "trigger_job":  job_id,
                    "path":         u.get("path"),
                    "rationale":    (u.get("rationale") or "")[:300],
                    "elapsed_ms":   elapsed_ms,
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        _log.info("[distiller-r1] history append failed for %s: %s", host, e)

    _log.info(
        "[distiller-r1] %s job=%s applied %d/%d updates (%d ms): %s",
        host,
        job_id,
        len(applied),
        len(updates),
        elapsed_ms,
        ", ".join(applied),
    )
    return new_knowledge
