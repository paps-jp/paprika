"""LLM-driven convention distillation from failure→success diffs.

After a codegen-loop job succeeds with ``len(attempts) > 1``, the hub
hands the last failing attempt + the succeeding attempt to the LLM
and asks: "What general rule did attempt 1 violate that attempt 2
fixed?". The LLM either returns a compact convention (good/bad
example + advice) or skips.

The default is to skip. Convention extraction is meant to capture
foot-guns that come up repeatedly, not one-off site-specific tweaks.
"""

from __future__ import annotations

import os
import time

import httpx

from server.hub.codegen import CODEGEN_LLM_URL, CODEGEN_MODEL_NAME
from server.hub.skill_llm import (
    SKILL_LLM_TIMEOUT_S,
    _extract_json,
)

# Default to the same LLM as codegen / skill distillation; overridable
# so a future deployment can use a smaller / cheaper model for
# convention work (the input is small so a tiny model would suffice).
CONVENTION_DISTILL_LLM_URL = os.environ.get(
    "CONVENTION_DISTILL_LLM_URL",
    CODEGEN_LLM_URL,
).rstrip("/")
CONVENTION_DISTILL_MODEL_NAME = os.environ.get(
    "CONVENTION_DISTILL_MODEL_NAME",
    CODEGEN_MODEL_NAME,
)
CONVENTION_AUTO_EXTRACT_ENABLED = os.environ.get(
    "CONVENTION_AUTO_EXTRACT_ENABLED", "true"
).lower() in ("1", "true", "yes", "on")


_DISTILL_SYSTEM = """\
You are reviewing a codegen-loop retry: attempt N FAILED, attempt N+1
SUCCEEDED with mostly the same script structure. Decide whether the
fix encodes a REUSABLE rule that future LLM-generated paprika-client
scripts should follow, or whether the fix was site-specific noise
that wouldn't help anyone else.

DEFAULT TO SKIP. Only emit a convention when the rule is:
  - General (would apply across many sites / many tasks)
  - About paprika-client API usage, async/await, control flow, or
    ordering of operations
  - Atomic (one rule, one foot-gun -- not a sweeping refactor)
  - The kind of thing a code reviewer would flag in any paprika
    script, not just this site

Skip when:
  - The fix was a one-character typo or rename
  - The fix swapped one CSS selector or URL for another (site-specific)
  - The fix is essentially "add an import that was already in the
    reference" (the LLM just forgot stdlib, no general lesson)
  - The "failure" was a network blip / sandbox timeout / external
    service flake -- not the script's fault
  - Attempt 2 is a near-total rewrite (no clean diff to learn from)
  - A similar convention is likely already present in the prompt
    (you'll see them in the "Local conventions" block of the system
    prompt; do not duplicate)

Output JSON ONLY (no markdown fences, no commentary). Two shapes:

  {"skip": true, "reason": "one short sentence"}

  {"skip": false,
   "slug":             <kebab-case, <= 60 chars>,
   "name":             <Human-readable short title, <= 60 chars>,
   "advice":           <One imperative sentence: "Always X" / "Never Y" / "When Z, do W">,
   "rationale":        <One sentence: WHY the rule exists, the failure mode it prevents>,
   "bad_example":      <1-5 lines of Python showing what NOT to do, copy-paste-ready>,
   "good_example":     <1-5 lines of Python showing the corrected form>,
   "applicable_when":  [<optional bullet conditions like "calling page.state()">],
   "tags":             [<short kebab tags like "async", "page-state", "ordering">]}

CONCRETE EXAMPLE (do NOT echo verbatim -- produce one tailored to the
actual diff):

  {"skip": false,
   "slug": "await-and-index-page-state",
   "name": "Await page.state() before indexing",
   "advice": "Always assign 'await page.state()' to a variable, or wrap in parentheses, before indexing -- never write 'await page.state()[\\"url\\"]' (the await binds only to page.state(), leaving you indexing a coroutine).",
   "rationale": "page.state() returns a coroutine; without wrapping the await, the index operation fires on the coroutine and raises TypeError: 'coroutine' object is not subscriptable. The mistake silently passes static checks because await IS present somewhere on the line.",
   "bad_example": "if urllib.parse.urlparse(await page.state()['url']).netloc != base_domain:\\n    ...",
   "good_example": "state = await page.state()\\nif urllib.parse.urlparse(state['url']).netloc != base_domain:\\n    ...",
   "applicable_when": ["calling page.state() / page.outline() / page.visited_urls() inline"],
   "tags": ["async", "page-state", "common-typo"]}

RULES:
1. JSON must be valid (parseable by json.loads).
2. Escape newlines as \\n, quotes as \\".
3. advice MUST be imperative; rationale MUST explain the failure mode.
4. bad_example MUST mirror what the failing attempt actually did.
5. Do not wrap the JSON in fences or prose.
"""


async def _chat(
    *,
    url: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 1500,
    temperature: float = 0.1,
) -> tuple[str, dict]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=SKILL_LLM_TIMEOUT_S) as cli:
        r = await cli.post(f"{url}/v1/chat/completions", json=body)
        r.raise_for_status()
        payload = r.json()
    raw = ""
    choices = payload.get("choices") or []
    if choices:
        raw = (choices[0].get("message") or {}).get("content") or ""
    return raw, payload


async def distill_convention_from_diff(
    *,
    job_id: str,
    goal: str,
    failed_code: str,
    failed_stderr: str,
    success_code: str,
    success_stdout: str = "",
    existing_curated_slugs: list[str] | None = None,
) -> tuple[dict | None, dict]:
    """Ask the LLM to extract a convention from one fail→success pair.

    Returns ``(convention_dict | None, meta)``. ``None`` means skip
    (the LLM judged the diff not worth saving, the JSON couldn't be
    parsed, or auto-extraction is disabled).
    """
    meta: dict = {
        "model": CONVENTION_DISTILL_MODEL_NAME,
        "elapsed_ms": 0,
        "raw": "",
        "parsed": None,
        "reason": None,
    }
    if not CONVENTION_AUTO_EXTRACT_ENABLED:
        meta["reason"] = "disabled by CONVENTION_AUTO_EXTRACT_ENABLED=false"
        return None, meta
    if not failed_code or not success_code:
        meta["reason"] = "missing failed_code or success_code"
        return None, meta

    user_parts = [
        f"job_id: {job_id}",
        f"GOAL: {(goal or '').strip()[:500]}",
        "",
    ]
    if existing_curated_slugs:
        user_parts += [
            "Already-known curated conventions (do NOT re-emit these):",
            ", ".join(existing_curated_slugs),
            "",
        ]
    user_parts += [
        "=== FAILED ATTEMPT (most recent) ===",
        "",
        "Code:",
        "```python",
        (failed_code or "").strip(),
        "```",
        "",
        "Stderr (tail):",
        "```",
        (failed_stderr or "").strip()[-1500:],
        "```",
        "",
        "=== SUCCEEDING ATTEMPT ===",
        "",
        "Code:",
        "```python",
        (success_code or "").strip(),
        "```",
    ]
    if success_stdout.strip():
        user_parts += [
            "",
            "Stdout (tail):",
            "```",
            success_stdout.strip()[-800:],
            "```",
        ]
    user = "\n".join(user_parts)

    t0 = time.time()
    try:
        raw, _ = await _chat(
            url=CONVENTION_DISTILL_LLM_URL,
            model=CONVENTION_DISTILL_MODEL_NAME,
            system=_DISTILL_SYSTEM,
            user=user,
            max_tokens=1500,
            temperature=0.2,
        )
    except Exception as e:
        meta["elapsed_ms"] = int((time.time() - t0) * 1000)
        meta["reason"] = f"llm error: {type(e).__name__}: {e}"
        try:
            from server.hub._ai_io_log import record_ai_io
            record_ai_io(purpose="convention_distill", engine_slug=CONVENTION_DISTILL_MODEL_NAME,
                         job_id=job_id, prompt=user, response=None,
                         latency_ms=meta["elapsed_ms"], error=meta["reason"])
        except Exception: pass
        return None, meta
    meta["elapsed_ms"] = int((time.time() - t0) * 1000)
    meta["raw"] = raw
    try:
        from server.hub._ai_io_log import record_ai_io
        record_ai_io(purpose="convention_distill", engine_slug=CONVENTION_DISTILL_MODEL_NAME,
                     job_id=job_id, prompt=user, response=raw,
                     latency_ms=meta["elapsed_ms"])
    except Exception: pass
    parsed = _extract_json(raw)
    meta["parsed"] = parsed
    if parsed is None:
        meta["reason"] = "could not parse JSON from LLM response"
        return None, meta
    if parsed.get("skip"):
        meta["reason"] = f"skip: {parsed.get('reason') or 'unspecified'}"
        return None, meta
    required = ("slug", "name", "advice", "rationale")
    missing = [k for k in required if not parsed.get(k)]
    if missing:
        meta["reason"] = f"missing required fields: {missing}"
        return None, meta
    return parsed, meta
