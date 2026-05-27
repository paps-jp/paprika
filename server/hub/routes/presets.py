"""Submit-form preset routes: /presets/* (list, CRUD, run).

A preset is a named snapshot of the Submit form (URL + mode + engine
+ macro rows + options). Operators load them via the dropdown above
Submit; /presets/{name}/run fires a job from the snapshot without
going through the UI -- useful for cron / external triggers.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from server.hub._state import state
from server.hub.presets import PresetRecord, PresetRegistry
from server.protocol import JobInfo, JobOptions, JobRequest

router = APIRouter(tags=["Presets"])


def _require_presets() -> PresetRegistry:
    if state.presets is None:
        raise HTTPException(503, "preset registry not initialised")
    return state.presets


@router.get("/presets")
async def list_presets(
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
    category: str | None = None,
) -> dict:
    """Return saved presets (light projection -- no big macro bodies).

    Supports search + pagination so the UI can serve 500+ presets
    without dragging the whole list across the wire:

      * ``q``        case-insensitive substring match against name /
                     category / description / url.
      * ``category`` exact-match filter (or empty string for
                     uncategorised).
      * ``offset``   how many records to skip from the start.
      * ``limit``    page size (1-500; capped at 500).

    Sorted by category then name. Returns ``{presets, count, total,
    categories}`` -- ``total`` is the full filtered count so the UI
    can render a pager, ``categories`` is the flat list of distinct
    categories (always returned so the filter dropdown stays current).
    """
    pr = _require_presets()
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    needle = (q or "").strip().lower()
    cat_filter = category

    all_records = pr.list_all()
    categories = sorted({r.category for r in all_records if r.category})

    def matches(r) -> bool:
        if cat_filter is not None:
            # cat_filter == "" means "show uncategorised only".
            if (r.category or "") != cat_filter:
                return False
        if not needle:
            return True
        haystack = (
            (r.name or "")
            + " "
            + (r.category or "")
            + " "
            + (r.description or "")
            + " "
            + (r.url or "")
        ).lower()
        return needle in haystack

    filtered = [r for r in all_records if matches(r)]
    total = len(filtered)
    page = filtered[offset : offset + limit]

    rows = [
        {
            "name": r.name,
            "category": r.category,
            "description": r.description,
            "ui_mode": r.ui_mode,
            "ai_engine": r.ai_engine,
            "url": r.url,
            "updated_at": r.updated_at,
            "last_used_at": r.last_used_at,
        }
        for r in page
    ]
    return {
        "presets": rows,
        "count": len(rows),
        "total": total,
        "offset": offset,
        "limit": limit,
        "categories": categories,
    }


@router.get("/presets/{name}")
async def get_preset(name: str) -> dict:
    pr = _require_presets()
    rec = pr.get(name)
    if rec is None:
        raise HTTPException(404, f"preset '{name}' not found")
    return rec.to_json()


@router.put("/presets/{name}")
async def put_preset(name: str, body: dict) -> dict:
    """Save / overwrite a preset. The ``name`` path param wins -- any
    different ``name`` field in the body is ignored. Returns the
    persisted record (with resolved timestamps)."""
    pr = _require_presets()
    body = body or {}
    body["name"] = name  # path param wins
    try:
        rec = PresetRecord.from_json(body)
        if not rec.name.strip():
            raise ValueError("preset name cannot be empty")
        saved = pr.upsert(rec)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return saved.to_json()


@router.delete("/presets/{name}")
async def delete_preset(name: str) -> dict:
    pr = _require_presets()
    ok = pr.delete(name)
    if not ok:
        raise HTTPException(404, f"preset '{name}' not found")
    return {"ok": True, "deleted": name}


@router.post("/presets/{name}/run")
async def run_preset(name: str, request: Request, body: dict = None) -> JobInfo:
    """Submit a job using a saved preset's url + options snapshot.

    ``body`` (optional) accepts a small set of per-run overrides that
    don't require editing the preset itself:

        {
          "url": "...",                  # override start URL
          "attempt_timeout_s": 300       # override timeout
        }

    Reuses the same code path as POST /jobs so all the
    cookie-auto-injection / popup_policy / scheduler logic applies.
    Bumps the preset's last_used_at on success.
    """
    pr = _require_presets()
    rec = pr.get(name)
    if rec is None:
        raise HTTPException(404, f"preset '{name}' not found")
    overrides = body or {}

    # Build JobRequest from the saved snapshot. The preset's
    # ``options`` dict is the resolved JobOptions JSON (mode +
    # mode-specific fields); Pydantic validates on construction.
    opts_dict = dict(rec.options or {})
    if "attempt_timeout_s" in overrides:
        try:
            opts_dict["attempt_timeout_s"] = int(overrides["attempt_timeout_s"])
        except Exception:
            pass
    try:
        opts = JobOptions(**opts_dict)
    except Exception as e:
        raise HTTPException(
            400,
            f"preset '{name}' has invalid options: {type(e).__name__}: {e}",
        )

    url = (overrides.get("url") or rec.url or "").strip() or "about:blank"
    req = JobRequest(url=url, options=opts)

    # Mark the preset as just used (best-effort).
    try:
        pr.touch_used(name)
    except Exception:
        pass

    # Hand off to the existing job-creation endpoint. Lazy import to
    # avoid the routes/presets -> app -> routes/presets cycle (create_job
    # still lives in app.py until #2B-G migrates /jobs).
    from server.hub.app import create_job

    return await create_job(req, request)
