"""Convention registry routes: /conventions/* (list, CRUD,
promote/demote).

Atomic LLM-facing rules distilled from failure->success diffs. Curated
conventions are ALWAYS injected into the codegen system prompt (the
``always-on rules`` block), so promotion is the operator's lever for
"I've seen this lesson stick; force every future attempt to follow
it". File-backed under ``{data_dir}/conventions/``.

Two tiers (auto vs curated) and promote/demote semantics mirror
``/skills`` -- kept parallel so the admin UI's Skills + Conventions
tabs share row controls.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server.hub._state import state
from server.hub.conventions import (
    ConventionRegistry,
)
from server.hub.conventions import (
    normalise_slug as normalise_convention_slug,
)

router = APIRouter(tags=["Conventions"])


def _require_conventions() -> ConventionRegistry:
    if state.conventions is None:
        raise HTTPException(503, "convention registry not initialised")
    return state.conventions


def _convention_to_dict(rec, include_body: bool = True) -> dict:
    d = {
        "slug": rec.slug,
        "name": rec.name,
        "advice": rec.advice,
        "rationale": rec.rationale,
        "applicable_when": list(rec.applicable_when or []),
        "tags": list(rec.tags or []),
        "extracted_from": list(rec.extracted_from or []),
        "tier": rec.tier,
        "use_count": rec.use_count,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "last_used_at": rec.last_used_at,
    }
    if include_body:
        d["bad_example"] = rec.bad_example
        d["good_example"] = rec.good_example
    return d


@router.get("/conventions")
async def list_conventions() -> dict:
    reg = _require_conventions()
    items = [_convention_to_dict(c, include_body=False) for c in reg.list_all()]
    return {
        "count": len(items),
        "conventions": items,
        "tiers": {
            "auto": sum(1 for s in items if s["tier"] == "auto"),
            "curated": sum(1 for s in items if s["tier"] == "curated"),
        },
    }


@router.get("/conventions/{slug}")
async def get_convention(slug: str) -> dict:
    reg = _require_conventions()
    rec = reg.get(slug)
    if rec is None:
        raise HTTPException(404, f"convention '{slug}' not found")
    return _convention_to_dict(rec, include_body=True)


@router.put("/conventions/{slug}")
async def put_convention(slug: str, body: dict) -> dict:
    """Create or update a convention. tier=curated → always injected
    into the codegen system prompt; tier=auto → stored for review."""
    reg = _require_conventions()
    body = body or {}
    tier = body.get("tier") or "auto"
    if tier not in ("auto", "curated"):
        raise HTTPException(400, "tier must be 'auto' or 'curated'")
    try:
        rec = reg.upsert(
            slug=slug,
            name=body.get("name") or slug,
            advice=body.get("advice") or "",
            rationale=body.get("rationale") or "",
            bad_example=body.get("bad_example") or "",
            good_example=body.get("good_example") or "",
            applicable_when=body.get("applicable_when") or [],
            tags=body.get("tags") or [],
            extracted_from=body.get("extracted_from") or [],
            tier=tier,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _convention_to_dict(rec, include_body=True)


@router.delete("/conventions/{slug}")
async def delete_convention(slug: str) -> dict:
    reg = _require_conventions()
    ok = reg.delete(slug)
    if not ok:
        raise HTTPException(404, f"convention '{slug}' not found")
    return {"slug": normalise_convention_slug(slug), "deleted": True}


@router.post("/conventions/{slug}/promote")
async def promote_convention(slug: str) -> dict:
    """auto/ → curated/. Promoted conventions are immediately injected
    into every subsequent codegen-loop attempt."""
    reg = _require_conventions()
    rec = reg.promote(slug)
    if rec is None:
        raise HTTPException(404, f"convention '{slug}' not found in auto/")
    return _convention_to_dict(rec, include_body=True)


@router.post("/conventions/{slug}/demote")
async def demote_convention(slug: str) -> dict:
    """curated/ → auto/. Stops the convention from being injected."""
    reg = _require_conventions()
    rec = reg.demote(slug)
    if rec is None:
        raise HTTPException(404, f"convention '{slug}' not found in curated/")
    return _convention_to_dict(rec, include_body=True)
