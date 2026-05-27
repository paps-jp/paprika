"""Goal-verification judge for the codegen-loop.

When an attempt exits 0 we used to declare success and move on. That
silently green-lit "ran to completion with zero results" outcomes --
a script that visited 5 pages and downloaded 0 files would be marked
``completed`` because the script itself didn't crash.

This module wraps a separate LLM call that takes:

  * the operator's goal text
  * a structured summary of the attempt's outcome (exit code,
    elapsed time, asset count by type, stdout/stderr tails, latest
    progress markers)

...and returns a verdict:

  * ``satisfied: bool``  -- did the attempt actually achieve the goal?
  * ``reason: str``       -- one-line "why I said yes/no"
  * ``hint: str``         -- if NG, a short note for the next attempt's
                              LLM telling it what went wrong

The verdict feeds back into ``iterative_codegen.run_iterative_codegen``:
on NG, the orchestrator treats the attempt as a soft failure and
retries with the hint appended to the retry context.

Kept in its own file because the prompt is fundamentally different
from the code-generation prompt (= judging vs. authoring) and we
want to evolve them independently. Uses the same LLM endpoint /
model that ``codegen.py`` does, configured via the same env vars.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from server.hub.codegen import (
    LLMTarget,
    _env_default_target,
    adapt_chat_body,
)

log = logging.getLogger(__name__)


@dataclass
class Verdict:
    """Outcome of one judge call."""

    satisfied: bool  # True == goal achieved, accept as success
    reason: str  # one-line rationale (always set)
    hint: str = ""  # advice for the next attempt (NG only)
    model: str = ""
    elapsed_ms: int = 0
    raw: str = ""  # full LLM response, for debugging


_SYSTEM_PROMPT = """\
You are a strict quality judge for an autonomous web-automation agent.

The agent was given a goal in natural language. It then generated a
Python script that drives a Chrome browser through the paprika fleet,
ran the script, and produced an outcome (exit code, captured assets,
stdout/stderr). You will see:

  * The GOAL the operator originally wrote.
  * The SCRIPT the agent generated (real Python).
  * The OUTCOME (exit code, assets, stdout/stderr).
  * Sometimes a FINAL SCREENSHOT of the browser when the script
    exited. Use it like a human reviewer would: if the visible page
    is a 404 / age-gate / login wall / ad popup / blank tab, the
    agent didn't actually reach the goal even if the script exit
    code was 0. If the visible page LOOKS like the content the goal
    asked for (search results, video list, target article body,
    etc.) AND the asset count is non-zero, that's strong evidence
    the agent succeeded.

Your job has two parts:

  1. Decide whether the OUTCOME actually achieves the GOAL.
  2. If NG, point at the *specific line(s)* in SCRIPT that caused
     the failure and propose a concrete fix.

Verdict criteria:
  * Did the script DO the requested work, or did it just complete
    without crashing? "exit 0 with 0 assets when the goal asked for
    downloads" is a FAILURE, not a success.
  * If the goal specifies a quantity ("at least 3 videos", "20
    pages") and the outcome falls clearly short (< half), that is
    a FAILURE.
  * Visible asset count, captured pages, downloaded files, and
    successful progress markers in stdout count toward satisfaction.
  * Stderr exceptions are NOT automatically failure -- the agent
    might have recovered. But a stderr full of repeated errors with
    no compensating progress markers is failure.
  * Be honest. A borderline attempt should be flagged as NG so the
    agent retries, NOT given the benefit of the doubt.

Hint quality:
  * READ THE SCRIPT. Don't generate a generic "try harder" hint.
    Pinpoint the actual line / construct that broke.
  * Common pitfalls to look for and call out by name when present:
      - URL filters via "X in url" that match the domain itself
        (e.g. `"video" in url` matches every page on a host whose
        name itself contains "video"). Suggest a tighter pattern
        (regex anchored to path segments like /v/, /watch/).
      - Outline parsing via hand-rolled regex when pap.walk() would
        do the same thing without the URL-filter pitfalls.
      - Calling page.download_video() on a page that's not a video
        page (= URL extraction picked menu / nav links).
      - Missing page.close_popups() after agent-driven clicks that
        open new tabs.
      - Not refreshing the outline after navigation (= reading
        stale state).
  * Refer to specific identifiers from the script when possible:
    "the loop at `for line in lines:` only collects URLs matching
    'video' which also matches the domain name; switch to
    `pap.walk(page, target_pages=20, same_domain=True)`".

Output strict JSON, no prose, no markdown fences:

  {
    "satisfied": <true|false>,
    "reason": "<one-line, <= 160 chars, why you said yes/no>",
    "hint": "<if satisfied=false: 1-2 sentences pinpointing the script line / construct that's wrong AND the concrete fix. Empty string if true.>"
  }
"""


def _format_outcome_summary(
    *,
    goal: str,
    script: str = "",
    exit_code: int,
    elapsed_ms: int,
    timed_out: bool,
    stdout: str,
    stderr: str,
    assets_dir: Path | None = None,
    progress_count: int = 0,
    target_pages: int | None = None,
) -> str:
    """Render the attempt's outcome into a compact string the judge
    LLM can read. Trims long stdout/stderr to recent tails because
    context budget is limited.
    """
    # Asset summary: count + group by extension. Keeps the judge's
    # prompt small (a profile with 200 cookies doesn't need every
    # filename); the breakdown is enough to decide goal satisfaction.
    asset_line = "assets: 0 files"
    if assets_dir is not None and assets_dir.exists():
        files = [p for p in assets_dir.rglob("*") if p.is_file()]
        if files:
            from collections import Counter

            ext_counts = Counter((p.suffix.lower() or "(none)") for p in files)
            breakdown = ", ".join(f"{ext}: {n}" for ext, n in ext_counts.most_common(10))
            asset_line = f"assets: {len(files)} files ({breakdown})"

    # Tail both streams. Stdout matters more for "what did the agent
    # accomplish"; stderr more for "what went wrong". Asymmetric
    # budget reflects that.
    def _tail(s: str, max_chars: int) -> str:
        s = s or ""
        if len(s) <= max_chars:
            return s
        return "...[truncated]...\n" + s[-max_chars:]

    # Script body. We send the whole thing (typical generated
    # scripts are 1-3 KB so it fits comfortably in the prompt budget).
    # Truncating mid-script confuses the judge -- "what does the loop
    # do?" needs the full loop body -- so we accept the slight token
    # cost in exchange for accurate pinpointing.
    script_section = ""
    if script:
        script_section = f"# AGENT SCRIPT (the Python the LLM wrote)\n```python\n{script}\n```\n\n"

    parts = [
        f"# GOAL\n{goal.strip()}",
        "",
        script_section.rstrip(),
        "",
        "# OUTCOME",
        f"exit_code: {exit_code}",
        f"timed_out: {timed_out}",
        f"elapsed_ms: {elapsed_ms}",
        f"progress_markers_in_stdout: {progress_count}"
        + (f" (target hint: {target_pages})" if target_pages else ""),
        asset_line,
        "",
        f"# STDOUT TAIL\n{_tail(stdout, 2500)}",
        "",
        f"# STDERR TAIL\n{_tail(stderr, 1200)}",
    ]
    return "\n".join(p for p in parts if p)


# We accept a few sloppy formats the model might produce in addition
# to clean JSON: bare "true"/"false" inside the JSON, ```json fences,
# trailing prose. The matcher extracts the first {...} block.
_JSON_RX = re.compile(r"\{[\s\S]*?\}", re.MULTILINE)


def _parse_verdict(raw: str) -> Verdict | None:
    """Pull a verdict out of the LLM's response. Returns None when
    the response can't be parsed -- caller should treat that as
    "judge unavailable" rather than NG, to avoid penalising the
    attempt for a judge-side failure.
    """
    if not raw:
        return None
    # Try whole-string parse first (= clean JSON, common path).
    txt = raw.strip()
    # Strip markdown fences if present.
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```\s*$", "", txt)
    candidates: list[str] = [txt]
    # Also try the first {...} we find -- catches "Here is my answer:
    # {...}" preambles.
    for m in _JSON_RX.finditer(raw):
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            d = json.loads(cand)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        if "satisfied" not in d:
            continue
        sat = bool(d.get("satisfied"))
        reason = str(d.get("reason") or "").strip() or ("(judge omitted reason)")
        hint = str(d.get("hint") or "").strip()
        return Verdict(
            satisfied=sat,
            reason=reason[:300],
            hint=hint[:600],
        )
    return None


async def judge_attempt(
    *,
    goal: str,
    script: str = "",
    exit_code: int,
    elapsed_ms: int,
    timed_out: bool,
    stdout: str,
    stderr: str,
    assets_dir: Path | None = None,
    screenshot_path: Path | None = None,
    progress_count: int = 0,
    target_pages: int | None = None,
    max_tokens: int = 400,
    temperature: float = 0.0,
    target: LLMTarget | None = None,
) -> Verdict | None:
    """Ask the LLM whether the attempt satisfied the goal.

    Returns ``None`` when the judge can't be reached or its output
    can't be parsed -- the caller should fall back to the existing
    heuristic-based decision (exit-code success) rather than failing
    the attempt.

    Synchronous-but-await-shaped to match codegen.generate_script's
    call surface so iterative_codegen.py uses the same pattern for
    both LLM round trips.
    """
    summary = _format_outcome_summary(
        goal=goal,
        script=script,
        exit_code=exit_code,
        elapsed_ms=elapsed_ms,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        assets_dir=assets_dir,
        progress_count=progress_count,
        target_pages=target_pages,
    )

    # Compose the user message. If a screenshot is available and the
    # model is vision-capable (Qwen2.5-VL family), send a multipart
    # content array so the LLM can SEE the final-frame state of the
    # agent's browser. The system prompt has been updated to expect
    # this image when present; text-only fallback still works for
    # non-vision models or when screenshot capture failed.
    user_content: object = summary
    has_image = False
    if screenshot_path is not None and screenshot_path.exists():
        try:
            import base64

            img_bytes = screenshot_path.read_bytes()
            b64 = base64.b64encode(img_bytes).decode("ascii")
            # JPEG via the worker's screenshot RPC; tag the mime
            # explicitly because some vLLM builds reject the
            # x-image-fallback heuristic.
            mime = (
                "image/jpeg" if screenshot_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            )
            user_content = [
                {
                    "type": "text",
                    "text": (
                        summary + "\n\n# FINAL SCREENSHOT\n"
                        "The image below is the browser's last visible state. "
                        "Use it to ground your verdict: an obviously-wrong "
                        "page (404 / login wall / age-gate / blank) means the "
                        "agent never reached the content the goal asked for."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
            ]
            has_image = True
        except Exception as e:
            log.info(
                f"[judge] could not embed screenshot ({type(e).__name__}: "
                f"{e}); falling back to text-only",
            )

    tgt = target or _env_default_target()
    body = {
        "model": tgt.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Vendor-specific: vLLM honours response_format for JSON; if
        # the endpoint ignores it we still parse the response, so this
        # is just a best-effort hint.
        "response_format": {"type": "json_object"},
    }
    body = adapt_chat_body(tgt, body)
    if has_image:
        log.info(
            f"[judge] including final screenshot in prompt ({len(img_bytes)} bytes)",
        )

    t0 = time.time()
    try:
        # Pre-call quota check. EngineQuotaExceeded surfaces as a
        # judge-unreachable (return None) so an exhausted-quota engine
        # doesn't block the iterative loop -- the heuristic-success
        # fallback still lets the attempt finish.
        from server.hub.codegen import (
            check_engine_quota,
            record_engine_usage,
            EngineQuotaExceeded,
        )
        try:
            check_engine_quota(tgt)
        except EngineQuotaExceeded as e:
            log.info(f"[judge] quota gate refused: {e}")
            return None
        async with httpx.AsyncClient(timeout=tgt.timeout) as client:
            r = await client.post(tgt.url, json=body, headers=tgt.headers)
            if r.status_code >= 400:
                log.info(
                    f"[judge] LLM {r.status_code} from {tgt.url} model={tgt.model}: {r.text[:600]}",
                )
                r.raise_for_status()
            payload = r.json()
            # Charge tokens to the per-engine daily counter.
            record_engine_usage(tgt, payload.get("usage") or {})
    except Exception as e:
        log.info(f"[judge] LLM call failed: {type(e).__name__}: {e}")
        return None
    elapsed_ms_call = int((time.time() - t0) * 1000)

    choices = payload.get("choices") or []
    raw = ""
    if choices:
        msg = choices[0].get("message") or {}
        raw = msg.get("content") or ""

    verdict = _parse_verdict(raw)
    if verdict is None:
        log.info(
            f"[judge] could not parse verdict from LLM response "
            f"(model={payload.get('model', '?')}, raw[:200]={raw[:200]!r})",
        )
        return None
    verdict.model = payload.get("model") or tgt.model
    verdict.elapsed_ms = elapsed_ms_call
    verdict.raw = raw
    return verdict


# ---------------------------------------------------------------------------
# v2 Phase 3: PerceptionResult + R1 judge
#
# Drop-in replacement for ``judge_attempt`` that reasons over a
# structured PerceptionResult (produced by the eye, Phase 1) instead of
# rummaging through stdout/stderr/screenshot. The brain (DeepSeek-R1)
# gets a compact factual brief and decides; it never sees raw HTML or
# pixels. Same Verdict shape so iterative_codegen consumes it without
# changes.
#
# Opt-in via PAPRIKA_USE_R1_JUDGE=1. Falls back to legacy ``judge_attempt``
# when:
#   * the flag is off,
#   * no PerceptionResult could be produced for the attempt,
#   * the R1 engine is unreachable / returns garbage.
# ---------------------------------------------------------------------------

_R1_JUDGE_SYSTEM_PROMPT = """You are the JUDGE for paprika browser automation (v2).

You receive:
  * GOAL        -- what the operator wanted to happen
  * PERCEPTION  -- structured observation of the FINAL page state,
                   produced by the eye (a vision LLM). Pure observation:
                   page_kind, barriers detected, content counts, free notes.
                   You do NOT see the screenshot itself.
  * SCRIPT      -- (sometimes) the Python source the agent generated
  * STDOUT      -- last lines of the script's stdout (where the
                   goal-relevant print() output lives)
  * STDERR      -- last lines of stderr (traceback if the script failed)
  * OUTCOME     -- exit code, asset counts by extension, error flag

Your job: decide whether the GOAL was achieved.

CRITICAL JUDGING RULE
A "print" / "output" / "extract" goal is satisfied ONLY when the
relevant content actually appears in STDOUT.  A script that navigated
successfully, parsed successfully, and exited 0 but printed NOTHING
matching the goal is NOT satisfied. Cross-check what STDOUT says
against what PERCEPTION shows: when STDOUT says "no h1 found" but
PERCEPTION shows an h1 on the page, that's a script bug, not success.

STRICT RULES
1. Output a single JSON object: {"satisfied": bool, "reason": "...", "hint": "..."}.
   No prose, no markdown fences, nothing else.
2. "satisfied": true only when the goal is actually fulfilled. Side-
   effects that didn't reach the goal (page navigated but no asset saved;
   age-gate / login-wall / cloudflare interstitial visible; print()
   never executed) => false.
3. "reason": one short sentence, <= 200 chars. Explain using BOTH
   perception facts AND stdout content. Quote the relevant stdout line
   when it makes the verdict clear.
4. "hint": only when satisfied=false. Specific actionable advice for the
   next attempt: which line in the script caused the failure, and what
   to do instead. <= 400 chars. Empty string when satisfied=true.

You may include a <think>...</think> block before the JSON; the system
strips it. Do NOT put the JSON inside <think>.
"""


def _format_perception_brief(perception: dict | None) -> str:
    """Compact human-readable brief of a PerceptionResult for the prompt.

    PerceptionResult is JSON-serialisable so a faithful str()-dump works,
    but R1 prompts trim better when we collapse it to labelled bullet
    points. ``perception`` is the raw dict (already-loaded JSON).
    """
    if not perception:
        return "(no perception available)"
    pk = perception.get("page_kind") or {}
    barriers = perception.get("barriers") or []
    content = perception.get("content") or {}
    progress = perception.get("progress_signals") or {}
    anomalies = perception.get("anomalies") or []
    free = perception.get("free_observation") or ""

    lines: list[str] = [
        f"url:           {perception.get('url')}",
        f"host:          {perception.get('host')}",
        f"page_kind:     {pk.get('value')!r} (confidence={pk.get('confidence')})",
    ]
    why = pk.get("why") or []
    if why:
        lines.append("  why:         " + "; ".join(str(w) for w in why[:4]))
    if barriers:
        for b in barriers:
            kind = b.get("kind")
            ev = (b.get("evidence") or "")[:120]
            lines.append(f"barrier:       {kind} -- {ev}")
    else:
        lines.append("barrier:       (none)")
    videos = content.get("videos") or []
    lines.append(
        f"videos:        {len(videos)} (kinds: "
        + ",".join(v.get("kind", "?") for v in videos[:5])
        + ")"
    )
    lines.append(f"images_count:  {content.get('images_count')}")
    links = content.get("links") or {}
    lines.append(
        f"links:         same_host={links.get('to_same_host_count')} "
        f"external={links.get('external_count')}"
    )
    lines.append(f"pagination:    {content.get('has_pagination')}")
    lines.append(
        "progress:      "
        f"url_changed={progress.get('url_changed_from_previous')} "
        f"page_loaded={progress.get('page_loaded')} "
        f"new_assets={progress.get('new_assets_since_last')} "
        f"stderr_err={progress.get('stderr_has_error')}"
    )
    if anomalies:
        for a in anomalies[:3]:
            lines.append(
                f"anomaly:       {a.get('kind')} -- "
                f"{(a.get('description') or '')[:100]}"
            )
    if free:
        lines.append(f"free:          {free[:250]}")
    return "\n".join(lines)


def _strip_think_block(raw: str) -> str:
    """Remove any <think>...</think> block from an R1 response.

    R1 emits its chain-of-thought as a literal ``<think>`` block before
    the JSON answer. The block can span many lines and contain almost
    anything. We remove it once, leaving the actual answer.
    """
    if "<think>" not in raw:
        return raw
    return re.sub(
        r"<think>[\s\S]*?</think>\s*",
        "",
        raw,
        count=1,
    )


def _tail(text: str, n_lines: int = 30, max_chars: int = 2000) -> str:
    """Return the last N lines of ``text``, capped at max_chars.

    Used to feed R1 the *concluding* part of stdout/stderr -- where the
    script's last print and any traceback live -- without blowing the
    prompt budget on verbose logging earlier in the run.
    """
    if not text:
        return ""
    lines = text.splitlines()
    tail = "\n".join(lines[-max(1, n_lines):])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


async def judge_via_r1(
    *,
    goal: str,
    exit_code: int,
    perception: dict | None,
    assets_summary: dict[str, int] | None = None,
    stderr_has_error: bool = False,
    stdout: str = "",
    stderr: str = "",
    script: str = "",
    target: LLMTarget,
) -> Verdict | None:
    """R1-based goal verification using a structured PerceptionResult.

    For codegen-loop attempts, the eye (Qwen-VL) generates a
    PerceptionResult from the final screenshot, and R1 reasons over
    THAT plus the script's stdout/stderr/script body. R1 never sees
    pixels -- the screenshot facts arrive as structured perception
    fields, the script outcome arrives as text.

    Returns None on any unrecoverable failure (LLM unreachable, parser
    can't extract verdict) so the caller can fall back to the legacy
    judge or treat the attempt as judge-unavailable. Never raises.
    """
    # Perception is optional for codegen-loop judging: when the script
    # exits faster than the screenshot-polling cadence there is no
    # capture to feed the eye, but R1 can still judge from stdout +
    # script alone. The perception_brief reads "(no perception available)"
    # in that case and the prompt's CRITICAL JUDGING RULE focuses R1
    # on stdout content.
    perception_brief = _format_perception_brief(perception)
    assets_str = (
        ", ".join(f"{ext}: {n}" for ext, n in (assets_summary or {}).items())
        or "(none)"
    )

    # Tails: stdout-last is where the goal-relevant prints live; stderr-
    # last is where a traceback lives if the script failed. Cap both to
    # keep the prompt small (R1's thinking tokens are expensive).
    stdout_tail = _tail(stdout, n_lines=30, max_chars=2000)
    stderr_tail = _tail(stderr, n_lines=20, max_chars=1500)

    # Script: include only when reasonably short (R1 doesn't need to see
    # 3000-line scripts to judge -- the assumption is that the script's
    # OUTPUT is the proof, not its source. We include short scripts to
    # help R1 explain WHY it failed in the hint field.)
    script_block = ""
    if script and len(script) <= 4000:
        script_block = f"SCRIPT\n{script}\n\n"

    user_msg = (
        f"GOAL\n{goal.strip()}\n\n"
        f"PERCEPTION\n{perception_brief}\n\n"
        f"{script_block}"
        f"STDOUT (last lines)\n{stdout_tail or '(empty)'}\n\n"
        f"STDERR (last lines)\n{stderr_tail or '(empty)'}\n\n"
        f"OUTCOME\n"
        f"  exit_code:        {exit_code}\n"
        f"  assets_by_ext:    {assets_str}\n"
        f"  stderr_has_error: {stderr_has_error}\n\n"
        f"Produce the verdict JSON now."
    )

    body = {
        "model": target.model,
        "messages": [
            {"role": "system", "content": _R1_JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.6,
        "max_tokens": 8192,
    }
    body = adapt_chat_body(target, body)

    t0 = time.time()
    try:
        from server.hub.codegen import (
            check_engine_quota,
            record_engine_usage,
            EngineQuotaExceeded,
        )
        try:
            check_engine_quota(target)
        except EngineQuotaExceeded as e:
            log.info(f"[judge:r1] quota gate refused: {e}")
            return None
        async with httpx.AsyncClient(timeout=target.timeout) as client:
            r = await client.post(target.url, json=body, headers=target.headers)
            if r.status_code >= 400:
                log.info(
                    f"[judge:r1] LLM {r.status_code} from {target.url} "
                    f"model={target.model}: {r.text[:400]}"
                )
                return None
            payload = r.json()
            record_engine_usage(target, payload.get("usage") or {})
    except Exception as e:
        log.info(f"[judge:r1] LLM call failed: {type(e).__name__}: {e}")
        return None
    elapsed_ms_call = int((time.time() - t0) * 1000)

    choices = payload.get("choices") or []
    raw = ""
    if choices:
        msg = choices[0].get("message") or {}
        raw = msg.get("content") or ""

    # DeepSeek can place reasoning in a separate reasoning_content field
    # (when configured that way). We ignore it -- only the answer matters.
    stripped = _strip_think_block(raw)

    verdict = _parse_verdict(stripped)
    if verdict is None:
        log.info(
            f"[judge:r1] could not parse verdict (model={payload.get('model','?')}, "
            f"raw[:200]={stripped[:200]!r})"
        )
        return None
    verdict.model = payload.get("model") or target.model
    verdict.elapsed_ms = elapsed_ms_call
    verdict.raw = raw  # keep the WITH-think version for debugging
    return verdict
