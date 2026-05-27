"""LLM availability surface: ``GET /llm/status``.

Read-only endpoint the admin UI polls to decide whether to grey-out
LLM-dependent controls (page.agent, codegen-loop / vision-agent in
the Submit form, the Skills + Conventions auto-distillation toggles)
and whether to show the top-of-page "LLM features disabled" banner.

Kept in its own router (not folded into /settings) so the page-load
payload stays small and a hot-poll on /llm/status is cheap. Driven by
:mod:`server.hub._llm_status` so the API matches what
:func:`require_llm` enforces on the LLM-touching endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter

from server.hub._llm_status import is_llm_available, usable_engines

router = APIRouter(tags=["LLM"])


@router.get("/llm/status")
async def llm_status() -> dict:
    """Snapshot of which engines are usable right now.

    Response::

        {
          "available": true|false,
          "engines": [
            {"slug": "openai-gpt5", "kind": "chat",        "name": "..."},
            {"slug": "claude",      "kind": "vision-chat", "name": "..."},
            ...
          ],
          "kinds": {
            "chat":        ["openai-gpt5", "claude", ...],
            "vision-chat": ["claude", ...],
            "gui-agent":   []
          },
          "missing_capabilities": ["gui-agent"]
        }

    * ``available``: at least one usable engine of any kind. False
      ⇒ render the disabled banner, grey-out LLM controls.
    * ``kinds``: per-capability slug list. Empty list for a kind means
      that capability is disabled (e.g. no gui-agent engine ⇒ disable
      mode=vision-agent in the Submit form).
    * ``missing_capabilities``: convenience derived list of the kinds
      that have zero usable engines, so the UI can show specific
      "register a vision-chat engine to enable extract()" hints.
    """
    engines = usable_engines()
    kinds: dict[str, list[str]] = {"chat": [], "vision-chat": [], "gui-agent": []}
    items: list[dict] = []
    for rec in engines:
        kinds.setdefault(rec.kind, []).append(rec.slug)
        items.append({"slug": rec.slug, "kind": rec.kind, "name": rec.name})
    missing = [k for k, slugs in kinds.items() if not slugs]
    return {
        "available": is_llm_available(),
        "engines": items,
        "kinds": kinds,
        "missing_capabilities": missing,
    }
