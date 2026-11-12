"""The unattended loop (design §8, §9, §7.2; §15.4 G3).

Two periodic jobs, neither of which waits for a person:

``lifecycle_tick`` (every few minutes)
    candidate -> trial -> active -> retired, decided by ``playbook.decide``
    from the measured counts, then the applicable playbooks are published
    for the hubs (server/hub/_playbooks.py reads that one Redis key).

``solver_tick`` (every ten minutes, one key at a time)
    budget left?  ->  rank the keys (``scheduler.rank``)  ->  for the best
    one: explore one failing URL with the Solver, compile the trace, and
    REPLAY it without a model on that URL and on two other URLs of the
    template. Only if the Verifier calls those replays a success does the
    result become a candidate playbook. Everything else is recorded as a
    failed run and the key backs off by itself.

Infrastructure trouble (no free lane, engine overheated, hub restart) is
never booked against a key: the run is logged as ``infra`` and the loop
pauses.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import random
import time
from dataclasses import dataclass

from learner import compiler, playbook as P, scheduler as S, solver as V
from learner.hubapi import HubError, HubUnavailable
from learner.verify import SUCCESS
from learner.verify import VERSION as VERIFIER_VERSION

log = logging.getLogger("learner.autopilot")

#: learn_solver_runs.result values beyond the Solver's own
SOLVED = "solved"
UNCOMPILABLE = "uncompilable"
REPLAY_FAIL = "replay_fail"       # the compiled steps did not work on the URL they came from
GENERALIZE_FAIL = "generalize_fail"  # they worked there, but not on other URLs of the template

_PENDING_REASONS = ("download_in_progress",)


@dataclass(frozen=True)
class AutopilotConfig:
    solver_enabled: bool = False
    lifecycle_interval_s: float = 180.0
    solver_interval_s: float = 600.0
    infra_backoff_s: float = 900.0
    replay_timeout_s: float = 1800.0
    replay_poll_s: float = 20.0
    gen_urls: int = 2                 # other URLs of the template that must also succeed
    publish: bool = True


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


class Autopilot:
    def __init__(self, *, store, hub, llm, redis, cfg: AutopilotConfig | None = None,
                 life_cfg: P.LifecycleConfig | None = None,
                 sched_cfg: S.SchedulerConfig | None = None,
                 solver_cfg: V.SolverConfig | None = None,
                 rng: random.Random | None = None, sleep=asyncio.sleep) -> None:
        self.store, self.hub, self.llm, self.redis = store, hub, llm, redis
        self.cfg = cfg or AutopilotConfig()
        self.life_cfg = life_cfg or P.LifecycleConfig()
        self.sched_cfg = sched_cfg or S.SchedulerConfig()
        self.solver_cfg = solver_cfg or V.SolverConfig()
        self.rng = rng or random.Random()
        self._sleep = sleep
        self._rev = int(time.time())
        self.stats = {"lifecycle_ticks": 0, "transitions": 0, "published": 0,
                      "runs": 0, "solved": 0, "infra": 0}

    def manifest(self) -> dict:
        """What produced a playbook: enough to tell, later, which prompt /
        model / compiler / verifier generation a result belongs to."""
        return {
            "solver_prompt": V.PROMPT_VERSION, "model": getattr(self.llm, "model", "") or "",
            "compiler": compiler.COMPILER_VERSION, "verifier": VERIFIER_VERSION,
        }

    # ---- life cycle ------------------------------------------------------

    async def lifecycle_tick(self) -> list[tuple[int, str, str]]:
        """Apply every due transition, then publish. Returns the
        transitions made as (playbook id, new state, reason)."""
        now = _utcnow()
        pbs = await self.store.load_playbooks()
        live = [p for p in pbs if p.state in (P.TRIAL, P.ACTIVE)]
        ids = [p.id for p in live]
        counts = await self.store.playbook_counts(ids)
        recent = await self.store.playbook_counts(ids, since_days=self.life_cfg.decay_window_days)
        idle = await self.store.last_applied_days(ids)
        moved: list[tuple[int, str, str]] = []
        for p in live:
            d = P.decide(p, counts.get(p.id, {}), now=now, cfg=self.life_cfg,
                         recent=recent.get(p.id, {}) if p.state == P.ACTIVE else None,
                         last_applied_days=idle.get(p.id))
            if d is None:
                continue
            await self.store.set_state(p.id, d.state, d.reason, promoted_diff=d.promoted_diff)
            log.info("playbook #%s %s/%s: %s -> %s (%s)", p.id, p.host, p.template,
                     p.state, d.state, d.reason)
            was_trial = p.state == P.TRIAL
            p.state, p.reason, p.state_changed_at = d.state, d.reason, now
            if d.promoted_diff is not None:
                p.promoted_diff = d.promoted_diff
            moved.append((p.id, d.state, d.reason))
            if was_trial and d.state == P.RETIRED:
                # The replay tests passed but real traffic did not confirm
                # it: that is a failed attempt on the key, so it backs off
                # like any other before the Solver tries it again.
                streak = await self.store.fail_streak(p.host, p.template, p.intent) + 1
                await self.store.update_key_state(
                    host=p.host, template=p.template, intent=p.intent,
                    klass=str((p.provenance or {}).get("klass") or ""),
                    solved=False, result=d.reason, dormant_until=now + S.dormancy(streak),
                )
        for p in P.admit_candidates(pbs):
            base = await self.store.key_base_counts(p.host, p.template, p.intent)
            pct = P.trial_pct_for(P.rate(base), self.life_cfg)
            await self.store.set_state(p.id, P.TRIAL, "trial_started", apply_pct=pct)
            log.info("playbook #%s %s/%s: candidate -> trial at %s%%", p.id, p.host, p.template, pct)
            p.state, p.apply_pct, p.state_changed_at = P.TRIAL, pct, now
            moved.append((p.id, P.TRIAL, "trial_started"))
        self.stats["lifecycle_ticks"] += 1
        self.stats["transitions"] += len(moved)
        if self.cfg.publish:
            await self.publish(pbs)
        return moved

    async def publish(self, pbs: list[P.Playbook]) -> dict:
        self._rev += 1
        doc = P.published_doc(pbs, now_ts=time.time(), rev=self._rev)
        await self.redis.set(P.REDIS_KEY, json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
        self.stats["published"] = len(doc["items"])
        return doc

    # ---- solving ---------------------------------------------------------

    async def solver_tick(self) -> str:
        """Run the Solver on the single most valuable key, if the budget
        allows. Returns what happened (for logs and tests)."""
        runs, tokens = await self.store.usage_today()
        if not S.budget_left(runs, tokens, self.sched_cfg):
            return "budget_exhausted"
        cands = await self.store.candidates(reasons=self.sched_cfg.target_reasons)
        if not cands:
            return "no_candidates"
        busy = {p.key for p in await self.store.load_playbooks()}
        ranked = S.rank(cands, await self.store.key_states(), await self.store.class_stats(),
                        busy, now=_utcnow(), cfg=self.sched_cfg, rng=self.rng)
        if not ranked:
            return "nothing_eligible"
        _value, cand = ranked[0]
        return await self.explore(cand)

    async def explore(self, cand: S.KeyCandidate) -> str:
        urls = S.pick_urls(cand.urls, 1 + self.cfg.gen_urls)
        if len(urls) < 1 + self.cfg.gen_urls:
            return "too_few_urls"
        explore_url = urls[0]
        res = await V.solve(self.hub, self.llm, explore_url, cfg=self.solver_cfg, sleep=self._sleep)
        result, reason, barrier = res.result, res.reason, res.barrier
        actions = None
        pb_id = None
        replay: dict = {}

        if result == V.TRACE:
            actions, why = compiler.compile_trace(res.trace)
            if actions is None:
                result, reason = UNCOMPILABLE, why
            else:
                try:
                    replay = await self.replay(urls, actions, cand)
                except HubUnavailable as e:
                    result, reason = V.INFRA, f"replay: {e}"[:120]
                except HubError as e:
                    result, reason = REPLAY_FAIL, f"replay rejected: {e}"[:120]
                else:
                    result, reason = judge_replay(replay, explore_url, self.cfg.gen_urls)
        if result == SOLVED:
            pb_id = await self.store.insert_candidate(
                host=cand.host, template=cand.template, intent=cand.intent, actions=actions,
                provenance={
                    "manifest": self.manifest(), "klass": cand.klass, "explore_url": explore_url,
                    "generalization": urls[1:], "replay": replay,
                    "steps": compiler.summarize(actions),
                },
                holdout_pct=self.life_cfg.holdout_pct,
            )

        await self.store.record_run(
            host=cand.host, template=cand.template, intent=cand.intent, klass=cand.klass,
            url=explore_url, result=result, reason=reason, steps=len(res.trace),
            llm_calls=res.llm_calls, tokens=res.tokens, duration_s=res.duration_s,
            pb_id=pb_id, manifest=self.manifest(),
            trace=res.trace + ([{"replay": replay}] if replay else []),
        )
        self.stats["runs"] += 1
        if result in (V.INFRA, V.BAD_OUTPUT):
            # Not the site's doing: the key is not charged and may be picked
            # again once the engine / fleet is back.
            self.stats["infra"] += 1
            log.info("solver %s %s: %s (%s) -- key not charged", cand.host, cand.template, result, reason)
            return result
        solved = result == SOLVED
        until = None
        if not solved:
            streak = await self.store.fail_streak(cand.host, cand.template, cand.intent) + 1
            until = _utcnow() + S.dormancy(streak, barrier)
        await self.store.update_key_state(
            host=cand.host, template=cand.template, intent=cand.intent, klass=cand.klass,
            solved=solved, result=result, dormant_until=until,
        )
        if solved:
            self.stats["solved"] += 1
        log.info("solver %s %s: %s (%s) clicks=%s tokens=%s pb=%s", cand.host, cand.template,
                 result, reason, res.clicks, res.tokens, pb_id)
        return result

    async def replay(self, urls: list[str], actions: list, cand: S.KeyCandidate) -> dict:
        """Run the compiled actions -- no model involved -- as ordinary
        fetch jobs and collect the Verifier's outcome for each URL."""
        jobs: dict[str, str] = {}
        for i, u in enumerate(urls):
            jobs[u] = await self.hub.submit_fetch(
                u, actions=actions,
                learn={"arm": "solver", "role": "explore" if i == 0 else "gen"},
                download_video=(cand.intent == "video"),
            )
        deadline = time.monotonic() + self.cfg.replay_timeout_s
        out: dict[str, dict] = {u: {"job_id": j, "outcome": None, "reason": "timeout"}
                                for u, j in jobs.items()}
        while True:
            got = await self.store.outcomes(list(jobs.values()))
            pending = 0
            for u, j in jobs.items():
                oc = got.get(j)
                if oc is None or oc[1] in _PENDING_REASONS:
                    pending += 1
                    continue
                out[u] = {"job_id": j, "outcome": oc[0], "reason": oc[1]}
            if not pending or time.monotonic() >= deadline:
                return out
            await self._sleep(self.cfg.replay_poll_s)

    # ---- loops -----------------------------------------------------------

    async def _lifecycle_loop(self, stop: asyncio.Event) -> None:
        # Its own task: a Solver run (minutes) plus its replay (up to half
        # an hour) must not delay transitions or let the published set go
        # stale -- the hubs ignore a set that is not refreshed.
        while not stop.is_set():
            try:
                await self.lifecycle_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("lifecycle tick failed", exc_info=True)
            await _wait(stop, self.cfg.lifecycle_interval_s)

    async def _solver_loop(self, stop: asyncio.Event) -> None:
        idle = ("no_candidates", "nothing_eligible", "budget_exhausted", "too_few_urls")
        while not stop.is_set():
            delay = self.cfg.solver_interval_s
            try:
                what = await self.solver_tick()
                if what in (V.INFRA, V.BAD_OUTPUT):
                    delay = self.cfg.infra_backoff_s
                elif what in idle:
                    log.info("solver idle: %s", what)
                else:
                    delay = 5.0  # a real run finished: look for the next key
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("solver tick failed", exc_info=True)
                delay = self.cfg.infra_backoff_s
            await _wait(stop, delay)

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._lifecycle_loop(stop))]
        if self.cfg.solver_enabled:
            tasks.append(asyncio.create_task(self._solver_loop(stop)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def judge_replay(replay: dict, explore_url: str, gen_required: int) -> tuple[str, str]:
    """(result, reason) of a replay. Pure.

    The steps must reproduce on the URL they were found on AND on
    ``gen_required`` other URLs of the template -- otherwise they are a
    one-URL trick, which is exactly what filled the old recipe store.
    An unsettled or infrastructure outcome is not evidence either way.
    """
    first = replay.get(explore_url) or {}
    others = [v for u, v in replay.items() if u != explore_url]
    unsettled = [v for v in [first] + others if v.get("outcome") in (None, "infra", "unknown")]
    if first.get("outcome") == SUCCESS:
        ok = sum(1 for v in others if v.get("outcome") == SUCCESS)
        if ok >= gen_required:
            return SOLVED, "replayed_on_all_urls"
        if unsettled:
            return V.INFRA, "replay_unsettled"
        return GENERALIZE_FAIL, f"only {ok}/{len(others)} other URLs succeeded"
    if unsettled and first.get("outcome") in (None, "infra", "unknown"):
        return V.INFRA, "replay_unsettled"
    return REPLAY_FAIL, f"explore URL: {first.get('outcome')}/{first.get('reason')}"
