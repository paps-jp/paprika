"""LLM availability checks for graceful degrade.

paprika has two halves:

  * **Core (always works)**: ``mode=fetch``, page primitives
    (goto/click/fill/scroll), ``pap.walk``, ``page.download_video``,
    profile / extension / host registries, the entire admin UI shell.
    None of these touch an external LLM.

  * **LLM-powered**: ``mode=codegen-loop``, ``mode=vision-agent``,
    ``page.agent()``, ``page.ask()``, ``page.extract()``,
    ``page.observe()``, the judge + planner inside the codegen-loop,
    skill / convention auto-distillation, the codegen Coder LLM's
    ``web_search`` tool.

This module is the single place that decides "is the LLM half
operational right now?". A Windows single-user install with no API
key configured gets a friendly 503 from every LLM-touching endpoint,
plus a banner in the admin UI, while everything else keeps working.

Definition of "operational": at least one engine in EngineRegistry
that has a non-placeholder endpoint AND either an inline ``api_key``
or an ``api_key_env`` that resolves to a non-empty value. Built-in
seed engines that still point at ``http://<gpu-host>:...`` count as
"placeholder" and don't satisfy the check.
"""

from __future__ import annotations

import os
from typing import Iterable

from fastapi import HTTPException

from server.hub._state import state
from server.hub.engines import EngineRecord

# Built-in seed endpoints that the operator hasn't filled in yet.
# An engine pointing at one of these is treated as "not configured"
# so a fresh install that hasn't touched the Engines tab still sees
# LLM as unavailable.
_PLACEHOLDER_HOSTS = ("<gpu-host>", "<your-host>", "agent:8001")


def _has_real_key(rec: EngineRecord) -> bool:
    """True iff the engine actually has an API key configured.

    Both paths count:
      * ``api_key`` set directly on the record (operator pasted into the UI)
      * ``api_key_env`` set AND the env var resolves to a non-empty string
        on the hub process

    An engine that legitimately needs no auth (e.g. an internal vLLM
    on the LAN) sets neither and is still considered "usable" -- the
    caller decides whether endpoint reachability is required."""
    if (rec.api_key or "").strip():
        return True
    env_name = (rec.api_key_env or "").strip()
    if env_name and os.environ.get(env_name, "").strip():
        return True
    # No-auth endpoint (typical for an on-LAN vLLM). Still usable.
    if not env_name and not rec.api_key:
        return True
    return False


def _endpoint_is_real(endpoint: str) -> bool:
    """Reject built-in seed placeholders like ``http://<gpu-host>:15082``
    so a never-configured install doesn't fool us into thinking it has
    a working LLM."""
    if not endpoint:
        return False
    for marker in _PLACEHOLDER_HOSTS:
        if marker in endpoint:
            return False
    return True


def is_engine_usable(rec: EngineRecord) -> bool:
    """One engine is usable when its endpoint is real (not a seed
    placeholder) AND it has auth (or doesn't need any)."""
    return _endpoint_is_real(rec.endpoint) and _has_real_key(rec)


def usable_engines() -> list[EngineRecord]:
    """All engines that pass :func:`is_engine_usable`. Empty list
    means LLM features are not currently available."""
    reg = state.engines
    if reg is None:
        return []
    return [r for r in reg.list_all() if is_engine_usable(r)]


def usable_engines_for_kind(kind: str) -> list[EngineRecord]:
    """Subset of usable engines whose ``kind`` matches (``chat`` /
    ``vision-chat`` / ``gui-agent``)."""
    return [r for r in usable_engines() if r.kind == kind]


def is_llm_available() -> bool:
    """Cheap top-level check: do we have AT LEAST one usable engine
    of any kind? Drives the admin UI banner and the Submit form's
    "LLM features disabled" mode."""
    return bool(usable_engines())


def is_llm_available_for_kinds(kinds: Iterable[str]) -> bool:
    """True iff at least one of the given engine kinds has a usable
    record. ``page.agent`` needs vision-chat or gui-agent; codegen-loop
    needs chat or vision-chat; etc."""
    available = {r.kind for r in usable_engines()}
    return any(k in available for k in kinds)


# ---------------------------------------------------------------------------
# FastAPI helper: raise 503 with an operator-actionable message
# ---------------------------------------------------------------------------


_HINT = (
    "LLM features are disabled because no engine is configured with a "
    "working endpoint + API key. Open the Settings tab and add an "
    "OpenAI / Claude / Mistral / on-LAN vLLM engine, then retry."
)


def require_llm(*, kinds: Iterable[str] | None = None) -> None:
    """Raise ``HTTPException(503)`` when no LLM is configured.

    Pass ``kinds`` (e.g. ``("chat", "vision-chat")``) to require a
    SPECIFIC capability; omit to accept any usable engine. The error
    body is JSON with an ``llm_disabled: true`` flag so the admin UI
    can render a dedicated banner instead of a generic 503 page::

        {
          "detail": "LLM features are disabled...",
          "llm_disabled": true,
          "kinds_required": ["chat", "vision-chat"]
        }

    Backend handlers call this at the top of the function body.
    Route-level ``Depends(...)`` would also work but Python's call-site
    visibility wins for this kind of pre-condition check.
    """
    if kinds is None:
        ok = is_llm_available()
    else:
        ok = is_llm_available_for_kinds(kinds)
    if ok:
        return
    raise HTTPException(
        status_code=503,
        detail={
            "message": _HINT,
            "llm_disabled": True,
            "kinds_required": list(kinds) if kinds else None,
        },
    )
