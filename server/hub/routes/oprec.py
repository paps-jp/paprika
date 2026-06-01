"""Operator-recorder routes: verbalise captured events via a VLM.

Companion to the agent-extension's operator-event logger
(MVP1 programming-by-demonstration). The admin UI POSTs the events
returned by ``ext getOperatorEvents`` to this endpoint; we hand each
one to ``server.hub.oprec_verbalize`` for a one-sentence natural-
language summary, then return the events with summaries attached.

Hub-only; no worker round trip needed.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from server.hub.oprec_verbalize import verbalize_events


log = logging.getLogger(__name__)
router = APIRouter(tags=["OpRec"])


@router.post("/oprec/verbalize")
async def oprec_verbalize(body: dict) -> dict:
    """Body::

        {"events": [<event>, ...]}

    Returns::

        {"events": [<event with .summary or .summary_error>, ...],
         "count": <int>,
         "with_summary": <int>}
    """
    body = body or {}
    events = body.get("events") or []
    if not isinstance(events, list):
        raise HTTPException(400, "'events' must be a list")
    if len(events) > 200:
        raise HTTPException(400, "too many events (max 200 per call)")

    out = await verbalize_events(list(events))
    with_summary = sum(1 for e in out if isinstance(e, dict) and e.get("summary"))
    log.info(
        "oprec.verbalize: events=%d with_summary=%d",
        len(out), with_summary,
    )
    return {"events": out, "count": len(out), "with_summary": with_summary}
