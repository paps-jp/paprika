"""Job-orchestrator entry points + tightly-coupled helpers.

Lives in its own module (not server/hub/app.py) so the route layer
(routes/jobs.py:create_job) can import the orchestrators directly,
without going through app.py's lazy-import bridge -- and so the
distillation / state-copy helpers stay co-located with the loop they
serve rather than diluting app.py with 800 lines of orchestration.

What's here:

* ``_run_codegen_loop_job``   the codegen-loop orchestrator. Drives a
                              queued JobInfo through plan -> generate ->
                              sandbox -> judge -> retry on the hub,
                              persists every attempt under
                              ``data/jobs/{id}/attempts/`` and updates
                              JobInfo / JobResult so the admin UI
                              shows progress in real time. The
                              generated paprika-client scripts open
                              their OWN /sessions/* against this hub
                              (no worker dispatch from here).
* ``_run_rerun_loop_job``     the rerun orchestrator. Same skeleton
                              as codegen-loop but skips the LLM
                              generation step -- script source comes
                              from disk (a prior attempt) or the
                              request body.
* ``_final_attempt_judge_ok`` post-loop judge for the last attempt
                              (used by both orchestrators).
* ``_distill_skill_background``    LLM call: after a SUCCESS, extract
                                  a reusable skill from the attempt.
* ``_distill_convention_background`` LLM call: from a failure->success
                                     diff, extract a convention rule.
* ``_copy_session_state_dir`` rerun helper: inherit walker state +
                              any per-parent state directory from the
                              source job so a resumed crawl picks up
                              where it left off.

These are all hub-side (no worker round trip) and import ``state``
from ``server.hub._state`` directly. app.py is now route-/lifespan-
only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import Request

from core.fetcher import merge_fmp4_fragments
from server.hub._state import config, get_storage_dir, state
from server.hub.convention_llm import (
    CONVENTION_AUTO_EXTRACT_ENABLED,
    distill_convention_from_diff,
)
from server.hub.conventions import (
    render_conventions_block,
)
from server.hub.iterative_codegen import (
    resolve_rerun_source,
    run_iterative_codegen,
    run_rerun_job,
)
from server.hub.skill_llm import (
    SKILL_AUTO_EXTRACT_ENABLED,
    SKILL_RETRIEVAL_TOP_K,
    build_skill_context_block,
    distill_skill_from_job,
    pick_relevant_skills,
)
from urllib.parse import quote as _url_quote

from server.protocol import (
    AssetInfo,
    JobInfo,
    JobProgress,
    JobResult,
    JobStatus,
)
from server.runner import DONE_SENTINEL

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Codegen-loop job runner (PR-14d)
# ----------------------------------------------------------------------------


async def _run_codegen_loop_job(request: Request, info: JobInfo) -> None:
    """Drive a codegen-loop job from queued -> running -> done/failed.

    Hub-orchestrated: spawns paprika-runner containers per attempt,
    persists all attempts under data/jobs/{id}/attempts/, updates
    JobInfo + JobResult so the admin UI shows progress.

    Every log line emitted by the orchestrator is both appended to
    the job's log.txt and published over /jobs/{id}/events so the
    live-log viewer in the admin UI shows progress in real time.
    """
    assert state.store is not None
    job_id = info.job_id
    opts = info.options

    # Move queued -> running
    info.status = JobStatus.running
    info.started_at = datetime.utcnow()
    info.progress = JobProgress(phase="codegen-loop:start")
    await state.store.save_job_info(info)

    hub_for_runner = os.environ.get(
        "PAPRIKA_RUNNER_HUB_URL",
        "http://hub:8000",
    )

    # log.txt -- the same file the worker job pipeline produces, so
    # /jobs/{id}/log.txt route and the live viewer's "fetch first" path
    # both work without special-casing codegen-loop.
    log_path = get_storage_dir() / job_id / "log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "a", encoding="utf-8", buffering=1)

    # Each line: persist on disk, broadcast to live viewers, keep the
    # "last seen" snippet on JobInfo for the Recent Jobs row.
    #
    # Python 3.12+ only keeps weak references to fire-and-forget tasks
    # created by asyncio.create_task().  Without a strong reference the
    # task can be garbage-collected before it finishes, silently dropping
    # the Redis RPUSH / PUBLISH.  ``_bg_tasks`` holds each task until
    # its done-callback removes it.
    _bg_tasks: set[asyncio.Task] = set()

    def _log(line: str) -> None:
        if not isinstance(line, str):
            line = str(line)
        info.progress.last_log = line[:200]
        try:
            log_fp.write(line + "\n")
        except Exception:
            pass
        try:
            t1 = asyncio.create_task(state.store.append_log_line(job_id, line))
            _bg_tasks.add(t1)
            t1.add_done_callback(_bg_tasks.discard)
            t2 = asyncio.create_task(state.store.publish_log(job_id, line))
            _bg_tasks.add(t2)
            t2.add_done_callback(_bg_tasks.discard)
        except Exception as _log_exc:
            # Logging must not break the run.
            log.warning("_log create_task failed: %s", _log_exc)

    # Smoke-test: await a single append so we know the store works.
    try:
        await state.store.append_log_line(job_id, "__livelog_smoke_test__")
        log.info("[%s] livelog smoke-test: append_log_line OK (store=%s)",
                     job_id, type(state.store).__name__)
    except Exception as _smoke_exc:
        log.warning("[%s] livelog smoke-test FAILED: %s", job_id, _smoke_exc)

    _log(
        f"==> codegen-loop start: max_attempts={opts.max_codegen_attempts} "
        f"attempt_timeout={opts.attempt_timeout_s}s"
    )
    _log(f"==> goal: {(opts.goal or '').splitlines()[0][:160]}")
    _log(f"==> start_url: {info.url}")

    # Between codegen attempts, drop any sessions the previous runner
    # opened but didn't close (script crash / sandbox timeout SIGKILL).
    # Without this, the next attempt sees "no free lane" until the TTL
    # reaper fires minutes later -- exactly the failure mode that bit
    # job 1de3f80e6487 (attempt 1 crashed on NameError, attempt 2 got
    # HTTP 502 "no free lane" because attempt 1's lane was still held).
    async def _cleanup_orphan_sessions(jid: str) -> int:
        if state.sessions is None:
            return 0
        # Skip sessions the script explicitly detach()-ed -- those are
        # operator-managed (typically active in noVNC) and yanking
        # them at script exit defeats the purpose of detach().
        # The TTL reaper still applies, so a forgotten detach can't
        # pin a lane forever; this just stops the parent script from
        # auto-closing intentionally-kept sessions.
        orphans = [s for s in state.sessions.all() if s.job_id == jid and not s.detached]
        if not orphans:
            return 0
        closed = 0
        for sess in orphans:
            sid = sess.session_id
            sess.state = "closing"
            state.sessions.remove(sid)
            worker = state.registry.connections.get(sess.worker_id)
            if worker is None:
                closed += 1  # worker already gone, just dropped from registry
                continue
            try:
                await worker.end_session(sid, timeout=10.0)
                closed += 1
            except Exception:
                # Best-effort -- don't block the retry on slow workers.
                closed += 1
        return closed

    # Capture the final-frame screenshot from whichever lane the
    # codegen attempt was using, so the Judge LLM can SEE what the
    # agent's last action produced. Called by iterative_codegen
    # AFTER the runner exits, BEFORE _cleanup_orphan_sessions wipes
    # the session-> lane mapping. Best-effort: failures are logged
    # and the judge falls back to text-only verdict.
    #
    # Implementation notes:
    #   * Looks up sessions tagged with this job_id (= what the
    #     runner opened via cli.session()). Picks the most recently
    #     created one as a heuristic for "the last thing the agent
    #     was doing".
    #   * Resolves lane_idx + worker, calls worker.request_screenshot
    #     with a quality (max_width=1280, quality=70) that's high
    #     enough for Qwen2.5-VL to read on-page text -- the default
    #     low-fi 480/5 used by the admin-UI live tiles makes
    #     paragraph text illegible.
    #   * Writes JPEG to data_dir/job_id/attempts/{n}/final_screenshot.jpg.
    async def _capture_attempt_screenshot(jid: str, attempt_n: int) -> Path | None:
        if state.sessions is None or state.registry is None:
            return None
        sessions_for_job = [
            s for s in state.sessions.all() if s.job_id == jid and s.lane_idx is not None
        ]
        if not sessions_for_job:
            return None
        # Latest session = "most likely the one the script was on
        # when it exited". Sort by created_at desc and pick the first
        # whose worker is still alive.
        sessions_for_job.sort(
            key=lambda s: s.created_at or 0,
            reverse=True,
        )
        for sess in sessions_for_job:
            worker = state.registry.connections.get(sess.worker_id)
            if worker is None:
                continue
            try:
                reply = await worker.request_screenshot(
                    sess.lane_idx,
                    max_width=1280,
                    quality=70,
                    timeout=10.0,
                )
            except Exception:
                log.warning(
                    "codegen attempt screenshot: lane %s on %s failed",
                    sess.lane_idx,
                    sess.worker_id,
                    exc_info=True,
                )
                continue
            if getattr(reply, "error", None):
                continue
            data = getattr(reply, "jpeg_b64", None)
            if not data:
                continue
            import base64

            try:
                raw = base64.b64decode(data)
            except Exception:
                continue
            out = get_storage_dir() / jid / "attempts" / str(attempt_n) / "final_screenshot.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw)
            return out
        return None

    # Curated conventions ride along on EVERY codegen attempt as a
    # static system-prompt addendum. They're meant to be small atomic
    # rules ("always X / never Y") distilled from prior failure→
    # success diffs, so injecting all curated ones at once stays
    # well under context limits.
    convention_addendum_block: str | None = None
    injected_convention_slugs: list[str] = []
    if state.conventions is not None:
        try:
            curated = state.conventions.list_curated()
            if curated:
                convention_addendum_block = render_conventions_block(curated)
                injected_convention_slugs = [c.slug for c in curated]
                _log(
                    f"==> curated conventions: prepending {len(curated)} "
                    f"rule(s) to the system prompt"
                )
                # Bump use_count so we can see which rules are riding
                # along on real jobs (vs. zombie conventions that never
                # get exercised).
                for c in curated:
                    try:
                        state.conventions.bump_use(c.slug)
                    except Exception:
                        pass
        except Exception as e:
            _log(f"!! convention prefetch crashed ({type(e).__name__}: {e}); continuing without")

    # Skill retrieval: ask the LLM which (if any) distilled skills are
    # worth showing for this goal. Best-effort -- a retrieval failure
    # MUST NOT block the run; we just proceed without skills.
    skill_context_block = ""
    picked_skill_slugs: list[str] = []
    if state.skills is not None:
        try:
            all_skills = state.skills.list_all()
            if all_skills:
                top_k = (
                    state.settings.get("skill_retrieval_top_k", SKILL_RETRIEVAL_TOP_K)
                    if state.settings is not None
                    else SKILL_RETRIEVAL_TOP_K
                )
                picked_slugs, meta = await pick_relevant_skills(
                    goal=opts.goal or "",
                    url=info.url,
                    candidates=all_skills,
                    top_k=top_k,
                )
                if picked_slugs:
                    picked = [s for s in all_skills if s.slug in picked_slugs]
                    # Preserve LLM's chosen order.
                    order = {s: i for i, s in enumerate(picked_slugs)}
                    picked.sort(key=lambda r: order.get(r.slug, 999))
                    skill_context_block = build_skill_context_block(picked)
                    picked_skill_slugs = [s.slug for s in picked]
                    _log(
                        f"==> skill retrieval: picked "
                        f"{', '.join(picked_skill_slugs)} "
                        f"out of {len(all_skills)} candidate(s) "
                        f"(llm_ms={meta.get('elapsed_ms')})"
                    )
                    # Bump use_count for actually-picked skills.
                    for sl in picked_skill_slugs:
                        try:
                            state.skills.bump_use(sl)
                        except Exception:
                            pass
                else:
                    _log(
                        f"==> skill retrieval: no relevant skill found "
                        f"(considered {len(all_skills)} candidate(s); "
                        f"reason={meta.get('reason')!r})"
                    )
        except Exception as e:
            _log(f"!! skill retrieval crashed ({type(e).__name__}: {e}); continuing without skills")

    # Resolve the codegen engine slug -> LLMTarget once, here, so the
    # codegen / planner / judge LLM calls all hit the same endpoint
    # for this job. None when the operator didn't specify
    # ``options.codegen_engine`` -- the LLM helpers then fall back
    # to the env-var defaults (CODEGEN_LLM_URL + CODEGEN_MODEL_NAME).
    llm_target = None
    if opts.codegen_engine:
        from server.hub.codegen import resolve_engine_target as _resolve_engine

        try:
            llm_target = _resolve_engine(opts.codegen_engine, state.engines)
            _log(
                f"==> codegen engine: {opts.codegen_engine!r} "
                f"({llm_target.model} @ {llm_target.url})"
            )
        except Exception as e:
            _log(
                f"!! engine resolution failed for "
                f"{opts.codegen_engine!r}: {type(e).__name__}: {e}; "
                f"falling back to env defaults"
            )
            llm_target = None

    try:
        outcome = await run_iterative_codegen(
            job_id=job_id,
            goal=opts.goal or "",
            start_url=info.url,
            hub_url=hub_for_runner,
            data_dir=get_storage_dir(),
            max_attempts=opts.max_codegen_attempts,
            attempt_timeout_s=float(opts.attempt_timeout_s),
            log=_log,
            cleanup_orphan_sessions=_cleanup_orphan_sessions,
            capture_attempt_screenshot=_capture_attempt_screenshot,
            skill_context=skill_context_block or None,
            convention_addendum=convention_addendum_block,
            llm_target=llm_target,
            # codegen-loop: always expose the video-DL docs so the LLM
            # can use page.network() + page.download_video() when the
            # goal asks for it.  The negative filter was designed for
            # fetch-mode where download_video=False means "skip video
            # capture entirely"; in codegen-loop the LLM decides.
            download_video=True,
        )
    except Exception as e:
        msg = f"!! codegen-loop crashed: {type(e).__name__}: {e}"
        _log(msg)
        info.status = JobStatus.failed
        info.error = f"{type(e).__name__}: {e}"
        info.completed_at = datetime.utcnow()
        info.progress.phase = "failed"
        # Sweep any sessions held by the runner that was in flight when
        # the orchestrator itself crashed.
        try:
            await _cleanup_orphan_sessions(job_id)
        except Exception:
            pass
        await state.store.save_job_info(info)
        try:
            await state.store.publish_log(job_id, DONE_SENTINEL)
        except Exception:
            pass
        try:
            log_fp.close()
        except Exception:
            pass
        return

    info.completed_at = datetime.utcnow()
    if outcome.success:
        info.status = JobStatus.completed
        info.progress.phase = "completed"
        msg = (
            f"==> SUCCESS after {len(outcome.attempts)} attempt(s) "
            f"({outcome.total_elapsed_ms} ms total)"
        )
    else:
        info.status = JobStatus.failed
        info.error = outcome.error or "all attempts failed"
        # state-model v1.1: if the FINAL attempt died on its time budget,
        # surface as closed·timed_out (vs generic failure) so capacity/
        # slow cases triage apart from real errors.
        _last = outcome.attempts[-1] if getattr(outcome, "attempts", None) else None
        info.progress.phase = (
            "timed_out"
            if (_last is not None
                and getattr(getattr(_last, "result", None), "timed_out", False))
            else "failed"
        )
        msg = (
            f"==> FAILED after {len(outcome.attempts)} attempt(s) "
            f"({outcome.total_elapsed_ms} ms total) -- last error: "
            f"{(outcome.error or '')[:120]}"
        )
    _log(msg)
    # Final sweep -- catches a session left over by the last attempt
    # (which run_iterative_codegen already swept, but if the job
    # crashed in the success path, the inner sweep was skipped).
    try:
        n = await _cleanup_orphan_sessions(job_id)
        if n:
            _log(f"==> cleanup: closed {n} leftover session(s)")
    except Exception:
        pass
    await state.store.save_job_info(info)

    # fMP4 fragment merge: if the codegen script downloaded individual
    # CMAF/fMP4 segments (init + numbered media segments), concatenate
    # them into a single playable MP4.  Same logic as the fetch-mode
    # post-processing in core/fetcher.py; run it here so codegen-loop
    # jobs that use page.download_video() on HLS streams also produce
    # a playable file.  Non-fatal: a merge failure must not abort the
    # success path or distillation.
    _assets_dir = get_storage_dir() / job_id / "assets"
    if _assets_dir.exists():
        try:
            await asyncio.to_thread(merge_fmp4_fragments, _assets_dir, _log)
        except Exception as _e:
            _log(f"  !! fMP4 merge (post-codegen) failed ({type(_e).__name__}: {_e}); continuing")

    # Scan the assets directory and update progress.assets_saved.
    # JobProgress.assets_saved is a fetch-pipeline counter that codegen-loop
    # never increments during execution (uploads happen inside the runner,
    # which doesn't talk back to the orchestrator's progress field).  Do a
    # one-shot scan after the merge so the admin-UI pill shows the correct
    # file count and /jobs/{id}/result carries a non-empty assets list.
    _codegen_assets: list[AssetInfo] = []
    if _assets_dir.exists():
        try:
            for _af in sorted(_assets_dir.iterdir(), key=lambda p: p.name.lower()):
                if _af.is_file() and not _af.name.startswith("."):
                    _codegen_assets.append(
                        AssetInfo(
                            name=_af.name,
                            size=_af.stat().st_size,
                            href=f"/jobs/{_url_quote(job_id, safe='')}/assets/{_url_quote(_af.name, safe='')}",
                        )
                    )
            if _codegen_assets:
                info.progress.assets_saved = len(_codegen_assets)
                _log(f"  📦 assets: {len(_codegen_assets)} file(s) in assets dir")
        except Exception as _ae:
            _log(f"  !! assets scan failed ({type(_ae).__name__}: {_ae}); continuing")

    # Post-SUCCESS distillation: skills + conventions, both bounded
    # by their own timeouts. We await them in series (parallel via
    # asyncio.gather is tempting but the two share an LLM endpoint
    # and concurrent calls don't speed things up on a single-GPU
    # vLLM deployment).
    #
    # Distillation MUST only fire when the final attempt was judged
    # OK (or the judge was unreachable -- preserving the "flaky
    # judge shouldn't kill otherwise-good attempts" semantics). The
    # primary guard is ``outcome.success`` which is set correctly
    # by run_iterative_codegen, but we double-check by reading the
    # final attempt's judge.json so an out-of-band success flag
    # can't pollute the registry with garbage. (Job c1bd3d798ae2:
    # 10 NG attempts that got marked completed by the pre-fix
    # judge-skip-on-last-attempt bug.)
    judge_ok = _final_attempt_judge_ok(job_id, len(outcome.attempts))

    # Selection signal: credit the skills + conventions that were injected
    # into THIS job iff it genuinely succeeded (same guard as distillation).
    # ``use_count`` was bumped at injection (= attempts); bumping
    # ``success_count`` here makes ``success_count / use_count`` a real
    # fitness ratio -- the basis for preferring proven skills/conventions
    # and retiring ones that ride along but never correlate with success.
    if outcome.success and judge_ok:
        if state.skills is not None:
            for sl in picked_skill_slugs:
                try:
                    state.skills.bump_success(sl)
                except Exception:
                    pass
        if state.conventions is not None:
            for cs in injected_convention_slugs:
                try:
                    state.conventions.bump_success(cs)
                except Exception:
                    pass
        if picked_skill_slugs or injected_convention_slugs:
            _log(
                "==> selection: credited success to "
                f"{len(picked_skill_slugs)} skill(s) + "
                f"{len(injected_convention_slugs)} convention(s)"
            )

    skill_enabled = (
        state.settings.get("skill_auto_extract_enabled", SKILL_AUTO_EXTRACT_ENABLED)
        if state.settings is not None
        else SKILL_AUTO_EXTRACT_ENABLED
    )
    if outcome.success and judge_ok and skill_enabled and state.skills is not None:
        try:
            await asyncio.wait_for(
                _distill_skill_background(
                    job_id=job_id,
                    goal=opts.goal or "",
                    winning_code=outcome.final_code or "",
                    attempt_count=len(outcome.attempts),
                    log_cb=_log,
                ),
                timeout=120.0,
            )
        except TimeoutError:
            _log("!! skill distillation timed out after 120s; skipping")
        except Exception as e:
            _log(f"!! skill distillation crashed at top level ({type(e).__name__}: {e})")

    # Convention distillation: only fires when the job needed at least
    # one retry (= we have a real failure→success diff to learn from).
    # A 1-attempt success teaches nothing about foot-guns. Bounded with
    # the same 120s timeout pattern as skill distillation.
    convention_enabled = (
        state.settings.get(
            "convention_auto_extract_enabled",
            CONVENTION_AUTO_EXTRACT_ENABLED,
        )
        if state.settings is not None
        else CONVENTION_AUTO_EXTRACT_ENABLED
    )
    if (
        outcome.success
        and judge_ok
        and convention_enabled
        and state.conventions is not None
        and len(outcome.attempts) >= 2
    ):
        try:
            await asyncio.wait_for(
                _distill_convention_background(
                    job_id=job_id,
                    goal=opts.goal or "",
                    attempts=outcome.attempts,
                    log_cb=_log,
                ),
                timeout=120.0,
            )
        except TimeoutError:
            _log("!! convention distillation timed out after 120s; skipping")
        except Exception as e:
            _log(f"!! convention distillation crashed at top level ({type(e).__name__}: {e})")

    # v2 Phase 1: end-of-job perception observation. Runs the eye (Qwen3/
    # vision LLM) on the final page state and saves the structured
    # PerceptionResult to data/jobs/{id}/perception.json for distillation
    # analysis. This is observation-only -- never affects the job outcome.
    # See internal/v2-architecture.html for context.
    try:
        from server.hub.perception_llm import save_perception_for_job
        # Pass mode + success so the sampling gate (PAPRIKA_PERCEPTION_*)
        # can decide whether to skip this job's perception. GPU-saturation
        # mitigation: on ぱっぷす environment Qwen-VL is local but the
        # single RTX 6000 is shared by 24 worker lines.
        await asyncio.wait_for(
            save_perception_for_job(
                job_id=job_id,
                url=info.url,
                data_dir=get_storage_dir(),
                log=_log,
                mode=(info.options.mode if info.options else None),
                success=(info.status == JobStatus.completed),
            ),
            timeout=90.0,
        )
    except TimeoutError:
        _log("!! perception observation timed out after 90s; skipping")
    except Exception as e:
        _log(f"!! perception observation crashed ({type(e).__name__}: {e})")

    # v2 Phase 5: lightweight distillation. Updates HostKnowledge.stats
    # (total_jobs / success_rate / overall_confidence) so the operator
    # UI and Phase-5 consultation always have fresh numbers. No LLM
    # call -- pure bookkeeping; the R1 distiller (Phase 6+) layers on
    # top of this with deeper updates.
    try:
        from server.hub.distiller_light import host_from_url, record_job_outcome
        _hk_host = host_from_url(info.url)
        if _hk_host:
            record_job_outcome(
                host=_hk_host,
                success=(info.status == JobStatus.completed),
                job_id=job_id,
                reason=(info.error or "")[:200] if info.status != JobStatus.completed else "",
                data_dir=get_storage_dir(),
            )
            _log(f"  📊 HostKnowledge.stats updated for {_hk_host}")
    except Exception as e:
        _log(f"  !! distiller-light crashed (non-fatal): {type(e).__name__}: {e}")

    # v2 Phase 6: R1 Distiller (deep updates).
    # Gated by PAPRIKA_R1_DISTILLER_MODE=off|on|new. When enabled, R1
    # reads the job brief + current HostKnowledge and proposes narrow,
    # validated updates (new barrier strategies, content_extraction
    # patterns, etc.). Pure addition -- never touches stats/provenance.
    try:
        from server.hub.distiller_r1 import distill_for_job
        from server.hub.distiller_light import host_from_url as _hfu

        _r1_host = _hfu(info.url)
        if _r1_host:
            # Load latest perception (saved by save_perception_for_job above).
            _perception_dict = None
            try:
                _pp = get_storage_dir() / job_id / "perception.json"
                if _pp.is_file():
                    _perception_dict = json.loads(_pp.read_text(encoding="utf-8"))
            except Exception:
                pass
            # Final attempt's stdout/stderr/script for codegen-loop briefs.
            _stdout_tail = ""
            _stderr_tail = ""
            _script = ""
            try:
                _attempts = sorted((get_storage_dir() / job_id / "attempts").glob("*"))
                if _attempts:
                    _last = _attempts[-1]
                    if (_last / "stdout.log").is_file():
                        _stdout_tail = (_last / "stdout.log").read_text(encoding="utf-8", errors="replace")[-2500:]
                    if (_last / "stderr.log").is_file():
                        _stderr_tail = (_last / "stderr.log").read_text(encoding="utf-8", errors="replace")[-1800:]
                    if (_last / "script.py").is_file():
                        _script = (_last / "script.py").read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

            _updated = await asyncio.wait_for(
                distill_for_job(
                    host=_r1_host,
                    job_id=job_id,
                    goal=opts.goal or "",
                    success=(info.status == JobStatus.completed),
                    error=(info.error or ""),
                    perception=_perception_dict,
                    stdout_tail=_stdout_tail,
                    stderr_tail=_stderr_tail,
                    script=_script,
                    data_dir=get_storage_dir(),
                ),
                timeout=120.0,
            )
            if _updated is not None:
                _log(f"  🧬 HostKnowledge updated by R1 distiller for {_r1_host}")
    except TimeoutError:
        _log("  !! r1-distiller timed out after 120s; skipping")
    except Exception as e:
        _log(f"  !! r1-distiller crashed (non-fatal): {type(e).__name__}: {e}")

    # Re-save info as the LAST writer. The early save_job_info() above
    # (before distillation) can be clobbered by a racing WorkerJobProgress
    # handler that reads a stale "status=running" from Redis during the
    # cleanup_orphan_sessions / distillation await points, modifies
    # progress fields, and writes back with status=running. By saving
    # again here we guarantee the orchestrator's view of completion is
    # what readers see once the orchestrator returns. (Symptom:
    # /jobs/{id} kept showing ``status=running`` minutes after SUCCESS
    # was logged + skill/convention distillation finished.)
    info.completed_at = info.completed_at or datetime.utcnow()
    await state.store.save_job_info(info)

    # Persist a JobResult so /jobs/{id}/result returns something useful.
    job_result = JobResult(
        job_id=job_id,
        status=info.status,
        html_href=None,
        log_href=f"/jobs/{job_id}/script.py",
        assets=_codegen_assets,
        assets_failed=0,
        video_detection={},
        video_urls_seen=[],
        iframe_srcs=[],
        ytdlp_results=[],
        visited_urls=[],
        error=info.error,
    )
    await state.store.save_job_result(job_result)

    # Signal /jobs/{id}/events subscribers that the run is over.
    try:
        await state.store.publish_log(job_id, DONE_SENTINEL)
    except Exception:
        pass
    try:
        log_fp.close()
    except Exception:
        pass


async def redispatch_orphan_job(job_id: str) -> bool:
    """Re-run a hub-orchestrated job that was orphaned when its hub died —
    multi-hub control-plane *phase 4*.

    Called by the lease reaper on a surviving hub after it has atomically
    re-claimed the job's lease (so this never double-runs: only the hub that
    won the ``SET NX`` claim gets here). Loads the persisted JobInfo and
    re-spawns the appropriate in-process orchestrator:

      * ``codegen-loop`` -> ``_run_codegen_loop_job`` (request is unused by
        the body, so we pass None).
      * ``rerun``        -> ``_run_rerun_loop_job`` (script re-resolved from
        ``options.rerun_from`` / ``options.code`` via resolve_rerun_source).

    Returns True if a task was spawned, False if the job can't be re-run here
    (missing/terminal job, unsupported mode, or unresolvable rerun source) so
    the caller can fail it out instead. Worker-dispatched fetch jobs are NOT
    re-dispatched here — the worker re-homes to another hub on its own.

    Shared object storage (phase 2 / MinIO) is what makes cross-hub re-run
    correct: the new hub can read the orphaned job's prior attempts/assets.
    """
    if state.store is None:
        return False
    info = await state.store.get_job_info(job_id)
    if info is None:
        return False
    # Only resurrect jobs that were actually in flight.
    if info.status not in (JobStatus.running, JobStatus.queued):
        return False
    mode = (info.options.mode if info.options else None) or "fetch"

    if mode == "codegen-loop":
        if not (info.options.goal or "").strip():
            return False
        task = asyncio.create_task(_run_codegen_loop_job(None, info))  # type: ignore[arg-type]
        state.local_tasks[job_id] = task
        log.info("[%s] re-dispatched orphaned codegen-loop job on this hub", job_id)
        return True

    if mode == "rerun":
        try:
            script_code, source_label, source_jid = resolve_rerun_source(
                get_storage_dir(),
                info.options.rerun_from,
                info.options.code,
            )
        except Exception as e:
            log.warning("[%s] rerun re-dispatch: source unresolvable: %s", job_id, e)
            return False
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
        log.info("[%s] re-dispatched orphaned rerun job on this hub", job_id)
        return True

    # fetch / worker-dispatched modes: not hub-orchestrated, out of scope.
    return False


def _final_attempt_judge_ok(job_id: str, attempt_count: int) -> bool:
    """Return True iff the final attempt's judge.json says satisfied=True.

    Defense-in-depth guard for skill / convention distillation: even
    if ``outcome.success`` is True, only learn from attempts the
    Judge explicitly OK'd.

    Falls back to True when:
      * no attempt was run (degenerate; the caller's other guards
        will reject the empty job anyway), or
      * the judge.json file is missing (the judge was unreachable
        on that attempt -- preserve the "flaky judge shouldn't
        kill otherwise-good attempts" behaviour), or
      * the file is unparseable (treat as unreachable).

    Returns False ONLY when judge.json explicitly says
    ``satisfied: false``.
    """
    if attempt_count <= 0:
        return True
    path = get_storage_dir() / job_id / "attempts" / str(attempt_count) / "judge.json"
    if not path.exists():
        return True
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True
    if not isinstance(d, dict):
        return True
    # Only block when the judge was explicit about NG.
    return bool(d.get("satisfied", True))


async def _distill_skill_background(
    *,
    job_id: str,
    goal: str,
    winning_code: str,
    attempt_count: int,
    log_cb,
) -> None:
    """Fire-and-forget skill extraction after a codegen-loop SUCCESS.

    Runs the distillation LLM, parses the response, and saves to the
    ``auto/`` tier when the model returns a non-skip skill. Every
    outcome (skip, parse error, save) is logged to the job's log.txt
    via ``log_cb`` so the operator can see what happened.
    """
    if state.skills is None:
        return
    try:
        parsed, meta = await distill_skill_from_job(
            job_id=job_id,
            goal=goal,
            winning_script=winning_code,
            attempt_count=attempt_count,
        )
    except Exception as e:
        try:
            log_cb(f"!! skill distillation crashed ({type(e).__name__}: {e})")
        except Exception:
            pass
        return
    if parsed is None:
        try:
            log_cb(
                f"==> skill distillation: no skill saved "
                f"(reason={meta.get('reason')!r}, "
                f"model={meta.get('model')}, "
                f"llm_ms={meta.get('elapsed_ms')})"
            )
        except Exception:
            pass
        return
    try:
        rec = state.skills.upsert(
            slug=parsed.get("slug") or "",
            name=parsed.get("name") or "",
            description=parsed.get("description") or "",
            code_template=parsed.get("code_template") or "",
            llm_instructions=parsed.get("llm_instructions") or "",
            applicable_when=parsed.get("applicable_when") or [],
            tags=parsed.get("tags") or [],
            auto_extracted=True,
            extracted_from=[job_id],
            tier="auto",
        )
    except Exception as e:
        try:
            log_cb(f"!! skill distillation: upsert failed ({type(e).__name__}: {e})")
        except Exception:
            pass
        return
    try:
        log_cb(
            f"==> skill distillation: saved auto/{rec.slug} "
            f"-- {rec.name!r} (model={meta.get('model')}, "
            f"llm_ms={meta.get('elapsed_ms')})"
        )
    except Exception:
        pass


async def _distill_convention_background(
    *,
    job_id: str,
    goal: str,
    attempts: list,
    log_cb,
) -> None:
    """Run convention extraction after a multi-attempt SUCCESS.

    Picks the LAST failing attempt (closest to the fix) and the
    succeeding attempt, hands the diff to the LLM, saves the result
    under ``auto/`` if the model emits a non-skip convention. Failures
    are logged via ``log_cb`` and never propagate.
    """
    if state.conventions is None:
        return
    # The orchestrator breaks the retry loop only on a real success
    # (process exit 0 AND no timeout AND no soft-failure marker), so
    # by construction the LAST attempt is the winning one and all
    # earlier attempts are failures -- including "soft failures"
    # like UNDER-TARGET / ZERO-PROGRESS where the process exited 0
    # but the orchestrator decided to retry anyway. Indexing the
    # list directly is more reliable than checking
    # ``attempt.result.success``, which only reflects the OS-level
    # exit code and misses soft failures.
    if len(attempts) < 2:
        try:
            log_cb("==> convention distillation: only one attempt; nothing to diff against")
        except Exception:
            pass
        return
    success = attempts[-1]
    failed = attempts[-2]
    # Optional: tell the LLM which curated rules already exist so it
    # doesn't redundantly re-emit them.
    try:
        existing_curated = [c.slug for c in state.conventions.list_curated()]
    except Exception:
        existing_curated = []
    try:
        parsed, meta = await distill_convention_from_diff(
            job_id=job_id,
            goal=goal,
            failed_code=failed.code or "",
            failed_stderr=failed.result.stderr or "",
            success_code=success.code or "",
            success_stdout=success.result.stdout or "",
            existing_curated_slugs=existing_curated,
        )
    except Exception as e:
        try:
            log_cb(f"!! convention distillation crashed ({type(e).__name__}: {e})")
        except Exception:
            pass
        return
    if parsed is None:
        try:
            log_cb(
                f"==> convention distillation: no rule saved "
                f"(reason={meta.get('reason')!r}, "
                f"model={meta.get('model')}, "
                f"llm_ms={meta.get('elapsed_ms')})"
            )
        except Exception:
            pass
        return
    try:
        rec = state.conventions.upsert(
            slug=parsed.get("slug") or "",
            name=parsed.get("name") or "",
            advice=parsed.get("advice") or "",
            rationale=parsed.get("rationale") or "",
            bad_example=parsed.get("bad_example") or "",
            good_example=parsed.get("good_example") or "",
            applicable_when=parsed.get("applicable_when") or [],
            tags=parsed.get("tags") or [],
            extracted_from=[job_id],
            tier="auto",
        )
    except Exception as e:
        try:
            log_cb(f"!! convention distillation: upsert failed ({type(e).__name__}: {e})")
        except Exception:
            pass
        return
    try:
        log_cb(
            f"==> convention distillation: saved auto/{rec.slug} "
            f"-- {rec.advice[:120]!r} "
            f"(model={meta.get('model')}, llm_ms={meta.get('elapsed_ms')})"
        )
    except Exception:
        pass


def _copy_session_state_dir(src_job_id: str, dst_job_id: str) -> int:
    """Copy ``data/jobs/{src}/state/`` into ``data/jobs/{dst}/state/``
    so a rerun-mode job inherits walker / per-attempt state from the
    job it's resuming. Returns the number of files copied (0 if the
    source had no state dir).

    Best-effort: any per-file failure is silently skipped. The
    caller logs the total count for visibility.
    """
    src = get_storage_dir() / src_job_id / "state"
    dst = get_storage_dir() / dst_job_id / "state"
    if not src.exists():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        for f in src.iterdir():
            if not f.is_file():
                continue
            try:
                shutil.copy2(f, dst / f.name)
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


async def _run_rerun_loop_job(
    info: JobInfo,
    script_code: str,
    source_label: str,
    *,
    inherited_state_files: int = 0,
) -> None:
    """Single-attempt cousin of _run_codegen_loop_job. Same plumbing
    (log.txt, /jobs/{id}/events publish, orphan cleanup, JobResult
    persistence) but skips the LLM."""
    assert state.store is not None
    job_id = info.job_id
    opts = info.options

    info.status = JobStatus.running
    info.started_at = datetime.utcnow()
    info.progress = JobProgress(phase="rerun:start")
    await state.store.save_job_info(info)

    log_path = get_storage_dir() / job_id / "log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "a", encoding="utf-8", buffering=1)

    _bg_tasks: set[asyncio.Task] = set()

    def _log(line: str) -> None:
        if not isinstance(line, str):
            line = str(line)
        info.progress.last_log = line[:200]
        try:
            log_fp.write(line + "\n")
        except Exception:
            pass
        try:
            t1 = asyncio.create_task(state.store.append_log_line(job_id, line))
            _bg_tasks.add(t1)
            t1.add_done_callback(_bg_tasks.discard)
            t2 = asyncio.create_task(state.store.publish_log(job_id, line))
            _bg_tasks.add(t2)
            t2.add_done_callback(_bg_tasks.discard)
        except Exception:
            pass

    _log(
        f"==> rerun start: source={source_label} bytes={len(script_code)} "
        f"timeout={opts.attempt_timeout_s}s"
    )
    if inherited_state_files > 0:
        _log(
            f"==> inherited {inherited_state_files} state file(s) from source "
            f"job -- pap.walk() will resume from previous progress"
        )

    async def _cleanup_orphan_sessions(jid: str) -> int:
        if state.sessions is None:
            return 0
        # Skip detach()-ed sessions: those are operator-managed.
        # See the codegen-loop variant of this function above for the
        # full rationale.
        orphans = [s for s in state.sessions.all() if s.job_id == jid and not s.detached]
        closed = 0
        for sess in orphans:
            sid = sess.session_id
            sess.state = "closing"
            state.sessions.remove(sid)
            worker = state.registry.connections.get(sess.worker_id)
            if worker is None:
                closed += 1
                continue
            try:
                await worker.end_session(sid, timeout=10.0)
            except Exception:
                pass
            closed += 1
        return closed

    try:
        outcome = await run_rerun_job(
            job_id=job_id,
            script_code=script_code,
            source_label=source_label,
            data_dir=get_storage_dir(),
            attempt_timeout_s=float(opts.attempt_timeout_s),
            log=_log,
            cleanup_orphan_sessions=_cleanup_orphan_sessions,
        )
    except Exception as e:
        msg = f"!! rerun crashed: {type(e).__name__}: {e}"
        _log(msg)
        info.status = JobStatus.failed
        info.error = f"{type(e).__name__}: {e}"
        info.completed_at = datetime.utcnow()
        info.progress.phase = "failed"
        try:
            await _cleanup_orphan_sessions(job_id)
        except Exception:
            pass
        await state.store.save_job_info(info)
        try:
            await state.store.publish_log(job_id, DONE_SENTINEL)
        except Exception:
            pass
        try:
            log_fp.close()
        except Exception:
            pass
        return

    info.completed_at = datetime.utcnow()
    if outcome.success:
        info.status = JobStatus.completed
        info.progress.phase = "completed"
        msg = f"==> SUCCESS ({outcome.total_elapsed_ms} ms)"
    else:
        info.status = JobStatus.failed
        info.error = outcome.error or "rerun failed"
        # state-model v1.1: timeout-class failure -> closed·timed_out.
        _last = outcome.attempts[-1] if getattr(outcome, "attempts", None) else None
        info.progress.phase = (
            "timed_out"
            if (_last is not None
                and getattr(getattr(_last, "result", None), "timed_out", False))
            else "failed"
        )
        msg = f"==> FAILED ({outcome.total_elapsed_ms} ms): {(outcome.error or '')[:120]}"
    _log(msg)
    try:
        n = await _cleanup_orphan_sessions(job_id)
        if n:
            _log(f"==> cleanup: closed {n} leftover session(s)")
    except Exception:
        pass
    await state.store.save_job_info(info)

    # fMP4 fragment merge (same as codegen-loop path above).
    _assets_dir = get_storage_dir() / job_id / "assets"
    if _assets_dir.exists():
        try:
            await asyncio.to_thread(merge_fmp4_fragments, _assets_dir, _log)
        except Exception as _e:
            _log(f"  !! fMP4 merge (post-rerun) failed ({type(_e).__name__}: {_e}); continuing")

    # Scan assets dir and update progress counter (same as codegen-loop path).
    _rerun_assets: list[AssetInfo] = []
    if _assets_dir.exists():
        try:
            for _af in sorted(_assets_dir.iterdir(), key=lambda p: p.name.lower()):
                if _af.is_file() and not _af.name.startswith("."):
                    _rerun_assets.append(
                        AssetInfo(
                            name=_af.name,
                            size=_af.stat().st_size,
                            href=f"/jobs/{_url_quote(job_id, safe='')}/assets/{_url_quote(_af.name, safe='')}",
                        )
                    )
            if _rerun_assets:
                info.progress.assets_saved = len(_rerun_assets)
                _log(f"  📦 assets: {len(_rerun_assets)} file(s) in assets dir")
        except Exception as _ae:
            _log(f"  !! assets scan failed ({type(_ae).__name__}: {_ae}); continuing")
    # Persist the updated assets_saved count (the save above was before the scan).
    await state.store.save_job_info(info)

    job_result = JobResult(
        job_id=job_id,
        status=info.status,
        html_href=None,
        log_href=f"/jobs/{job_id}/script.py",
        assets=_rerun_assets,
        assets_failed=0,
        video_detection={},
        video_urls_seen=[],
        iframe_srcs=[],
        ytdlp_results=[],
        visited_urls=[],
        error=info.error,
    )
    await state.store.save_job_result(job_result)
    try:
        await state.store.publish_log(job_id, DONE_SENTINEL)
    except Exception:
        pass
    try:
        log_fp.close()
    except Exception:
        pass
