"""Forensics / analyze agent: LLM-driven investigation loop over a live
session.

Given a goal like *"explain why <X> happens on this page"*, the LLM
iteratively asks for JS probes (``page.evaluate``) and reads results
until it can write a structured report. One tool only -- raw JS in the
page's main world -- with a pre-flight regex safety check that rejects
side-effects (navigation, form submit, clicks, cookie / storage writes,
``POST`` fetches, ``window.open``, ``innerHTML =``, etc.).

Different from the codegen-loop, which WRITES a paprika script to
complete a task and is judged pass/fail:

  * codegen-loop  : SCRIPT goal-completion, sandbox-run, judge OK/NG.
  * forensics     : READ-ONLY investigation, no script, human-readable
                    report. Suited for "why doesn't X work?" diagnosis.

The session must already exist (operator-controlled scope). The loop
caps probe steps + truncates results so the LLM context budget stays
bounded. Every probe is recorded in a trace so operators can audit
exactly what the agent looked at.

Env (defaults):
  * ``FORENSICS_LLM_URL``           -- chat-completions endpoint
                                       (default: ``CODEGEN_LLM_URL``)
  * ``FORENSICS_MODEL_NAME``        -- model name
                                       (default: ``CODEGEN_MODEL_NAME``)
  * ``FORENSICS_DEFAULT_MAX_STEPS`` -- default cap on probe steps (18)
  * ``FORENSICS_PER_CALL_TIMEOUT_S``-- per LLM call timeout (120)
  * ``FORENSICS_RESULT_MAX_CHARS``  -- truncate each probe result (8000)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from server.hub.codegen import (
    CODEGEN_LLM_URL,
    CODEGEN_MODEL_NAME,
    LLMTarget,
    adapt_chat_body,
)

log = logging.getLogger(__name__)


FORENSICS_LLM_URL = os.environ.get(
    "FORENSICS_LLM_URL", CODEGEN_LLM_URL,
).rstrip("/")
FORENSICS_MODEL_NAME = os.environ.get(
    "FORENSICS_MODEL_NAME", CODEGEN_MODEL_NAME,
)
FORENSICS_DEFAULT_MAX_STEPS = int(
    os.environ.get("FORENSICS_DEFAULT_MAX_STEPS", "18"),
)
FORENSICS_PER_CALL_TIMEOUT_S = float(
    os.environ.get("FORENSICS_PER_CALL_TIMEOUT_S", "120"),
)
FORENSICS_RESULT_MAX_CHARS = int(
    os.environ.get("FORENSICS_RESULT_MAX_CHARS", "8000"),
)


# ---------------------------------------------------------------------------
# Pre-flight probe safety check.
# ---------------------------------------------------------------------------
# The LLM is told (system prompt) that probes are read-only. This regex
# pass is a defense-in-depth net so a malformed plan doesn't accidentally
# perturb the session under investigation. It's not adversarial-proof
# (the LLM could base64-encode a bad call) -- the threat model assumes
# the LLM is paprika's trusted endpoint, not an attacker.
_BLOCKED: list[tuple[re.Pattern, str]] = [
    (re.compile(r"location\s*\.\s*(?:href|replace|assign)\s*="),
     "location.href / replace / assign"),
    (re.compile(r"\bwindow\s*\.\s*location\s*="),
     "window.location ="),
    (re.compile(r"history\s*\.\s*(?:pushState|replaceState|go|back|forward)\b"),
     "history.pushState / replaceState / go / back / forward"),
    (re.compile(r"\.\s*submit\s*\(\s*\)"),
     "form .submit()"),
    (re.compile(r"\.\s*click\s*\(\s*\)"),
     ".click()  (use page.evaluate to INSPECT, not click)"),
    (re.compile(r"document\s*\.\s*cookie\s*="),
     "document.cookie ="),
    (re.compile(r"\b(?:localStorage|sessionStorage)\s*\.\s*"
                r"(?:setItem|removeItem|clear)\b"),
     "storage write (setItem / removeItem / clear)"),
    (re.compile(r"\bindexedDB\s*\.\s*(?:deleteDatabase|open)\b"),
     "indexedDB write"),
    (re.compile(r"window\s*\.\s*open\s*\("),
     "window.open("),
    (re.compile(r"method\s*:\s*['\"]POST['\"]", re.I),
     "fetch with method: 'POST' (read-only only)"),
    (re.compile(r"\.\s*open\s*\(\s*['\"]POST['\"]"),
     "XMLHttpRequest .open('POST', ...)"),
    (re.compile(r"document\s*\.\s*write\s*\("),
     "document.write("),
    (re.compile(r"\.\s*innerHTML\s*="),
     ".innerHTML ="),
    (re.compile(r"\.\s*outerHTML\s*="),
     ".outerHTML ="),
    (re.compile(r"navigator\s*\.\s*sendBeacon\b"),
     "navigator.sendBeacon"),
    (re.compile(r"chrome\s*\.\s*(?:downloads|tabs|runtime)\s*\."),
     "chrome.downloads / tabs / runtime"),
]


def safety_check(js: str) -> str | None:
    """Return a reason string when ``js`` should be rejected, else
    ``None``. Best-effort; see threat model in module docstring."""
    for rx, why in _BLOCKED:
        if rx.search(js):
            return why
    return None


# ---------------------------------------------------------------------------
# LLM system prompt + JSON action protocol.
# ---------------------------------------------------------------------------
_SYSTEM = """\
You are a FORENSICS ANALYST. You are inspecting a live web page in a
real browser to answer a specific question. Work iteratively: each
turn, either ask for ONE JS probe and read the result, or finish with a
report. You have a SINGLE tool:

  evaluate(js, await_promise=false)
    Runs `js` in the page's main world; returns the JSON-serialised value.
    Set await_promise=true when `js` returns a Promise.

You CAN:
  * Read DOM: document.querySelectorAll, getComputedStyle,
    getBoundingClientRect, attributes, computed styles.
  * Read network: performance.getEntriesByType('resource') -- URLs,
    sizes, timings of every resource the page loaded.
  * fetch(url) for GET / no-cors requests (same-origin or public assets);
    handy to grab JS / JSON the page loaded so you can de-obfuscate
    or pattern-match without re-running it.
  * Un-eval a Dean-Edwards packer:  src = eval(text.replace(/^\\s*eval/, ""))
    The packer's IIFE is pure (returns the source string); evaluating
    it without the leading `eval` extracts the unpacked source safely.
  * Probe globals: Object.keys(window).filter(k => /pattern/i.test(k))
    to find site-defined functions / vars.
  * Call existing site JS functions (e.g. window.someShowFn()) when you
    suspect a reveal/trigger; this is allowed because it's the site's
    own JS in the site's own page. Do NOT click DOM elements.
  * Pattern-match with regex. STRINGIFY before returning (objects come
    back as RemoteObject descriptors otherwise -- always JSON.stringify).

You MUST NOT (probes will be REJECTED automatically):
  * Navigate or change URL  (location.href=, location.replace, history.*).
  * Submit forms or call element .click() / .submit().
  * Mutate cookies, localStorage, sessionStorage, IndexedDB.
  * Write to the DOM  (.innerHTML=, .outerHTML=, document.write).
  * POST anything  (fetch with method:'POST', XHR .open('POST', ...)).
  * Open new windows (window.open).

PROBE DESIGN
  * Keep `expression` a single self-contained JS expression. IIFEs are
    fine: `(()=>{ ... return JSON.stringify(out); })()`.
  * Return SMALL, SUMMARISED values (counts, samples, booleans). Each
    result is truncated to ~8000 characters before you see it -- a
    bare `document.documentElement.outerHTML` is wasted budget.
  * When fetching a remote resource, return its length + a short head
    snippet + the booleans/URLs you actually need, not the full body.
  * If you fetch JS that looks packed (`eval(function(p,a,c,k,e,r){...`),
    un-eval it and scan the unpacked source.

OUTPUT FORMAT (every turn -- STRICT JSON, no markdown fences, no prose):

  // To probe:
  {"thought": "<one short sentence: why this probe>",
   "action": "evaluate",
   "expression": "<JS as one expression>",
   "await_promise": false}

  // When you have your answer:
  {"thought": "<why you're done>",
   "action": "finish",
   "report": "<your report; Markdown OK; include FINDINGS, EVIDENCE (verbatim values you observed), CONCLUSION, NEXT STEPS>"}

RULES
1. Output VALID JSON only (parseable by json.loads). Escape quotes and
   newlines properly.
2. ONE probe per turn. Each turn must be either action=evaluate or
   action=finish.
3. Don't loop forever -- once your evidence supports a coherent answer,
   finish. There is a hard step cap.
4. Cite verbatim values in your final report (function names, URLs,
   counts you observed) so the operator can audit.
"""


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
@dataclass
class ProbeStep:
    n: int
    thought: str
    expression: str
    await_promise: bool
    result: str | None = None
    error: str | None = None
    elapsed_ms: int = 0


@dataclass
class ForensicsResult:
    completed: bool
    steps_taken: int
    max_steps: int
    report: str
    trace: list[ProbeStep] = field(default_factory=list)
    model: str = ""
    elapsed_ms: int = 0


EvaluateFn = Callable[[str, bool], Awaitable[tuple[bool, Any, int]]]


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------
async def _chat(history: list[dict], *, max_tokens: int = 2400) -> str:
    body: dict = {
        "model": FORENSICS_MODEL_NAME,
        "messages": [{"role": "system", "content": _SYSTEM}] + history,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    # Reuse codegen's model-family quirks (Qwen3 no-think, DeepSeek-R1
    # bigger budget, etc.). adapt_chat_body inspects body["model"] only,
    # so a minimal LLMTarget stub is enough.
    target = LLMTarget(url=FORENSICS_LLM_URL, model=FORENSICS_MODEL_NAME)
    body = adapt_chat_body(target, body)
    async with httpx.AsyncClient(timeout=FORENSICS_PER_CALL_TIMEOUT_S) as cli:
        r = await cli.post(f"{FORENSICS_LLM_URL}/v1/chat/completions", json=body)
        r.raise_for_status()
        payload = r.json()
    content = ""
    choices = payload.get("choices") or []
    if choices:
        content = (choices[0].get("message") or {}).get("content") or ""
    return content


_JSON_TAIL_RE = re.compile(r"\{[\s\S]*\}\s*$")
_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", re.I)
_FENCE_CLOSE = re.compile(r"\s*```\s*$")


def _parse_action(raw: str) -> dict:
    """Best-effort parser. Strips Markdown fences, falls back to the
    last `{...}` substring. Returns a stub finish action on failure so
    the loop terminates cleanly with a diagnosable report."""
    s = (raw or "").strip()
    s = _FENCE_OPEN.sub("", s)
    s = _FENCE_CLOSE.sub("", s).strip()
    try:
        return json.loads(s)
    except Exception:
        m = _JSON_TAIL_RE.search(s)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {
        "action": "finish",
        "thought": "parser-fallback",
        "report": (
            "Investigation aborted: LLM returned output that could not "
            "be parsed as JSON.\n\nRaw output (truncated):\n"
            + (raw or "")[:1500]
        ),
    }


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
async def run_forensics(
    *,
    goal: str,
    page_url: str | None,
    evaluate_fn: EvaluateFn,
    max_steps: int | None = None,
) -> ForensicsResult:
    """Drive the forensics loop. ``evaluate_fn(js, await_promise)``
    returns ``(ok, value_or_err, elapsed_ms)``; the caller wraps the
    existing session evaluate transport, so this module stays
    decoupled from FastAPI / the worker protocol."""
    t0 = time.time()
    cap = int(max_steps or FORENSICS_DEFAULT_MAX_STEPS)
    cap = max(1, min(cap, 60))  # bound the bound

    user_msg = (
        f"GOAL: {goal.strip()}\n"
        f"Session URL: {page_url or '(unknown)'}\n"
        f"Probe step cap: {cap}\n"
        "Begin. Output JSON only."
    )
    history: list[dict] = [{"role": "user", "content": user_msg}]
    trace: list[ProbeStep] = []
    completed = False
    final_report = ""

    for n in range(1, cap + 1):
        try:
            raw = await _chat(history)
        except Exception as e:
            final_report = (
                f"Investigation aborted at step {n}: LLM call failed "
                f"({type(e).__name__}: {e})."
            )
            break

        act = _parse_action(raw)
        action = str(act.get("action") or "").lower()

        if action == "finish":
            completed = True
            final_report = str(act.get("report") or "(no report)")
            history.append({"role": "assistant", "content": raw})
            break

        thought = str(act.get("thought") or "").strip()
        expr = str(act.get("expression") or "").strip()
        awp = bool(act.get("await_promise"))
        step = ProbeStep(
            n=n, thought=thought, expression=expr, await_promise=awp,
        )

        if not expr:
            step.error = "empty expression"
        else:
            blocked = safety_check(expr)
            if blocked:
                step.error = f"BLOCKED by safety check: {blocked}"
            else:
                try:
                    ok, val, ms = await evaluate_fn(expr, awp)
                    step.elapsed_ms = int(ms or 0)
                    if ok:
                        if isinstance(val, (dict, list)):
                            s = json.dumps(
                                val, ensure_ascii=False, default=str,
                            )
                        elif val is None:
                            s = "null"
                        else:
                            s = str(val)
                        if len(s) > FORENSICS_RESULT_MAX_CHARS:
                            s = (
                                s[:FORENSICS_RESULT_MAX_CHARS]
                                + f"\n…[truncated; total was {len(s)} chars]"
                            )
                        step.result = s
                    else:
                        step.error = str(val)[:1500]
                except Exception as e:
                    step.error = f"{type(e).__name__}: {e}"

        trace.append(step)
        history.append({"role": "assistant", "content": raw})
        if step.error:
            tool_msg = f"PROBE {n} ERROR: {step.error}"
        else:
            tool_msg = f"PROBE {n} RESULT:\n{step.result}"
        # Budget hint: tell the model how many probe steps remain so it
        # can decide to wrap up. When the budget is nearly exhausted,
        # escalate to an explicit instruction to finish -- otherwise an
        # exploratory model burns every step and never synthesises a
        # report (the operator then gets a raw probe dump, not an answer).
        remaining = cap - n
        if remaining <= 0:
            tool_msg += (
                "\n\n[BUDGET] This was the LAST probe step. Do NOT probe "
                "again. Reply now with action=finish and a complete report "
                "explaining your findings based on everything observed."
            )
        elif remaining <= 3:
            tool_msg += (
                f"\n\n[BUDGET] Only {remaining} probe step(s) left. If you "
                "already have enough to explain the issue, reply with "
                "action=finish now instead of probing further."
            )
        history.append({"role": "user", "content": tool_msg})

    if not completed and not final_report:
        # Hit the cap without the model calling finish. Give it ONE last
        # turn whose only job is to synthesise a report from everything
        # gathered -- no further probing allowed. This turns "ran out of
        # budget" from a raw probe dump into an actual answer.
        history.append({
            "role": "user",
            "content": (
                "STOP PROBING. You have reached the step cap. Using only "
                "the evidence already gathered above, output a final JSON "
                'object: {"action":"finish","report":"<your full findings '
                'and best explanation of the goal, in Markdown>"}. '
                "Do not request any more probes."
            ),
        })
        try:
            raw = await _chat(history)
            act = _parse_action(raw)
            rep = str(act.get("report") or "").strip()
            if rep and rep != "(no report)":
                final_report = rep
                # Mark as completed only if the model genuinely emitted a
                # finish action; otherwise leave completed=False so the UI
                # still flags it as budget-truncated.
                if str(act.get("action") or "").lower() == "finish":
                    completed = True
        except Exception:
            pass

    if not completed and not final_report:
        # Synthesis call also failed/empty -- fall back to a bounded dump
        # of the conversation tail so the operator gets *something*
        # debuggable rather than nothing.
        tail = history[-1]["content"] if history else "(empty)"
        final_report = (
            f"Investigation hit max_steps={cap} without a final report. "
            f"Last context message:\n\n{tail[:1500]}"
        )

    return ForensicsResult(
        completed=completed,
        steps_taken=len(trace),
        max_steps=cap,
        report=final_report,
        trace=trace,
        model=FORENSICS_MODEL_NAME,
        elapsed_ms=int((time.time() - t0) * 1000),
    )
