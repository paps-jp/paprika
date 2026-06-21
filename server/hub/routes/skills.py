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
from server.hub._invalidate import share_delete, share_upsert
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
        "success_count": getattr(rec, "success_count", 0),
        # Fitness ratio for the operator's curate/retire decisions:
        # fraction of jobs this skill rode along on that were judged OK.
        "success_rate": (
            round(getattr(rec, "success_count", 0) / rec.use_count, 3)
            if rec.use_count else None
        ),
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "last_used_at": rec.last_used_at,
        "last_success_at": getattr(rec, "last_success_at", None),
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
    await share_upsert("skills", reg, rec)
    return _skill_to_dict(rec, include_body=True)


@router.delete("/skills/{slug}")
async def delete_skill(slug: str) -> dict:
    reg = _require_skills()
    ok = reg.delete(slug)
    if not ok:
        raise HTTPException(404, f"skill '{slug}' not found")
    await share_delete("skills", normalise_slug(slug))
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
    await share_upsert("skills", reg, rec)
    return _skill_to_dict(rec, include_body=True)


@router.post("/skills/{slug}/demote")
async def demote_skill(slug: str) -> dict:
    """Move a curated/ skill back to auto/. Reverses promote."""
    reg = _require_skills()
    rec = reg.demote(slug)
    if rec is None:
        raise HTTPException(404, f"skill '{slug}' not found in curated/")
    await share_upsert("skills", reg, rec)
    return _skill_to_dict(rec, include_body=True)


@router.post("/skills/merge")
async def merge_skills(body: dict) -> dict:
    """Fold near-duplicate auto skills (``drops``) into the survivor
    (``keep``): sum counts, union provenance, delete the rest. Auto tier
    only. Body: ``{keep: slug, drops: [slug, ...]}``."""
    reg = _require_skills()
    body = body or {}
    keep = body.get("keep")
    drops = body.get("drops") or []
    if not keep or not isinstance(drops, list) or not drops:
        raise HTTPException(400, "body must be {keep: <slug>, drops: [<slug>, ...]}")
    rec = reg.merge(keep, drops)
    if rec is None:
        raise HTTPException(404, f"keep skill '{keep}' not found in auto/")
    await share_upsert("skills", reg, rec)
    for _drop in drops:
        await share_delete("skills", normalise_slug(_drop))
    return _skill_to_dict(rec, include_body=False)


@router.get("/ai/oracle-stats")
async def ai_oracle_stats(limit: int = 200) -> dict:
    """L1 media-oracle stats across recently-captured video assets.

    Walks ``{storage_dir}/*/assets/`` for video files, runs ffprobe on
    each, and returns aggregate + per-file L1 verdicts. Hub-only -- no
    worker change needed. Probe results are live (not cached); use
    ``limit`` (default 200) to cap the scan to the N most-recently-written
    video files across all jobs.
    """
    import asyncio
    import json as _json
    import subprocess

    from server.hub._state import get_storage_dir

    _VIDEO_EXTS = {"mp4", "webm", "mov", "m4v", "mkv"}
    _MIN_DUR = 1.0

    def _probe_one(fp):
        try:
            r = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-print_format", "json",
                    "-show_streams", "-show_format",
                    str(fp),
                ],
                capture_output=True,
                timeout=15,
            )
            if r.returncode != 0:
                return {
                    "valid": False, "reason": "ffprobe_error",
                    "duration_s": None, "codec": None,
                    "width": None, "height": None,
                }
            d = _json.loads(r.stdout)
        except FileNotFoundError:
            return {
                "valid": False, "reason": "ffprobe_not_found",
                "duration_s": None, "codec": None,
                "width": None, "height": None,
            }
        except Exception:
            return {
                "valid": False, "reason": "probe_exc",
                "duration_s": None, "codec": None,
                "width": None, "height": None,
            }
        streams = d.get("streams", [])
        vs = next((s for s in streams if s.get("codec_type") == "video"), None)
        if vs is None:
            return {
                "valid": False, "reason": "no_video_stream",
                "duration_s": None, "codec": None,
                "width": None, "height": None,
            }
        try:
            dur = float(
                d.get("format", {}).get("duration")
                or vs.get("duration")
                or 0
            )
        except (ValueError, TypeError):
            dur = 0.0
        if dur < _MIN_DUR:
            return {
                "valid": False, "reason": "too_short",
                "duration_s": round(dur, 2),
                "codec": vs.get("codec_name"),
                "width": vs.get("width"),
                "height": vs.get("height"),
            }
        return {
            "valid": True, "reason": "ok",
            "duration_s": round(dur, 2),
            "codec": vs.get("codec_name"),
            "width": vs.get("width"),
            "height": vs.get("height"),
        }

    def _collect_files():
        storage = get_storage_dir()
        found: list = []
        try:
            job_dirs = sorted(
                (p for p in storage.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return found
        for jd in job_dirs:
            assets = jd / "assets"
            if not assets.is_dir():
                continue
            for f in sorted(
                (p for p in assets.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ):
                if f.suffix.lower().lstrip(".") in _VIDEO_EXTS:
                    found.append((jd.name, f))
                    if len(found) >= limit:
                        return found
        return found

    loop = asyncio.get_event_loop()
    pairs = await loop.run_in_executor(None, _collect_files)

    async def _probe(jid, fp):
        verdict = await loop.run_in_executor(None, _probe_one, fp)
        try:
            size = fp.stat().st_size
        except Exception:
            size = 0
        return {"job_id": jid, "name": fp.name, "bytes": size, **verdict}

    rows = list(await asyncio.gather(*(_probe(jid, fp) for jid, fp in pairs)))
    total = len(rows)
    valid_count = sum(1 for r in rows if r["valid"])
    by_reason: dict = {}
    for r in rows:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
    return {
        "total": total,
        "valid": valid_count,
        "invalid": total - valid_count,
        "valid_pct": round(valid_count / total, 3) if total else None,
        "by_reason": by_reason,
        "files": rows,
    }


@router.get("/ai/grooming-status")
async def ai_grooming_status() -> dict:
    """Liveness + last-pass snapshot of the skill/convention reaper. Lets
    the admin UI surface "the reaper IS running, and here's WHY the
    candidate list is empty" (most often: cold-start dud guard).

    Pure module-read; ``last_run_at`` is None until the first pass
    completes (~2 min after hub startup)."""
    try:
        from server.hub._reaper import get_skill_convention_reaper_status
        st = get_skill_convention_reaper_status()
    except Exception as e:
        st = {"error": f"{type(e).__name__}: {e}"}
    try:
        v = (state.settings.all() if state.settings else {}) or {}
        st["auto_retire_enabled"] = bool(v.get("auto_retire_enabled", False))
        st["auto_dedup_enabled"] = bool(v.get("auto_dedup_enabled", False))
    except Exception:
        pass
    return st


@router.get("/ai/io")
async def ai_io_log_query(
    limit: int = 100,
    purpose: str | None = None,
    engine_slug: str | None = None,
    job_id: str | None = None,
    since_s: float = 3600.0,
    errors_only: bool = False,
) -> dict:
    """Query the ai_io_log table -- per-LLM-call (purpose, engine, prompt,
    response, latency) capture for observing the whole loop end-to-end.

    Filters:
      * ``purpose``     -- planner / skill_retrieval / codegen / judge /
                           skill_distill / convention_distill /
                           reasoning_distill / perception
      * ``engine_slug`` -- e.g. deepseek-r1 / qwen3.5 / qwen3-vl-4b
      * ``job_id``      -- pin to one job's call tree
      * ``since_s``     -- only rows from the last N seconds
      * ``errors_only`` -- skip successful calls

    Long prompts/responses are truncated to the inline 32KB preview; the
    full body lives in MinIO under ``ai_io/<sha1>.bin`` when ``prompt_ref``
    / ``response_ref`` is set.
    """
    from server.hub._state import state
    pool = getattr(state, "mariadb_pool", None)
    if pool is None:
        return {"count": 0, "events": []}
    import time as _time
    conds: list[str] = []
    params: list = []
    if since_s and since_s > 0:
        conds.append("ts >= FROM_UNIXTIME(%s)")
        params.append(_time.time() - float(since_s))
    if purpose:
        conds.append("purpose = %s"); params.append(purpose[:32])
    if engine_slug:
        conds.append("engine_slug = %s"); params.append(engine_slug[:64])
    if job_id:
        conds.append("job_id = %s"); params.append(job_id[:64])
    if errors_only:
        conds.append("error IS NOT NULL")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    sql = (
        "SELECT id, UNIX_TIMESTAMP(ts) AS ts, job_id, purpose, engine_slug, "
        "       parent_call, prompt_len, response_len, tokens_in, tokens_out, "
        "       latency_ms, prompt_text, response_text, prompt_ref, "
        "       response_ref, error "
        "FROM ai_io_log" + where + " ORDER BY ts DESC LIMIT %s"
    )
    params.append(max(1, min(int(limit), 1000)))
    rows = []
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, tuple(params))
                rows = list(await cur.fetchall())
    except Exception as e:
        raise HTTPException(500, f"ai_io query failed: {type(e).__name__}: {e}")
    cols = ("id","ts","job_id","purpose","engine_slug","parent_call",
            "prompt_len","response_len","tokens_in","tokens_out","latency_ms",
            "prompt_text","response_text","prompt_ref","response_ref","error")
    events = [dict(zip(cols, r)) for r in rows]
    return {"count": len(events), "events": events}


@router.get("/ai/audit-stats")
async def ai_audit_stats(since_s: float = 86400.0) -> dict:
    """Aggregate Success Audit verdicts (4-quadrant) over the last
    ``since_s`` seconds.

    Quadrants: ``true_ok`` / ``false_positive`` (reported OK but actually NG)
    / ``false_negative`` (reported failed but actually OK) / ``true_failure``
    / ``unparsed``. Also returns the top hosts driving false positives and
    false negatives separately, since they imply different fixes (download_
    video tuning for FPs, escalation/judge tuning for FNs).
    """
    from server.hub._state import state
    pool = getattr(state, "mariadb_pool", None)
    empty = {
        "audited": 0, "true_ok": 0, "false_positive": 0,
        "false_negative": 0, "true_failure": 0, "unparsed": 0,
        "report_agreement_rate": None,
        "true_success_rate": None,
        "top_false_positive_hosts": [],
        "top_false_negative_hosts": [],
    }
    if pool is None:
        return empty
    import time as _t
    cutoff_ts = _t.time() - max(0.0, float(since_s))
    counts = {"true_ok": 0, "false_positive": 0, "false_negative": 0,
              "true_failure": 0, "unparsed": 0}
    fp_hosts: dict = {}
    fn_hosts: dict = {}
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT verdict_kind, COUNT(*) FROM audit_results "
                    "WHERE ts >= FROM_UNIXTIME(%s) GROUP BY verdict_kind",
                    (cutoff_ts,),
                )
                for vk, c in await cur.fetchall():
                    if vk in counts:
                        counts[vk] = int(c)
                # FP and FN host breakdowns.
                from urllib.parse import urlparse as _up
                for kind, bucket in (("false_positive", fp_hosts),
                                     ("false_negative", fn_hosts)):
                    await cur.execute(
                        "SELECT url, COUNT(*) FROM audit_results "
                        "WHERE ts >= FROM_UNIXTIME(%s) AND verdict_kind = %s "
                        "GROUP BY url ORDER BY COUNT(*) DESC LIMIT 30",
                        (cutoff_ts, kind),
                    )
                    for url, c in await cur.fetchall():
                        try:
                            h = (_up(url or "").hostname or "?").lower()
                            if h.startswith("www."): h = h[4:]
                            bucket[h] = bucket.get(h, 0) + int(c)
                        except Exception:
                            pass
    except Exception as e:
        raise HTTPException(500, f"audit_stats query failed: {type(e).__name__}: {e}")
    audited = sum(counts.values())
    decisive = counts["true_ok"] + counts["false_positive"] + counts["false_negative"] + counts["true_failure"]
    agree = counts["true_ok"] + counts["true_failure"]
    actually_ok = counts["true_ok"] + counts["false_negative"]
    return {
        "audited": audited,
        **counts,
        "report_agreement_rate": (agree / decisive) if decisive else None,
        "true_success_rate": (actually_ok / decisive) if decisive else None,
        "top_false_positive_hosts": sorted(fp_hosts.items(), key=lambda kv: -kv[1])[:8],
        "top_false_negative_hosts": sorted(fn_hosts.items(), key=lambda kv: -kv[1])[:8],
    }


@router.get("/ai/audit-recent")
async def ai_audit_recent(
    limit: int = 100,
    only_failures: bool = False,
    verdict_kind: str | None = None,
) -> dict:
    """Most-recent rows from ``audit_results`` for the admin UI table.
    Each row includes ``verdict_kind`` (true_ok / false_positive /
    false_negative / true_failure / unparsed) and ``reported_status``."""
    from server.hub._state import state
    pool = getattr(state, "mariadb_pool", None)
    if pool is None:
        return {"count": 0, "rows": []}
    where_parts: list[str] = []
    params: list = []
    if only_failures:
        # "failures" here means disagreement -- false positive OR false negative.
        where_parts.append("verdict_kind IN ('false_positive','false_negative')")
    if verdict_kind:
        where_parts.append("verdict_kind = %s")
        params.append(verdict_kind[:32])
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    sql = (
        "SELECT id, UNIX_TIMESTAMP(ts) AS ts, job_id, url, goal_short, "
        "       reported_status, verdict_kind, video_file, truly_succeeded, "
        "       confidence, reason, engine_slug, latency_ms, error "
        f"FROM audit_results {where} ORDER BY ts DESC LIMIT %s"
    )
    params.append(max(1, min(int(limit), 500)))
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, tuple(params))
                rows = list(await cur.fetchall())
    except Exception as e:
        raise HTTPException(500, f"audit_recent query failed: {type(e).__name__}: {e}")
    cols = ("id","ts","job_id","url","goal_short","reported_status",
            "verdict_kind","video_file","truly_succeeded","confidence",
            "reason","engine_slug","latency_ms","error")
    return {"count": len(rows), "rows": [dict(zip(cols, r)) for r in rows]}


@router.post("/ai/audit-now")
async def ai_audit_now() -> dict:
    """Operator-triggered one-pass audit (no wait for the scheduled
    interval). Returns the same summary the periodic loop logs."""
    from server.hub._success_audit import run_one_pass
    try:
        return await run_one_pass()
    except Exception as e:
        raise HTTPException(500, f"audit run failed: {type(e).__name__}: {e}")


@router.get("/ai/groom-candidates")
async def ai_groom_candidates() -> dict:
    """Retire (dud/zombie) + dedup (near-duplicate) candidates for the
    Grooming UI, for BOTH skills and conventions. Reuses the same logic
    the hourly reaper applies as a dry-run -- nothing is mutated here. The
    operator acts via the merge / delete endpoints or the auto_* toggles.
    """
    from server.hub._reaper import (
        _dedup_clusters,
        _dedup_pick,
        _retire_reason,
    )

    def _rate(r):
        uc = getattr(r, "use_count", 0) or 0
        return round((getattr(r, "success_count", 0) or 0) / uc, 3) if uc else None

    out: dict = {}
    for kind, reg in (("skill", state.skills), ("convention", state.conventions)):
        if reg is None:
            out[kind] = {"retire": [], "dedup": []}
            continue
        try:
            records = reg.list_all()
        except Exception:
            records = []
        total_success = sum(getattr(r, "success_count", 0) or 0 for r in records)
        allow_dud = kind == "skill" and total_success > 0
        retire = [
            {
                "slug": getattr(rec, "slug", "?"),
                "tier": getattr(rec, "tier", "auto"),
                "reason": reason,
                "use_count": getattr(rec, "use_count", 0),
                "success_count": getattr(rec, "success_count", 0),
            }
            for rec in records
            if (reason := _retire_reason(rec, allow_dud=allow_dud))
        ]
        dedup = []
        for cluster in _dedup_clusters(records, kind):
            keep, drops = _dedup_pick(cluster)
            dedup.append({
                "keep": keep.slug,
                "drops": [d.slug for d in drops],
                "members": [
                    {"slug": r.slug, "use_count": getattr(r, "use_count", 0),
                     "success_rate": _rate(r)}
                    for r in cluster
                ],
            })
        out[kind] = {"retire": retire, "dedup": dedup}
    return out
