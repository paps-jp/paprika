"""Success Audit System -- sample completed video-download jobs and ask a
VisionAI whether the saved video is actually the page's main content.

The objective pregate + ffprobe oracle catch ``0 files`` and ``unplayable``,
but the page-content match -- "is THIS the right video?" -- is a vision
question. Cases we want to catch:

  - The saved file is a SHORT PREVIEW / TRAILER rather than the full piece.
  - The saved file is an ad / unrelated content that landed in assets/.
  - Duration is far below what a page-of-this-type usually delivers.
  - Page is clearly about content X but the midframe shows content Y.

This module:

  1. Picks a sample (default 10%) of recently-completed ``codegen-loop``
     jobs that produced at least one ffprobe-valid video.
  2. Extracts a midframe JPEG from the saved video with ``ffmpeg``.
  3. Sends ``(URL, goal, page final.jpg, video midframe, duration)`` to a
     vision-capable engine for a one-shot OK/NG verdict + reason.
  4. Writes the verdict to ``audit_results`` (MariaDB) so the admin UI can
     surface the true-success rate, false-positive hotspot hosts, and the
     individual jobs that got an objective-OK but a vision-NG.

Fully best-effort: any single audit failing (ffmpeg unhappy, LLM timeout,
table missing) is swallowed -- the worker fleet must never be blocked by
the audit pipeline. Auto-loop is gated by Settings ``success_audit_enabled``
(default False; flip ON when the operator wants the observation).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

_log = logging.getLogger(__name__)

# Vision-LLM prompt -- ask for a TWO-WAY decision so we catch both false
# positives (reported OK but actually wrong) and false negatives (reported
# failed but actually meets the goal).
_AUDIT_SYSTEM = (
    "You are auditing whether a fetch / video-download job's REPORTED status "
    "matches reality. You receive: the page URL, the goal text, the page's "
    "final screenshot, the REPORTED status (completed / failed / review), "
    "an asset summary (counts by extension), and -- when a video was saved "
    "-- its midframe + duration. Decide whether the saved assets actually "
    "meet the goal, regardless of what the reported status says. The job "
    "MIGHT be a false positive (reported OK but assets are wrong / preview / "
    "ad / placeholder) OR a false negative (reported failed but the assets "
    "actually meet the goal anyway -- e.g. the page genuinely has no video "
    "so 0 video files IS the correct outcome for a non-video page; or "
    "image-only goals are satisfied; or saved html/text suffices). Output "
    "JSON ONLY. "
    "LANGUAGE: the ``reason`` field MUST be written in JAPANESE (日本語); "
    "``actually_succeeded`` is a boolean and ``confidence`` is a number, "
    "neither needs translation."
)

_AUDIT_USER_TEMPLATE = """\
URL: {url}
GOAL: {goal_short}
REPORTED status: {reported_status}
ASSETS summary: {assets_summary}
{video_block}

Attached: {n_images} image(s). {image_legend}

Decide:
  actually_succeeded -- true iff a human reviewer would say the saved assets
                        meet the goal on this page, regardless of the
                        REPORTED status. False otherwise.
  confidence         -- 0..1. Lower it if the page is hard to read.
  reason             -- one sentence on what made you say it. If your
                        verdict disagrees with REPORTED, say WHY (preview /
                        ad / wrong content / page genuinely has no video /
                        image goal met / etc.).

Output JSON only:
{{"actually_succeeded": true|false, "confidence": 0.0-1.0, "reason": "..."}}
"""

# Quadrant labels for the verdict_kind column.
VERDICT_TRUE_OK         = "true_ok"          # reported OK + actually OK
VERDICT_FALSE_POSITIVE  = "false_positive"   # reported OK + actually NG
VERDICT_FALSE_NEGATIVE  = "false_negative"   # reported NG + actually OK
VERDICT_TRUE_FAILURE    = "true_failure"     # reported NG + actually NG
VERDICT_UNPARSED        = "unparsed"         # vision-LLM verdict missing


def _classify_quadrant(reported_status: str, actually_succeeded) -> str:
    """Map (reported_status, vision verdict) into a 4-quadrant label."""
    if actually_succeeded is None:
        return VERDICT_UNPARSED
    reported_ok = reported_status in ("completed",)
    if reported_ok and actually_succeeded:
        return VERDICT_TRUE_OK
    if reported_ok and not actually_succeeded:
        return VERDICT_FALSE_POSITIVE
    if (not reported_ok) and actually_succeeded:
        return VERDICT_FALSE_NEGATIVE
    return VERDICT_TRUE_FAILURE

# How recently a job had to complete to be eligible. Older jobs are
# probably long evicted from the local-disk cache.
_RECENT_WINDOW_S = 1800  # 30 min
_DEFAULT_SAMPLE_PCT = 0.10
_DEFAULT_MAX_PER_RUN = 12

_VIDEO_EXTS = {"mp4", "webm", "mkv", "mov", "m4v"}


def _enabled() -> bool:
    try:
        from server.hub._state import state
        if state.settings is not None:
            return bool(state.settings.get("success_audit_enabled", False))
    except Exception:
        return False
    return False


def _sample_pct() -> float:
    try:
        from server.hub._state import state
        if state.settings is not None:
            v = float(state.settings.get("success_audit_sample_pct", _DEFAULT_SAMPLE_PCT))
            return max(0.0, min(1.0, v))
    except Exception:
        pass
    return _DEFAULT_SAMPLE_PCT


def _max_per_run() -> int:
    try:
        from server.hub._state import state
        if state.settings is not None:
            return int(state.settings.get("success_audit_max_per_run", _DEFAULT_MAX_PER_RUN))
    except Exception:
        pass
    return _DEFAULT_MAX_PER_RUN


def _interval_min() -> int:
    try:
        from server.hub._state import state
        if state.settings is not None:
            return int(state.settings.get("success_audit_interval_min", 30))
    except Exception:
        pass
    return 30


# ---------------------------------------------------------------------------
# ffprobe / ffmpeg helpers
# ---------------------------------------------------------------------------

async def _ffprobe_video(path: Path) -> dict | None:
    """Return ``{duration_s, codec, width, height}`` for ``path``, or None
    when ffprobe is missing / the file is unreadable / has no video stream."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception:
        return None
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except Exception:
        return None
    vstreams = [s for s in (data.get("streams") or []) if s.get("codec_type") == "video"]
    if not vstreams:
        return None
    v = vstreams[0]
    fmt = data.get("format") or {}
    try:
        dur = float(fmt.get("duration") or v.get("duration") or 0.0)
    except Exception:
        dur = 0.0
    return {
        "duration_s": dur,
        "codec": v.get("codec_name") or "?",
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
    }


async def _extract_midframe(video: Path, out_jpg: Path, *, at_s: float | None = None) -> bool:
    """Extract one midframe JPEG via ffmpeg. Best-effort: returns True on
    success, False otherwise. Default seek = midpoint."""
    if at_s is None:
        meta = await _ffprobe_video(video)
        at_s = max(0.5, (meta or {}).get("duration_s", 4.0) * 0.5)
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{at_s:.2f}", "-i", str(video),
            "-frames:v", "1", "-vf", "scale=640:-2",
            "-q:v", "5", str(out_jpg),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=20)
    except Exception:
        return False
    return out_jpg.is_file() and out_jpg.stat().st_size > 256


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

async def _list_candidates() -> list[dict]:
    """Return recent codegen-loop jobs across BOTH outcomes -- ``completed``
    (catch false positives) AND ``failed`` / ``review`` (catch false
    negatives). Whether to audit a particular job is decided downstream
    (cheap fs check for "has any assets at all")."""
    from server.hub._state import state
    if state.store is None:
        return []
    out: list[dict] = []
    now = time.time()
    for st in ("completed", "failed", "review"):
        try:
            jobs, _ = await state.store.list_job_infos(status=[st], limit=200)
        except Exception:
            continue
        for j in jobs:
            opts = getattr(j, "options", None)
            if opts is None:
                continue
            if (getattr(opts, "mode", None) or "fetch") != "codegen-loop":
                continue
            try:
                ts = (getattr(j, "completed_at", None)
                      or getattr(j, "started_at", None))
                if ts is None:
                    continue
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                elif hasattr(ts, "timestamp"):
                    ts = ts.timestamp()
                if now - float(ts) > _RECENT_WINDOW_S:
                    continue
            except Exception:
                continue
            out.append({
                "job_id": j.job_id,
                "url": getattr(j, "url", "") or "",
                "goal": getattr(opts, "goal", "") or "",
                "reported_status": st,
            })
    return out


def _summarize_assets(job_id: str) -> tuple[dict, Path | None]:
    """Return ``(counts_by_extension, biggest_video_or_None)`` for a job's
    saved assets. Used to give the vision audit a quick "what was saved"
    context even when there's no video to midframe."""
    from server.hub._state import get_storage_dir
    assets = get_storage_dir() / job_id / "assets"
    counts: dict[str, int] = {}
    biggest_video: tuple[int, Path] | None = None
    if not assets.is_dir():
        return counts, None
    try:
        for p in assets.iterdir():
            if not p.is_file():
                continue
            ext = p.suffix.lstrip(".").lower() or "(no_ext)"
            counts[ext] = counts.get(ext, 0) + 1
            if ext in _VIDEO_EXTS:
                sz = p.stat().st_size
                if biggest_video is None or sz > biggest_video[0]:
                    biggest_video = (sz, p)
    except Exception:
        pass
    return counts, (biggest_video[1] if biggest_video else None)


def _find_video_file(job_id: str) -> Path | None:
    """Return the largest video file under ``storage_dir/<job_id>/assets/``,
    or None when there isn't one. Largest is a reasonable proxy for "the
    actual content" when multiple files were saved."""
    from server.hub._state import get_storage_dir
    assets = get_storage_dir() / job_id / "assets"
    if not assets.is_dir():
        return None
    best: tuple[int, Path] | None = None
    try:
        for p in assets.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lstrip(".").lower() not in _VIDEO_EXTS:
                continue
            sz = p.stat().st_size
            if best is None or sz > best[0]:
                best = (sz, p)
    except Exception:
        return None
    return best[1] if best else None


# ---------------------------------------------------------------------------
# VisionAI call
# ---------------------------------------------------------------------------

async def _call_vision_audit(
    *, url: str, goal: str, reported_status: str,
    assets_summary: dict, final_jpg: Path | None,
    midframe: Path | None, video_meta: dict | None,
) -> tuple[dict | None, dict]:
    """Send (page + optional midframe + reported_status + asset summary) to
    a vision engine and parse the JSON verdict. ``midframe`` is optional --
    when there's no saved video we still ask the auditor whether the goal
    was met (false-negative path)."""
    out: dict = {"engine_slug": "", "latency_ms": 0, "raw": "", "error": None}
    try:
        from server.hub.perception_llm import find_vision_capable_target
        tgt = await find_vision_capable_target()
    except Exception as e:
        out["error"] = f"target resolve failed: {type(e).__name__}: {e}"
        return None, out
    if tgt is None:
        out["error"] = "no vision target available"
        return None, out
    out["engine_slug"] = getattr(tgt, "engine_slug", "") or getattr(tgt, "model", "")
    import base64
    def _b64(p: Path | None) -> str | None:
        if p is None or not p.is_file():
            return None
        try:
            return base64.b64encode(p.read_bytes()).decode("ascii")
        except Exception:
            return None
    page_b64 = _b64(final_jpg)
    mid_b64 = _b64(midframe) if midframe else None
    if page_b64 is None and mid_b64 is None:
        out["error"] = "no images available for audit"
        return None, out
    if video_meta:
        video_block = (
            f"SAVED VIDEO duration: {float(video_meta.get('duration_s') or 0.0):.1f}s\n"
            f"SAVED VIDEO codec: {video_meta.get('codec') or '?'}\n"
            f"SAVED VIDEO dims: {video_meta.get('width') or 0}x{video_meta.get('height') or 0}"
        )
    else:
        video_block = "SAVED VIDEO: (none saved -- 0 video files in assets)"
    image_parts: list[str] = []
    if page_b64: image_parts.append("(a) page final screenshot")
    if mid_b64:  image_parts.append(f"({'b' if page_b64 else 'a'}) saved video midframe")
    user_msg = _AUDIT_USER_TEMPLATE.format(
        url=url[:200], goal_short=(goal or "")[:300],
        reported_status=reported_status,
        assets_summary=json.dumps(assets_summary, sort_keys=True),
        video_block=video_block,
        n_images=(1 if page_b64 else 0) + (1 if mid_b64 else 0),
        image_legend=" / ".join(image_parts) if image_parts else "(none)",
    )
    content: list[dict] = [{"type": "text", "text": user_msg}]
    if page_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{page_b64}"},
        })
    if mid_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{mid_b64}"},
        })
    body = {
        "model": tgt.model,
        "messages": [
            {"role": "system", "content": _AUDIT_SYSTEM},
            {"role": "user", "content": content},
        ],
        "temperature": 0.0,
        "max_tokens": 300,
    }
    try:
        from server.hub.codegen import adapt_chat_body
        body = adapt_chat_body(tgt, body)
    except Exception:
        pass
    t0 = time.time()
    raw = ""
    try:
        async with httpx.AsyncClient(timeout=tgt.timeout) as cli:
            r = await cli.post(tgt.url, json=body, headers=tgt.headers)
            if r.status_code >= 400:
                out["error"] = f"http {r.status_code}: {r.text[:200]}"
                return None, out
            payload = r.json()
        out["latency_ms"] = int((time.time() - t0) * 1000)
        choices = payload.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            raw = msg.get("content") or ""
        out["raw"] = raw
        try:
            from server.hub._ai_io_log import record_ai_io
            _u = payload.get("usage") or {}
            record_ai_io(
                purpose="success_audit",
                engine_slug=out["engine_slug"],
                job_id=None,
                prompt=user_msg, response=raw,
                latency_ms=out["latency_ms"],
                tokens_in=_u.get("prompt_tokens"),
                tokens_out=_u.get("completion_tokens"),
            )
        except Exception:
            pass
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return None, out
    # Lenient JSON parse: try whole, then first {...}.
    import re as _re
    s = raw.strip()
    if s.startswith("```"):
        s = _re.sub(r"^```[a-zA-Z]*\n?|\n?```\s*$", "", s, flags=_re.M).strip()
    try:
        return json.loads(s), out
    except Exception:
        pass
    m = _re.search(r"\{[\s\S]*\}", s)
    if m:
        try:
            return json.loads(m.group(0)), out
        except Exception:
            pass
    out["error"] = "parse failed"
    return None, out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def _persist_result(row: dict) -> None:
    try:
        from server.hub._state import state
        pool = getattr(state, "mariadb_pool", None)
        if pool is None:
            return
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO audit_results
                       (ts, job_id, url, goal_short, reported_status,
                        verdict_kind, video_file, midframe_ref,
                        truly_succeeded, confidence, reason,
                        engine_slug, latency_ms, error)
                       VALUES (CURRENT_TIMESTAMP(3),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        row.get("job_id"), row.get("url"), row.get("goal_short"),
                        row.get("reported_status"), row.get("verdict_kind"),
                        row.get("video_file"), row.get("midframe_ref"),
                        (None if row.get("truly_succeeded") is None
                         else (1 if row["truly_succeeded"] else 0)),
                        row.get("confidence"), row.get("reason"),
                        row.get("engine_slug"), row.get("latency_ms"),
                        row.get("error"),
                    ),
                )
    except Exception as e:
        _log.debug("audit persist failed: %s", e)


# ---------------------------------------------------------------------------
# Audit one job
# ---------------------------------------------------------------------------

async def audit_one_job(job: dict) -> dict | None:
    """Audit one job in BOTH directions:
      * report=completed: check it's not a false positive (wrong video / ad).
      * report=failed|review: check it's not a false negative (assets actually
        meet the goal even though the job was marked failed).
    """
    from server.hub._state import get_storage_dir
    job_id = job.get("job_id")
    url = job.get("url") or ""
    goal = job.get("goal") or ""
    reported = job.get("reported_status") or ""
    assets_summary, biggest_video = _summarize_assets(job_id)
    # Optional: extract midframe when a video exists. Audits without a video
    # are still useful (false-negative check on image / html / text goals).
    video_meta: dict | None = None
    midframe_path: Path | None = None
    midhash = ""
    if biggest_video is not None:
        video_meta = await _ffprobe_video(biggest_video) or None
        if video_meta and (video_meta.get("duration_s") or 0) > 0.5:
            audit_dir = get_storage_dir() / job_id / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            mf = audit_dir / "midframe.jpg"
            if await _extract_midframe(biggest_video, mf, at_s=video_meta["duration_s"] * 0.5):
                midframe_path = mf
                try:
                    import hashlib
                    midhash = hashlib.sha1(mf.read_bytes()).hexdigest()
                except Exception:
                    pass
    final_jpg = get_storage_dir() / job_id / "final.jpg"
    # Need at least the page screenshot OR a midframe to audit. When the
    # fetcher never wrote a final.jpg AND the job saved no video, there's
    # nothing visual to audit; skip.
    if not final_jpg.is_file() and midframe_path is None:
        return None
    parsed, dbg = await _call_vision_audit(
        url=url, goal=goal, reported_status=reported,
        assets_summary=assets_summary,
        final_jpg=final_jpg if final_jpg.is_file() else None,
        midframe=midframe_path, video_meta=video_meta,
    )
    actually = None
    conf = None
    reason = ""
    if parsed:
        v = parsed.get("actually_succeeded")
        if v is None:
            v = parsed.get("truly_succeeded")  # back-compat with older prompts
        actually = bool(v) if v is not None else None
        try:
            conf = float(parsed.get("confidence") or 0.0)
        except Exception:
            conf = None
        reason = str(parsed.get("reason") or "")
    verdict_kind = _classify_quadrant(reported, actually)
    row = {
        "job_id": job_id, "url": url[:500], "goal_short": goal[:500],
        "reported_status": reported[:32],
        "verdict_kind": verdict_kind,
        "video_file": (biggest_video.name if biggest_video else "")[:255],
        "midframe_ref": midhash,
        "truly_succeeded": actually,
        "confidence": conf,
        "reason": reason[:1000],
        "engine_slug": dbg.get("engine_slug"),
        "latency_ms": dbg.get("latency_ms"),
        "error": dbg.get("error"),
    }
    await _persist_result(row)
    return row


# ---------------------------------------------------------------------------
# One audit run (called by the periodic loop)
# ---------------------------------------------------------------------------

async def run_one_pass() -> dict:
    """Sample → audit → persist. Returns summary counts."""
    if not _enabled():
        return {"skipped": "disabled"}
    cands = await _list_candidates()
    if not cands:
        return {"candidates": 0, "audited": 0}
    pct = _sample_pct()
    max_n = _max_per_run()
    take = max(1, min(max_n, int(round(len(cands) * pct))))
    sample = random.sample(cands, min(take, len(cands)))
    audited = 0
    counts = {
        VERDICT_TRUE_OK: 0, VERDICT_FALSE_POSITIVE: 0,
        VERDICT_FALSE_NEGATIVE: 0, VERDICT_TRUE_FAILURE: 0,
        VERDICT_UNPARSED: 0,
    }
    for job in sample:
        try:
            row = await audit_one_job(job)
            if row is None:
                continue
            audited += 1
            counts[row.get("verdict_kind") or VERDICT_UNPARSED] += 1
        except Exception as e:
            _log.debug("audit job %s crashed: %s", job.get("job_id"), e)
            counts[VERDICT_UNPARSED] += 1
    return {
        "candidates": len(cands), "sampled": len(sample), "audited": audited,
        "true_ok": counts[VERDICT_TRUE_OK],
        "false_positive": counts[VERDICT_FALSE_POSITIVE],
        "false_negative": counts[VERDICT_FALSE_NEGATIVE],
        "true_failure": counts[VERDICT_TRUE_FAILURE],
        "unparsed": counts[VERDICT_UNPARSED],
    }


# ---------------------------------------------------------------------------
# Periodic loop (wired from app.py lifespan)
# ---------------------------------------------------------------------------

_loop_task: asyncio.Task | None = None


async def _loop() -> None:
    while True:
        try:
            interval_s = max(60, _interval_min() * 60)
            if _enabled():
                _log.info("[success-audit] running one pass")
                res = await run_one_pass()
                _log.info("[success-audit] pass result: %s", res)
        except Exception as e:
            _log.info("[success-audit] loop iter crashed: %s", e)
        await asyncio.sleep(interval_s)


def start_loop() -> None:
    """Idempotent: spawn the periodic auditor as a background task. Called
    from app.py lifespan AFTER state/mariadb_pool are ready."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        _loop_task = asyncio.create_task(_loop(), name="success-audit-loop")
    except RuntimeError:
        pass
