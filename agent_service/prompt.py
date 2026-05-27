"""System prompt + tool schemas + tool-call parser for the agent service.

We use the OpenAI-compatible chat/completions endpoint exposed by Ollama,
with gpt-oss-20b's native tool-calling. Every action the agent can take
is a separate tool; the LLM must pick exactly one per /act call.
"""
from __future__ import annotations

import json
from typing import Any

from schema import ParsedAction


SYSTEM_PROMPT = """\
You are an autonomous web-browsing agent. You drive a real Chrome browser
to accomplish a goal the user gave you.

Each turn you receive:
  - the goal,
  - the URL the browser is currently on,
  - an outline of every visible interactive element on the page, each
    one tagged with a unique numeric id like `[@N]`,
  - the history of actions you have already taken, with their outcomes
    (OK / NO_MATCH / ERR: ...) appended.

You must produce EXACTLY ONE action per turn, expressed as a tool call.
The browser executes the action and calls you back with the updated page
state. You never call multiple tools in one turn.

ELEMENT SELECTORS
=================
Every interactive element in the outline is shown as

    [@N] tag "visible text"  href=... value=...

…or, for links you have already opened during this job:

    [@N] tag "visible text" href=... visited=true

The `visited=true` flag (just another key=value column) marks an `<a>`
whose destination URL is in this session's visited set. The browser
shows visited links in purple via the :visited CSS pseudo-class, but
JavaScript can't read that for privacy reasons -- paprika
reconstructs the same hint from its own history of which URLs the
agent has been on. Treat `visited=true` as "I've been here, pick
another link" when iterating over a list. Re-clicking a marked link
is fine if the goal explicitly requires it (e.g. "refresh page 3").

The `[@N]` part is the DISPLAY LABEL only. The actual CSS selector to
pass to `click` / `type` is `[data-paprika-id="N"]` (note the quotes
and the full attribute syntax).

CORRECT example:

    outline shows:  [@3] a "More information…" href=https://example.org
    you call:       click(selector='[data-paprika-id="3"]')

WRONG examples (these will fail):

    click(selector='[@3]')                  <- the label, not a selector
    click(selector='a')                      <- ambiguous
    click(selector='[aria-label="Learn"]')   <- aria-label may not exist

DO NOT invent selectors like `[aria-label="..."]`, `.next-button`, or
`#some-id` -- they are unreliable and most of them won't match. Always
use the `[data-paprika-id="N"]` form for the N shown in the outline.

If the element you need does not appear in the outline, it isn't on
the page (or isn't currently visible). In that case scroll, navigate,
or call `done` -- don't guess.

OUTCOMES
========
History entries look like:

    click(selector='[data-paprika-id="3"]') -> OK
    click(selector='[data-paprika-id="9"]') -> NO_MATCH

`OK` means the click/type succeeded. `NO_MATCH` means the selector
didn't find an element -- the ids are regenerated each turn, so an id
from an earlier outline may not be the right one anymore: re-read the
current outline. If you see two consecutive failures, change strategy.

CAPTURE / DONE
==============
Call `capture(label=...)` on every page whose content the user wants to
keep (e.g. each article in a "collect the top 10 articles" goal). It
saves the page's HTML, screenshot, and outline; the browser stays put.
You can call `capture` multiple times during a job. Pick short, unique
labels so the operator can tell snapshots apart later.

NAVIGATING IN A LOOP
====================
For goals shaped like "click each link on the index page in turn":

  1. From the index, `click` link 1.
  2. (optional) `capture` the linked page.
  3. `back` to return to the index.
  4. From the index again, `click` an UNMARKED link -- look for one
     WITHOUT a `visited=true` flag in its outline line. The outline
     tags every link whose destination you've already opened during
     this job, so just scan top-to-bottom and pick the first entry
     that doesn't have `visited=true`. The numeric `@N` ids reshuffle
     each turn so don't trust them across turns -- the `visited=true`
     flag is the reliable "already done" signal.
  5. Repeat until you've covered the links you care about, then `done`.

Use `back` (the Back-button equivalent) instead of trying to memorise
the index page's URL and `navigate` back to it -- `back` is one tool
call and always works.

WHEN TO STOP
============
Call `done(summary=...)` AS SOON AS the goal is satisfied. Read the
goal literally:

  - "click the first link, then capture the page" -> ONE click, ONE
    capture, then done. Not three.
  - "find the top 10 articles" -> ten captures, then done.
  - "log in" -> one login, then done.

You are NOT a free-roaming explorer. If you have done what the goal
asked for, STOP. Extra clicks after the goal is met are wrong, not
helpful.

Also call `done` early when:
  - the goal turns out to be impossible (e.g. login required, page 404),
  - you've tried the same selector twice without progress,
  - you're not sure what to do next (better to stop and report than to
    keep clicking blind).

Keep `reasoning` short -- one sentence is plenty -- the user only sees
it for debugging.
"""


# Each tool maps 1:1 to a ParsedAction.kind. The descriptions are written
# for the model, not for the human reader.
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an element on the page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector that uniquely identifies the element to click.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Short reason for the click (one sentence).",
                    },
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": "Type text into an input/textarea element. Focuses it first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for the input or textarea to type into.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type. Replaces the element's current value.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Short reason for typing this text (one sentence).",
                    },
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a keyboard key (Enter, Tab, Escape, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key name as understood by CDP (e.g. 'Enter').",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Short reason for pressing this key.",
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right"],
                        "description": "Direction to scroll in.",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Pixels to scroll. Defaults to one viewport.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Short reason for scrolling.",
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Load a different URL in the same tab.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute URL to load.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Short reason for navigating to this URL.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "back",
            "description": (
                "Go back one entry in the browser history (equivalent to "
                "the browser's Back button / window.history.back()). Use "
                "this to return to the page you were on before the last "
                "navigation. Cheaper and more reliable than navigating to "
                "a memorised URL when you want to undo a click. The page "
                "you land on is the page you came from, so you may need "
                "to scroll back to where you were."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reasoning": {
                        "type": "string",
                        "description": "Short reason for going back.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Let the page settle for a moment (e.g. after navigation, before observing).",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "Seconds to wait, default 2.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Short reason for waiting.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture",
            "description": (
                "Save the current page (HTML + all loaded assets + a screenshot) "
                "to the job's output. Call this on every page whose content the "
                "user wants to keep -- e.g. each article in a list. The browser "
                "stays on the same page; this is purely a save operation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short label for this snapshot, used in the gallery (e.g. 'article-1', 'search-results').",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Short reason for capturing this page.",
                    },
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Signal that the goal is complete (or unrecoverably stuck).",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One-paragraph summary of what was accomplished or why we stopped.",
                    },
                },
                "required": ["summary"],
            },
        },
    },
]


# Map tool names to ParsedAction.kind values.
_KIND_BY_TOOL = {
    "click": "click",
    "type": "type",
    "press_key": "press_key",
    "scroll": "scroll",
    "navigate": "navigate",
    "back": "back",
    "wait": "wait",
    "capture": "capture",
    "done": "done",
}


def parse_tool_call(call: dict[str, Any]) -> ParsedAction:
    """Convert one OpenAI-style tool_call dict into a ParsedAction.

    Resilient to a few common formatting quirks: arguments may arrive as
    a JSON string or as a pre-parsed dict, and gpt-oss occasionally adds
    a stray trailing newline.
    """
    fn = call.get("function") or {}
    name = (fn.get("name") or "").strip()
    args_raw = fn.get("arguments", {})
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            return ParsedAction(kind="unknown", reasoning=f"unparseable args: {args_raw[:200]}")
    else:
        args = dict(args_raw or {})

    kind = _KIND_BY_TOOL.get(name)
    if kind is None:
        return ParsedAction(kind="unknown", reasoning=f"unknown tool: {name}")

    reasoning = _opt_str(args.get("reasoning"))

    if kind == "click":
        return ParsedAction(
            kind="click",
            selector=_opt_str(args.get("selector")),
            reasoning=reasoning,
        )
    if kind == "type":
        return ParsedAction(
            kind="type",
            selector=_opt_str(args.get("selector")),
            text=_opt_str(args.get("text")) or "",
            reasoning=reasoning,
        )
    if kind == "press_key":
        return ParsedAction(
            kind="press_key",
            key=_opt_str(args.get("key")),
            reasoning=reasoning,
        )
    if kind == "scroll":
        direction = _opt_str(args.get("direction")) or "down"
        amount_raw = args.get("amount")
        amount = int(amount_raw) if isinstance(amount_raw, (int, float)) else None
        return ParsedAction(
            kind="scroll",
            direction=direction if direction in ("up", "down", "left", "right") else "down",
            amount=amount,
            reasoning=reasoning,
        )
    if kind == "navigate":
        return ParsedAction(
            kind="navigate",
            url=_opt_str(args.get("url")),
            reasoning=reasoning,
        )
    if kind == "back":
        return ParsedAction(kind="back", reasoning=reasoning)
    if kind == "wait":
        seconds_raw = args.get("seconds")
        seconds = float(seconds_raw) if isinstance(seconds_raw, (int, float)) else None
        return ParsedAction(kind="wait", seconds=seconds, reasoning=reasoning)
    if kind == "capture":
        return ParsedAction(
            kind="capture",
            label=_opt_str(args.get("label")),
            reasoning=reasoning,
        )
    if kind == "done":
        return ParsedAction(
            kind="done",
            summary=_opt_str(args.get("summary")),
            reasoning=reasoning,
        )
    return ParsedAction(kind="unknown")  # unreachable: every kind handled


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def build_user_message(
    goal: str,
    url: str,
    ax_tree: str,
    text_content: str | None,
    history: list[str],
    step: int | None = None,
    max_steps: int | None = None,
) -> str:
    """Compose the per-turn user prompt the agent sees."""
    parts: list[str] = [f"Goal: {goal}", "", f"Current URL: {url}", ""]
    if step is not None and max_steps is not None:
        remaining = max(0, max_steps - step + 1)
        parts.append(
            f"Step {step} of {max_steps} (you have {remaining} action(s) "
            f"left before the loop is forcibly stopped). "
            f"Call `done` as soon as the goal is satisfied -- "
            f"do not keep going just because budget remains."
        )
        parts.append("")
    parts.append("Page outline (interactive elements indexed by id):")
    parts.append(ax_tree.strip() or "(empty)")
    if text_content:
        parts.extend(["", "Rendered text (truncated):", text_content.strip()])
    if history:
        parts.extend(["", "Actions taken so far (oldest first):"])
        for i, h in enumerate(history):
            parts.append(f"  {i + 1}. {h}")
    parts.extend(["", "Pick exactly ONE tool to call next."])
    return "\n".join(parts)
