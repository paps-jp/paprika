"""Forensics route: ``POST /sessions/{session_id}/forensics``.

Drives an LLM analysis loop on an EXISTING session. The session must
already be open (operator-controlled scope). Each probe is a JS
expression evaluated in the page's main world via the same transport
as ``/sessions/{id}/evaluate``; results are fed back to the LLM until
it writes a final report or hits ``max_steps``.

See ``server/hub/forensics.py`` for the loop + safety check.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from server.hub.forensics import (
    FORENSICS_DEFAULT_MAX_STEPS,
    run_forensics,
)
from server.hub.routes.sessions import (
    _get_session_or_404,
    _route_to_page,
    _send_session_action,
)

log = logging.getLogger(__name__)
router = APIRouter(tags=["Forensics"])


@router.post("/sessions/{session_id}/forensics")
async def session_forensics(session_id: str, body: dict) -> dict:
    """Run a forensics / analyze agent loop on this session.

    Body::

        {"goal": "Explain why <X> doesn't happen on this page",
         "max_steps": 18,
         "page_url": "https://...(optional, hint for the LLM)"}

    Returns::

        {"completed":   bool,    # True if the LLM emitted action=finish
         "steps_taken": int,
         "max_steps":   int,
         "report":      str,     # the LLM's final report (Markdown)
         "trace":       [{n, thought, expression, await_promise,
                          result, error, elapsed_ms}, ...],
         "model":       str,
         "elapsed_ms":  int}

    READ-ONLY by contract: the loop pre-flights each probe against a
    safety regex that rejects navigation, form submit, .click(), cookie
    / storage writes, ``POST`` fetches, and DOM mutation. Operators
    investigating accounts with side effects (purchases, posts, etc.)
    should run forensics on a throwaway session.
    """
    body = body or {}
    goal = (body.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "missing 'goal'")
    raw_steps = body.get("max_steps")
    try:
        max_steps = int(raw_steps) if raw_steps is not None else None
    except Exception:
        raise HTTPException(400, "max_steps must be an integer")
    if max_steps is not None and max_steps < 1:
        raise HTTPException(400, "max_steps must be >= 1")

    # Per-run interaction permissions chosen by the operator (checkboxes
    # in the admin modal). Only "media" / "click" are recognised; anything
    # else is ignored. Empty -> pure read-only run (default). The absolute
    # no-go set (navigation / submit / POST / writes / exfil / destructive
    # clicks) stays blocked regardless -- see forensics.safety_check.
    raw_allow = body.get("allow") or []
    if isinstance(raw_allow, str):
        raw_allow = [raw_allow]
    allow = {str(c).strip().lower() for c in raw_allow if str(c).strip()}

    # Confirm the session exists and grab its URL as a hint for the LLM
    # when the operator didn't supply page_url.
    info = _get_session_or_404(session_id)
    page_url = body.get("page_url") or getattr(info, "initial_url", None)

    # Bridge the loop's evaluate_fn to the existing session-action
    # transport. ``_send_session_action`` returns the worker reply dict
    # (``{status, result, elapsed_ms}``); we adapt that to the tuple
    # shape forensics.run_forensics expects.
    async def _evaluate(js: str, await_promise: bool) -> tuple[bool, Any, int]:
        action = _route_to_page(
            {
                "kind": "evaluate",
                "expression": js,
                "await_promise": bool(await_promise),
                # Mark this evaluate as read-only so the worker permits it
                # on a session still owned by a running fetch job. Forensics
                # pre-flights every probe with safety_check() (no navigate /
                # click / submit / mutation / POST / storage writes), so the
                # probe only reads the page. See agent.py's is_fetch_owned
                # guard.
                "read_only": True,
            },
            body,
        )
        reply = await _send_session_action(session_id, action, timeout=45.0)
        status = str(reply.get("status") or "")
        elapsed = int(reply.get("elapsed_ms") or 0)
        if not status.startswith("OK"):
            # Surface the worker's error string verbatim so the LLM can
            # adjust the next probe.
            return False, status or "evaluate failed", elapsed
        return True, reply.get("result"), elapsed

    result = await run_forensics(
        goal=goal,
        page_url=page_url,
        evaluate_fn=_evaluate,
        max_steps=max_steps,
        allow=allow,
    )

    log.info(
        "forensics %s: completed=%s steps=%d/%d elapsed=%dms model=%s allow=%s",
        session_id,
        result.completed,
        result.steps_taken,
        result.max_steps,
        result.elapsed_ms,
        result.model,
        sorted(allow) or "-",
    )

    return {
        "completed": result.completed,
        "steps_taken": result.steps_taken,
        "max_steps": result.max_steps,
        "report": result.report,
        "trace": [
            {
                "n": s.n,
                "thought": s.thought,
                "expression": s.expression,
                "await_promise": s.await_promise,
                "result": s.result,
                "error": s.error,
                "elapsed_ms": s.elapsed_ms,
            }
            for s in result.trace
        ],
        "model": result.model,
        "elapsed_ms": result.elapsed_ms,
        "default_max_steps": FORENSICS_DEFAULT_MAX_STEPS,
    }
