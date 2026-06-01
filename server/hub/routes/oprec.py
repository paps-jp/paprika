"""Operator-recorder routes: verbalise captured events via a VLM,
and persist whole demonstrations as the per-host learning library.

Companion to the agent-extension's operator-event logger
(MVP1 programming-by-demonstration).

  POST /oprec/verbalize   - one-shot VLM summary per event (M1)
  POST /oprec/demos       - save a recording as a named demo  (M2)
  GET  /oprec/demos       - list demos, optionally per-host
  GET  /oprec/demos/{id}  - fetch full body (events + clips)
  PATCH /oprec/demos/{id} - edit title / note
  DELETE /oprec/demos/{id}

Hub-only; no worker round trip needed.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from server.hub.oprec_store import DemoIndex, get_store
from server.hub.oprec_verbalize import verbalize_events


log = logging.getLogger(__name__)
router = APIRouter(tags=["OpRec"])


def _idx_to_dict(e: DemoIndex) -> dict:
    return {
        "id": e.id,
        "host": e.host,
        "start_url": e.start_url,
        "title": e.title,
        "note": e.note,
        "event_count": e.event_count,
        "clip_count": e.clip_count,
        "created_at": e.created_at,
        "updated_at": e.updated_at,
    }


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


@router.post("/oprec/demos")
async def oprec_save_demo(body: dict) -> dict:
    """Persist a demonstration. Body::

        {"events": [...], "start_url": "...",
         "title": "(optional)", "note": "(optional)"}
    """
    body = body or {}
    events = body.get("events") or []
    if not isinstance(events, list) or len(events) == 0:
        raise HTTPException(400, "'events' must be a non-empty list")
    if len(events) > 500:
        raise HTTPException(400, "too many events (max 500 per save)")
    start_url = str(body.get("start_url") or "")
    title = str(body.get("title") or "")
    note = str(body.get("note") or "")
    store = get_store()
    idx = store.save(
        events=events, start_url=start_url,
        title=title, note=note,
    )
    log.info(
        "oprec.save: id=%s host=%s events=%d clips=%d",
        idx.id, idx.host, idx.event_count, idx.clip_count,
    )
    return _idx_to_dict(idx)


@router.get("/oprec/demos")
async def oprec_list_demos(
    host: str | None = None,
    limit: int = 50,
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be in 1..500")
    store = get_store()
    items = store.list(host=host, limit=limit)
    return {
        "demos": [_idx_to_dict(e) for e in items],
        "count": len(items),
    }


@router.get("/oprec/demos/{demo_id}")
async def oprec_get_demo(demo_id: str) -> dict:
    store = get_store()
    body = store.get(demo_id)
    if body is None:
        raise HTTPException(404, f"demo {demo_id!r} not found")
    idx = store.get_index(demo_id)
    return {
        "id": body.id,
        "host": body.host,
        "start_url": body.start_url,
        "title": body.title,
        "note": body.note,
        "created_at": body.created_at,
        "updated_at": idx.updated_at if idx else body.created_at,
        "event_count": body.event_count(),
        "clip_count": body.clip_count(),
        "events": body.events,
    }


@router.patch("/oprec/demos/{demo_id}")
async def oprec_patch_demo(demo_id: str, body: dict) -> dict:
    body = body or {}
    title = body.get("title") if "title" in body else None
    note = body.get("note") if "note" in body else None
    if title is not None and not isinstance(title, str):
        raise HTTPException(400, "title must be a string")
    if note is not None and not isinstance(note, str):
        raise HTTPException(400, "note must be a string")
    store = get_store()
    idx = store.patch(demo_id, title=title, note=note)
    if idx is None:
        raise HTTPException(404, f"demo {demo_id!r} not found")
    return _idx_to_dict(idx)


@router.delete("/oprec/demos/{demo_id}")
async def oprec_delete_demo(demo_id: str) -> dict:
    store = get_store()
    if not store.delete(demo_id):
        raise HTTPException(404, f"demo {demo_id!r} not found")
    return {"deleted": True, "id": demo_id}
