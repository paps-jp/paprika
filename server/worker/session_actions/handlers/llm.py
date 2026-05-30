"""Session-action handlers (llm). Auto-registered into
_SESSION_ACTIONS via the @_session_action decorator."""
from __future__ import annotations
import os

from server.worker.session_actions._registry import _session_action, _ActionCtx
from server.worker import browser_ops


@_session_action("ask", read_only=True)
async def _act_ask(agent, ctx: "_ActionCtx") -> None:
    # LLM-based yes/no question. Sends current outline
    # + URL + the question to the configured text LLM
    # (Qwen 2.5-VL via AGENT_LLM_URL) with a strict
    # "answer yes or no" prompt. Parses the response
    # leniently; anything unparseable defaults to
    # False (the safe / non-acting branch).
    action = ctx.action
    reply = ctx.reply
    tab = ctx.tab
    state = ctx.state
    cur = ctx.cur
    _slog = ctx.slog
    question = (action.get("question") or "").strip()
    if not question:
        reply.status = "ERR: ask failed: empty question"
        reply.result = False
    else:
        # Outline = compact accessibility tree (text +
        # role + visible-element list). Cap to a few
        # KB to fit in the prompt.
        try:
            outline_text = await browser_ops.outline(
                tab,
                visited_urls=state.visited_urls,
            )
        except Exception as e:
            outline_text = f"(outline failed: {e})"
        outline_text = (outline_text or "")[:3500]

        # Engine resolution: the script can pick a
        # specific chat backend via ``engine=`` (e.g.
        # "chatgpt51"), or "auto" / unset to use the
        # promoted chat engine on the hub. We hit the
        # hub's /engines/.../resolve endpoint, which
        # returns the endpoint + model + API key the
        # operator configured in the admin UI. Falls
        # back to AGENT_LLM_URL when the registry has
        # nothing to say (fresh deploy, hub unreachable).
        requested_engine = (action.get("engine") or "auto").strip()
        resolved = await agent.resolve_engine(
            requested_engine,
            fallback_kind="chat",
        )
        if resolved:
            llm_base = (resolved.get("endpoint") or "").rstrip("/")
            llm_model = resolved.get("model") or "qwen2.5-vl-72b"
            llm_api_key = resolved.get("api_key") or ""
            llm_headers = dict(resolved.get("headers") or {})
            llm_timeout = float(resolved.get("timeout_s") or 30)
            llm_protocol = resolved.get("protocol") or "openai"
        else:
            llm_base = os.environ.get(
                "AGENT_LLM_URL",
                "http://<gpu-host>:15082",
            ).rstrip("/")
            llm_model = os.environ.get(
                "AGENT_MODEL_NAME",
                "qwen2.5-vl-72b",
            )
            llm_api_key = ""
            llm_headers = {}
            llm_timeout = 30.0
            llm_protocol = "openai"

        prompt = (
            "You are inspecting a web page. Answer the user's "
            'question with strictly the single word "yes" or '
            '"no". No explanation, no quotes, no punctuation. '
            'If you cannot tell with confidence, answer "no".\n\n'
            f"Current URL: {cur or '(unknown)'}\n"
            f"Page outline (excerpt):\n{outline_text}\n\n"
            f"Question: {question}\n"
            "Answer (yes or no):"
        )
        import httpx as _httpx

        req_headers = {"Content-Type": "application/json"}
        if llm_api_key:
            req_headers["Authorization"] = f"Bearer {llm_api_key}"
        req_headers.update(llm_headers)
        body_req = {
            "model": llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 8,
        }
        answer_text = ""
        # ``page.ask`` is documented as a chat-style
        # check, so we require an OpenAI-compat
        # protocol. agent-service / cogagent / native
        # anthropic aren't wired up for arbitrary chat
        # at this layer yet.
        if llm_protocol not in ("openai",):
            _slog(
                f"ask: engine '{requested_engine}' "
                f"protocol={llm_protocol!r} not supported "
                f"for page.ask (need openai-compat); "
                f"falling back to AGENT_LLM_URL"
            )
            llm_base = os.environ.get(
                "AGENT_LLM_URL",
                "http://<gpu-host>:15082",
            ).rstrip("/")
            llm_model = os.environ.get(
                "AGENT_MODEL_NAME",
                "qwen2.5-vl-72b",
            )
            req_headers = {"Content-Type": "application/json"}
            body_req["model"] = llm_model
        try:
            async with _httpx.AsyncClient(timeout=llm_timeout) as cli:
                rr = await cli.post(
                    f"{llm_base}/v1/chat/completions",
                    headers=req_headers,
                    json=body_req,
                )
                rr.raise_for_status()
                data = rr.json()
                answer_text = (
                    (data.get("choices") or [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
        except Exception as e:
            _slog(
                f"ask: LLM call failed via "
                f"engine={requested_engine!r} "
                f"endpoint={llm_base!r}: "
                f"{type(e).__name__}: {e}"
            )
            reply.status = f"ERR: ask failed: LLM unreachable ({type(e).__name__})"
            reply.result = False
        else:
            # Lenient parsing: strip punctuation / quotes,
            # check leading word.
            a = answer_text.strip().lower()
            a = a.lstrip("'\"`*. ").rstrip("'\"`*. ,!?")
            head = a.split()[0] if a else ""
            if head.startswith("yes") or head == "y" or head == "true":
                reply.result = True
            elif head.startswith("no") or head == "n" or head == "false":
                reply.result = False
            else:
                _slog(
                    f"ask: unparseable LLM answer: "
                    f"{answer_text!r}, defaulting to False"
                )
                reply.result = False
            _slog(f"ask {question!r} -> {reply.result} (LLM said {answer_text!r})")


@_session_action("extract", read_only=False)
@_session_action("observe", read_only=False)
async def _act_extract_observe(agent, ctx: "_ActionCtx") -> None:
    # paprika-native structured LLM helpers. Both share
    # the same engine-resolution + chat-completions
    # plumbing as ``ask`` above; the difference is the
    # prompt shape (JSON Schema for extract, candidate
    # list for observe) and the response parsing (the
    # SDK does Pydantic validation on extract; the hub
    # passes observe's array back as-is for the SDK to
    # wrap in Candidate objects).
    kind = ctx.action.get("kind") or ""
    action = ctx.action
    reply = ctx.reply
    tab = ctx.tab
    state = ctx.state
    cur = ctx.cur
    _slog = ctx.slog
    instruction = (
        action.get("instruction") or action.get("intent") or ""
    ).strip()
    if not instruction:
        reply.status = f"ERR: {kind}: empty instruction"
        reply.result = [] if kind == "observe" else None
    else:
        # Collect the page context. ``extract`` lets the
        # caller pick outline vs html via context=; defaults
        # to outline (compact, [@N]-annotated, plenty for
        # most extraction tasks). ``observe`` is always
        # outline-based -- it specifically maps intent to
        # the [@N] markers.
        ctx_mode = "outline"
        if kind == "extract":
            ctx_mode = (action.get("context") or "outline").lower()
            if ctx_mode not in ("outline", "html"):
                ctx_mode = "outline"
        max_chars = int(action.get("max_chars") or 12000)
        try:
            if ctx_mode == "html":
                page_ctx = await browser_ops.html_excerpt(
                    tab,
                    max_chars=max_chars,
                ) if hasattr(browser_ops, "html_excerpt") else ""
                if not page_ctx:
                    page_ctx = await browser_ops.outline(
                        tab,
                        visited_urls=state.visited_urls,
                    )
            else:
                page_ctx = await browser_ops.outline(
                    tab,
                    visited_urls=state.visited_urls,
                )
        except Exception as e:
            page_ctx = f"(context fetch failed: {e})"
        page_ctx = (page_ctx or "")[:max_chars]

        # Engine resolve -- same pattern as ``ask``.
        requested_engine = (action.get("engine") or "auto").strip()
        resolved = await agent.resolve_engine(
            requested_engine,
            fallback_kind="chat",
        )
        if resolved:
            llm_base = (resolved.get("endpoint") or "").rstrip("/")
            llm_model = resolved.get("model") or "qwen2.5-vl-72b"
            llm_api_key = resolved.get("api_key") or ""
            llm_headers = dict(resolved.get("headers") or {})
            llm_timeout = float(resolved.get("timeout_s") or 60)
            llm_protocol = resolved.get("protocol") or "openai"
        else:
            llm_base = os.environ.get(
                "AGENT_LLM_URL",
                "http://<gpu-host>:15082",
            ).rstrip("/")
            llm_model = os.environ.get(
                "AGENT_MODEL_NAME",
                "qwen2.5-vl-72b",
            )
            llm_api_key = ""
            llm_headers = {}
            llm_timeout = 60.0
            llm_protocol = "openai"
        if llm_protocol not in ("openai",):
            _slog(
                f"{kind}: engine {requested_engine!r} "
                f"protocol={llm_protocol!r} not supported "
                f"(need openai-compat); falling back to "
                f"AGENT_LLM_URL"
            )
            llm_base = os.environ.get(
                "AGENT_LLM_URL",
                "http://<gpu-host>:15082",
            ).rstrip("/")
            llm_model = os.environ.get(
                "AGENT_MODEL_NAME",
                "qwen2.5-vl-72b",
            )
            llm_api_key = ""
            llm_headers = {}

        # Build the prompt. The schema_json string (for
        # extract) and the candidate-shape spec (for
        # observe) are explicit so the LLM has no excuse
        # to drift from JSON. Variables are NEVER
        # substituted in the prompt -- the LLM sees the
        # raw ``${name}`` placeholders, never the real
        # values; substitution happens only at the CDP
        # edge (browser_ops.execute).
        if kind == "extract":
            schema_json = (action.get("schema_json") or "").strip()
            sys_prompt = (
                "You are a precise structured-data extractor. "
                "Read the page context below and return data "
                "that matches the JSON Schema. Output JSON ONLY "
                "-- no markdown fences, no prose, no comments. "
                "If a field cannot be determined from the page, "
                "use null (or omit when the schema allows). "
                "Do not invent values."
            )
            user_prompt = (
                f"Current URL: {cur or '(unknown)'}\n"
                f"Page context ({ctx_mode}):\n{page_ctx}\n\n"
                f"JSON Schema:\n{schema_json}\n\n"
                f"Instruction: {instruction}\n\n"
                "Output (JSON only):"
            )
        else:  # observe
            max_results = int(action.get("max_results") or 5)
            sys_prompt = (
                "You identify interactive elements on a web "
                "page that match the user's intent. The page "
                "outline labels each element with [@N] markers. "
                "Return up to N candidates as a JSON array. "
                "Each candidate is an object with these keys:\n"
                '  "paprika_id"  integer matching an [@N]\n'
                '  "selector"    "[data-paprika-id=\\"N\\"]" '
                "(same N as paprika_id)\n"
                '  "description" short JP/EN label for the '
                "element\n"
                '  "method"      one of "click", "fill", '
                '"press", "type", "hover", "select_option" '
                "or null when unsure\n"
                '  "arguments"   array of strings when the '
                "method needs args (e.g. fill value), else "
                "null. ${name} placeholders are allowed and "
                "will be substituted later.\n"
                '  "confidence"  float 0..1 (your own '
                "estimate)\n"
                "Output JSON ONLY (the array). No markdown, "
                "no prose, no trailing text."
            )
            user_prompt = (
                f"Current URL: {cur or '(unknown)'}\n"
                f"Page outline:\n{page_ctx}\n\n"
                f"Intent: {instruction}\n"
                f"Max results: {max_results}\n\n"
                "Output (JSON array only):"
            )

        import httpx as _httpx
        req_headers = {"Content-Type": "application/json"}
        if llm_api_key:
            req_headers["Authorization"] = f"Bearer {llm_api_key}"
        req_headers.update(llm_headers)
        body_req = {
            "model": llm_model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            # extract/observe can need more room than ask's
            # 8 tokens; the LLM emits a JSON object/array.
            "max_tokens": 1500,
        }
        answer_text = ""
        try:
            async with _httpx.AsyncClient(timeout=llm_timeout) as cli:
                rr = await cli.post(
                    f"{llm_base}/v1/chat/completions",
                    headers=req_headers,
                    json=body_req,
                )
                rr.raise_for_status()
                data = rr.json()
                answer_text = (
                    (data.get("choices") or [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
        except Exception as e:
            _slog(
                f"{kind}: LLM call failed via "
                f"engine={requested_engine!r} "
                f"endpoint={llm_base!r}: "
                f"{type(e).__name__}: {e}"
            )
            reply.status = (
                f"ERR: {kind} failed: LLM unreachable "
                f"({type(e).__name__})"
            )
            reply.result = [] if kind == "observe" else None
        else:
            # Strip common LLM-decorations (```json fences,
            # leading "Here is the JSON:" prose, etc.) so
            # plain json.loads succeeds without a regex zoo.
            import json as _json

            raw = answer_text.strip()
            if raw.startswith("```"):
                # Drop opening fence + optional language tag.
                nl = raw.find("\n")
                if nl != -1:
                    raw = raw[nl + 1:]
                # Drop closing fence.
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
            try:
                parsed = _json.loads(raw)
            except Exception as e:
                _slog(
                    f"{kind}: LLM response was not JSON: "
                    f"{raw[:200]!r}"
                )
                reply.status = (
                    f"ERR: {kind} failed: LLM response "
                    f"was not valid JSON ({type(e).__name__})"
                )
                reply.result = [] if kind == "observe" else None
            else:
                reply.result = parsed
                _slog(
                    f"{kind} {instruction!r} -> "
                    f"{type(parsed).__name__} "
                    f"({len(parsed) if hasattr(parsed, '__len__') else '-'})"
                )
