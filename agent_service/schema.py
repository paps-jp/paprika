"""Request / response schemas for the paprika browsing-agent service.

The service wraps a text-only LLM (default: gpt-oss-20b via Ollama) and
returns one selector-based browser action per call. Coordinates live in
CDP land; the LLM only ever sees / emits CSS selectors, which the
paprika worker hands straight to nodriver / DevTools Protocol.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---- Request --------------------------------------------------------------

class ActRequest(BaseModel):
    """One inference step.

    Inputs are all text. The caller is responsible for capturing the page
    state (URL, accessibility tree, optionally plain-text content) and
    serialising the action history; the service is stateless.
    """
    # Free-text task description, e.g. "Find the top 10 popular articles
    # and click into each one in turn".
    goal: str = Field(..., min_length=1)
    # The URL the browser is currently on. Helps the model orient itself
    # without having to infer from the AX tree.
    url: str = Field(..., min_length=1)
    # Accessibility tree rendered as text. The worker builds this from
    # CDP's Accessibility.getFullAXTree and trims it to fit the context.
    ax_tree: str = Field(..., min_length=1)
    # Optional: rendered visible-text snapshot of the page. Useful for
    # extraction-style goals ("find articles about X") where the AX tree
    # alone might not surface the prose.
    text_content: str | None = None
    # Prior step records, oldest first. Each entry is a short summary like
    # 'click(selector="a.next-page")' so the model can avoid loops.
    history: list[str] = Field(default_factory=list)
    # Optional screenshot of the current page (PNG, base64-encoded).
    # When set AND the configured model is a vision LLM, agent_service
    # ships it as a multimodal image part to the chat-completions call.
    # Non-vision models silently ignore it. Helps the model recognise
    # page state changes that the text outline alone can't convey
    # (modal dismissed, content unlocked, etc.).
    image_b64: str | None = None
    # Current step index (1-based) and max budget. When set, the user
    # message includes a "step N of M, X remaining" hint so the model
    # knows when to call done() instead of running until max_steps.
    step: int | None = None
    max_steps: int | None = None
    # Hard cap on generated tokens for one /act call. 4096 leaves room
    # for verbose reasoning models (gpt-oss-* in particular can dump
    # 1-2k tokens of analysis before settling on a tool call). When the
    # budget runs out mid-reasoning, vLLM returns an empty message and
    # the agent loop has nothing to act on -- finish_reason="length"
    # in that case, surfaced in the /act response.
    max_new_tokens: int = 4096
    # Sampling temperature. 0.0 = greedy = deterministic-ish.
    temperature: float = 0.0


# ---- Response -------------------------------------------------------------

ActionKind = Literal[
    "click", "type", "press_key", "scroll", "navigate", "back",
    "wait", "capture", "done", "unknown",
]


class ParsedAction(BaseModel):
    """The next step the agent wants the browser to take.

    `kind` is always set; the type-specific fields are filled only when
    relevant. Callers should check `kind` first and ignore the rest.
    """
    # Pydantic v2 complains about field names starting with 'model_';
    # we don't have any here, but disabling protected namespaces also
    # silences future warnings if a downstream BaseModel inherits this.
    model_config = ConfigDict(protected_namespaces=())

    kind: ActionKind
    # CLICK / TYPE: CSS selector for the target element.
    selector: str | None = None
    # TYPE: text to enter.
    text: str | None = None
    # PRESS_KEY: key name as understood by CDP (e.g. "Enter", "Tab").
    key: str | None = None
    # SCROLL: direction and optional pixel amount.
    direction: Literal["up", "down", "left", "right"] | None = None
    amount: int | None = None
    # NAVIGATE: explicit URL to load.
    url: str | None = None
    # WAIT: seconds to sleep before the next observation.
    seconds: float | None = None
    # CAPTURE: label the worker will tag the saved snapshot with, so the
    # operator can tell multiple captures apart in the gallery later.
    label: str | None = None
    # DONE: short human-readable summary of the result.
    summary: str | None = None
    # Optional free-text reasoning the model attached. Logged for debug.
    reasoning: str | None = None


class ActResponse(BaseModel):
    action: ParsedAction
    # Free-text content the model emitted alongside the tool call (some
    # models put their chain-of-thought here). Always carried back so the
    # paprika side can log it for replay.
    raw: str
    # Wall-clock latency of the underlying LLM call, ms.
    inference_ms: int
    # Model name actually used (after env resolution).
    model_name: str
    # What the LLM server said about why generation ended: "stop",
    # "tool_calls", "length" (max_tokens hit), etc. Exposed because
    # "length" -> empty content is a recurring diagnostic. Optional for
    # backends that don't report it.
    finish_reason: str | None = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    ok: bool
    ollama_url: str
    ollama_reachable: bool
    model_name: str
    model_present: bool
    # Free-form description of why ok=False, if applicable.
    error: str | None = None
