"""FastAPI app: thin wrapper over an Ollama-served gpt-oss-20b.

Receives a stateless ActRequest (goal + URL + accessibility tree +
history), forwards a chat-completion call to Ollama with our tool
schemas, and returns the parsed action as an ActResponse.

Env vars:
  OLLAMA_URL    Base URL of the Ollama server (default http://ollama:11434)
  MODEL_NAME    Tag the LLM is served as (default gpt-oss:20b)
  TEMPERATURE   Sampling temperature (default 0.0)
  REQUEST_TIMEOUT_S  HTTP timeout for the Ollama call (default 120)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

from prompt import (
    SYSTEM_PROMPT,
    TOOLS,
    build_user_message,
    parse_tool_call,
)
from schema import (
    ActRequest,
    ActResponse,
    HealthResponse,
    ParsedAction,
)


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-oss:20b")
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "120"))


app = FastAPI(title="paprika-agent", version="0.1")
_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
    print(
        f"[agent] OLLAMA_URL={OLLAMA_URL}  MODEL_NAME={MODEL_NAME}",
        flush=True,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client is not None:
        await _client.aclose()


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Probe the configured backend for liveness and model presence.

    Tries Ollama's /api/tags first (native shape), falls back to the
    OpenAI-compatible /v1/models endpoint (vLLM, llama-server, oga, ...).
    """
    assert _client is not None
    # Try Ollama's /api/tags first.
    try:
        r = await _client.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        if r.status_code == 200:
            tags = r.json().get("models", [])
            names = {t.get("name", "") for t in tags}
            present = MODEL_NAME in names or any(
                n.startswith(MODEL_NAME.split(":")[0] + ":") for n in names
            )
            return HealthResponse(
                ok=present,
                ollama_url=OLLAMA_URL,
                ollama_reachable=True,
                model_name=MODEL_NAME,
                model_present=present,
                error=None if present else
                    f"model '{MODEL_NAME}' not pulled in Ollama",
            )
    except Exception:
        pass
    # Fall back to OpenAI-compatible /v1/models.
    try:
        r = await _client.get(f"{OLLAMA_URL}/v1/models", timeout=5.0)
        r.raise_for_status()
        data = r.json()
        ids = {m.get("id", "") for m in (data.get("data") or [])}
        present = MODEL_NAME in ids
        return HealthResponse(
            ok=present,
            ollama_url=OLLAMA_URL,
            ollama_reachable=True,
            model_name=MODEL_NAME,
            model_present=present,
            error=None if present else
                f"model '{MODEL_NAME}' not served (available: {sorted(ids)[:5]})",
        )
    except Exception as e:
        return HealthResponse(
            ok=False,
            ollama_url=OLLAMA_URL,
            ollama_reachable=False,
            model_name=MODEL_NAME,
            model_present=False,
            error=str(e),
        )


@app.post("/act", response_model=ActResponse)
async def act(req: ActRequest) -> ActResponse:
    """Run one inference step and return the next action."""
    assert _client is not None

    user_text = build_user_message(
        goal=req.goal,
        url=req.url,
        ax_tree=req.ax_tree,
        text_content=req.text_content,
        history=req.history,
        step=req.step,
        max_steps=req.max_steps,
    )

    # When the caller ships a screenshot AND we're talking to a vision
    # model (auto-detect via "vl" / "vision" in MODEL_NAME), build a
    # multimodal user message. Text-only models get the text-only form
    # so we don't ship them an image they can't process.
    user_content: Any
    is_vision_model = any(
        marker in MODEL_NAME.lower()
        for marker in ("vl", "vision", "vlm", "multimodal")
    )
    if req.image_b64 and is_vision_model:
        user_content = [
            {"type": "text", "text": user_text},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{req.image_b64}",
                },
            },
        ]
    else:
        user_content = user_text

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "tools": TOOLS,
        # Force the model to emit a tool call (instead of free-form text
        # like `wait(reasoning="...")` that we'd then fail to parse).
        # OpenAI-style "required" is supported by Ollama 0.3+ and works
        # well with qwen2.5; the agent loop is built around exactly one
        # tool call per turn so "auto" was always the wrong call.
        "tool_choice": "required",
        "temperature": req.temperature,
        "max_tokens": req.max_new_tokens,
        "stream": False,
    }

    t0 = time.time()
    try:
        r = await _client.post(
            f"{OLLAMA_URL}/v1/chat/completions",
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            502,
            f"ollama returned {e.response.status_code}: "
            f"{e.response.text[:300]}",
        )
    except httpx.RequestError as e:
        raise HTTPException(502, f"ollama request failed: {e}")
    elapsed_ms = int((time.time() - t0) * 1000)

    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")
    tool_calls = msg.get("tool_calls") or []
    raw_content = msg.get("content") or ""

    if tool_calls:
        parsed = parse_tool_call(tool_calls[0])
    else:
        # gpt-oss occasionally emits the function call inside the content
        # as `{"name": "...", "arguments": {...}}` JSON. Try a best-effort
        # recovery before giving up.
        parsed = _recover_from_vllm_harmony(msg) or _recover_from_content(raw_content)
        # When recovery fails and finish_reason is "length", surface that
        # in the reasoning so the operator can tell apart "model refused
        # to pick a tool" from "model ran out of token budget".
        if parsed.kind == "unknown":
            extra = []
            if finish_reason == "length":
                extra.append("MAX_TOKENS: response truncated before tool call")
            rc = msg.get("reasoning_content") or msg.get("reasoning") or ""
            if rc:
                extra.append(f"reasoning_content (truncated): {rc[:300]}")
            if extra:
                base = parsed.reasoning or ""
                parsed.reasoning = (base + " | " + " | ".join(extra)).strip(" |")

    return ActResponse(
        action=parsed,
        raw=json.dumps(msg, ensure_ascii=False),
        inference_ms=elapsed_ms,
        model_name=MODEL_NAME,
        finish_reason=finish_reason,
    )


import re as _re


# Names the LLM might emit textually. Must stay in lockstep with
# prompt.TOOLS / _KIND_BY_TOOL.
_TOOL_NAMES_RE = _re.compile(
    r"\b(click|type|press_key|scroll|navigate|wait|capture|done)\s*\(",
    _re.IGNORECASE,
)
# vLLM serving gpt-oss-style models in the "harmony" response format
# without --tool-call-parser leaves tool_calls empty and stuffs the
# arguments alone (as bare JSON) into `content`, while leaking the
# intended tool name in `reasoning` / `reasoning_content` like
# "...Use the functions.done function.". Pull the name out of there.
_FUNCTIONS_NAME_RE = _re.compile(r"functions\.(\w+)", _re.IGNORECASE)


def _infer_tool_from_args(args: dict[str, Any]) -> str | None:
    """Guess which tool the model meant from the shape of its arguments.

    gpt-oss-120b on vLLM sometimes outputs bare-arguments JSON with
    neither tool_calls nor a `functions.X` mention in reasoning. We can
    still recover by looking at which argument keys are present, since
    each tool has a distinctive parameter signature.

    Returns None if no signature matches uniquely.
    """
    keys = set(args.keys()) - {"reasoning", "name"}
    if "selector" in keys and "text" in keys:
        return "type"
    if "selector" in keys:
        return "click"
    if "url" in keys and "selector" not in keys:
        return "navigate"
    if "direction" in keys:
        return "scroll"
    if "key" in keys:
        return "press_key"
    if "seconds" in keys:
        return "wait"
    if "label" in keys:
        return "capture"
    if "summary" in keys:
        return "done"
    return None


def _recover_from_vllm_harmony(msg: dict[str, Any]) -> ParsedAction | None:
    """Reconstruct a tool call when vLLM/harmony-format models leave
    `tool_calls` empty and stuff the arguments into `content`.

    Two recovery strategies, in order:

      1. The tool name is mentioned in `reasoning` /
         `reasoning_content` as `functions.<name>`. Use that.
      2. The tool name is nowhere -- infer from the *shape* of the
         arguments JSON (presence of `selector`, `direction`,
         `seconds`, etc.).

    Returns None when neither strategy fits; caller falls back to
    `_recover_from_content` for free-form text patterns.
    """
    if msg.get("tool_calls"):
        return None
    content = (msg.get("content") or "").strip()
    # Try to parse content as a JSON object first -- both recovery paths
    # need it.
    args: dict[str, Any] | None = None
    if content:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                args = parsed
        except json.JSONDecodeError:
            args = None

    # Strategy 1: explicit tool name in reasoning.
    reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
    if reasoning:
        m = _FUNCTIONS_NAME_RE.search(reasoning)
        if m:
            tool_name = m.group(1).lower()
            return parse_tool_call({
                "function": {"name": tool_name, "arguments": args or {}}
            })

    # Strategy 2: infer from argument shape.
    if args is not None:
        inferred = _infer_tool_from_args(args)
        if inferred:
            return parse_tool_call({
                "function": {"name": inferred, "arguments": args}
            })

    return None
# Key=value pair where the value is single-quoted, double-quoted, or
# bareword (number/identifier). Handles quotes-inside-quotes (e.g.
# selector='[data-paprika-id="3"]') because the lazy match closes on the
# matching outer quote.
_KV_RE = _re.compile(
    r"""(\w+)\s*=\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|([^,)]+))""",
    _re.DOTALL,
)


def _recover_from_content(text: str) -> ParsedAction:
    """Best-effort: pull a tool-call shape out of plain text if the model
    didn't put it in tool_calls. Two formats we know we'll see:

      1. Python-style `funcname(arg='v', arg2="x")` -- qwen2.5 falls back
         to this surprisingly often even with tool_choice=required, often
         followed by a free-form `-> {"reasoning": "..."}` blob.
      2. Bare JSON `{"name": "...", "arguments": {...}}` which some other
         instruction-tuned models default to.

    Returns kind='unknown' only when neither hits.
    """
    text = text.strip()
    if not text:
        return ParsedAction(kind="unknown")

    # ---- Format 1: funcname(args) -------------------------------------
    m = _TOOL_NAMES_RE.search(text)
    if m:
        name = m.group(1).lower()
        # Find the matching closing paren by depth-counting so nested
        # parens inside a value don't fool us.
        start = m.end() - 1  # index of '('
        depth = 0
        end = -1
        for i in range(start, len(text)):
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            args_str = text[start + 1:end]
            args: dict = {}
            for kv in _KV_RE.finditer(args_str):
                key = kv.group(1)
                val: object
                # Quoted -> string (groups 2/3). Bareword -> coerce to
                # int/float when possible, otherwise keep as a string.
                # parse_tool_call's numeric fields (scroll.amount,
                # wait.seconds) need real numbers, not strings.
                if kv.group(2) is not None:
                    val = kv.group(2)
                elif kv.group(3) is not None:
                    val = kv.group(3)
                else:
                    bareword = (kv.group(4) or "").strip()
                    try:
                        val = int(bareword)
                    except ValueError:
                        try:
                            val = float(bareword)
                        except ValueError:
                            val = bareword
                args[key] = val
            # The model often tacks `-> {"reasoning": "..."}` after the
            # function call. Fish that out for the reasoning field.
            tail = text[end + 1:].lstrip()
            if tail.startswith("->"):
                tail = tail[2:].strip()
            json_start = tail.find("{")
            if json_start != -1:
                try:
                    obj = json.loads(tail[json_start:tail.rfind("}") + 1])
                    if "reasoning" in obj and "reasoning" not in args:
                        args["reasoning"] = obj["reasoning"]
                except json.JSONDecodeError:
                    pass
            return parse_tool_call({"function": {"name": name, "arguments": args}})

    # ---- Format 2: {"name": "...", "arguments": {...}} JSON -----------
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        obj = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if "name" in obj and "arguments" in obj:
                        return parse_tool_call({"function": obj})
                    break
        start = text.find("{", start + 1)
    return ParsedAction(kind="unknown", reasoning=f"no tool call; raw: {text[:200]}")
