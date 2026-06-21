"""Job CRUD: list / summary / get / result / cancel / delete / cleanup / create.

Part of the jobs/ route package (split from the old monolithic
routes/jobs.py). Shared helpers + router live in jobs/_base.py."""

from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from server.hub._state import config, get_storage_dir, state
from server.hub import objstore
from server.hub._helpers import _safe_job_file
from server.hub.routes.novnc import _proxy_session_dict
from server.hub.routes.sessions import (
    _novnc_autoconnect,
    _route_to_page,
    _send_session_action,
)
from server.protocol import JobInfo
import os
import shutil
from datetime import datetime
from server.hub.routes.novnc import _proxy_info
from server.protocol import AssetInfo, JobResult, JobStatus
from server.runner import DONE_SENTINEL
import uuid
from fastapi import WebSocket, WebSocketDisconnect
from server.protocol import Event
import time
from server.hub.hosts import _normalise_host, cookies_for_cdp
from server.hub.iterative_codegen import resolve_rerun_source
from server.hub.sessions import SessionInfo, new_session_id
from server.protocol import (
    HubAssignJob,
    JobProgress,
    JobRequest,
)
from server.hub.app import (  # noqa: E402
    _JOB_DISPATCH_POLL_S,
    JOB_DISPATCH_GRACE_S,
)

log = logging.getLogger(__name__)

from server.hub.routes.jobs._base import *  # noqa: F401,F403 (router + helpers)

def _scope_owner(request) -> str | None:
    """Owner id to filter job reads by, or None when the caller sees everything
    (Phase 2 tenancy). Only a non-admin user under enforce is scoped; admin /
    system / off / optional return None = unfiltered (non-breaking)."""
    from server.hub.auth import owner_of, should_scope
    p = getattr(getattr(request, "state", None), "principal", None)
    return owner_of(request) if should_scope(p) else None


async def _require_owned_job_info(job_id: str, request):
    """Fetch a job (404 if absent), then 404 again for a scoped caller that
    doesn't own it — so other tenants can't even confirm a job exists."""
    info = await _require_job_info(job_id)
    _own = _scope_owner(request)
    if _own is not None and getattr(info, "owner_id", "default") != _own:
        raise HTTPException(404, "job not found")
    return info


# Short-TTL cache for the /jobs list (consumed in list_jobs below). The admin
# polls /jobs every ~2s with a small, fixed set of param combos (the 4 status
# sub-tabs x page size), so a ~1.5s memo of the (infos, total) result skips the
# MariaDB query + per-row deserialisation on most polls -- the two costs that
# dominate this endpoint under the asset-upload-heavy load (py-spy 2026-06-08).
# Bounded + env-tunable; PAPRIKA_JOBS_LIST_CACHE_TTL_S=0 disables it.
_JOBS_LIST_CACHE: dict = {}
_JOBS_LIST_CACHE_TTL_S = float(os.environ.get("PAPRIKA_JOBS_LIST_CACHE_TTL_S") or 1.5)
_JOBS_LIST_CACHE_MAX = 64


async def _fetch_job_role_overrides(job_ids: list) -> dict:
    """Bulk-fetch per-job role overrides from MariaDB. ``{job_id: role}``."""
    if not job_ids:
        return {}
    try:
        pool = getattr(state, "mariadb_pool", None)
        if pool is None:
            return {}
        from server.hub.mariadb import job_role_overrides_get_many
        return await job_role_overrides_get_many(pool, job_ids)
    except Exception:
        return {}


async def _build_page_roles_map(page) -> dict:
    """Compute URL-based per-job page role (`detail` / `listing` / `top` /
    `error` / `unknown` + confidence + reason) for every job on a page.

    Pure URL structure + per-host template stats (server/hub/_page_role.py)
    — no LLM, ~ms. The roles are returned as a separate envelope field
    ``page_roles: {job_id: {value, confidence, reason}}`` so the JobInfo
    Pydantic model stays unchanged. Best-effort: any failure (transient
    MariaDB hiccup, kill-switched off) yields {} so the jobs list never
    breaks on classification.
    """
    import os as _os
    if (_os.environ.get("PAPRIKA_JOBS_PAGE_ROLE", "1") or "1").strip().lower() in ("0", "false", "no", "off"):
        return {}
    try:
        from server.hub._page_role import role_for_url
    except Exception:
        return {}
    urls = [(getattr(p, "job_id", "") or "", getattr(p, "url", "") or "") for p in page]
    out: dict = {}
    # Persisted read-back: role_for_url is URL-derived + stable, so it's
    # computed ONCE and stored on the jobs row (page_role). Read the stored
    # auto-roles first and only compute (then write back) the jobs we don't
    # have yet, instead of re-running role_for_url for every job on every
    # /jobs request (the #jobs "初回表示が遅い" cost the operator hit).
    _ids = [jid for jid, _u in urls if jid]
    _getter = getattr(state.store, "get_page_roles", None)
    if callable(_getter):
        try:
            out.update(await _getter(_ids))
        except Exception:
            pass
    _missing = [(jid, u) for jid, u in urls if jid and u and jid not in out]
    if _missing:
        try:
            results = await asyncio.gather(
                *[role_for_url(u) for _, u in _missing],
                return_exceptions=True,
            )
        except Exception:
            results = []
        computed: dict = {}
        for (jid, _u), r in zip(_missing, results):
            if isinstance(r, tuple) and len(r) == 3:
                try:
                    val, conf, reason = r
                    computed[jid] = {
                        "value": str(val or "unknown"),
                        "confidence": float(conf or 0.0),
                        "reason": str(reason or ""),
                    }
                except Exception:
                    pass
        out.update(computed)
        # Write back so the next request reads instead of recomputing.
        _setter = getattr(state.store, "set_page_roles", None)
        if callable(_setter) and computed:
            try:
                await _setter(computed)
            except Exception:
                pass
    # Per-job overrides take precedence over the URL heuristic + per-host-
    # template overrides (the latter are already baked into role_for_url's
    # return value above, so what's left is the per-job pin).
    try:
        per_job = await _fetch_job_role_overrides([jid for jid, _u in urls])
        for jid, role in per_job.items():
            cur = out.get(jid) or {}
            out[jid] = {
                "value": str(role),
                "confidence": 1.0,
                "reason": "operator override (per-job)",
                "auto_value": cur.get("value", ""),
                "auto_reason": cur.get("reason", ""),
            }
    except Exception:
        pass
    return out


@router.get("/jobs")
async def list_jobs(
    request: Request,
    offset: int = 0,
    limit: int = 20,
    status: str | None = None,
    mode: str | None = None,
    q: str | None = None,
    downloading: int = 0,
) -> dict:
    """List jobs with optional server-side pagination and filtering.

    Query params:
      * ``offset`` -- skip this many entries (default 0).
      * ``limit``  -- max entries to return. **Default 20, hard cap 500.**
                      ``limit<=0`` is treated as the default (NOT "all").
                      Page through more via ``offset``.
      * ``status`` -- filter by status (``running``, ``completed``,
                      ``failed``, ``cancelled``, ``queued``).
                      Comma-separated for multiple: ``status=completed,failed``.
      * ``mode``   -- filter by job mode (``fetch``, ``codegen-loop``, etc.).
      * ``q``      -- case-insensitive substring match against URL.

    Returns a paginated envelope::

        {total, count, offset, limit, jobs: [...]}

    .. versionchanged:: 2026-06-05
       ``limit`` now DEFAULTS TO 20 (was 0 = unbounded) and is hard-capped at
       500. A full fetch hydrated thousands of JobInfos -- ~25s at 8.5k rows --
       and made the admin "最近のジョブ" tab block on it. Callers that need
       everything must page via ``offset`` (the response carries ``total``).

    .. versionchanged:: 2026-05-26
       Added pagination (offset/limit) and filters (status/mode/q).
       Response shape changed from ``list[JobInfo]`` to the envelope
       dict above.  The admin UI was updated in the same commit.
    """
    assert state.store is not None
    # Default 20, hard cap 500. limit<=0 means "use the default" (NOT "all"):
    # a full fetch hydrates thousands of JobInfos (~25s at 8.5k rows) and
    # blocked the admin Jobs tab. Page via offset for more.
    _l = int(limit or 0)
    lim = 20 if _l <= 0 else min(_l, 500)
    off = max(0, int(offset or 0))

    # ------------------------------------------------------------------
    # Fast path: MariaDBJobStore exposes a single-query bulk hydrate
    # that pushes status/mode/url filtering + paging into SQL. Without
    # this, every status sub-tab click triggered an N+1 walk: one query
    # to list all 2,000+ ids + one query per id to hydrate -- ~2 s per
    # click. The fast path collapses that to ~2 indexed queries (count +
    # paged SELECT) running in <50 ms.
    # ------------------------------------------------------------------
    fast = getattr(state.store, "list_job_infos", None)
    if callable(fast):
        status_list = (
            [s.strip().lower() for s in status.split(",") if s.strip()]
            if status else None
        )
        # ``downloading=1`` is a virtual sub-filter on the running set: only
        # running jobs can be actively downloading, and we further trim to
        # those whose JobInfo.progress carries a download_pct (set by the
        # WorkerJobLog → _persist_dl_progress pipeline). We force-narrow the
        # status filter to ``running`` so the underlying SQL paging sees the
        # smallest possible base set, then post-filter the rows in python --
        # cheap because the active-download count is bounded by the fleet's
        # lane count (~120) and typically <20.
        _dl_only = bool(downloading)
        if _dl_only:
            status_list = ["running"]
        mode_list = (
            [m.strip().lower() for m in mode.split(",") if m.strip()]
            if mode else None
        )
        own = _scope_owner(request)
        # ~1.5s memo keyed on the exact query params (see _JOBS_LIST_CACHE note
        # above). Cache HIT skips the MariaDB query + row deser entirely.
        # _proxy_info below still runs per request -- it model_copy's, never
        # mutates the cached JobInfos -- so each response stays request-correct.
        _ck = (off, lim, tuple(status_list or ()), tuple(mode_list or ()),
               q or "", own or "", _dl_only)
        _now = time.time()
        _hit = _JOBS_LIST_CACHE.get(_ck)
        if _hit is not None and (_now - _hit[0]) < _JOBS_LIST_CACHE_TTL_S:
            infos, filtered_total, page_roles = _hit[1]
        else:
            if _dl_only:
                # Active-DL view: pull the full running set (typically <100),
                # post-filter on JobInfo.progress.download_pct, then paginate
                # the filtered list ourselves so ``total`` reflects the
                # filtered count (not the raw running count). The fleet's lane
                # cap bounds this set in practice; the 500 hard cap on
                # list_job_infos is plenty.
                all_running, _ = await fast(
                    offset=0,
                    limit=500,
                    status=status_list,
                    mode=mode_list,
                    url_substr=q,
                    owner_id=own,
                )
                _matched = [
                    i for i in all_running
                    if getattr(i, "progress", None) is not None
                    and getattr(i.progress, "download_pct", None) is not None
                    and (i.progress.download_pct or 0) < 100.0
                ]
                filtered_total = len(_matched)
                infos = _matched[off:off + lim]
            else:
                infos, filtered_total = await fast(
                    offset=off,
                    limit=lim,
                    status=status_list,
                    mode=mode_list,
                    url_substr=q,
                    owner_id=own,
                )
            # page_roles depends only on each job's id + url (stable across the
            # cache TTL), so memoise it WITH the infos. It was re-running
            # role_for_url for every job on EVERY request -- even cache hits,
            # outside this cache -- which was the bulk of the
            # /jobs?status=running cost (~0.5s for ~40 running jobs, the #jobs
            # "初回表示が遅い" the operator hit).
            page_roles = await _build_page_roles_map(infos)
            if _JOBS_LIST_CACHE_TTL_S > 0:
                if len(_JOBS_LIST_CACHE) >= _JOBS_LIST_CACHE_MAX:
                    _oldest = min(
                        _JOBS_LIST_CACHE,
                        key=lambda k: _JOBS_LIST_CACHE[k][0],
                    )
                    _JOBS_LIST_CACHE.pop(_oldest, None)
                _JOBS_LIST_CACHE[_ck] = (_now, (infos, filtered_total, page_roles))
        page = [_proxy_info(i, request) for i in infos]
        return {
            "total": filtered_total,
            "count": len(page),
            "offset": off,
            "limit": lim,
            "jobs": page,
            "page_roles": page_roles,
        }

    # ------------------------------------------------------------------
    # Slow path: in-memory / Redis stores fall through to the
    # hydrate-then-filter-in-Python approach. Equivalent behaviour,
    # acceptable cost when N is small.
    # ------------------------------------------------------------------
    _own = _scope_owner(request)
    has_filter = bool(status or mode or q or _own)
    if has_filter or lim == 0:
        ids = await state.store.list_job_ids()
        total_in_store = len(ids)
    else:
        total_in_store = await state.store.count_jobs()
        ids = await state.store.list_job_ids(offset=off, limit=lim)

    # Hydrate
    infos: list[JobInfo] = []
    for jid in ids:
        info = await state.store.get_job_info(jid)
        if info is not None:
            infos.append(_proxy_info(info, request))

    # Apply filters (post-hydration because we need fields)
    if status:
        allowed = {s.strip().lower() for s in status.split(",")}
        infos = [i for i in infos if i.status.value in allowed]
    if mode:
        allowed_modes = {m.strip().lower() for m in mode.split(",")}
        infos = [
            i for i in infos
            if (i.options.get("mode") if isinstance(i.options, dict)
                else getattr(i.options, "mode", "fetch") or "fetch"
               ).lower() in allowed_modes
        ]
    if q:
        ql = q.lower()
        infos = [i for i in infos if ql in (i.url or "").lower()]
    if _own is not None:
        infos = [i for i in infos if getattr(i, "owner_id", "default") == _own]

    filtered_total = len(infos)

    # Paginate (only when filters were applied client-side)
    if has_filter:
        if lim > 0:
            page = infos[off : off + lim]
        else:
            page = infos[off:] if off else infos
    else:
        # Already sliced at the store level (or lim=0 → all)
        if lim == 0:
            page = infos[off:] if off else infos
        else:
            page = infos  # already sliced
            filtered_total = total_in_store

    return {
        "total": filtered_total,
        "count": len(page),
        "offset": off,
        "limit": lim,
        "jobs": page,
        "page_roles": await _build_page_roles_map(page),
    }


@router.get("/jobs/summary")
async def get_jobs_summary() -> dict:
    """Dashboard-shaped overview of the job store. One round-trip,
    designed to be polled from the admin UI AND scripted from
    paprika-client (``cli.jobs_summary()``).

    Returned shape::

        {
          "as_of":  "2026-06-01T22:45:00Z",
          "total":  3154,
          "by_status": {queued, running, completed, failed, cancelled, ...},
          "by_mode":   {fetch, "codegen-loop", rerun, ...},
          "recent_1h":  {created, by_status: {...}, success_rate: 0.93},
          "recent_24h": {created, by_status: {...}, success_rate: 0.91},
          "active": {
            "queued":  N,
            "running": M,
            "running_preview": [
              {job_id, url, mode, worker_id, lane_idx, started_at, age_s},
              ...up to _JOBS_SUMMARY_RUNNING_PREVIEW
            ]
          }
        }

    ``success_rate`` is computed over terminal jobs only (completed
    + failed); pending / running jobs aren't counted in the
    denominator. ``None`` when no terminal jobs in the window
    (avoids dividing by 0 for fresh deploys).

    Performance: a store with ``count_by_status_and_mode`` (e.g.
    MariaDB) computes everything in ~5 indexed SQL queries (<50ms
    total at 100k rows). Stores without those methods (in-memory
    / Redis) fall back to a single Python iteration that's fine up
    to ~10k rows; beyond that they should grow store-side aggregation
    of their own (Redis: per-status sorted set; SQL: GROUP BY w/
    composite index on (status, created_at)).

    Replaces the older ``/jobs/counts`` endpoint -- the admin UI
    and SDK both moved to this name.
    """
    import time as _time
    from datetime import datetime, timezone

    now = _time.monotonic()
    cached = _JOBS_SUMMARY_CACHE.get("value")
    if cached is not None and (now - _JOBS_SUMMARY_CACHE.get("ts", 0.0)) < _JOBS_SUMMARY_TTL_S:
        return cached

    assert state.store is not None
    wall_now = _time.time()

    # ----- by_status / by_mode / total + recent windows.
    # Prefer the combined one-acquire ``summary_counts`` (MariaDB: 2 queries for
    # EVERYTHING, incl. the recent windows via conditional aggregation -- ~5x
    # fewer DB round-trips than the per-window path below, which ran 3 queries
    # per window = 9 total and made the admin Jobs tab block ~2s). Fall back to
    # ``count_by_status_and_mode`` (3 queries/window) or the Python walk.
    windows_h = list(_JOBS_SUMMARY_RECENT_WINDOWS_H)
    by_status = None
    by_mode: dict = {}
    total = 0
    win_results: list[tuple[dict, int]] | None = None
    fast_summary = getattr(state.store, "summary_counts", None)
    if callable(fast_summary):
        try:
            by_status, by_mode, total, win_results = await fast_summary(
                window_ts=[wall_now - h * 3600 for h in windows_h],
            )
        except Exception:
            log.warning(
                "jobs/summary: summary_counts failed; falling back", exc_info=True
            )
            by_status = None
    if by_status is None:
        fast_counts = getattr(state.store, "count_by_status_and_mode", None)
        if callable(fast_counts):
            by_status, by_mode, total = await fast_counts()
        else:
            by_status, by_mode, total = await _summary_python_count(None)
        win_results = []
        for hours in windows_h:
            ts_cut = wall_now - hours * 3600
            if callable(fast_counts):
                rb, _rm, rt = await fast_counts(created_after_ts=ts_cut)
            else:
                rb, _rm, rt = await _summary_python_count(ts_cut)
            win_results.append((rb, rt))

    recent: dict[str, dict] = {}
    for hours, (rec_by_status, rec_total) in zip(windows_h, win_results or []):
        completed = rec_by_status.get("completed", 0)
        failed = rec_by_status.get("failed", 0)
        terminal = completed + failed
        success_rate = (completed / terminal) if terminal > 0 else None
        recent[f"recent_{hours}h"] = {
            "created": rec_total,
            "by_status": rec_by_status,
            "success_rate": success_rate,
        }

    # ----- active section: running preview + queued count
    queued_n = by_status.get("queued", 0)
    running_n = by_status.get("running", 0)
    # ----- downloading count (running jobs with an active video download).
    # Subset of running_n where JobInfo.progress.download_pct is set and < 100.
    # Bounded by the fleet's lane count (~120), so we walk the running set
    # once -- python loop over ~60-100 hydrated infos is microseconds. Injected
    # into by_status as a virtual sub-status so the admin sub-tab badge can
    # read it like the others. NB: status='downloading' is NOT a real job
    # status, just a synthesised count for the UI.
    downloading_n = 0
    fast_list = getattr(state.store, "list_job_infos", None)
    if callable(fast_list) and running_n > 0:
        try:
            _all_running, _ = await fast_list(
                offset=0,
                limit=500,
                status=["running"],
            )
            for _i in _all_running:
                _pg = getattr(_i, "progress", None)
                if _pg is None:
                    continue
                _pct = getattr(_pg, "download_pct", None)
                if _pct is not None and _pct < 100.0:
                    downloading_n += 1
        except Exception:
            log.debug("jobs/summary: downloading count failed", exc_info=True)
    by_status["downloading"] = downloading_n
    running_preview: list[dict] = []
    if callable(fast_list) and running_n > 0:
        try:
            infos, _ = await fast_list(
                offset=0,
                limit=_JOBS_SUMMARY_RUNNING_PREVIEW,
                status=["running"],
            )
            for i in infos:
                started = getattr(i, "started_at", None)
                age_s: float | None = None
                if started is not None:
                    try:
                        # JobInfo.started_at can be a naive datetime
                        # (the worker side writes datetime.utcnow() in
                        # several places). Treat tz-less timestamps as
                        # UTC so the subtraction below doesn't raise
                        # "can't subtract naive from aware".
                        s_aware = (
                            started if started.tzinfo is not None
                            else started.replace(tzinfo=timezone.utc)
                        )
                        age_s = max(
                            0.0,
                            (datetime.now(timezone.utc) - s_aware).total_seconds(),
                        )
                    except Exception:
                        age_s = None
                opts = getattr(i, "options", None)
                if isinstance(opts, dict):
                    mode = opts.get("mode") or "fetch"
                else:
                    mode = getattr(opts, "mode", "fetch") or "fetch"
                running_preview.append({
                    "job_id":     getattr(i, "job_id", ""),
                    "url":        getattr(i, "url", ""),
                    "mode":       mode,
                    "worker_id":  getattr(i, "worker_id", "") or "",
                    "lane_idx":   getattr(i, "lane_idx", None),
                    "started_at": started.isoformat() if started else None,
                    "age_s":      age_s,
                })
        except Exception:
            running_preview = []

    result = {
        "as_of":     datetime.now(timezone.utc).isoformat(),
        "total":     total,
        "by_status": by_status,
        "by_mode":   by_mode,
        **recent,
        "active": {
            "queued":          queued_n,
            "running":         running_n,
            "running_preview": running_preview,
        },
    }
    _JOBS_SUMMARY_CACHE["ts"] = now
    _JOBS_SUMMARY_CACHE["value"] = result
    return result


@router.get("/jobs/{job_id}", response_model=JobInfo)
async def get_job(job_id: str, request: Request) -> JobInfo:
    info = await _require_job_info(job_id)
    # Phase 2 tenancy: a scoped (non-admin, enforce) caller may only read its
    # own jobs; others 404 (not 403, to avoid confirming existence).
    _own = _scope_owner(request)
    if _own is not None and getattr(info, "owner_id", "default") != _own:
        raise HTTPException(404, "job not found")
    # Rewrite novnc_url to point at the hub's noVNC proxy so external
    # clients don't need to reach individual worker LAN IPs. See
    # ``_hub_proxied_novnc_url`` and the /jobs/{id}/novnc/* endpoints
    # below for the proxy implementation.
    return _proxy_info(info, request)


@router.get("/jobs/{job_id}/result", response_model=JobResult)
async def get_job_result(job_id: str, request: Request) -> JobResult:
    info = await _require_owned_job_info(job_id, request)
    if info.status not in (
        JobStatus.completed,
        JobStatus.failed,
        JobStatus.cancelled,
        JobStatus.review,
    ):
        raise HTTPException(409, f"job not finished (status={info.status})")
    result = await state.store.get_job_result(job_id)
    if result is None:
        return JobResult(job_id=job_id, status=info.status, error=info.error)
    # Patch missing url / page_url / mime from on-disk .meta/ sidecars.
    # Covers jobs persisted before the protocol gained page_url.
    return _backfill_asset_metadata(job_id, result)


@router.get("/jobs/{job_id}/visited")
async def get_job_visited(job_id: str, request: Request) -> dict:
    """Return the list of canonical URLs the agent visited during the job.

    Mostly empty for plain-fetch jobs; populated for agent-mode jobs
    (i.e. those launched with JobOptions.goal). Same data exposed in
    JobResult.visited_urls, broken out so dashboards / scripts can hit
    a stable JSON shape without parsing the full result object.
    """
    info = await _require_owned_job_info(job_id, request)
    result = await state.store.get_job_result(job_id)
    urls = list(result.visited_urls) if result else []
    return {
        "job_id": job_id,
        "status": info.status,
        "count": len(urls),
        "visited_urls": urls,
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict:
    """Cancel an in-flight job. Cancels the orchestrator task (which
    propagates to execute_in_sandbox -> the docker runner subprocess
    gets killed), then marks the job as ``cancelled`` and broadcasts a
    final DONE_SENTINEL so /jobs/{id}/events subscribers unblock.

    Idempotent: cancelling an already-finished job is a no-op
    returning ``{cancelled: false}``."""
    info = await _require_owned_job_info(job_id, request)
    if info.status not in (JobStatus.queued, JobStatus.running):
        return {
            "job_id": job_id,
            "cancelled": False,
            "reason": f"job already {info.status}",
        }
    t = state.local_tasks.pop(job_id, None)
    cancelled_task = False
    if t and not t.done():
        t.cancel()
        cancelled_task = True

    # Force a terminal state on the JobInfo so the admin UI flips the
    # badge to "cancelled" immediately, even before the orchestrator's
    # except path persists its own update.
    info.status = JobStatus.cancelled
    info.error = "cancelled by user"
    info.completed_at = datetime.utcnow()
    if info.progress is not None:
        info.progress.phase = "cancelled"
        info.progress.last_log = "[user] job cancelled"
    await state.store.save_job_info(info)
    try:
        await state.store.publish_log(job_id, "[user] job cancelled")
        await state.store.publish_log(job_id, DONE_SENTINEL)
    except Exception:
        pass
    # Best-effort: close any sessions this job still owns. _cleanup_
    # orphan_sessions is defined inside _run_codegen_loop_job's scope
    # so we replicate the minimal flow here.
    if state.sessions is not None and state.registry is not None:
        for sess in [s for s in state.sessions.all() if s.job_id == job_id]:
            sid = sess.session_id
            state.sessions.remove(sid)
            worker = state.registry.connections.get(sess.worker_id)
            if worker is None:
                continue
            try:
                await worker.end_session(sid, timeout=5.0)
            except Exception:
                pass
    return {"job_id": job_id, "cancelled": True, "task_was_running": cancelled_task}


@router.get("/jobs/{job_id}/page-role")
async def get_job_page_role(job_id: str) -> dict:
    """Single-job version of the /jobs envelope's page_roles map. Returns
    ``{value, confidence, reason, override, override_value}`` for the
    Live job panel to render the 種類 badge + correction dropdown."""
    info = await state.store.get_job_info(job_id)
    if info is None:
        raise HTTPException(404, f"job {job_id!r} not found")
    url = getattr(info, "url", "") or ""
    from server.hub._page_role import role_for_url, host_from_url, templatize
    out: dict = {
        "job_id": job_id,
        "url": url,
        "host": host_from_url(url) or "",
        "url_template": templatize(url) or "",
    }
    if url:
        try:
            val, conf, reason = await role_for_url(url)
            out["value"] = val
            out["confidence"] = round(float(conf), 3)
            out["reason"] = reason
        except Exception as e:
            out["value"] = "unknown"
            out["confidence"] = 0.0
            out["reason"] = f"error: {e}"
    else:
        out["value"] = "unknown"
        out["confidence"] = 0.0
        out["reason"] = "no url"
    # Surface per-job override + host-template override separately so the UI
    # can show "currently pinned to category, but heuristic says listing".
    try:
        pool = getattr(state, "mariadb_pool", None)
        if pool is not None:
            from server.hub.mariadb import job_role_override_get, host_url_role_overrides_get
            pj = await job_role_override_get(pool, job_id)
            out["job_override"] = pj
            host = out["host"]
            if host:
                hm = await host_url_role_overrides_get(pool, host)
                out["host_override"] = hm.get(out["url_template"], "")
            else:
                out["host_override"] = ""
        else:
            out["job_override"] = ""
            out["host_override"] = ""
    except Exception:
        out["job_override"] = ""
        out["host_override"] = ""
    return out


@router.put("/jobs/{job_id}/page-role-override")
async def put_job_page_role_override(job_id: str, body: dict) -> dict:
    """Operator pins a corrected page-role for this single job. With
    ``apply_to_host: true`` the same override is also registered on the
    host's URL template so every future job whose URL templates to the
    same value gets the corrected role automatically.

    Body: ``{"value": "detail|listing|category|tag|top|error|unknown",
             "apply_to_host": bool}``. ``value: ""`` clears."""
    body = body or {}
    role = (body.get("value") or "").strip()
    _VALID = {"detail", "listing", "category", "tag", "top", "error", "unknown"}
    if role and role not in _VALID:
        raise HTTPException(400, f"value must be one of: {sorted(_VALID)} or '' to clear")
    pool = getattr(state, "mariadb_pool", None)
    if pool is None:
        raise HTTPException(503, "no MariaDB pool configured")
    # Resolve the job's URL so we can also pin the host template.
    info = await state.store.get_job_info(job_id)
    if info is None:
        raise HTTPException(404, f"job {job_id!r} not found")
    url = getattr(info, "url", "") or ""
    try:
        from server.hub.mariadb import (
            job_role_override_upsert, job_role_override_delete,
            host_url_role_override_upsert,
        )
        if role:
            await job_role_override_upsert(pool, job_id, role, set_by="operator")
        else:
            await job_role_override_delete(pool, job_id)
        if bool(body.get("apply_to_host")) and role and url:
            from server.hub._page_role import (
                host_from_url, templatize, invalidate_host_overrides,
            )
            h = host_from_url(url)
            tpl = templatize(url)
            if h and tpl:
                await host_url_role_override_upsert(pool, h, tpl, role, set_by="operator")
                invalidate_host_overrides(h)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"override write failed: {e}")
    # Bust the /jobs response cache so the next list reflects the change.
    try:
        _JOBS_LIST_CACHE.clear()
    except Exception:
        pass
    return {
        "job_id": job_id, "value": role, "applied_to_host": bool(body.get("apply_to_host")) and bool(role),
        "cleared": (not role),
    }


@router.post("/jobs/{job_id}/resolve-review")
async def resolve_review_job(job_id: str, request: Request) -> dict:
    """Resolve a 課題(review) job back to ``completed`` and clear its
    ``review_reason`` -- removing it from the #jobs 課題 sub-tab. The fetch
    itself already completed (課題 is a hub-side re-bucketing of a completed
    fetch via _review.classify_review), so ``completed`` is its natural
    terminal status. Used by the 課題 row's 「対象外」 button (which also
    registers the host as ``excluded``). No-op for non-review jobs."""
    info = await _require_owned_job_info(job_id, request)
    if info.status != JobStatus.review:
        return {"job_id": job_id, "resolved": False, "reason": f"job is {info.status}"}
    info.status = JobStatus.completed
    if hasattr(info, "review_reason"):
        info.review_reason = None
    await state.store.save_job_info(info)
    try:
        await state.store.publish_log(job_id, "[user] 課題 → completed (host を対象外に登録)")
    except Exception:
        pass
    return {"job_id": job_id, "resolved": True, "status": "completed"}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, request: Request) -> dict:
    assert state.store is not None
    _own = _scope_owner(request)
    if _own is not None:
        _i = await state.store.get_job_info(job_id)
        if _i is not None and getattr(_i, "owner_id", "default") != _own:
            raise HTTPException(404, "job not found")
    t = state.local_tasks.pop(job_id, None)
    if t and not t.done():
        t.cancel()
    # Purge the durable MinIO/S3 prefix BEFORE dropping the row. The local
    # dir + jobs row are caches over the bucket; without this the row vanished
    # but the bucket kept the bytes (orphan). POST /jobs/cleanup and
    # POST /jobs/minio-orphan-sweep already did this; per-job DELETE now does
    # it too so caller intent ("delete this job") actually reclaims storage
    # everywhere instead of leaking it into the bucket.
    minio_objects = 0
    minio_bytes = 0
    if objstore.enabled():
        try:
            r = await objstore.delete_prefix(job_id)
            minio_objects = int(r.get("objects") or 0)
            minio_bytes = int(r.get("bytes") or 0)
        except Exception:
            pass
    deleted = await state.store.delete_job(job_id)
    job_dir = get_storage_dir() / job_id
    try:
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:
        pass
    if not deleted:
        raise HTTPException(404, f"job '{job_id}' not found")
    return {
        "deleted": job_id,
        "minio_objects": minio_objects,
        "minio_bytes": minio_bytes,
    }


@router.post("/jobs/cleanup")
async def cleanup_jobs(body: dict) -> dict:
    """Bulk delete old / large jobs to reclaim disk.

    Sits under ``/jobs`` (alongside the per-job verb actions
    ``cancel`` / ``screenshot``) so the Swagger ``Jobs`` section
    surfaces it where operators expect bulk maintenance to live.

    The legacy URL ``POST /admin/cleanup_jobs`` is retained as a
    hidden alias for one release cycle so existing cron scripts /
    admin-UI HTML keep working without a same-day flag day.

    Body knobs (all optional, AND-ed together when multiple are given):

      older_than_days: int
          Only candidates whose ``completed_at`` (or ``created_at``
          if completed_at is null) is older than N days.
      status_in: list[str]
          Only candidates whose ``status`` is in this list. Default
          ["completed", "failed", "cancelled"] -- in-flight jobs are
          NEVER cleaned even if explicitly requested.
      min_size_mb: int
          Only candidates whose on-disk size is at least N MiB.
      keep_last: int
          Always keep the N most-recently-created jobs regardless of
          age / size. Default 10 -- protects "show me the latest" UX.
      dry_run: bool
          If true, just return what WOULD be deleted. Default false.

    Returns ``{candidates: [...], deleted: [...], total_freed_bytes,
    skipped: [...], dry_run}``.
    """
    assert state.store is not None
    body = body or {}
    older_than_days = body.get("older_than_days")
    min_size_mb = body.get("min_size_mb")
    keep_last = int(body.get("keep_last") or 10)
    dry_run = bool(body.get("dry_run") or False)
    status_in = set(body.get("status_in") or ["completed", "failed", "cancelled"])

    # Enumerate all jobs with metadata.
    job_ids = await state.store.list_job_ids()
    rows: list[dict] = []
    for jid in job_ids:
        info = await state.store.get_job_info(jid)
        if info is None:
            continue
        size = _job_dir_size_bytes(jid)
        when = info.completed_at or info.created_at
        rows.append(
            {
                "job_id": jid,
                "status": str(info.status).split(".")[-1],
                "created_at": (info.created_at.isoformat() + "Z") if info.created_at else None,
                "completed_at": (info.completed_at.isoformat() + "Z")
                if info.completed_at
                else None,
                "size_bytes": size,
                "_when": when,
            }
        )

    # Sort newest first, reserve the keep_last "always keep" set.
    rows.sort(key=lambda r: r["_when"] or datetime.min, reverse=True)
    protected = {r["job_id"] for r in rows[: max(0, keep_last)]}

    now = datetime.utcnow()
    candidates: list[dict] = []
    skipped: list[dict] = []
    for r in rows:
        reason_keep: str | None = None
        if r["job_id"] in protected:
            reason_keep = f"protected by keep_last={keep_last}"
        elif r["status"] not in status_in:
            reason_keep = f"status={r['status']!r} not in delete set (probably still running)"
        elif older_than_days is not None:
            when = r["_when"]
            if when is None or (now - when).total_seconds() < int(older_than_days) * 86400:
                reason_keep = f"younger than {older_than_days} day(s)"
        if reason_keep is None and min_size_mb is not None:
            if r["size_bytes"] < int(min_size_mb) * 1024 * 1024:
                reason_keep = f"size {r['size_bytes']} < {int(min_size_mb)} MiB"
        if reason_keep:
            skipped.append({"job_id": r["job_id"], "reason": reason_keep})
        else:
            candidates.append(
                {
                    "job_id": r["job_id"],
                    "status": r["status"],
                    "size_bytes": r["size_bytes"],
                    "age_days": (now - r["_when"]).total_seconds() / 86400 if r["_when"] else None,
                }
            )

    # Also purge each job's DURABLE bucket data (MinIO/S3). The local size shown
    # above is the on-disk cache only (evicted to ~0 for old jobs), so without
    # this the cleanup deleted the DB row + cache but ORPHANED the real bytes in
    # the bucket. Default on; pass purge_minio=false to keep the durable copy.
    purge_minio = bool(body.get("purge_minio", True))
    deleted: list[str] = []
    total_freed = 0
    minio_freed = 0
    minio_objects = 0
    if not dry_run:
        for c in candidates:
            try:
                t = state.local_tasks.pop(c["job_id"], None)
                if t and not t.done():
                    t.cancel()
                if purge_minio and objstore.enabled():
                    try:
                        r = await objstore.delete_prefix(c["job_id"])
                        minio_freed += int(r.get("bytes") or 0)
                        minio_objects += int(r.get("objects") or 0)
                    except Exception:
                        pass
                await state.store.delete_job(c["job_id"])
                d = get_storage_dir() / c["job_id"]
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
                deleted.append(c["job_id"])
                total_freed += c["size_bytes"]
            except Exception as e:
                skipped.append({"job_id": c["job_id"], "reason": f"delete failed: {e}"})

    return {
        "dry_run": dry_run,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "candidate_total_bytes": sum(c["size_bytes"] for c in candidates),
        "deleted": deleted,
        "total_freed_bytes": total_freed,
        "purge_minio": purge_minio,
        "minio_freed_bytes": minio_freed,
        "minio_objects_deleted": minio_objects,
        "skipped": skipped,
        "protected_count": len(protected),
    }


@router.post("/admin/cleanup_jobs", include_in_schema=False)
async def cleanup_jobs_legacy(body: dict) -> dict:
    return await cleanup_jobs(body)


@router.post("/jobs/minio-orphan-sweep")
async def minio_orphan_sweep(body: dict) -> dict:
    """Reclaim bucket (MinIO/S3) data for jobs whose DB row no longer exists --
    orphaned by a prior delete/cleanup that only removed the row + local cache.

    DRY-RUN by default (counts orphans, deletes nothing). ``execute=true``
    deletes; ``limit`` caps deletions per call (0 = no cap). DESTRUCTIVE +
    permanent when execute=true: the bucket is the durable store.

    Safety: aborts execute when the DB job-id read looks incomplete (empty, or
    far off from ``count_jobs``) so a transient DB hiccup can't make every
    prefix look orphaned and nuke live data."""
    assert state.store is not None
    body = body or {}
    execute = bool(body.get("execute") or False)
    limit = int(body.get("limit") or 0)
    if not objstore.enabled():
        return {"error": "objstore disabled"}
    db_ids = set(await state.store.list_job_ids())
    minio_ids = await objstore.list_job_prefixes()
    orphans = [j for j in minio_ids if j not in db_ids]
    out: dict = {
        "execute": execute,
        "db_jobs": len(db_ids),
        "minio_prefixes": len(minio_ids),
        "orphans": len(orphans),
        "sample_orphans": orphans[:10],
    }
    if not execute:
        return out
    # Destructive path -- guard against a partial DB read making everything
    # look orphaned (which would wipe the whole bucket).
    try:
        db_count = await state.store.count_jobs()
    except Exception:
        db_count = -1
    if len(db_ids) == 0 or (
        db_count >= 0 and abs(len(db_ids) - db_count) > max(50, db_count // 20)
    ):
        out["error"] = (
            f"aborted: DB id read looks incomplete (ids={len(db_ids)} "
            f"count={db_count}); refusing to delete to avoid nuking live data"
        )
        return out
    targets = orphans[:limit] if limit > 0 else orphans
    freed_objects = 0
    freed_bytes = 0
    deleted = 0
    for jid in targets:
        try:
            r = await objstore.delete_prefix(jid)
            freed_objects += int(r.get("objects") or 0)
            freed_bytes += int(r.get("bytes") or 0)
            deleted += 1
        except Exception:
            pass
    out.update(
        {
            "deleted_prefixes": deleted,
            "freed_objects": freed_objects,
            "freed_bytes": freed_bytes,
            "remaining_orphans": len(orphans) - deleted,
        }
    )
    return out


async def _fleet_has_spare_capacity() -> bool:
    """True if a worker-dispatched (fetch) job can be placed right now -- THIS
    hub has a free lane, or (failing that) a peer hub does.

    Used by the at-capacity gate in ``create_job`` to 503 BEFORE issuing a
    job_id, rather than persisting a phantom ``queued`` row that just waits
    behind the fleet's long-running (legitimate, slow) video downloads. Mirrors
    the dispatch's own pick_worker + cross-hub logic.

    Grace policy: if the local registry is momentarily EMPTY (the hub-restart
    reconnect window) wait briefly for workers to re-announce; but if workers
    ARE connected and simply all-busy, that's genuinely full -> reject fast
    (don't hold the request behind a 20-minute download)."""
    reg = state.registry
    if reg is None:
        return False
    if reg.pick_worker() is not None:
        return True
    # Empty registry => probably the reconnect window; give it a short grace.
    if not reg.alive_workers():
        deadline = time.monotonic() + min(JOB_DISPATCH_GRACE_S, 5.0)
        while time.monotonic() < deadline:
            await asyncio.sleep(_JOB_DISPATCH_POLL_S)
            if reg.pick_worker() is not None:
                return True
    # Locally full -> only "has capacity" if a peer hub has a free lane (the
    # dispatch below would cross-hub forward to it).
    try:
        if state.hubs is not None and await _peer_hub_with_spare_capacity():
            return True
    except Exception:
        pass
    return False


@router.post("/jobs", response_model=JobInfo)
async def create_job(req: JobRequest, request: Request) -> JobInfo:
    if not req.url:
        raise HTTPException(400, "url is required")
    # SSRF guard: refuse loopback / RFC1918 / link-local (incl. cloud
    # metadata) / multicast hosts up front, before we hand the URL to
    # a worker Chrome. Bypass via env PAPRIKA_ALLOW_PRIVATE_URLS=1.
    # rerun mode gets the same check on req.url even though the
    # script may navigate elsewhere -- the initial nav is still us
    # dispatching, and an attacker who could pass an inline-code
    # script could just put http://10.0.0.5/ in page.goto() anyway,
    # so the URL check is just operator courtesy. The deeper defense
    # is the worker-side iptables egress firewall.
    from server.hub.url_safety import assert_public_url
    assert_public_url(req.url)
    assert state.store is not None and state.registry is not None

    # Same-URL dedup (operator policy 2026-06-06): if the EXACT same URL is
    # already queued or running as a fetch, reject this submission with a 409.
    # The top source of duplicate lanes was clients re-submitting a slow/long
    # fetch (e.g. a 20-min video download) while the first is still in flight --
    # two lanes then burn on the same work. The DB-side url LIKE narrows to the
    # 0-2 candidate rows; the exact `j.url == req.url` filter is the real test.
    # Scope: fetch only (codegen-loop/rerun differ by goal/script even on the
    # same URL; attach_to_job is intentionally tied to another job).
    if (req.options.mode or "fetch") == "fetch" and not req.options.attach_to_job:
        try:
            _active, _ = await state.store.list_job_infos(
                status=["queued", "running"], url_substr=req.url, limit=100
            )
        except Exception:
            _active = []
        _dup = next(
            (
                j for j in _active
                if j.url == req.url
                and (j.options.mode if j.options else "fetch") == "fetch"
            ),
            None,
        )
        if _dup is not None:
            _st = _dup.status.value if hasattr(_dup.status, "value") else str(_dup.status)
            # No job_id is issued for the duplicate -- we reject BEFORE creating
            # one, and (per operator policy) the error must not carry a job_id at
            # all (not even the existing job's), to match the at-capacity 503.
            raise HTTPException(
                409,
                f"a fetch job for this URL is already {_st}; "
                f"not creating a duplicate (retry after it finishes)",
            )

    # At-capacity gate (operator policy 2026-06-06): when the fleet is full,
    # REJECT worker-dispatched (fetch) jobs up front with a 503 -- do NOT issue
    # a job_id or persist a queued row. The fleet's lanes are routinely tied up
    # by legitimate but SLOW video downloads (yt-dlp, 100MB+ at ~70KiB/s ≈ 20
    # min), so a phantom queued job would just wait behind them. A clean "full,
    # retry" error lets the client back off instead. Skipped for:
    #   * codegen-loop / rerun -- hub-orchestrated (GPU-gated, no worker lane);
    #   * attach_to_job (pinned) -- targets one specific worker regardless of
    #     its in_flight, so the fleet-wide capacity check doesn't apply.
    if (
        (req.options.mode or "fetch") not in ("codegen-loop", "rerun")
        and not req.options.attach_to_job
        and not await _fleet_has_spare_capacity()
    ):
        raise HTTPException(
            503, "fleet at capacity (all lanes busy); retry with backoff"
        )

    # download_video resolution (operator design 2026-06-10): an explicit
    # opts value (True/False) wins; the default None falls back to the target
    # host's HostRecord.download_video flag (default False). Stops the
    # indiscriminate download_video=True that bumped every fetch/escalation to
    # a 1h lane-hogging timeout on non-video pages -- a fetch now pulls video
    # only when asked OR the host is a registered video host. The auto-escalator
    # inherits this resolved value via model_copy.
    if req.options.download_video is None:
        _dv = False
        try:
            from urllib.parse import urlparse as _urlparse
            _dvh = _normalise_host(_urlparse(req.url).hostname or "")
            _dvrec = state.hosts.get(_dvh) if (_dvh and state.hosts is not None) else None
            _dv = bool(getattr(_dvrec, "download_video", False)) if _dvrec else False
        except Exception:
            _dv = False
        req.options.download_video = _dv
        if _dv and not req.options.capture_assets:
            req.options.capture_assets = True

    # v2 Phase 5: HostKnowledge consultation.
    # If we have learned knowledge for this URL's host, apply hints
    # before the job is dispatched. Today this just tweaks JobOptions
    # (popup_policy from navigation_hints); future phases will inject
    # barrier strategies and content-extraction tool selection.
    # The consultation log goes into the job log so operators can see
    # what knowledge was applied.
    _hk_consultation = _consult_host_knowledge(req.url, req.options)

    # Phase 2b: a scoped (enforce, non-admin) caller may only launch a job with
    # a Chrome profile they own or that is shared. Fail fast with 403 BEFORE
    # creating the job row or grabbing a worker — the API-layer guard against
    # borrowing another tenant's login state via ``options.use_profile``.
    # off / optional / admin are unaffected (should_scope=False). Covers every
    # mode (the codegen-loop / rerun branches below run after this).
    _use_prof = (req.options.use_profile or "").strip() if req.options else ""
    if _use_prof:
        from server.hub.auth import owner_of as _owner_of, should_scope as _should_scope
        _p = getattr(getattr(request, "state", None), "principal", None)
        if _should_scope(_p):
            from server.hub.routes.profiles import _shared_meta
            _pm = await _shared_meta(_use_prof)
            if _pm is not None and not (
                bool(_pm.get("shared", True))
                or str(_pm.get("owner_id") or "default") == _owner_of(request)
            ):
                raise HTTPException(
                    403,
                    f"use_profile: profile '{_use_prof}' is not available to "
                    "this account",
                )

    job_id = uuid.uuid4().hex[:12]
    from server.hub.auth import owner_of
    info = JobInfo(
        job_id=job_id,
        status=JobStatus.queued,
        url=req.url,
        options=req.options,
        created_at=datetime.utcnow(),
        progress=JobProgress(phase="queued"),
        owner_id=owner_of(request),
    )
    await state.store.save_job_info(info)

    # state-model v1.1: queued-timeout guard. Dispatch is normally
    # immediate (codegen/rerun create_task; fetch dispatches inline), so
    # this almost always no-ops -- but if a job is still `queued` after
    # the window (dispatch task died silently, or no worker/lane ever
    # picked it up), fail it as closed·timed_out instead of leaving it
    # stuck queued. Fires once; harmless once the status moved on.
    _spawn_queued_timeout_guard(job_id)

    # Persist the consultation summary to the job log for operator
    # visibility. ``append_log_line`` rpushes to the Redis list (and the
    # subscribe stream relays via the pubsub channel). Best-effort;
    # never blocks job dispatch.
    if _hk_consultation:
        try:
            for ln in _hk_consultation:
                await state.store.append_log_line(job_id, ln)
                try:
                    await state.store.publish_log(job_id, ln)
                except Exception:
                    pass
        except Exception:
            pass

    # v2 Phase 7c: pre-flight plugin auto-invocation.
    # If HostKnowledge declared a suggested_tool for a present barrier
    # (e.g. paprika-flare for cloudflare_challenge), run it now and
    # merge cookies into HostRecord BEFORE the worker dispatch reads
    # rec.cookies below. Best-effort: failures are logged, never raise.
    try:
        _preflight_lines = await _preflight_cf_plugin(req.url, job_id)
    except Exception as e:
        _preflight_lines = [
            f"==> pre-flight plugin crashed unexpectedly "
            f"({type(e).__name__}: {str(e)[:200]}); continuing without"
        ]
    if _preflight_lines:
        try:
            for ln in _preflight_lines:
                await state.store.append_log_line(job_id, ln)
                try:
                    await state.store.publish_log(job_id, ln)
                except Exception:
                    pass
        except Exception:
            pass

    (get_storage_dir() / job_id).mkdir(parents=True, exist_ok=True)
    (get_storage_dir() / job_id / "assets").mkdir(parents=True, exist_ok=True)

    # NOTE: the v1 "vision-agent" mode (CogAgent-driven pixel-space
    # action loop) was removed in the v2 cleanup. Pydantic now rejects
    # ``mode="vision-agent"`` at the protocol layer (see JobOptions),
    # so we never reach this point with that value.

    # ---- codegen-loop mode short-circuits the worker pipeline ----
    # The hub runs the LLM-generate -> sandbox-execute -> retry loop
    # itself; the generated script then opens its OWN /sessions/*
    # against this hub from inside the runner container, which routes
    # to a real worker. We don't dispatch a worker job here.
    if (req.options.mode or "fetch") == "codegen-loop":
        if not (req.options.goal or "").strip():
            raise HTTPException(400, "codegen-loop mode requires 'goal'")
        task = asyncio.create_task(
            _run_codegen_loop_job(request, info),
        )
        state.local_tasks[job_id] = task
        # novnc_url stays None at this point (lane not bound yet), so
        # the proxy rewrite is a no-op. Kept here for symmetry with the
        # other return paths so a future change that pre-binds lanes
        # doesn't accidentally surface a worker-direct URL.
        return _proxy_info(info, request)

    # ---- rerun mode: same pipeline as codegen-loop minus the LLM ----
    # Source: req.options.rerun_from (job/attempt ref on disk) or
    # req.options.code (inline). Resolved up-front so a 400 fires
    # synchronously if the source is missing/invalid.
    if (req.options.mode or "fetch") == "rerun":
        try:
            script_code, source_label, source_jid = resolve_rerun_source(
                get_storage_dir(),
                req.options.rerun_from,
                req.options.code,
            )
        except ValueError as e:
            raise HTTPException(400, f"rerun: {e}") from e
        # If we're rerunning from an existing job, inherit its walker
        # state (and any sibling per-parent state) so pap.walk() picks
        # up where the source left off rather than re-crawling from 0.
        # This is the kernel of the "▶ resume" UX: pause = cancel
        # (state stays on disk), resume = mode=rerun pointing at the
        # paused job's last attempt (state gets copied into the new
        # job's state dir before the sandbox starts).
        copied = 0
        if source_jid:
            try:
                copied = _copy_session_state_dir(source_jid, job_id)
            except Exception:
                copied = 0
        task = asyncio.create_task(
            _run_rerun_loop_job(info, script_code, source_label, inherited_state_files=copied),
        )
        state.local_tasks[job_id] = task
        return _proxy_info(info, request)

    # ---- resolve attach_to_job (Phase 4) ----
    # attach_to_job is best-effort: if the referenced job is gone (deleted,
    # expired, or just stale because the caller cached the id from a
    # previous session), or never used a lane pool, we *don't* fail the
    # request -- we fall back to plain "pick a free active worker" and
    # log the reason. Callers can pass attach_to_job optimistically
    # without having to first check whether the id still exists.
    lane_hint: int | None = None
    pinned_worker = None  # if attach_to_job: route to the same worker
    attach_fallback_reason: str | None = None
    if req.options.attach_to_job:
        prev = await state.store.get_job_info(req.options.attach_to_job)
        if prev is None:
            attach_fallback_reason = f"attach_to_job '{req.options.attach_to_job}' not found"
        elif prev.lane_idx is None:
            attach_fallback_reason = (
                f"attach_to_job '{req.options.attach_to_job}' had no lane_idx "
                f"(prior run did not use a lane pool)"
            )
        else:
            lane_hint = prev.lane_idx
            # Try to pin to the same worker so that lane exists on it. If
            # the worker has disconnected since then, fall back too.
            if prev.worker_id and prev.worker_id in state.registry.connections:
                pinned_worker = state.registry.connections[prev.worker_id]
            else:
                attach_fallback_reason = (
                    f"attach_to_job worker '{prev.worker_id}' no longer "
                    f"connected; routing as a fresh job"
                )
                lane_hint = None  # forget the hint, let the scheduler pick
        if attach_fallback_reason is not None:
            log.info(f"[hub] job {job_id}: {attach_fallback_reason}")
            # Record on the job so the operator can see why it didn't attach.
            info.progress.last_log = attach_fallback_reason
            await state.store.save_job_info(info)

    # Hub-managed min-size filter. Fill it in from the operator's
    # Settings default ONLY when the client omitted the field entirely
    # (e.g. a bare API call). Any value the client set explicitly --
    # including 0 ("capture everything") -- wins, so the Submit form is
    # authoritative (WYSIWYG) and can't be silently overridden.
    if state.settings is not None and "min_asset_size_bytes" not in req.options.model_fields_set:
        # Client didn't send the field at all -> use the operator's
        # Settings default. An explicit client value (including 0 =
        # "no filter") is left untouched so WYSIWYG holds for the form.
        try:
            req.options.min_asset_size_bytes = int(
                state.settings.get("min_asset_size_bytes", 0) or 0
            )
        except Exception:
            pass

    # Hub-managed Fetch defaults. For each fetch_* knob in Settings,
    # overlay onto JobOptions UNLESS the client explicitly set the
    # corresponding field (Pydantic's model_fields_set). That way an
    # operator can set "default scroll = True" once in Settings and
    # have every Fetch submit pick it up, but a one-off API caller
    # passing scroll=False explicitly still gets their value through.
    if state.settings is not None:
        try:
            explicit = req.options.model_fields_set
        except Exception:
            explicit = set()
        # Map Settings key -> JobOptions field name.
        _FETCH_DEFAULT_MAP = {
            "fetch_wait_seconds": "wait_seconds",
            "fetch_settle_seconds": "settle_seconds",
            "fetch_idle_seconds": "idle_seconds",
            "fetch_max_wait_seconds": "max_wait_seconds",
            "fetch_scroll": "scroll",
            "fetch_scroll_step": "scroll_step",
            "fetch_scroll_max": "scroll_max",
            "fetch_scroll_early_after": "scroll_early_after",
            "fetch_post_click_seconds": "post_click_seconds",
        }
        for setting_key, opt_field in _FETCH_DEFAULT_MAP.items():
            if opt_field in explicit:
                continue
            try:
                v = state.settings.get(setting_key)
                if v is None:
                    continue
                setattr(req.options, opt_field, v)
            except Exception:
                pass

    # Per-host cookie auto-injection + popup_policy lookup.
    #
    # Mirrors the session path: if the host of ``url`` has a record
    # in the registry, attach its cookies to the assign-job so the
    # worker CDP-installs them before navigation. The same host is
    # also echoed back as ``save_cookies_host`` so the worker dumps
    # the post-fetch jar back to /hosts/{host}, capturing any
    # session cookies the page set (and refreshing the existing
    # record's ``updated_at``).
    #
    # popup_policy is looked up for ANY worker-dispatched mode (fetch
    # OR vision-agent) because both run the tab-killer at the lane
    # boundary and need to know whether to follow popups (some video
    # sites open videos in new tabs etc.). Codegen-loop / rerun go
    # through /sessions instead and get it from that path.
    #
    # cookies + save_cookies_host stay fetch-only -- vision-agent
    # doesn't dump cookies on exit (no clean "fetch done" boundary
    # to hang the dump callback on; the loop just stops).
    auto_cookies: list[dict] | None = None
    auto_host: str | None = None
    auto_popup_policy: str = "kill"
    if state.hosts is not None and req.options.mode == "fetch":
        try:
            from urllib.parse import urlparse as _urlparse

            host_raw = _urlparse(req.url).hostname or ""
            auto_host = _normalise_host(host_raw)
            if auto_host:
                # Auto re-login gate: if this host has a login recipe
                # and its session is stale (last login older than the
                # configured TTL, or never), refresh it BEFORE reading
                # the cookies below. Keeps a login-gated fetch
                # (market.laxd.com etc.) working past the
                # session-cookie expiry without manual re-login. Only
                # for fetch mode -- the cookies are fetch-only too.
                # Best-effort: a failed re-login just proceeds with the
                # current (possibly stale) cookies.
                if req.options.mode == "fetch" and state.hosts.is_login_stale(auto_host):
                    try:
                        relog = await _ensure_host_login(auto_host)
                        log.info(
                            f"[hub] job {job_id}: pre-fetch auto-login "
                            f"{auto_host} -> {relog.get('relogin')}",
                        )
                    except Exception as e:
                        log.info(
                            f"[hub] job {job_id}: pre-fetch auto-login "
                            f"{auto_host} crashed: {type(e).__name__}: {e}",
                        )
                rec = state.hosts.get(auto_host)
                if rec:
                    auto_popup_policy = rec.popup_policy or "kill"
                    # Phase 2b: hosts are keyed by hostname (one record per
                    # host). Under enforce, a record pushed by another tenant
                    # is invisible to this job — don't inject its cookies AND
                    # don't let this job's post-fetch dump clobber it (the
                    # save_cookies_host below keys off ``auto_host``). Nulling
                    # auto_host makes the job behave as if no record existed.
                    # No-op under off / optional / admin (owner_can_use=True).
                    from server.hub.auth import owner_can_use
                    _host_usable = owner_can_use(
                        getattr(rec, "owner_id", "default"),
                        job_owner=info.owner_id,
                        shared=getattr(rec, "shared", True),
                    )
                    if not _host_usable:
                        auto_host = None
                    elif rec.cookies and req.options.mode == "fetch":
                        auto_cookies = cookies_for_cdp(rec.cookies)
                    # Pick the best-matching pre-baked recipe (Phase 1)
                    # and stamp it onto JobOptions so the worker can
                    # run it right after navigation. Only for Fetch
                    # mode -- vision-agent / codegen-loop have their
                    # own LLM-driven flow and don't need the recipe.
                    if _host_usable and (
                        req.options.mode == "fetch"
                        and not req.options.fetch_recipe
                        and getattr(req.options, "fetch_strategy", "recipe") != "normal"
                    ):
                        try:
                            picked = rec.pick_recipe(req.url)
                            if picked is not None:
                                req.options.fetch_recipe = picked.to_json()
                                log.info(
                                    f"[hub] job {job_id}: matched "
                                    f"fetch_recipe pattern="
                                    f"{picked.pattern!r} for "
                                    f"host={auto_host!r}"
                                )
                        except Exception as e:
                            log.info(
                                f"[hub] job {job_id}: fetch_recipe "
                                f"lookup crashed "
                                f"({type(e).__name__}: {e}); "
                                f"continuing without recipe"
                            )
        except Exception:
            auto_cookies = None
            auto_host = None
            auto_popup_policy = "kill"

    # ---- GPU concurrency gate (codegen-loop only) ----
    # ぱっぷす環境では Qwen-VL を自前 GPU (RTX 6000 Pro Max-Q) で走らせるが
    # 1 枚を 24 ライン で奪い合うので、page.agent / observe / ask を呼び得る
    # codegen-loop ジョブが多数並ぶと GPU 飽和で全体が詰まる。
    # PAPRIKA_CODEGEN_LOOP_CONCURRENCY で同時実行数を絞り、上限に到達したら
    # grace window で他ジョブの完了を待つ。Pinned (attach_to_job) は対象外。
    _is_codegen_loop = (req.options.mode or "fetch") == "codegen-loop"
    if _is_codegen_loop and pinned_worker is None:
        from server.hub._gpu_gate import (
            codegen_loop_at_capacity,
            codegen_loop_in_flight,
            get_codegen_loop_limit,
        )
        if codegen_loop_at_capacity():
            _gpu_deadline = time.monotonic() + max(JOB_DISPATCH_GRACE_S, 5.0)
            _gpu_waited = False
            while codegen_loop_at_capacity() and time.monotonic() < _gpu_deadline:
                await asyncio.sleep(_JOB_DISPATCH_POLL_S)
                _gpu_waited = True
            if codegen_loop_at_capacity():
                log.info(
                    f"[hub] job {job_id}: codegen-loop GPU gate full "
                    f"({codegen_loop_in_flight()}/{get_codegen_loop_limit()}); "
                    f"refusing dispatch",
                )
                # Mark failed with a clear reason so the admin UI / SDK
                # can see "GPU gate" not "fleet at capacity". The job
                # never reached a worker -- no recovery work needed.
                info.status = JobStatus.failed
                info.error = (
                    f"GPU gate full ({codegen_loop_in_flight()}/"
                    f"{get_codegen_loop_limit()} codegen-loop already "
                    f"running); retry with backoff"
                )
                info.progress.phase = "failed"
                info.completed_at = datetime.utcnow()
                try:
                    await state.store.save_job_info(info)
                except Exception:
                    pass
                raise HTTPException(
                    503,
                    info.error,
                )
            if _gpu_waited:
                log.info(
                    f"[hub] job {job_id}: codegen-loop GPU gate freed during "
                    f"grace ({codegen_loop_in_flight()}/{get_codegen_loop_limit()})",
                )

    # ---- dispatch in priority order ----
    # 1) WebSocket-connected worker (pinned or any free).
    #
    # When not pinned and no worker currently has free capacity, poll
    # for up to JOB_DISPATCH_GRACE_S before giving up. This smooths
    # over the hub-restart reconnect window: right after a `docker
    # compose restart hub`, the WS registry is momentarily empty and a
    # job submitted in that gap used to instantly 503 ("fleet at
    # capacity") even though workers reconnect within a couple seconds
    # (job d435107ed59b hit exactly this). Pinned jobs (attach_to_job)
    # skip the grace loop -- they need one specific worker and the
    # assign below queues onto it regardless of its in_flight count.
    worker = pinned_worker
    if worker is None:
        # Reserve a pending_assigns slot at pick time so a concurrent picker
        # (POST inline OR redrive OR post_assign_ack_guard) running on an
        # await boundary below sees the worker as full. EVERY non-assign exit
        # path (including the 503 fall-through after this block) must
        # release_pending_assign. assign() itself idempotently re-adds the
        # same job_id (set semantics).
        worker = state.registry.pick_worker(reserve_for_job=job_id)
        if worker is None and JOB_DISPATCH_GRACE_S > 0:
            _grace_deadline = time.monotonic() + JOB_DISPATCH_GRACE_S
            _waited = False
            while worker is None and time.monotonic() < _grace_deadline:
                await asyncio.sleep(_JOB_DISPATCH_POLL_S)
                worker = state.registry.pick_worker(reserve_for_job=job_id)
                _waited = True
            if _waited and worker is not None:
                log.info(
                    f"[hub] job {job_id}: worker became available "
                    f"during dispatch grace window "
                    f"({worker.worker_id})",
                )
    if worker is not None:
        # Prefer the URL the worker actually dialled when it connected to
        # us (recorded on the WS handshake). Falls back to the operator
        # config / incoming HTTP request only when the worker connected
        # without a Host header.
        base = worker.public_base_url or _hub_base_url(request)

        # Allocate a session_id for this fetch so the admin UI can
        # inspect the running browser via /sessions/{sid}/* while the
        # fetch is in flight. The worker registers a read-only
        # SessionState under this id when the browser is attached and
        # tears it down right before browser.stop() (see fetch()'s
        # on_browser_ready / on_browser_closing callbacks). The
        # SessionInfo is removed here in the hub on WorkerJobComplete
        # / WorkerJobFailed; the id stays on JobInfo as a historical
        # reference but stops resolving once removed.
        fetch_sid: str | None = None
        if (req.options.mode or "fetch") == "fetch" and state.sessions is not None:
            fetch_sid = new_session_id()
            try:
                # keep_session: operator stays attached via noVNC and
                # the API. The noVNC proxy taps client-side RFB events
                # (mouse / key / clipboard) and touches the session's
                # last_active_at so the 60-second idle window is
                # genuinely "no operator activity for 60 s" rather
                # than "no API call". 60 s default chosen so a
                # forgotten / abandoned session doesn't hog a lane;
                # operator can override per-detach via
                # ``await sess.detach(idle_ttl_s=...)`` if they want
                # a longer leash. 24h absolute is the hard backstop.
                #
                # State machine after crawl ends:
                #   keepalive --(RFB activity)--> running
                #   running --(QUIET_S no RFB)--> keepalive
                #   keepalive --(idle_ttl_s no RFB)--> IDLE (= reaped)
                keep_session_req = bool(getattr(req.options, "keep_session", False))
                _idle_ttl_s = 60 if keep_session_req else 600
                _abs_ttl_s = 24 * 3600 if keep_session_req else 3600
                sinfo = SessionInfo(
                    session_id=fetch_sid,
                    worker_id=worker.worker_id,
                    initial_url=req.url,
                    idle_ttl_s=_idle_ttl_s,
                    absolute_ttl_s=_abs_ttl_s,
                    job_id=job_id,
                )
                sinfo.state = "fetch_running"
                state.sessions.add(sinfo)
                info.session_id = fetch_sid
            except Exception as e:
                log.info(
                    f"[hub] job {job_id}: could not register fetch "
                    f"session: {type(e).__name__}: {e}",
                )
                fetch_sid = None

        # Resolve ``options.use_profile`` (or fall back to the
        # operator-set default profile) to a profile_url the worker
        # can GET. We pass the URL (not just the name) so the worker
        # doesn't need to know hub-side path conventions and so the
        # tarball can in principle live on a different host later.
        # Reject explicit names that don't exist with a synchronous
        # 400; a missing default is silent (the job just runs with
        # the lane's stock profile, same as before defaults existed).
        _profile_url: str | None = None
        _profile_etag: str | None = None
        _profile_name = (req.options.use_profile or "").strip() or None
        _explicit = _profile_name is not None
        # Profiles are shared across hubs (MariaDB metadata + MinIO bytes), so
        # resolve the default + existence/etag from the shared view -- any hub
        # can dispatch a job using a profile uploaded on any other hub. The
        # worker fetches the tarball from THIS hub's /profiles/{name}, which
        # serves it from local disk or pulls it from MinIO on a cache miss.
        from server.hub.routes.profiles import _shared_default, _shared_meta
        if _profile_name is None:
            _profile_name = await _shared_default()
        if _profile_name:
            _pmeta = await _shared_meta(_profile_name)
            if _pmeta is None:
                if _explicit:
                    raise HTTPException(
                        400,
                        f"use_profile: profile '{_profile_name}' not "
                        "found. Upload it first via POST /profiles/{name} "
                        "(paprika-client upload-profile).",
                    )
                # default went stale between read + recheck -- treat as
                # "no default" rather than failing the dispatch.
                _profile_name = None
            else:
                # Phase 2b: tenant isolation on the dispatch path. Inject the
                # profile only when the job's owner may use it (shared, or
                # same tenant). The explicit-profile case is already 403'd up
                # front; this is the belt-and-suspenders for the AMBIENT
                # default-profile fallback — a private default never rides onto
                # another tenant's job. No-op under off/optional (owner_can_use
                # returns True), so ambient behaviour is unchanged.
                from server.hub.auth import owner_can_use
                if owner_can_use(
                    _pmeta.get("owner_id"),
                    job_owner=info.owner_id,
                    shared=bool(_pmeta.get("shared", True)),
                ):
                    _profile_url = f"{base}/profiles/{_profile_name}"
                    # Etag lets the worker skip the download when its cache
                    # already has this exact version (typical after sync).
                    _profile_etag = _pmeta.get("etag") or None
                else:
                    _profile_name = None  # run with the lane's stock profile

        # Asset URL blacklist (V): pull operator-managed list from Settings
        # and stamp onto every assignment so an admin UI edit takes effect
        # on the next dispatched job. Stored as newline-separated string;
        # split + trim + drop blanks here so the worker just iterates.
        _bl_raw = ""
        if state.settings is not None:
            try:
                _bl_raw = (state.settings.get("asset_url_blacklist", "") or "").strip()
            except Exception:
                _bl_raw = ""
        _asset_url_blacklist = [
            line.strip()
            for line in _bl_raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assign = HubAssignJob(
            job_id=job_id,
            url=req.url,
            options=req.options,
            asset_upload_base=_asset_upload_url(base, job_id),
            lane_hint=lane_hint,
            cookies=auto_cookies,
            save_cookies_host=auto_host if req.options.mode == "fetch" else None,
            session_id=fetch_sid,
            popup_policy=auto_popup_policy,
            profile_url=_profile_url,
            profile_name=_profile_name,
            profile_etag=_profile_etag,
            asset_url_blacklist=_asset_url_blacklist,
        )
        # Bump the registry's last_used_at so the Hosts tab reflects
        # that the cookies actually rode along on a real job.
        if auto_cookies and auto_host and state.hosts is not None:
            try:
                state.hosts.touch_used(auto_host)
            except Exception:
                pass
        # CAS-claim the row from queued -> running BEFORE sending the WS
        # assign. This makes POST symmetric with redrive (which already uses
        # claim_queued_job) and closes the race that forced redrive's
        # _MIN_AGE_S to 90s:
        #   POST sends WS, then blindly upserts queued+worker_id; if redrive
        #   had simultaneously CAS'd to running+W2, POST's blind save would
        #   overwrite running+W2 back to queued+W1 -- both workers then run
        #   the same job. The CAS here pins ownership at the DB layer, so a
        #   concurrent redrive sees worker_id NOT NULL (or status running)
        #   and skips. _MIN_AGE_S can now be lowered safely.
        _started_at = datetime.utcnow()
        try:
            _claim_ok = await state.store.claim_queued_job(
                job_id, worker.worker_id, _started_at,
            )
        except Exception:
            log.warning(f"!! claim_queued_job({job_id}) raised", exc_info=True)
            _claim_ok = False
        if not _claim_ok:
            # Another path (redrive on this or a peer hub) already moved the
            # row off queued. Release our reserved slot and fall through to
            # the 503 / cross-hub path -- nothing to do here, the other path
            # will run the job. The session we eagerly registered (if any)
            # also rolls back so it doesn't pin to a worker that never got
            # the assign.
            try:
                state.registry.release_pending_assign(worker.worker_id, job_id)
            except Exception:
                pass
            if fetch_sid:
                try:
                    state.sessions.remove(fetch_sid)
                except Exception:
                    pass
                info.session_id = None
            log.info(
                f"[hub] job {job_id}: claim_queued_job lost the CAS "
                f"(redrive / peer hub already claimed); deferring to it"
            )
            # Re-read the now-running row so the response reflects who took
            # it. If even that fails, return the local view; the client only
            # needs the job_id.
            try:
                _now = await state.store.get_job_info(job_id)
                if _now is not None:
                    info = _now
            except Exception:
                pass
            return _proxy_info(info, request)
        ok = await state.registry.assign(worker, assign)
        if ok:
            # Record which worker + (if known) the noVNC URL so clients can
            # watch the job live. Status was already flipped to running by
            # the CAS above; mirror that locally so the upsert below doesn't
            # downgrade it.
            info.status = JobStatus.running
            info.worker_id = worker.worker_id
            info.started_at = _started_at
            novnc = worker.capabilities.novnc_url
            if novnc:
                sep = "&" if "?" in novnc else "?"
                info.novnc_url = (
                    f"{novnc}{sep}autoconnect=1&resize=scale&reconnect=1"
                    if "autoconnect" not in novnc
                    else novnc
                )
            await state.store.save_job_info(info)
            # GPU gate: register the codegen-loop job so subsequent
            # submissions see the right in-flight count. Unregister
            # happens in workers.py when WorkerJobComplete / Failed lands.
            if _is_codegen_loop:
                try:
                    from server.hub._gpu_gate import register_codegen_loop
                    register_codegen_loop(job_id)
                except Exception:
                    pass
            # ACK watchdog: the WS assign returning True only proves the
            # message was ENQUEUED, not that the worker actually picked the
            # job up. If WorkerJobAccepted never lands (worker hung, accept
            # handler crashed, deploy churn between assign + ack), the row
            # sits queued+worker_id forever -- pre-fix this drove 80%+ of
            # "queue timeout" failures (incident 2026-06-15). Spawn a guard
            # that flips back to "stranded" after _POST_ASSIGN_ACK_TIMEOUT_S
            # and immediately re-dispatches onto another free lane. Periodic
            # redrive reclaim is the safety-net for guards killed by a hub
            # restart before they could fire.
            try:
                import asyncio as _asyncio
                from server.hub._redrive import post_assign_ack_guard
                _asyncio.create_task(
                    post_assign_ack_guard(job_id, worker.worker_id)
                )
            except Exception:
                pass
            log.info(
                f"[hub] job {job_id} → worker {worker.worker_id} "
                f"(in_flight={worker.in_flight}/"
                f"{worker.capabilities.max_concurrent})  "
                f"novnc={info.novnc_url or '(none)'}",
            )
            return _proxy_info(info, request)
        # If send failed, fall through to the 503 path below. Roll back
        # the SessionInfo we eagerly registered so it doesn't stick
        # around pointing at a worker that never accepted the job.
        if fetch_sid:
            try:
                state.sessions.remove(fetch_sid)
            except Exception:
                pass
            info.session_id = None
        # We CAS-claimed the row to running before the assign; the WS send
        # then failed (worker WS dropped between pick + send). Release the
        # claim back to queued so the redrive (or cross-hub forward below)
        # can re-dispatch it. Without this the row would sit running+W
        # until the post_assign_ack_guard expires (30s).
        try:
            await state.store.release_claimed_job(job_id)
            info.status = JobStatus.queued
            info.worker_id = None
            info.started_at = None
        except Exception:
            log.warning(
                f"!! release_claimed_job({job_id}) failed after assign send error",
                exc_info=True,
            )
        try:
            state.registry.release_pending_assign(worker.worker_id, job_id)
        except Exception:
            pass
        log.info(f"!! failed to send job to worker {worker.worker_id}")

    # P1 cross-hub dispatch: local dispatch did NOT place the job (no free
    # local worker, or the local send failed). Before rejecting, forward the
    # whole /jobs POST to a peer hub that has spare active capacity and return
    # its result -- so the fleet's lanes are used fleet-wide instead of 503-ing
    # while peers sit idle. ``info`` is not persisted until the 503 / success
    # paths below, so there's no orphan to clean up. _FWD_MARK makes the
    # forwarded hop dispatch locally only (one hop, no inter-hub bounce loop).
    from server.hub.routes.sessions import _FWD_MARK, _proxy_request_to_hub
    if not request.headers.get(_FWD_MARK) and state.hubs is not None:
        _peer = await _peer_hub_with_spare_capacity()
        if _peer:
            try:
                _resp = await _proxy_request_to_hub(_peer, request, 60.0)
            except Exception:
                _resp = None
            if _resp is not None and getattr(_resp, "status_code", 503) != 503:
                log.info(
                    f"[hub] job {job_id}: no free local worker -> forwarded to "
                    f"peer hub {_peer} (cross-hub dispatch)"
                )
                return _resp
            # peer also full / unreachable -> fall through to the local 503.

    # 2) No worker available -- reject with 503. The hub used to run an
    # in-process nodriver fallback here, but the hub container has no
    # Chrome installed, so that path failed with FileNotFoundError under
    # load (load test 2026-05: 48/100 jobs failed once 52 lanes were
    # saturated). Clients should retry with backoff; the operator UI
    # surfaces fleet capacity. Mark the JobInfo as failed so the admin
    # history shows the rejection rather than leaving a phantom queued
    # entry.
    # Reached only in the rare race where capacity existed at the at-capacity
    # gate (top of create_job) but vanished before we could place the job (the
    # picked worker filled up / the send failed AND no peer had room). Honour
    # the "no phantom job_id when full" policy: drop the row + dir we eagerly
    # created so the client just sees a clean 503 with nothing left behind
    # (instead of a lingering failed "fleet at capacity" job in the admin list).
    try:
        await state.store.delete_job(job_id)
    except Exception:
        pass
    try:
        import shutil
        shutil.rmtree(get_storage_dir() / job_id, ignore_errors=True)
    except Exception:
        pass
    raise HTTPException(
        503,
        "no worker available (fleet at capacity); retry with backoff",
    )

