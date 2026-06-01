"""Verbalise operator-recorder events via a vision LLM.

Each captured event arrives from the agent extension as a structured
dict (type, target.outer/text/selector, bbox, url, viewport, optional
clip dataURL). On its own it's just data -- impossible to grep through,
hard to feed into a few-shot LLM context. This module turns each event
into ONE natural-language sentence describing what the operator did,
using Qwen3-VL-32B (the vision-chat engine the operator already has
wired in via the 'qwen' engine record).

Architecture
------------
Per-event one-shot: send (url + target HTML + clip image) to the VLM,
ask for a single sentence. Slow but predictable; failure on event N
doesn't poison event N+1. Concurrency is bounded by a small semaphore
so a 20-event batch doesn't fan-out 20 vLLM requests at once and OOM
the worker.

Engine selection
----------------
Reads the 'qwen' engine record from ``state.engines``. That record
points at the local vLLM (currently Qwen3-VL-32B FP8 on
10.10.50.26:15082, served-model-name 'qwen3.5'). Operator changes
visible via /engines/qwen are picked up automatically.

Failure mode
------------
Best-effort throughout. Any event that doesn't get a summary keeps the
empty string in its summary field; the caller can still display the
raw event. Errors surfaced per-event in a `summary_error` field for
debug.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from server.hub._state import state


log = logging.getLogger(__name__)


# Concurrency cap for the vLLM batch. A single vLLM instance handles
# multiple requests via continuous batching, but operator screenshots
# are big multimodal payloads -- limit fan-out so we don't blow KV
# cache headroom.
_MAX_CONCURRENT = int(os.environ.get("OPREC_VERBALIZE_CONCURRENCY", "4"))
_PER_CALL_TIMEOUT_S = float(
    os.environ.get("OPREC_VERBALIZE_TIMEOUT_S", "45"),
)

# Engine slug to use. The qwen engine has a VL-capable model
# (Qwen3-VL-32B-FP8) as of our 2026-06 deployment.
_ENGINE_SLUG = os.environ.get("OPREC_VERBALIZE_ENGINE", "qwen")


_SYSTEM_PROMPT = (
    "You are an interaction analyst. The user shows you ONE moment from "
    "an operator's browser session: a small screenshot of the element "
    "they interacted with (with a bit of surrounding context), plus the "
    "element's HTML, its text, the page URL, and what kind of event "
    "fired (click, change, keydown, submit, unload).\n\n"
    "Output ONE concise sentence describing what the operator did and "
    "what UI element they targeted. Be specific about labels, button "
    "text, link text, or form field roles when visible. NEVER invent "
    "details that aren't in the input. Reply in the same language as "
    "the page when obvious; otherwise English.\n\n"
    "Examples of good output:\n"
    "  - 'Clicked the \"Sign in\" button in the top-right of the page header.'\n"
    "  - 'Pressed Enter inside the email input field on a login form.'\n"
    "  - 'Typed a search query into the toolbar search box (value redacted).'\n"
    "  - 'モーダル内のオレンジ色の「はい」ボタンをクリックした (年齢確認の同意)。'\n\n"
    "Reply with the sentence ONLY. No JSON, no markdown fences, no prefix."
)


def _engine_target() -> tuple[str, str, dict] | None:
    """Return (endpoint, model, headers) for the configured engine, or
    None if no such engine is wired in."""
    er = state.engines
    if er is None:
        return None
    rec = er.get(_ENGINE_SLUG)
    if rec is None or not rec.endpoint:
        return None
    headers = dict(rec.headers or {})
    # Auth: prefer direct value, fall back to named env var.
    key = (rec.api_key or "").strip()
    if not key and rec.api_key_env:
        key = os.environ.get(rec.api_key_env, "").strip()
    if key:
        headers.setdefault("Authorization", f"Bearer {key}")
    return rec.endpoint.rstrip("/"), rec.model or _ENGINE_SLUG, headers


def _build_user_message(event: dict) -> list[dict]:
    """Compose the user-side content blocks for one event.

    OpenAI-style multimodal: a list with text + image_url parts.
    When the event has no clip, send text-only (still useful: outer
    HTML + URL + type often tell the story for nav/unload events)."""
    tgt = event.get("target") or {}
    url = event.get("url") or ""
    typ = event.get("type") or "?"
    outer = (tgt.get("outer") or "")[:300]
    text = (tgt.get("text") or "")[:150]
    sel = (tgt.get("selector") or "")[:200]
    bbox = tgt.get("bbox") or {}
    parts: list[dict] = []
    text_block = (
        f"Event type: {typ}\n"
        f"Page URL:   {url}\n"
        f"Element selector: {sel}\n"
        f"Element text:     {text!r}\n"
        f"Element HTML:     {outer}\n"
        f"Element bbox (CSS px): {bbox}\n"
    )
    if typ == "change":
        val = event.get("value")
        red = event.get("redacted")
        if red:
            text_block += "Value:           (redacted — password / sensitive field)\n"
        else:
            text_block += f"Value typed:     {val!r}\n"
    elif typ == "keydown":
        text_block += f"Key pressed:     {event.get('key')!r}\n"
    parts.append({"type": "text", "text": text_block})
    clip = event.get("clip")
    if clip and isinstance(clip, str) and clip.startswith("data:image/"):
        parts.append({
            "type": "image_url",
            "image_url": {"url": clip},
        })
    return parts


async def _verbalise_one(
    event: dict,
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    model: str,
    headers: dict,
) -> tuple[str, str]:
    """Return (summary, error) for one event."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(event)},
        ],
        "temperature": 0.1,
        "max_tokens": 160,
    }
    try:
        r = await client.post(
            f"{endpoint}/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=_PER_CALL_TIMEOUT_S,
        )
        if r.status_code >= 400:
            return "", f"HTTP {r.status_code}: {r.text[:200]}"
        payload = r.json()
        choices = payload.get("choices") or []
        if not choices:
            return "", "no choices returned"
        msg = (choices[0].get("message") or {}).get("content") or ""
        summary = msg.strip()
        # Some models love to wrap in quotes / fences -- strip.
        if summary.startswith('"') and summary.endswith('"') and len(summary) > 1:
            summary = summary[1:-1].strip()
        return summary, ""
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"


async def verbalize_events(events: list[dict]) -> list[dict]:
    """Add a `.summary` (and on failure `.summary_error`) field to each
    event in-place; return the same list. Events without a clip and
    without enough context (e.g. unload with no target) get a stub
    summary derived from type+url so the output is still useful.

    Caller responsibility: deepcopy the events if they want the
    originals unchanged."""
    tgt = _engine_target()
    if tgt is None:
        for e in events:
            e["summary_error"] = (
                f"engine '{_ENGINE_SLUG}' not configured -- "
                "register it under /engines first"
            )
        return events
    endpoint, model, headers = tgt

    sem = asyncio.Semaphore(_MAX_CONCURRENT)
    async with httpx.AsyncClient(timeout=_PER_CALL_TIMEOUT_S + 5.0) as cli:
        async def _one(ev: dict) -> None:
            # Trivial-event shortcut: an `unload` carries no DOM target;
            # synthesise a deterministic summary instead of burning a VLM
            # call. Same for any event whose target has no outer HTML.
            typ = ev.get("type") or "?"
            outer = ((ev.get("target") or {}).get("outer") or "").strip()
            if typ == "unload" or not outer:
                ev["summary"] = (
                    f"[{typ}] page {ev.get('url', '?')}"
                    if typ == "unload"
                    else f"[{typ}] no target HTML captured"
                )
                return
            async with sem:
                summary, err = await _verbalise_one(
                    ev, client=cli, endpoint=endpoint,
                    model=model, headers=headers,
                )
            if summary:
                ev["summary"] = summary
            else:
                ev["summary_error"] = err

        await asyncio.gather(*(_one(e) for e in events))
    return events
