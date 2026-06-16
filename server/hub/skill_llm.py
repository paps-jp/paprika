"""LLM-driven skill distillation + retrieval.

Two pure-async helpers backed by an OpenAI-compatible /v1/chat/completions
endpoint:

* :func:`distill_skill_from_job` — given a successful codegen-loop
  attempt, ask the LLM to extract an abstract reusable pattern (or
  return ``skip`` if the script is too site-specific).
* :func:`pick_relevant_skills` — given a new job's goal and a roster
  of existing skills, ask the LLM which (if any) top-K skills should
  ride along in the codegen prompt.

Both use the same model + endpoint by default (``CODEGEN_LLM_URL`` /
``CODEGEN_MODEL_NAME``) but can be overridden via env so a future
swap (smaller / faster distiller) is one env var change away.
"""

from __future__ import annotations

import json
import os
import re
import time

import httpx

from server.hub.codegen import CODEGEN_LLM_URL, CODEGEN_MODEL_NAME
from server.hub.skills import SkillRecord

# Override-able env (default = same model as codegen).
SKILL_DISTILL_LLM_URL = os.environ.get(
    "SKILL_DISTILL_LLM_URL",
    CODEGEN_LLM_URL,
).rstrip("/")
SKILL_DISTILL_MODEL_NAME = os.environ.get(
    "SKILL_DISTILL_MODEL_NAME",
    CODEGEN_MODEL_NAME,
)
SKILL_RETRIEVAL_LLM_URL = os.environ.get(
    "SKILL_RETRIEVAL_LLM_URL",
    CODEGEN_LLM_URL,
).rstrip("/")
SKILL_RETRIEVAL_MODEL_NAME = os.environ.get(
    "SKILL_RETRIEVAL_MODEL_NAME",
    CODEGEN_MODEL_NAME,
)
SKILL_LLM_TIMEOUT_S = float(os.environ.get("SKILL_LLM_TIMEOUT_S", "60"))
SKILL_AUTO_EXTRACT_ENABLED = os.environ.get("SKILL_AUTO_EXTRACT_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
SKILL_RETRIEVAL_TOP_K = int(os.environ.get("SKILL_RETRIEVAL_TOP_K", "3"))


_DISTILL_SYSTEM = """\
You are reviewing a successful browser-automation script. Decide
whether it teaches a REUSABLE pattern other future tasks could
benefit from, or whether it is too site-specific to be worth saving.

DEFAULT TO SKIP. Only save a skill when there is a clear, abstract
technique that generalises across sites or tasks. Examples of
patterns worth saving:
  - Specific use of paprika-client's higher-level primitives
    (pap.walk + per-page work, page.agent() for unknown UI)
  - Idioms that avoid a common foot-gun (e.g. always handle
    age-gate BEFORE the first outline call, always await
    page.state(), etc.)
  - Combinations of features that work well together for a class of
    tasks (e.g. "crawl pages + download videos" or "infinite scroll
    gallery + capture-on-stop")

Skip when:
  - The script is mostly URLs / selectors / domain-specific tweaks
    with no general technique
  - The script is just a near-verbatim call of a single
    paprika-client function (no compound pattern)
  - This was a one-attempt success on a trivial page (no learning)

REUSE OVER CREATE: You will be shown EXISTING SKILLS. If your distilled
technique is essentially the same as one of them (same barrier / goal class,
their code would work with a small edit), output {"reuse": "<slug>"} INSTEAD
of a new skill -- never emit a near-duplicate variant of an existing skill
(e.g. yet another "get past the age-gate then grab the video"). Prefer reuse
so the skill set CONVERGES instead of proliferating into one-off variants.

Output JSON ONLY (no commentary, no markdown fences). Three shapes:

  {"skip": true, "reason": "one short sentence"}

  {"reuse": "<existing-slug>"}   // technique already captured by an EXISTING
                                 // SKILL listed below -- reinforce it, do NOT
                                 // create a near-duplicate variant

  {"skip": false,
   "slug":              <kebab-case name, <= 60 chars>,
   "name":              <Human-readable name, <= 80 chars>,
   "description":       <One sentence: when to use this skill>,
   "applicable_when":   [<bullet condition 1>, <bullet condition 2>, ...],
   "tags":              [<short kebab-tags>],
   "code_template":     <CONCRETE Python source (string with \\n line
                         breaks). Must be VALID paprika-client code
                         that another developer could paste, edit the
                         URL, and run. Replace site-specific bits with
                         placeholders like 'https://EXAMPLE.com/'.>,
   "llm_instructions":  <1-3 paragraphs of advice for an LLM generating
                         a future script. Spell out *when* this pattern
                         applies and *what to do differently* compared
                         with a naive implementation>}

EXAMPLE OUTPUT for a real success (DO NOT echo this back -- it is here
to illustrate the shape; produce one tailored to the actual script
you are reviewing):

  {"skip": false,
   "slug": "crawl-with-agent-prep",
   "name": "BFS crawl with agent-prepped session",
   "description": "Crawl N pages of a site after using page.agent() to dismiss any startup overlay (age-gate, consent).",
   "applicable_when": ["goal mentions crawling N pages", "site likely has an age-gate or consent dialog"],
   "tags": ["crawl", "pap-walk", "age-gate"],
   "code_template": "import asyncio\\nimport paprika_client as pap\\nfrom paprika_client import async_paprika\\n\\nasync def main():\\n    async with async_paprika.connect() as cli:\\n        async with cli.session(initial_url='https://EXAMPLE.com/') as page:\\n            await page.agent('If an age verification or consent dialog appears, accept it. Otherwise return done immediately.', max_steps=3)\\n            async for visit in pap.walk(page, target_pages=10, same_domain=True, order='bfs'):\\n                print(f'[{visit.n}/{visit.target}] {visit.url}')\\n                await page.capture(f'page-{visit.n}')\\n\\nasyncio.run(main())\\n",
   "llm_instructions": "When the task is to crawl multiple pages of a site, ALWAYS dismiss any startup overlay with page.agent() BEFORE the first pap.walk() iteration -- otherwise the first outline returned to the walker will only contain the overlay's buttons, and the crawl terminates immediately with 'no links found'. Use pap.walk() rather than hand-rolled BFS; it handles dedup, dead-end URL filters (.xml/.json/feed/sitemap), and off-domain redirects for you. Capture each page after pap.walk yields, never before -- the yield is the signal that the page is loaded."}

RULES:
1. JSON MUST be valid (parseable by json.loads).
2. Strings must be properly escaped (use \\n for newlines, \\" for quotes inside code_template).
3. code_template MUST be real Python, not a placeholder description.
4. Do not wrap the JSON in markdown fences or any explanatory prose.
"""


_RETRIEVE_SYSTEM = """\
You are choosing which previously-distilled "skills" (reusable
patterns) should be shown to an LLM that is about to generate a
paprika-client script for a new task.

You will be given:
  1. The new task description (goal + URL).
  2. A list of available skills, each with slug + short description plus
     its track record: use_count (times injected into past jobs),
     success_count, and success_rate (success_count / use_count).

Pick up to N skills that you genuinely believe will improve the
generated script. Skip skills that look only superficially related.
DEFAULT TO PICKING FEWER. It is better to return zero skills than
to return irrelevant ones.

When two skills are similarly relevant, PREFER the one with the higher
success_rate. Treat a skill with a high use_count but low success_rate
(e.g. used 5+ times yet success_rate < 0.2) as a repeated dud and avoid
it unless it is clearly the best match. A null success_rate just means
untried (no track record yet) -- that is not a strike against it.

Output JSON ONLY:

  {"slugs": ["slug-a", "slug-b"]}      // 0 to N entries

Slugs must be from the provided list verbatim.
"""


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```\s*$", re.M)


def _strip_fences(s: str) -> str:
    """Some models still wrap JSON in fences. Strip if present."""
    s = (s or "").strip()
    if s.startswith("```"):
        s = _FENCE_RE.sub("", s).strip()
    return s


def _extract_json(raw: str) -> dict | None:
    """Best-effort JSON parse. Tries the whole text first, then
    falls back to the first { ... } block. Returns None on failure."""
    s = _strip_fences(raw)
    try:
        return json.loads(s)
    except Exception:
        pass
    # Greedy first-object scan: find balanced {...}
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        return json.loads(s[start:end])
    except Exception:
        return None


async def _chat(
    *,
    url: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 2000,
    temperature: float = 0.1,
) -> tuple[str, dict]:
    """One-shot chat completion. Returns (raw_text, payload)."""
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


async def distill_skill_from_job(
    *,
    job_id: str,
    goal: str,
    winning_script: str,
    attempt_count: int,
    extra_context: str | None = None,
    known_skills: list[dict] | None = None,
) -> tuple[dict | None, dict]:
    """Ask the LLM to abstract a skill from a successful job.

    Returns a tuple ``(skill_dict | None, meta)``.

    ``skill_dict`` is ``None`` when:
      * the model returned ``{"skip": true, ...}``
      * the response couldn't be parsed
      * auto-extraction is disabled by env

    ``meta`` always contains ``{model, elapsed_ms, raw, parsed, reason}``
    for debug / log persistence.
    """
    meta: dict = {
        "model": SKILL_DISTILL_MODEL_NAME,
        "elapsed_ms": 0,
        "raw": "",
        "parsed": None,
        "reason": None,
    }
    if not SKILL_AUTO_EXTRACT_ENABLED:
        meta["reason"] = "disabled by SKILL_AUTO_EXTRACT_ENABLED=false"
        return None, meta

    user_parts = [
        f"job_id: {job_id}",
        f"attempts_taken: {attempt_count}",
        "",
        "GOAL:",
        (goal or "").strip(),
        "",
        "WINNING SCRIPT:",
        "```python",
        (winning_script or "").strip(),
        "```",
    ]
    if extra_context:
        user_parts += ["", "ADDITIONAL CONTEXT:", extra_context.strip()]
    if known_skills:
        kl = ["", "EXISTING SKILLS (prefer {\"reuse\":\"<slug>\"} over a near-duplicate):"]
        for s in known_skills[:50]:
            _tags = ",".join(s.get("tags") or [])
            kl.append(
                f"- {s.get('slug')} [{s.get('tier') or 'auto'}]"
                f"{(' (' + _tags + ')') if _tags else ''}: {(s.get('description') or '').strip()}"
            )
        user_parts += kl
    user = "\n".join(user_parts)

    t0 = time.time()
    try:
        raw, _ = await _chat(
            url=SKILL_DISTILL_LLM_URL,
            model=SKILL_DISTILL_MODEL_NAME,
            system=_DISTILL_SYSTEM,
            user=user,
            max_tokens=2200,
            temperature=0.2,
        )
    except Exception as e:
        meta["elapsed_ms"] = int((time.time() - t0) * 1000)
        meta["reason"] = f"llm error: {type(e).__name__}: {e}"
        try:
            from server.hub._ai_io_log import record_ai_io
            record_ai_io(purpose="skill_distill", engine_slug=SKILL_DISTILL_MODEL_NAME,
                         job_id=job_id, prompt=user, response=None,
                         latency_ms=meta["elapsed_ms"], error=meta["reason"])
        except Exception: pass
        return None, meta
    meta["elapsed_ms"] = int((time.time() - t0) * 1000)
    meta["raw"] = raw
    try:
        from server.hub._ai_io_log import record_ai_io
        record_ai_io(purpose="skill_distill", engine_slug=SKILL_DISTILL_MODEL_NAME,
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
    # Reuse: the technique matches an existing skill -- don't mint a variant.
    if parsed.get("reuse"):
        meta["reason"] = f"reuse existing skill: {parsed.get('reuse')}"
        return {"reuse": str(parsed.get("reuse"))}, meta
    # Validate required fields. Be lenient -- we want to capture
    # anything the LLM bothered to produce, even partial.
    required = ("slug", "name", "description", "code_template", "llm_instructions")
    missing = [k for k in required if not parsed.get(k)]
    if missing:
        meta["reason"] = f"missing required fields: {missing}"
        return None, meta
    return parsed, meta


async def pick_relevant_skills(
    *,
    goal: str,
    url: str,
    candidates: list[SkillRecord],
    top_k: int | None = None,
) -> tuple[list[str], dict]:
    """Ask the LLM which candidate skills are worth showing for a new job.

    Returns ``(picked_slugs, meta)``. Empty list when nothing matches.
    """
    k = top_k if top_k is not None else SKILL_RETRIEVAL_TOP_K
    meta: dict = {
        "model": SKILL_RETRIEVAL_MODEL_NAME,
        "elapsed_ms": 0,
        "raw": "",
        "parsed": None,
        "reason": None,
        "candidates_offered": len(candidates),
    }
    if k <= 0 or not candidates:
        meta["reason"] = "no candidates or top_k=0"
        return [], meta

    # Build candidate roster. Order by fitness so the highest-signal
    # skills appear first (LLMs weight earlier items): curated before
    # auto, then by track record. Untried skills get a neutral 0.5 prior
    # so a brand-new skill isn't buried beneath a proven-but-mediocre one,
    # while a repeatedly-injected dud (high use, ~0 success) sinks.
    def _score(s) -> float:
        uc = s.use_count or 0
        if not uc:
            return 0.5
        return getattr(s, "success_count", 0) / uc

    ordered = sorted(
        candidates,
        key=lambda s: (
            0 if s.tier == "curated" else 1,
            -_score(s),
            -(s.use_count or 0),
        ),
    )
    rows = []
    for s in ordered:
        uc = s.use_count or 0
        sc = getattr(s, "success_count", 0)
        rows.append(
            {
                "slug": s.slug,
                "tier": s.tier,
                "description": s.description,
                "applicable_when": s.applicable_when,
                "tags": s.tags,
                # Track record so the model can prefer proven skills.
                "use_count": uc,
                "success_count": sc,
                "success_rate": (round(sc / uc, 2) if uc else None),
            }
        )
    user = (
        f"NEW TASK:\n"
        f"URL: {url}\n"
        f"GOAL: {goal}\n\n"
        f"AVAILABLE SKILLS (up to {k}; pick fewer if uncertain):\n"
        + json.dumps(rows, ensure_ascii=False, indent=2)
    )

    t0 = time.time()
    try:
        raw, _ = await _chat(
            url=SKILL_RETRIEVAL_LLM_URL,
            model=SKILL_RETRIEVAL_MODEL_NAME,
            system=_RETRIEVE_SYSTEM,
            user=user,
            max_tokens=300,
            temperature=0.0,
        )
    except Exception as e:
        meta["elapsed_ms"] = int((time.time() - t0) * 1000)
        meta["reason"] = f"llm error: {type(e).__name__}: {e}"
        try:
            from server.hub._ai_io_log import record_ai_io
            record_ai_io(purpose="skill_retrieval", engine_slug=SKILL_RETRIEVAL_MODEL_NAME,
                         job_id=None, prompt=user, response=None,
                         latency_ms=meta["elapsed_ms"], error=meta["reason"])
        except Exception: pass
        return [], meta
    meta["elapsed_ms"] = int((time.time() - t0) * 1000)
    meta["raw"] = raw
    try:
        from server.hub._ai_io_log import record_ai_io
        record_ai_io(purpose="skill_retrieval", engine_slug=SKILL_RETRIEVAL_MODEL_NAME,
                     job_id=None, prompt=user, response=raw,
                     latency_ms=meta["elapsed_ms"])
    except Exception: pass
    parsed = _extract_json(raw)
    meta["parsed"] = parsed
    if parsed is None:
        meta["reason"] = "could not parse JSON"
        return [], meta
    slugs = parsed.get("slugs") or []
    if not isinstance(slugs, list):
        meta["reason"] = "'slugs' is not a list"
        return [], meta
    valid_slugs = {s.slug for s in candidates}
    picked = []
    for sl in slugs:
        if not isinstance(sl, str):
            continue
        if sl in valid_slugs and sl not in picked:
            picked.append(sl)
        if len(picked) >= k:
            break
    return picked, meta


def build_skill_context_block(skills: list[SkillRecord]) -> str:
    """Render selected skills as a single string suitable for
    appending to the codegen-loop's ``extra_context``. Empty string
    when no skills."""
    if not skills:
        return ""
    parts = [
        "=== Relevant skills (distilled from prior successful jobs) ===",
        "",
    ]
    for s in skills:
        parts.append(f"## {s.name}  [{s.tier} · slug={s.slug}]")
        if s.description:
            parts.append(s.description.strip())
        if s.applicable_when:
            parts.append("Applicable when:")
            for c in s.applicable_when:
                parts.append(f"  - {c}")
        if s.llm_instructions:
            parts.append("")
            parts.append(s.llm_instructions.strip())
        if s.code_template:
            parts.append("")
            parts.append("Example pattern:")
            parts.append("```python")
            parts.append(s.code_template.strip())
            parts.append("```")
        parts.append("")
    return "\n".join(parts)
