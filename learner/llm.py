"""The single place the learner talks to a language model (design §9.3).

One client, one JSON parser, one accounting of calls and tokens -- the old
system had about ten of each. Two things matter to the rest of the
learner:

  * ``LLMUnavailable`` is INFRASTRUCTURE (engine overheated, unreachable,
    timed out, 5xx). It is never counted as "the Solver failed on this
    site"; the old system counted 31% of such events as attempts.
  * the model's reply is only ever a proposal for the next action. Nothing
    it says is taken as a fact about success.

Which engine: explicit env (``LEARNER_LLM_ENDPOINT`` / ``_MODEL`` /
``_API_KEY``) or, when no endpoint is given, whatever the hub's engine
registry currently resolves for ``LEARNER_LLM_KIND`` -- which also gives
the hub's thermal failover for free. Only OpenAI-compatible chat engines
are supported.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import re
import time


class LLMUnavailable(Exception):
    """The engine could not answer. Infra, not a learning signal."""


class LLMBadOutput(Exception):
    """The engine answered with something that is not the JSON asked for."""


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_json_object(text: str) -> dict:
    """The first JSON object in ``text``. Tolerates reasoning tags, code
    fences and prose around it. Raises LLMBadOutput."""
    if not isinstance(text, str) or not text.strip():
        raise LLMBadOutput("empty reply")
    t = _THINK_RE.sub("", text)
    m = _FENCE_RE.search(t)
    if m:
        t = m.group(1)
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
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
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:i + 1])
                    except Exception:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = t.find("{", start + 1)
    raise LLMBadOutput(f"no JSON object in reply: {text[:120]!r}")


def chat_url(endpoint: str) -> str:
    e = (endpoint or "").rstrip("/")
    if e.endswith("/chat/completions"):
        return e
    if e.endswith("/v1"):
        return e + "/chat/completions"
    return e + "/v1/chat/completions"


class Meter:
    """Calls and tokens of ONE Solver run.

    The client is shared by the Solver runs that go on at the same time, so
    "the client's counter now minus the counter when I started" would bill a
    run for its neighbours' calls. Each run starts its own meter instead; it
    lives in a context variable, which every asyncio task has its own copy of.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.tokens = 0


_METER: contextvars.ContextVar = contextvars.ContextVar("learner_llm_meter", default=None)


def start_meter() -> Meter:
    m = Meter()
    _METER.set(m)
    return m


def count(*, calls: int = 0, tokens: int = 0) -> None:
    """Add to the current run's meter, if a run is being metered."""
    m = _METER.get()
    if m is not None:
        m.calls += calls
        m.tokens += tokens


class LLM:
    def __init__(self, *, client=None, endpoint: str = "", model: str = "", api_key: str = "",
                 hub_url: str = "", kind: str = "vision-chat", worker_secret: str = "",
                 timeout_s: float = 90.0, resolve_ttl_s: float = 60.0,
                 max_tokens: int = 1500, thinking: bool | None = None) -> None:
        # ``max_tokens`` is a cap, not a target. It has to leave room for a
        # reasoning model's thinking: llama.cpp / vLLM return that in a
        # separate ``reasoning_content`` field but count it against the cap,
        # and a reply cut off mid-thought has no answer in it at all.
        self._max_tokens = int(max_tokens)
        # Reasoning models (Qwen3 and the like): None = leave the engine's
        # default alone, True / False = ask for thinking on / off through
        # the chat template. Whatever the setting, a reply that was cut off
        # while still thinking is retried once with thinking off -- seen
        # live 2026-11-12: a 235B model spent the whole 1,500-token cap
        # deliberating over one click, three times in a row.
        self._thinking = thinking
        self.cutoffs = 0
        self._client = client
        self._own_client = client is None
        self._endpoint, self._model, self._api_key = endpoint, model, api_key
        self._hub_url = hub_url.rstrip("/")
        self._kind = kind
        self._secret = worker_secret
        self._timeout = timeout_s
        self._ttl = resolve_ttl_s
        self._resolved: dict | None = None
        self._resolved_at = 0.0
        self.calls = 0
        self.tokens = 0
        self.model = model

    async def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def resolve(self) -> dict:
        """{endpoint, model, api_key, headers} of the engine to call now."""
        if self._endpoint:
            return {"endpoint": self._endpoint, "model": self._model,
                    "api_key": self._api_key, "headers": {}}
        now = time.monotonic()
        if self._resolved is not None and now - self._resolved_at < self._ttl:
            return self._resolved
        if not self._hub_url:
            raise LLMUnavailable("no LLM endpoint configured")
        client = await self._http()
        try:
            resp = await client.post(
                f"{self._hub_url}/engines/auto/{self._kind}/resolve",
                json={"secret": self._secret}, timeout=15.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise LLMUnavailable(f"engine resolve: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            # 503 = every engine of the kind is thermally throttled.
            raise LLMUnavailable(f"engine resolve: HTTP {resp.status_code}")
        rec = resp.json() or {}
        if str(rec.get("protocol") or "openai") != "openai":
            raise LLMUnavailable(f"engine {rec.get('slug')!r} is not OpenAI-compatible")
        self._resolved = {
            "endpoint": str(rec.get("endpoint") or ""), "model": str(rec.get("model") or ""),
            "api_key": str(rec.get("api_key") or ""), "headers": dict(rec.get("headers") or {}),
        }
        self._resolved_at = now
        self.model = self._resolved["model"]
        return self._resolved

    async def chat_json(self, system: str, user: str, *, max_tokens: int | None = None,
                        temperature: float = 0.0) -> dict:
        """One chat turn that must come back as a JSON object."""
        eng = await self.resolve()
        if not eng["endpoint"]:
            raise LLMUnavailable("engine has no endpoint")
        headers = dict(eng.get("headers") or {})
        if eng.get("api_key"):
            headers["Authorization"] = f"Bearer {eng['api_key']}"
        body = {
            "model": eng["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": int(max_tokens or self._max_tokens),
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        if self._thinking is not None:
            body["chat_template_kwargs"] = {"enable_thinking": bool(self._thinking)}
        content, cut_off = await self._post(eng, headers, body)
        if cut_off and self._thinking is not False:
            self.cutoffs += 1
            body["chat_template_kwargs"] = {"enable_thinking": False}
            content, cut_off = await self._post(eng, headers, body)
        if cut_off:
            raise LLMBadOutput("reply cut off before the answer (raise max_tokens)")
        return parse_json_object(content)

    async def _post(self, eng: dict, headers: dict, body: dict) -> tuple[str, bool]:
        """One completion call -> (answer text, was it cut off with no answer)."""
        client = await self._http()
        self.calls += 1
        count(calls=1)
        try:
            resp = await client.post(chat_url(eng["endpoint"]), json=body, headers=headers,
                                     timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._resolved = None  # re-resolve next time: maybe another engine is up
            raise LLMUnavailable(f"chat: {type(e).__name__}: {e}") from e
        code = int(resp.status_code)
        if code >= 500 or code in (408, 429):
            self._resolved = None
            raise LLMUnavailable(f"chat: HTTP {code}")
        if code >= 400:
            raise LLMUnavailable(f"chat: HTTP {code}: {str(getattr(resp, 'text', ''))[:160]}")
        try:
            data = resp.json()
            usage = data.get("usage") or {}
            self.tokens += int(usage.get("total_tokens") or 0)
            count(tokens=int(usage.get("total_tokens") or 0))
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
        except Exception as e:
            raise LLMBadOutput(f"malformed completion: {type(e).__name__}") from e
        return content, (not content.strip() and choice.get("finish_reason") == "length")
