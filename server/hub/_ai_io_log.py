"""Central AI I/O logger -- record every LLM call's (prompt, response, meta).

Why: paprika makes 6-10 LLM calls per codegen-loop job (planner, retrieval,
codegen x N, judge, then 3 distillers) split across many helpers
(codegen.py, planner_llm, judge_llm, skill_llm, convention_llm, distiller_r1,
perception_llm). The job log shows the final scripts and verdicts but NOT
the prompts; the distillers' I/O is loguru-only and never persisted. So an
operator can't reconstruct "for THIS job, what did each LLM see and answer?"
-- the whole AI loop is observed only at its endpoints.

This module is the single sink: every LLM helper calls ``record_ai_io(...)``
right after its own ``_chat()`` returns. We:

  * redact obvious secrets (cookies / api keys / login goals) from prompt+
    response before storing,
  * write a per-day, per-job JSONL line for fast grep (Phase 1),
  * upsert a row in MariaDB ``ai_io_log`` for SQL queries (Phase 2),
  * offload prompts / responses larger than ``_INLINE_MAX`` to MinIO and
    keep only the object key inline (Phase 2),
  * surface filters / aggregates over GET /ai/io for the admin UI (Phase 3).

The writer is fire-and-forget (``asyncio.create_task``) so a slow MariaDB
or MinIO blip never blocks the hot LLM call path. Kill-switch via
``PAPRIKA_AI_IO_LOG_DISABLE=1`` or Settings ``ai_io_log_enabled=false``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Recognised purpose strings -- each LLM helper passes its own, so the UI
# can filter "show me every planner call for job X". Free-form strings are
# allowed but stick to this set for cross-helper consistency.
PURPOSES = (
    "planner",
    "skill_retrieval",
    "codegen",
    "judge",
    "skill_distill",
    "convention_distill",
    "reasoning_distill",
    "perception",
    "other",
)

# Inline limit for prompt / response text columns in MariaDB. Anything
# bigger gets sha1-keyed and shipped to MinIO; only the key sits in the row.
# MEDIUMTEXT can hold 16 MB so this is purely a "don't bloat the row" cap.
_INLINE_MAX = 32 * 1024

# JSONL file rotation: per-day, per-job. One file per job keeps grep cheap
# and lets the admin UI pull a single jsonl when looking at a job tree.
_JSONL_SUBDIR = "ai_io"


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Cookie headers ("cookie: foo=bar; baz=qux") and bearer tokens are the
# obvious leaks. login_goal prompts include credentials inline. Pattern set
# is conservative (false negatives over false positives -- a missed redaction
# is worse than over-masking).
_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Set-Cookie / Cookie headers (whole header line)
    (re.compile(r"(?im)^\s*(set-)?cookie\s*:\s*.*$"), r"\g<0>"),  # placeholder; replaced below
    # bearer tokens / Authorization headers
    (re.compile(r"(?i)\bauthorization\s*:\s*\S+"), "authorization: [REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "bearer [REDACTED]"),
    # cookie KV pairs inside text (e.g. injected into CDP install logs)
    (re.compile(r"\b(set-cookie|cookie)\b\s*[:=]\s*[^\s;,]+", re.I), r"\1: [REDACTED]"),
    # API keys / secrets in env-style assignments
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*[^\s,;]+"),
     r"\1: [REDACTED]"),
    # sk-... openai-shaped keys, ds-... deepseek-shaped, etc.
    (re.compile(r"\b(sk|ds|sess|tok|tk)-[A-Za-z0-9_\-]{12,}\b"), "[REDACTED_KEY]"),
]


def _redact(text: str | None) -> str:
    """Mask obvious secrets in ``text`` before persisting. Returns "" for
    None / non-str. Best-effort: false negatives are acceptable; we never
    raise so a redaction bug can't break logging."""
    if not text:
        return ""
    try:
        s = str(text)
    except Exception:
        return ""
    try:
        # cookie header line: replace whole-line value with masked
        s = re.sub(
            r"(?im)^(\s*(?:set-)?cookie\s*:\s*).*$",
            r"\1[REDACTED]", s,
        )
        for pat, repl in _REDACT_PATTERNS[1:]:
            s = pat.sub(repl, s)
    except Exception:
        return s
    return s


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    """Settings (live) -> env -> default ON. Stays ON unless the operator
    flipped Settings ``ai_io_log_enabled=False`` or set the env disable."""
    if os.environ.get("PAPRIKA_AI_IO_LOG_DISABLE", "").lower() in ("1", "true", "yes", "on"):
        return False
    try:
        from server.hub._state import state
        if state.settings is not None:
            v = state.settings.get("ai_io_log_enabled", None)
            if v is not None:
                return bool(v)
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _today_dir() -> Path:
    """Today's per-day directory under storage_dir/ai_io/YYYY-MM-DD/. Created
    lazily so the first call per day pays the mkdir cost and the rest are
    cheap appends."""
    from server.hub._state import get_storage_dir
    base = get_storage_dir() / _JSONL_SUBDIR / datetime.utcnow().strftime("%Y-%m-%d")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _jsonl_path(job_id: str | None) -> Path:
    """Per-job JSONL file. Falls back to ``_misc.jsonl`` when there's no
    job_id (rare -- some helpers run outside any job scope)."""
    name = (job_id or "_misc").replace("/", "_")[:64]
    return _today_dir() / f"{name}.jsonl"


def _truncate_for_inline(s: str) -> tuple[str, str | None]:
    """Return (inline_text, sha1_key_for_offload).

    Short content rides inline; long content sits in MinIO under
    ai_io/<sha1>.bin and the row keeps only the sha1 key. The inline text
    in the offload case is the first ``_INLINE_MAX`` bytes plus an explicit
    "[truncated]" marker so a quick UI look still shows context."""
    if not s:
        return "", None
    if len(s) <= _INLINE_MAX:
        return s, None
    h = hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()
    preview = s[: _INLINE_MAX] + f"\n\n[truncated -- full {len(s)} bytes at ai_io/{h}.bin]"
    return preview, h


async def _offload_to_minio(sha1: str, text: str) -> None:
    """Persist long content under ``ai_io/<sha1>.bin`` via the existing
    objstore mirror. Idempotent at the object-store layer (same key = same
    bytes). Best-effort: a MinIO outage leaves only the row's inline preview
    and that's still useful enough for the UI."""
    try:
        from server.hub import objstore
        # Reuse the same data-dir-staged + mirror_file path the asset
        # uploader uses; gives us automatic dormant-when-disabled semantics
        # via PAPRIKA_S3_ENABLED.
        from server.hub._state import get_storage_dir
        staged = get_storage_dir() / _JSONL_SUBDIR / "_blobs" / f"{sha1}.bin"
        staged.parent.mkdir(parents=True, exist_ok=True)
        if not staged.exists():
            staged.write_text(text, encoding="utf-8", errors="replace")
        await objstore.mirror_file(staged)
    except Exception as e:
        _log.debug("ai_io offload (%s) failed: %s", sha1, e)


async def _persist_mariadb(row: dict) -> None:
    """INSERT one row into ai_io_log. Best-effort: any error swallowed so a
    transient MariaDB hiccup never blocks the LLM hot path."""
    try:
        from server.hub._state import state
        pool = getattr(state, "mariadb_pool", None)
        if pool is None:
            return
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO ai_io_log
                       (ts, job_id, purpose, engine_slug, parent_call,
                        prompt_len, response_len, tokens_in, tokens_out,
                        latency_ms, prompt_text, response_text,
                        prompt_ref, response_ref, error)
                       VALUES (CURRENT_TIMESTAMP(3),%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        row.get("job_id"),
                        row.get("purpose"),
                        row.get("engine_slug"),
                        row.get("parent_call"),
                        row.get("prompt_len"),
                        row.get("response_len"),
                        row.get("tokens_in"),
                        row.get("tokens_out"),
                        row.get("latency_ms"),
                        row.get("prompt_text") or "",
                        row.get("response_text") or "",
                        row.get("prompt_ref"),
                        row.get("response_ref"),
                        row.get("error"),
                    ),
                )
    except Exception as e:
        _log.debug("ai_io mariadb insert failed: %s", e)


def _append_jsonl(path: Path, row: dict) -> None:
    """Append one JSON line. Synchronous + tiny; called inside the async
    writer task so the LLM hot path isn't touched."""
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        _log.debug("ai_io jsonl append failed: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def _write(row: dict) -> None:
    """Fan-out writer: JSONL + MariaDB + optional MinIO offload. Runs as the
    background task spawned by ``record_ai_io``; never raises."""
    try:
        path = _jsonl_path(row.get("job_id"))
        _append_jsonl(path, row)
    except Exception:
        pass
    pref = row.pop("_prompt_full_for_offload", None)
    rref = row.pop("_response_full_for_offload", None)
    if pref and row.get("prompt_ref"):
        await _offload_to_minio(row["prompt_ref"], pref)
    if rref and row.get("response_ref"):
        await _offload_to_minio(row["response_ref"], rref)
    await _persist_mariadb(row)


def record_ai_io(
    *,
    purpose: str,
    engine_slug: str | None,
    job_id: str | None,
    prompt: str,
    response: str | None,
    latency_ms: int = 0,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    error: str | None = None,
    parent_call: int | None = None,
    extra: dict | None = None,
) -> None:
    """Record one LLM call. Fire-and-forget; never raises.

    Call this from every LLM helper RIGHT AFTER its ``_chat()`` returns
    (success OR error path), with ``response=None`` + ``error="..."`` for
    failures. Long prompts/responses are auto-offloaded to MinIO via sha1.
    """
    if not _enabled():
        return
    try:
        p_red = _redact(prompt)
        r_red = _redact(response)
        p_inline, p_ref = _truncate_for_inline(p_red)
        r_inline, r_ref = _truncate_for_inline(r_red)
        row: dict[str, Any] = {
            "ts": time.time(),
            "purpose": (purpose or "other")[:32],
            "engine_slug": (engine_slug or "")[:64],
            "job_id": (job_id or "")[:64] or None,
            "parent_call": parent_call,
            "prompt_len": len(p_red),
            "response_len": len(r_red),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": int(latency_ms or 0),
            "prompt_text": p_inline,
            "response_text": r_inline,
            "prompt_ref": p_ref,
            "response_ref": r_ref,
            "error": (error or None),
        }
        if extra:
            row["extra"] = extra
        # Stash full text for the offload (the row itself only keeps the
        # truncated preview); the writer pops these before insert.
        if p_ref:
            row["_prompt_full_for_offload"] = p_red
        if r_ref:
            row["_response_full_for_offload"] = r_red
        asyncio.create_task(_write(row))
    except Exception as e:
        _log.debug("ai_io record_ai_io failed: %s", e)
