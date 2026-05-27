"""Skill registry routes: /skills/* (list, CRUD, promote/demote).

LLM-distilled reusable patterns. Codegen-loop retrieves relevant skills
before each job and the auto-extractor writes new ones after every
SUCCESS. File-backed under ``{data_dir}/skills/``.

Two tiers:
  * ``auto``    -- written by the distiller, subject to future overwrites
  * ``curated`` -- hand-reviewed; operator-managed, stable
Promote moves auto -> curated, demote reverses.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server.hub._state import state
from server.hub.skills import SkillRegistry, normalise_slug

router = APIRouter(tags=["Skills"])


def _require_skills() -> SkillRegistry:
    if state.skills is None:
        raise HTTPException(503, "skill registry not initialised")
    return state.skills


def _skill_to_dict(rec, include_body: bool = True) -> dict:
    """Render a SkillRecord for the API. Body (code_template +
    llm_instructions) is omitted from the list endpoint so payloads
    stay small."""
    d = {
        "slug": rec.slug,
        "name": rec.name,
        "description": rec.description,
        "applicable_when": list(rec.applicable_when or []),
        "tags": list(rec.tags or []),
        "auto_extracted": rec.auto_extracted,
        "extracted_from": list(rec.extracted_from or []),
        "tier": rec.tier,
        "use_count": rec.use_count,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "last_used_at": rec.last_used_at,
    }
    if include_body:
        d["code_template"] = rec.code_template
        d["llm_instructions"] = rec.llm_instructions
    else:
        d["code_template_len"] = len(rec.code_template or "")
        d["llm_instructions_len"] = len(rec.llm_instructions or "")
    return d


@router.get("/skills")
async def list_skills() -> dict:
    """List every distilled skill. Curated first, then auto, each
    sorted by most-recently-updated. Body omitted -- fetch the full
    record via GET /skills/{slug} when the operator clicks Edit."""
    reg = _require_skills()
    items = [_skill_to_dict(s, include_body=False) for s in reg.list_all()]
    return {
        "count": len(items),
        "skills": items,
        "tiers": {
            "auto": sum(1 for s in items if s["tier"] == "auto"),
            "curated": sum(1 for s in items if s["tier"] == "curated"),
        },
    }


@router.get("/skills/{slug}")
async def get_skill(slug: str) -> dict:
    reg = _require_skills()
    rec = reg.get(slug)
    if rec is None:
        raise HTTPException(404, f"skill '{slug}' not found")
    return _skill_to_dict(rec, include_body=True)


@router.put("/skills/{slug}")
async def put_skill(slug: str, body: dict) -> dict:
    """Create or update a skill. Body fields::

        {
          "name": "Human-readable",
          "description": "When to use this skill.",
          "code_template": "...",
          "llm_instructions": "...",
          "applicable_when": ["bullet", ...],
          "tags": ["short", "kebab"],
          "tier": "auto" | "curated"   // default: auto
        }

    Hand-written skills should be PUT with tier=curated. The auto
    extractor writes to tier=auto from the codegen-loop callback."""
    reg = _require_skills()
    body = body or {}
    tier = body.get("tier") or "auto"
    if tier not in ("auto", "curated"):
        raise HTTPException(400, "tier must be 'auto' or 'curated'")
    try:
        rec = reg.upsert(
            slug=slug,
            name=body.get("name") or slug,
            description=body.get("description") or "",
            code_template=body.get("code_template") or "",
            llm_instructions=body.get("llm_instructions") or "",
            applicable_when=body.get("applicable_when") or [],
            tags=body.get("tags") or [],
            auto_extracted=bool(body.get("auto_extracted", tier == "auto")),
            extracted_from=body.get("extracted_from") or [],
            tier=tier,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _skill_to_dict(rec, include_body=True)


@router.delete("/skills/{slug}")
async def delete_skill(slug: str) -> dict:
    reg = _require_skills()
    ok = reg.delete(slug)
    if not ok:
        raise HTTPException(404, f"skill '{slug}' not found")
    return {"slug": normalise_slug(slug), "deleted": True}


@router.post("/skills/{slug}/promote")
async def promote_skill(slug: str) -> dict:
    """Move an auto/ skill to curated/. Equivalent to "I've reviewed
    this and it's worth keeping". After promotion the skill becomes
    operator-managed (no further auto modifications)."""
    reg = _require_skills()
    rec = reg.promote(slug)
    if rec is None:
        raise HTTPException(404, f"skill '{slug}' not found in auto/")
    return _skill_to_dict(rec, include_body=True)


@router.post("/skills/{slug}/demote")
async def demote_skill(slug: str) -> dict:
    """Move a curated/ skill back to auto/. Reverses promote."""
    reg = _require_skills()
    rec = reg.demote(slug)
    if rec is None:
        raise HTTPException(404, f"skill '{slug}' not found in curated/")
    return _skill_to_dict(rec, include_body=True)
