"""SearXNG-backed web search exposed to codegen as an OpenAI tool.

The codegen LLM has access to internal "skills" (paprika-specific
recipes) but no way to look up external knowledge -- a Twitter selector
that changed last week, the docstring of a niche stdlib function, the
URL structure of a site it hasn't seen, etc. This module gives the
model exactly one tool, ``web_search(query, max_results)``, that hits
the operator's local SearXNG instance and returns trimmed snippets.

Design notes
------------
* **Tool-call only, no pre-injection.** The model decides whether to
  search; we don't burn tokens on every attempt. Cheaper and more
  adaptive than auto-search.
* **One tool, not two.** Page fetch (``web_fetch``) is intentionally
  omitted from v1: snippets are usually enough, and a full-page fetch
  invites token blow-up + adversarial-page prompt injection. If a
  result page truly needs reading, the model can open it via
  ``cli.session()`` in its generated script.
* **In-memory LRU cache** keyed by ``(query, max_results)`` with a 30-
  minute TTL. Codegen retries within an attempt often repeat the same
  query; the cache cuts SearXNG load + latency for free.
* **Graceful degradation.** When SearXNG is down or unreachable, the
  tool returns ``{"error": "search unavailable", ...}`` rather than
  raising. The model can decide to proceed without external context;
  failing the whole job because a sidecar is down is overkill.
* **Off when unconfigured.** No ``SEARXNG_URL`` env var -> module-level
  flag ``ENABLED`` is False -> ``generate_script`` will skip the tools
  array entirely. The feature is purely opt-in via configuration.

Wired into the Coder LLM call by ``codegen.generate_script``. Not
exposed to Planner or Judge in v1 (the user picked Coder-only).
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict

import httpx

# ---------------------------------------------------------------------------
# Config (resolved at call time from hub settings -> env var -> static
# default. Operators can toggle these from the admin UI without restart;
# see server/hub/settings.py keys ``searxng_url``, ``searxng_timeout_s``,
# ``web_search_max_calls``.)
# ---------------------------------------------------------------------------

# Per-tool-call result cap. The model can request fewer via ``max_results``;
# this is the absolute ceiling (the model can't trick us into asking SearXNG
# for 100 results and dumping a wall of text into the next round-trip).
MAX_RESULTS_CAP: int = 10

# Per-result snippet character cap. SearXNG ``content`` fields are usually
# 1-3 sentences (~150 chars) but occasionally run several hundred; trim so
# the model sees title+url+gist without an unbounded token tail.
SNIPPET_CHAR_CAP: int = 240

# LRU cache. (Small. The model's queries within one job repeat a lot.)
_CACHE_TTL_S: float = 30 * 60
_CACHE_MAX_ENTRIES: int = 512
_cache: OrderedDict[tuple, tuple[float, list[dict]]] = OrderedDict()


def _settings_registry():
    """Resolve the hub's SettingsRegistry, or None if not initialised.

    Reads ``state.settings`` directly -- since #2B-A extracted
    ``state`` into ``server.hub._state``, the import-cycle dance with
    ``server.hub.app`` is no longer required. ``None`` happens
    naturally during the brief startup window before ``lifespan``
    instantiates the registry (and during unit tests that never
    enter lifespan).
    """
    from server.hub._state import state

    return getattr(state, "settings", None)


def get_url() -> str:
    """Effective SearXNG URL. settings.json -> SEARXNG_URL env -> empty.
    Empty string means the tool is disabled."""
    reg = _settings_registry()
    if reg is not None:
        v = reg.get("searxng_url", "")
        if v:
            return str(v).rstrip("/")
    # _settings_registry's get() already does env fallback via
    # _env_default(); the branch here covers the brief startup window
    # before state.settings exists.
    return (os.environ.get("SEARXNG_URL") or "").rstrip("/")


def get_timeout() -> float:
    """Effective SearXNG request timeout (seconds)."""
    reg = _settings_registry()
    if reg is not None:
        v = reg.get("searxng_timeout_s", None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    raw = os.environ.get("SEARXNG_TIMEOUT_S")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 15.0


def get_max_calls() -> int:
    """Effective per-attempt cap on web_search tool calls. A well-
    behaved model finishes in 1-3 searches; the cap exists to bound
    token cost / latency when a model gets confused and keeps re-
    searching. 0 -> tool effectively disabled (no calls allowed) even
    when SearXNG is reachable."""
    reg = _settings_registry()
    if reg is not None:
        v = reg.get("web_search_max_calls", None)
        if v is not None:
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                pass
    raw = os.environ.get("WEB_SEARCH_MAX_CALLS")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return 5


def is_enabled() -> bool:
    """Whether the web_search tool is active. False when SearXNG is
    unconfigured OR the operator capped calls at 0."""
    return bool(get_url()) and get_max_calls() > 0


# ---------------------------------------------------------------------------
# OpenAI tool definition (the shape the LLM sees)
# ---------------------------------------------------------------------------

TOOL_DEFINITION: dict = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web via the operator's local SearXNG instance. "
            "Use this when paprika's built-in skills/conventions do not "
            "cover what you need -- e.g. an external API's call shape, a "
            "stdlib function's exact signature, the URL structure or a "
            "CSS class name on a site you haven't seen. Do NOT use it "
            "for general background ('what is X'); only when a concrete "
            "code-level fact would unblock you. Returns a list of "
            "{title,url,snippet}; keep snippets in mind as hints, not "
            "ground truth -- verify by reading the URL only if needed "
            "(open it from your generated script via cli.session)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The search query. Plain keywords work best; site: filters are supported."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": ("Number of results to return. Default 5, max 10."),
                    "default": 5,
                    "minimum": 1,
                    "maximum": MAX_RESULTS_CAP,
                },
            },
            "required": ["query"],
        },
    },
}


# Short system-prompt addendum appended to the Coder's base prompt when
# the tool is active. Kept terse so it doesn't drown the existing rules.
SYSTEM_PROMPT_ADDENDUM: str = (
    "\nTool available\n"
    "--------------\n"
    "You may call the ``web_search(query, max_results=5)`` function to "
    "look up external facts that paprika's built-in skills don't cover "
    "(third-party API shapes, unfamiliar site selectors, stdlib detail). "
    "Use it sparingly -- 0-2 calls is typical, 5 is the hard cap. After "
    "you have what you need, emit the final Python script as your "
    "assistant message content (no further tool calls).\n"
)


# ---------------------------------------------------------------------------
# Cache + SearXNG client
# ---------------------------------------------------------------------------


def _cache_get(key: tuple) -> list[dict] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if (time.time() - ts) > _CACHE_TTL_S:
        # Expired -- drop it and miss.
        try:
            del _cache[key]
        except KeyError:
            pass
        return None
    # Refresh LRU position.
    _cache.move_to_end(key)
    return value


def _cache_put(key: tuple, value: list[dict]) -> None:
    _cache[key] = (time.time(), value)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


async def _searxng_query(query: str, max_results: int) -> list[dict]:
    """Hit SearXNG, normalise results, return ``[{title,url,snippet}]``.

    Raises :class:`httpx.HTTPError` on transport failures; the
    ``run_tool`` wrapper above turns those into a tool result the model
    can see, rather than letting the codegen request blow up.
    """
    url = get_url()
    timeout = get_timeout()
    params = {"q": query, "format": "json"}
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(f"{url}/search", params=params)
        r.raise_for_status()
        data = r.json()
    out: list[dict] = []
    for item in (data.get("results") or [])[:max_results]:
        snippet = (item.get("content") or "").strip()
        if len(snippet) > SNIPPET_CHAR_CAP:
            snippet = snippet[: SNIPPET_CHAR_CAP - 1].rstrip() + "…"
        out.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": (item.get("url") or "").strip(),
                "snippet": snippet,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Public entry point used by codegen.generate_script's tool-call loop
# ---------------------------------------------------------------------------


async def run_tool(name: str, arguments: dict) -> dict:
    """Execute one tool call, return the JSON-able result the LLM sees.

    The return value is always a dict; on errors / unknown tools it
    carries an ``error`` field rather than raising, so the calling loop
    can pass it back to the model and let the model react (typically by
    proceeding without external context).
    """
    if name != "web_search":
        return {"error": f"unknown tool {name!r}"}
    if not is_enabled():
        # Hit when either SEARXNG_URL is unset OR web_search_max_calls
        # was lowered to 0 from the admin UI. The model sees an
        # informative error and can decide to proceed without external
        # context.
        return {"error": "web_search disabled (SearXNG URL unset or max_calls=0)"}

    query = (arguments or {}).get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "query is required and must be a non-empty string"}
    query = query.strip()

    requested = (arguments or {}).get("max_results")
    try:
        max_results = int(requested) if requested is not None else 5
    except (TypeError, ValueError):
        max_results = 5
    max_results = max(1, min(max_results, MAX_RESULTS_CAP))

    cache_key = (query.lower(), max_results)
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"query": query, "results": cached, "cached": True}

    try:
        results = await _searxng_query(query, max_results)
    except Exception as e:
        # Treat SearXNG-side failures as "unavailable" rather than
        # propagating: the model can decide whether to proceed without
        # external context. Logged by the caller via the returned dict.
        return {
            "query": query,
            "error": f"search unavailable: {type(e).__name__}: {e}",
            "results": [],
        }

    _cache_put(cache_key, results)
    return {"query": query, "results": results, "cached": False}
