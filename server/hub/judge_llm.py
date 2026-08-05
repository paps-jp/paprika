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

import asyncio
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
You MUST follow the COMMIT-FIRST protocol below -- do NOT skip Step 1.

# COMMIT-FIRST PROTOCOL (研究裏付け: arxiv 2607.05904)
# reference-free judging that just says "yes/no" measures plausibility,
# not correctness (false-positive rate 0.719). Forcing the judge to
# commit an answer of its own FIRST -- specifically, to write down the
# CRITERIA the goal implies before looking at what the agent did --
# collapses the false-positive rate to 0.012 in the original paper.
# We apply the same principle here.

## Step 1: DECOMPOSE THE GOAL INTO OBJECTIVE CRITERIA (before looking at outcome)
Read only the GOAL. Ignore the outcome for now. Write down 2-6 concrete,
checkable success criteria the goal implies. Each criterion must have a
"kind" and a "threshold" you can objectively verify against the OUTCOME
block later. Common kinds:

  * video_count       — "at least N video files saved"
  * video_valid       — "each video has duration >= X sec AND resolution >= WxH"
                        (thumbnails, previews, 8-second ads, and 728x90
                        banners MUST fail this. This is the #1 false-
                        positive class in paprika audit.)
  * image_count       — "at least N image files"
  * page_count        — "at least N pages crawled (progress markers)"
  * page_reached      — "the final page shown is on the requested site
                        AND is NOT 404 / login wall / age-gate / blank"
  * content_match     — "the saved content actually contains what the
                        goal asked for (topic, entity, page role)"
  * no_error_flood    — "stderr is not a runaway loop of the same error"

If the goal is genuinely under-specified (e.g. "just fetch it"), fall
back to the DEFAULT set: page_reached=true AND (assets>0 OR
progress_count>0).

## Step 2: CHECK EACH CRITERION AGAINST THE ACTUAL OUTCOME
For each criterion, look at the OUTCOME block (asset breakdown, video
probe results, screenshot, stdout progress markers, stderr) and record
result: "pass" | "fail" | "unknown". "unknown" = you cannot tell from
the evidence.

CRITICAL: use the VIDEO PROBES section when present. A file with a
``.mp4`` extension that ffprobe reports as duration<3s OR width<200 OR
no video stream IS NOT A VIDEO -- it is a thumbnail / preview / ad /
mis-labelled image. Mark video_valid as FAIL.

## Step 3: VERDICT
``satisfied = true`` ONLY if ALL criteria are ``pass`` (unknowns count
as fail for high-stakes categories: video_valid, page_reached,
content_match). If ANY criterion is fail/unknown, satisfied = false.

# HINT QUALITY (only on NG)
Point at the specific line / construct in SCRIPT that caused the failed
criterion, and propose a concrete fix. Common pitfalls:
  - URL filters via "X in url" that match the domain itself
    (e.g. `"video" in url` matches every page on a host whose name
    contains "video"). Suggest an anchored pattern.
  - Calling page.download_video() on a page that's not a video page.
  - Missing page.close_popups() after agent-driven clicks.
  - Not refreshing the outline after navigation.

# OUTPUT
Output strict JSON, no prose, no markdown fences:

  {
    "criteria": [
      {"kind": "<from list above>", "requirement": "<one line>", "result": "pass|fail|unknown", "evidence": "<why>"},
      ...
    ],
    "satisfied": <true|false>,
    "reason": "<one-line, <= 160 chars, WHICH criterion(a) drove the verdict>",
    "hint": "<NG only: 1-2 sentences pinpointing the script line / construct that's wrong AND the concrete fix. Empty string on OK.>"
  }

LANGUAGE: ``requirement`` / ``evidence`` / ``reason`` / ``hint`` MUST be
written in JAPANESE (日本語). Keep code / selector references inline in
English as written (e.g. ``await page.state()['url']``). ``kind``,
``result``, and the ``satisfied`` boolean stay as specified.
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
    blind: bool = False,
    video_probes: list[dict] | None = None,
) -> str:
    """Render the attempt's outcome into a compact string the judge
    LLM can read. Trims long stdout/stderr to recent tails because
    context budget is limited.

    ``blind`` (Settings ``judge_blind_mode``, default True) strips
    the AGENT SCRIPT block and STDOUT/STDERR tails from the prompt
    so the judge can ONLY rule on hard outcomes (asset counts,
    exit_code, progress marker count, screenshot). Goal: prevent the
    judge from being persuaded by the maker's narrative — the
    "evaluator-optimizer" pattern requires the verifier to be
    blind to the maker's reasoning trace.

    ``video_probes`` is a list of ffprobe results for video assets
    (see judge_attempt). When present, embedded verbatim into the
    OUTCOME block so the commit-first judge can objectively check
    the ``video_valid`` criterion (duration + resolution + codec)
    without being fooled by ``.mp4`` extension alone.
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

    # VIDEO PROBES: for every ``.mp4`` / ``.webm`` / ``.mkv`` / ``.mov``
    # / ``.m4v`` asset, embed ffprobe results (duration, width, height,
    # codec) so the judge can rule on ``video_valid`` objectively. When
    # video_probes is empty the block is omitted (judge sees "assets"
    # only, same as before).
    video_probe_line = ""
    if video_probes:
        rows = []
        for p in video_probes:
            name = (p.get("name") or "?")[:60]
            if p.get("probe") is None:
                rows.append(f"  - {name}: NO VIDEO STREAM (likely mis-labelled)")
                continue
            pr = p["probe"]
            dur = pr.get("duration_s") or 0
            w = pr.get("width") or 0
            h = pr.get("height") or 0
            codec = pr.get("codec") or "?"
            flags = []
            if dur < 3: flags.append("short<3s")
            if w < 200 or h < 200: flags.append(f"tiny{w}x{h}")
            if w and h and (w / max(1, h)) > 6: flags.append(f"banner-aspect{w}x{h}")
            flag_str = f" [{'/'.join(flags)}]" if flags else ""
            rows.append(f"  - {name}: {dur:.1f}s {w}x{h} {codec}{flag_str}")
        video_probe_line = "video probes (ffprobe):\n" + "\n".join(rows)

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
    # Suppressed in blind mode (see docstring).
    script_section = ""
    if script and not blind:
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
    ]
    if video_probe_line:
        parts += ["", video_probe_line]
    if not blind:
        parts += [
            "",
            f"# STDOUT TAIL\n{_tail(stdout, 2500)}",
            "",
            f"# STDERR TAIL\n{_tail(stderr, 1200)}",
        ]
    else:
        parts += [
            "",
            "# NOTE",
            "Blind-judge mode: script source, stdout, and stderr are",
            "INTENTIONALLY withheld. Rule on the screenshot and the",
            "asset counts above. If the evidence does not unambiguously",
            "show the goal achieved, return satisfied=false.",
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
    max_tokens: int = 800,
    temperature: float = 0.0,
    target: LLMTarget | None = None,
    blind: bool = False,
    job_id: str | None = None,
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
    # COMMIT-FIRST support: probe every video asset with ffprobe BEFORE
    # composing the judge prompt, so the judge can objectively check
    # ``video_valid`` (duration / resolution / codec) instead of trusting
    # the ``.mp4`` extension. This closes the #1 false-positive class in
    # the paprika audit (thumbnails / 8-sec previews / 728x90 banners).
    video_probes: list[dict] = []
    if assets_dir is not None and assets_dir.exists():
        try:
            _VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
            vfiles = [p for p in assets_dir.rglob("*")
                      if p.is_file() and p.suffix.lower() in _VIDEO_EXTS]
            # Cap: never probe more than 8 videos (typical page has 1-3;
            # a huge crawl has many but we only need a representative
            # sample for the judge's video_valid decision).
            vfiles = vfiles[:8]
            if vfiles:
                from server.hub._success_audit import _ffprobe_video
                probes = await asyncio.gather(
                    *[_ffprobe_video(p) for p in vfiles],
                    return_exceptions=True,
                )
                for p, pr in zip(vfiles, probes):
                    if isinstance(pr, Exception):
                        pr = None
                    video_probes.append({"name": p.name, "probe": pr})
        except Exception as e:
            log.debug("judge video probe crashed: %s", e)

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
        blind=blind,
        video_probes=video_probes,
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
            check_engine_thermal,
            record_engine_usage,
            EngineQuotaExceeded,
            EngineThermalThrottled,
        )
        try:
            check_engine_quota(tgt)
            await check_engine_thermal(tgt)
        except EngineQuotaExceeded as e:
            log.info(f"[judge] quota gate refused: {e}")
            return None
        except EngineThermalThrottled as e:
            log.info(f"[judge] thermal gate refused: {e}")
            return None
        from server.hub._ai_activity import track
        async with httpx.AsyncClient(timeout=tgt.timeout) as client:
            with track("judge", slug=getattr(tgt, "engine_slug", "")):
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
        try:
            from server.hub._ai_io_log import record_ai_io
            _user_str = ""
            try:
                _user_str = next((m.get("content","") for m in body.get("messages") or [] if m.get("role")=="user"), "") if isinstance(body, dict) else ""
            except Exception: pass
            if isinstance(_user_str, list):
                _user_str = " ".join(p.get("text","") for p in _user_str if isinstance(p, dict))
            record_ai_io(purpose="judge",
                         engine_slug=getattr(tgt, "engine_slug", "") or tgt.model,
                         job_id=job_id, prompt=str(_user_str), response=None,
                         latency_ms=int((time.time()-t0)*1000),
                         error=f"{type(e).__name__}: {e}")
        except Exception: pass
        return None
    elapsed_ms_call = int((time.time() - t0) * 1000)

    choices = payload.get("choices") or []
    raw = ""
    if choices:
        msg = choices[0].get("message") or {}
        raw = msg.get("content") or ""
    try:
        from server.hub._ai_io_log import record_ai_io
        _user_str = ""
        try:
            _user_str = next((m.get("content","") for m in body.get("messages") or [] if m.get("role")=="user"), "") if isinstance(body, dict) else ""
        except Exception: pass
        if isinstance(_user_str, list):
            _user_str = " ".join(p.get("text","") for p in _user_str if isinstance(p, dict))
        _u = payload.get("usage") or {}
        record_ai_io(purpose="judge",
                     engine_slug=getattr(tgt, "engine_slug", "") or tgt.model,
                     job_id=job_id, prompt=str(_user_str), response=raw,
                     latency_ms=elapsed_ms_call,
                     tokens_in=_u.get("prompt_tokens"),
                     tokens_out=_u.get("completion_tokens"))
    except Exception: pass

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
# Reasoning judge (v2 Phase 3)
#
# Higher-quality judge that reasons over a structured PerceptionResult
# (produced by the eye, Phase 1) instead of rummaging through
# stdout/stderr/screenshot. The reasoning engine (DeepSeek-R1, Claude,
# GPT, etc.) gets a compact factual brief and decides; it never sees
# raw HTML or pixels.  Same Verdict shape so iterative_codegen consumes
# it without changes.
#
# Opt-in via Settings → reasoning_judge_mode (or env
# PAPRIKA_R1_JUDGE_MODE for legacy compat).  Falls back to legacy
# ``judge_attempt`` when:
#   * the mode is off,
#   * no PerceptionResult could be produced for the attempt,
#   * the reasoning engine is unreachable / returns garbage.
# ---------------------------------------------------------------------------

_REASONING_JUDGE_SYSTEM_PROMPT = """You are the JUDGE for paprika browser automation (v2).

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

LANGUAGE: The natural-language fields (``reason``, ``hint`` if present,
any prose explanations) MUST be written in JAPANESE (日本語). Code,
selectors, identifiers, and API method names stay in English. Your
internal ``<think>...</think>`` reasoning may be in either language; the
operator-visible verdict text must be Japanese.
"""


# ---------------------------------------------------------------------------
# P-B: Adversarial refuter (arxiv 2603.06594 / 2607.05904)
#
# The primary judge (judge_attempt above) uses commit-first prompting to
# reduce false positives. But single-judge verdicts still leak "convincing
# but wrong" satisfied=true answers, and the paper's mitigation is to
# collect MULTIPLE independent judge-positive samples before trusting the
# verdict. This module adds an adversarial refuter that tries HARD to
# refute a claimed satisfied=true, run N times (see Setting
# ``judge_adversarial_n``). If ≥majority refute, iterative_codegen.py
# downgrades the verdict to satisfied=false.
#
# Only runs on satisfied=true verdicts (negative claims are self-verifying
# -- the agent retries anyway). Cost: ~$0.001 per refute pass at V4-Flash
# rates, called on ~10-20% of attempts.
# ---------------------------------------------------------------------------

_ADVERSARIAL_REFUTE_SYSTEM = """\
You are an ADVERSARIAL REFUTER for an agent-verdict decision.

CONTEXT: An autonomous web-automation agent tried to achieve a GOAL and
its primary judge said "satisfied=true" (goal achieved). YOUR JOB is to
try HARD to prove that judgment WRONG. Default to refuted=true when
uncertain -- your bias is to catch false positives that the primary
judge missed. If you cannot find any concrete failure evidence, only
then say refuted=false.

Look aggressively for these failure classes (all observed in real
paprika audits):

  * VIDEO_FAKE — a ``.mp4`` file that ffprobe says has duration<3s,
    resolution<200x200, banner aspect ratio, or NO video stream. These
    are thumbnails / previews / 728x90 ads mis-labelled as video.
  * PAGE_WALL — screenshot shows 404 / login / age-gate / captcha /
    blank / cookie-banner instead of the requested content. The agent
    never reached the goal even if the script exited 0.
  * COUNT_SHORT — goal asked for N items and outcome has < N/2. The
    letter of the goal is missed.
  * WRONG_CONTENT — assets exist but are from a wrong section (e.g.
    "video list" goal but downloaded thumbnails of an ad carousel).
  * SILENT_FAIL — stdout has "success" markers but stderr has repeated
    tool errors that were not compensated for.

Output strict JSON:

  {
    "refuted": <true|false>,
    "evidence": "<concrete evidence in 1-2 sentences: WHICH failure class + WHICH file / line / count. Empty if refuted=false.>",
    "confidence": <0.0-1.0>
  }

LANGUAGE: ``evidence`` MUST be written in JAPANESE (日本語). Keep file
names, error strings, URLs in English as written.
"""


async def judge_adversarial_refute(
    *,
    goal: str,
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
    temperature: float = 0.3,
    target: LLMTarget | None = None,
    blind: bool = False,
    job_id: str | None = None,
    lens_hint: str = "",
) -> dict | None:
    """One adversarial refutation pass. Returns
    ``{refuted, evidence, confidence}`` or None on failure. Uses the same
    outcome summary + ffprobe video probes as ``judge_attempt`` so both
    judges rule on identical evidence. ``lens_hint`` optionally biases
    the refuter toward one failure class (correctness / evidence /
    reproducibility) so parallel refuters cover complementary angles."""
    # Reuse the primary judge's video probes for consistency.
    video_probes: list[dict] = []
    if assets_dir is not None and assets_dir.exists():
        try:
            _VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
            vfiles = [p for p in assets_dir.rglob("*")
                      if p.is_file() and p.suffix.lower() in _VIDEO_EXTS][:8]
            if vfiles:
                from server.hub._success_audit import _ffprobe_video
                probes = await asyncio.gather(
                    *[_ffprobe_video(p) for p in vfiles],
                    return_exceptions=True,
                )
                for p, pr in zip(vfiles, probes):
                    if isinstance(pr, Exception):
                        pr = None
                    video_probes.append({"name": p.name, "probe": pr})
        except Exception as e:
            log.debug("refuter video probe crashed: %s", e)

    summary = _format_outcome_summary(
        goal=goal, exit_code=exit_code, elapsed_ms=elapsed_ms,
        timed_out=timed_out, stdout=stdout, stderr=stderr,
        assets_dir=assets_dir, progress_count=progress_count,
        target_pages=target_pages, blind=blind, video_probes=video_probes,
    )

    lens_prefix = ""
    if lens_hint:
        lens_prefix = (
            f"# REFUTATION LENS: {lens_hint}\n"
            f"Focus your refutation attempts through this lens specifically.\n\n"
        )

    user_content: object = lens_prefix + summary
    has_image = False
    if screenshot_path is not None and screenshot_path.exists():
        try:
            import base64
            img_bytes = screenshot_path.read_bytes()
            b64 = base64.b64encode(img_bytes).decode("ascii")
            mime = ("image/jpeg" if screenshot_path.suffix.lower() in (".jpg", ".jpeg")
                    else "image/png")
            user_content = [
                {"type": "text", "text": lens_prefix + summary + "\n\n# FINAL SCREENSHOT\n"
                    "Judge screenshot against goal -- 404 / login / age-gate / blank means fail."},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]
            has_image = True
        except Exception as e:
            log.debug("refuter screenshot embed crashed: %s", e)

    tgt = target or _env_default_target()
    body = {
        "model": tgt.model,
        "messages": [
            {"role": "system", "content": _ADVERSARIAL_REFUTE_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    body = adapt_chat_body(tgt, body)

    t0 = time.time()
    try:
        from server.hub.codegen import (
            check_engine_quota, check_engine_thermal, record_engine_usage,
            EngineQuotaExceeded, EngineThermalThrottled,
        )
        try:
            check_engine_quota(tgt)
            await check_engine_thermal(tgt)
        except (EngineQuotaExceeded, EngineThermalThrottled):
            return None
        from server.hub._ai_activity import track
        async with httpx.AsyncClient(timeout=tgt.timeout) as client:
            with track("judge_refute", slug=getattr(tgt, "engine_slug", "")):
                r = await client.post(tgt.url, json=body, headers=tgt.headers)
            if r.status_code >= 400:
                return None
            payload = r.json()
            record_engine_usage(tgt, payload.get("usage") or {})
    except Exception as e:
        log.info(f"[judge_refute] call failed: {type(e).__name__}: {e}")
        return None
    elapsed = int((time.time() - t0) * 1000)
    choices = payload.get("choices") or []
    if not choices:
        return None
    raw = (choices[0].get("message") or {}).get("content") or ""

    # Persist to ai_io_log for observability.
    try:
        from server.hub._ai_io_log import record_ai_io
        _prompt_str = user_content if isinstance(user_content, str) else "(multimodal + " + lens_hint + ")"
        _u = payload.get("usage") or {}
        record_ai_io(purpose="judge_refute",
                     engine_slug=getattr(tgt, "engine_slug", "") or tgt.model,
                     job_id=job_id, prompt=str(_prompt_str)[:2000], response=raw,
                     latency_ms=elapsed,
                     tokens_in=_u.get("prompt_tokens"),
                     tokens_out=_u.get("completion_tokens"),
                     extra={"lens": lens_hint, "has_image": has_image})
    except Exception:
        pass

    # Parse
    d = None
    try:
        d = json.loads(raw.strip())
    except Exception:
        for m in _JSON_RX.finditer(raw):
            try:
                d = json.loads(m.group(0))
                break
            except Exception:
                continue
    if not isinstance(d, dict) or "refuted" not in d:
        return None
    return {
        "refuted": bool(d.get("refuted")),
        "evidence": str(d.get("evidence") or "")[:400],
        "confidence": max(0.0, min(1.0, float(d.get("confidence") or 0.5))),
    }


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


async def judge_via_reasoning(
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
    blind: bool = False,
    job_id: str | None = None,
) -> Verdict | None:
    """Reasoning-model-based goal verification.

    A higher-quality judge that works with any reasoning-capable LLM
    (DeepSeek-R1, Claude, GPT, etc.). It receives structured
    PerceptionResult from the vision LLM plus stdout/stderr/script,
    and decides whether the goal was achieved. Never sees pixels --
    the visual signal arrives as structured text fields.

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
    # In blind mode these are intentionally withheld so R1 rules on
    # perception + asset counts only (see `judge_blind_mode` setting).
    stdout_tail = "" if blind else _tail(stdout, n_lines=30, max_chars=2000)
    stderr_tail = "" if blind else _tail(stderr, n_lines=20, max_chars=1500)

    # Script: include only when reasonably short (R1 doesn't need to see
    # 3000-line scripts to judge -- the assumption is that the script's
    # OUTPUT is the proof, not its source. We include short scripts to
    # help R1 explain WHY it failed in the hint field.)
    # Suppressed entirely in blind mode.
    script_block = ""
    if script and len(script) <= 4000 and not blind:
        script_block = f"SCRIPT\n{script}\n\n"

    if blind:
        user_msg = (
            f"GOAL\n{goal.strip()}\n\n"
            f"PERCEPTION\n{perception_brief}\n\n"
            f"OUTCOME\n"
            f"  exit_code:        {exit_code}\n"
            f"  assets_by_ext:    {assets_str}\n\n"
            f"NOTE\nBlind-judge mode: script source, stdout, stderr are\n"
            f"INTENTIONALLY withheld. Rule on the perception facts and\n"
            f"asset counts above. If the evidence does not unambiguously\n"
            f"show the goal achieved, return satisfied=false.\n\n"
            f"Produce the verdict JSON now."
        )
    else:
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
            {"role": "system", "content": _REASONING_JUDGE_SYSTEM_PROMPT},
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
            check_engine_thermal,
            record_engine_usage,
            EngineQuotaExceeded,
            EngineThermalThrottled,
        )
        try:
            check_engine_quota(target)
            await check_engine_thermal(target)
        except EngineQuotaExceeded as e:
            log.info(f"[judge:reasoning] quota gate refused: {e}")
            return None
        except EngineThermalThrottled as e:
            log.info(f"[judge:reasoning] thermal gate refused: {e}")
            return None
        from server.hub._ai_activity import track
        async with httpx.AsyncClient(timeout=target.timeout) as client:
            with track("judge", slug=getattr(target, "engine_slug", "")):
                r = await client.post(target.url, json=body, headers=target.headers)
            if r.status_code >= 400:
                log.info(
                    f"[judge:reasoning] LLM {r.status_code} from {target.url} "
                    f"model={target.model}: {r.text[:400]}"
                )
                return None
            payload = r.json()
            record_engine_usage(target, payload.get("usage") or {})
    except Exception as e:
        log.info(f"[judge:reasoning] LLM call failed: {type(e).__name__}: {e}")
        try:
            from server.hub._ai_io_log import record_ai_io
            record_ai_io(purpose="judge",
                         engine_slug=getattr(target, "engine_slug", "") or target.model,
                         job_id=job_id, prompt=user_msg, response=None,
                         latency_ms=int((time.time()-t0)*1000),
                         error=f"{type(e).__name__}: {e}",
                         extra={"reasoning_judge": True})
        except Exception: pass
        return None
    elapsed_ms_call = int((time.time() - t0) * 1000)

    choices = payload.get("choices") or []
    raw = ""
    if choices:
        msg = choices[0].get("message") or {}
        raw = msg.get("content") or ""
    try:
        from server.hub._ai_io_log import record_ai_io
        _u = payload.get("usage") or {}
        record_ai_io(purpose="judge",
                     engine_slug=getattr(target, "engine_slug", "") or target.model,
                     job_id=job_id, prompt=user_msg, response=raw,
                     latency_ms=elapsed_ms_call,
                     tokens_in=_u.get("prompt_tokens"),
                     tokens_out=_u.get("completion_tokens"),
                     extra={"reasoning_judge": True})
    except Exception: pass

    # DeepSeek can place reasoning in a separate reasoning_content field
    # (when configured that way). We ignore it -- only the answer matters.
    stripped = _strip_think_block(raw)

    verdict = _parse_verdict(stripped)
    if verdict is None:
        log.info(
            f"[judge:reasoning] could not parse verdict (model={payload.get('model','?')}, "
            f"raw[:200]={stripped[:200]!r})"
        )
        return None
    verdict.model = payload.get("model") or target.model
    verdict.elapsed_ms = elapsed_ms_call
    verdict.raw = raw  # keep the WITH-think version for debugging
    return verdict


# Backward-compat alias -- existing imports use the old name.
judge_via_r1 = judge_via_reasoning
