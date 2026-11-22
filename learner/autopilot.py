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

from learner import compiler, playbook as P, scheduler as S, solver as V, status as ST
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
#: Verifier reasons that say a replay job was never really run: the worker
#: had no room on the ramdisk for the video, so nothing was downloaded. That
#: is no verdict on the steps -- the URL is submitted again.
_RERUN_REASONS = ("no_ram",)

#: Runs a person asked for (``python -m learner.request URL``): a Redis list
#: of JSON requests, taken before anything the scheduler would pick, and a
#: result document per request so the asker can wait for the answer.
REQUESTS_KEY = "paprika:learn:solve_requests"
RESULT_KEY = "paprika:learn:solve_result:"
RESULT_TTL_S = 86400

#: What ordinary fetches of a key ran into, told to the model so that it
#: looks for the right thing. Without it the model sees an advertisement
#: playing on a "video_short" page and answers "done".
KLASS_NOTES = {
    "overlay_wall": "ordinary fetches of this kind of page were stopped by something covering the page.",
    "video_not_captured": (
        "ordinary fetches of this kind of page found signs of a video but captured no video file. "
        "The player may load the video only after its play button is clicked."
    ),
    "video_short": (
        "ordinary fetches of this kind of page saved only a short clip of about 30 seconds, usually an "
        "advertisement shown before the main video. The goal is the MAIN video, minutes long. A short "
        "clip playing is NOT done: wait for it to end (one wait of about 40 seconds) or skip it, and say "
        "done only when the longest video is well over a minute. If this kind of page has no main video at all, "
        "give_up with no_video."
    ),
}


@dataclass(frozen=True)
class AutopilotConfig:
    solver_enabled: bool = False
    lifecycle_interval_s: float = 180.0
    solver_interval_s: float = 120.0  # wait when there is nothing to work on
    #: Solver runs going on at the same time. Each holds one browser lane of
    #: the fleet and one slot of the engine while it runs.
    solver_concurrency: int = 3
    #: The candidate list costs a few hundred queries; the runs share one copy.
    candidates_ttl_s: float = 600.0
    infra_backoff_s: float = 900.0
    replay_timeout_s: float = 1800.0
    replay_poll_s: float = 20.0
    #: A replay job the hub reports finished but whose outcome never reaches
    #: the ledger is submitted again (the hub does not emit an outcome for
    #: every completion path; seen 2026-11-13 on jobs whose video download
    #: ended them). How long to wait for the outcome first, and how often.
    replay_lost_after_s: float = 120.0
    replay_resubmits: int = 2
    #: A replay job that ended without its download being attempted (no room
    #: on the worker's ramdisk) is run again, up to this often per URL. With
    #: half the downloads of the fleet skipped that way (2026-11-19), three
    #: URLs all getting through in one go is a 1-in-8 chance; four more tries
    #: each make it 9 in 10.
    replay_reruns: int = 4
    gen_urls: int = 2                # other URLs of the template that must also succeed
    #: URLs of one key a single exploration may open. A failing URL is
    #: often failing for its own reason (the video was deleted), which says
    #: nothing about the key: the Solver moves on to the next one.
    max_explore_urls: int = 4
    publish: bool = True


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


class Autopilot:
    def __init__(self, *, store, hub, llm, redis, cfg: AutopilotConfig | None = None,
                 life_cfg: P.LifecycleConfig | None = None,
                 sched_cfg: S.SchedulerConfig | None = None,
                 solver_cfg: V.SolverConfig | None = None,
                 rng: random.Random | None = None, sleep=asyncio.sleep, vision=None) -> None:
        self.store, self.hub, self.llm, self.redis = store, hub, llm, redis
        #: Proposes where on a screenshot to click the player (learner/vision.py).
        #: None, or an engine that is not configured: the middle is clicked.
        self.vision = vision if (vision is not None and getattr(vision, "enabled", True)) else None
        self.cfg = cfg or AutopilotConfig()
        self.life_cfg = life_cfg or P.LifecycleConfig()
        self.sched_cfg = sched_cfg or S.SchedulerConfig()
        self.solver_cfg = solver_cfg or V.SolverConfig()
        self.rng = rng or random.Random()
        self._sleep = sleep
        self._rev = int(time.time())
        self._started = time.time()
        self._in_flight: set = set()          # keys a Solver run is on right now
        self._cands: list | None = None
        self._cands_at = 0.0
        self._cands_lock = asyncio.Lock()
        #: Set by the entry point: a callable returning the consumer's
        #: counters, shown in the status document next to our own.
        self.consumer_stats = None
        self.stats = {"lifecycle_ticks": 0, "transitions": 0, "published": 0,
                      "runs": 0, "solved": 0, "infra": 0}

    def manifest(self) -> dict:
        """What produced a playbook: enough to tell, later, which prompt /
        model / compiler / verifier generation a result belongs to."""
        return {
            "solver_prompt": V.PROMPT_VERSION, "model": getattr(self.llm, "model", "") or "",
            "vision": (getattr(self.vision, "model", "") or "") if self.vision is not None else "",
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
        recent = await self._recent_counts([p for p in live if p.state == P.ACTIVE], now)
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

    async def _recent_counts(self, active: list[P.Playbook], now: _dt.datetime) -> dict:
        """The counts decay is judged on: the last ``decay_window_days``,
        but never from before the day the playbook entered its present
        state. A playbook put back into service (after a retirement that
        turned out to be our own measuring error) would otherwise be judged
        on the very days that got it retired, and go again within minutes."""
        win = max(1, int(self.life_cfg.decay_window_days))
        by_days: dict[int, list[int]] = {}
        for p in active:
            days = win
            if p.state_changed_at is not None:
                days = min(win, max(1, (now.date() - p.state_changed_at.date()).days + 1))
            by_days.setdefault(days, []).append(p.id)
        out: dict = {}
        for days, ids in by_days.items():
            out.update(await self.store.playbook_counts(ids, since_days=days))
        return out

    async def publish(self, pbs: list[P.Playbook]) -> dict:
        self._rev += 1
        doc = P.published_doc(pbs, now_ts=time.time(), rev=self._rev)
        await self.redis.set(P.REDIS_KEY, json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
        self.stats["published"] = len(doc["items"])
        return doc

    # ---- status ----------------------------------------------------------

    async def status_doc(self) -> dict:
        """What the learner is doing, for a person (``learner/status.py``)."""
        pbs = await self.store.load_playbooks(include_retired=True)
        counts = await self.store.playbook_counts([p.id for p in pbs])
        c = self.sched_cfg
        return ST.build(
            now_ts=time.time(), started_ts=self._started, manifest=self.manifest(),
            config={
                "solver_enabled": self.cfg.solver_enabled,
                "target_reasons": list(c.target_reasons),
                "daily_runs": c.daily_runs, "daily_tokens": c.daily_tokens,
                "min_video_7d": c.min_video_7d,
                "solver_concurrency": self.cfg.solver_concurrency,
                "trial_pct": self.life_cfg.trial_pct, "holdout_pct": self.life_cfg.holdout_pct,
                "min_n": self.life_cfg.min_n,
            },
            consumer=self.consumer_stats() if callable(self.consumer_stats) else None,
            autopilot=self.stats,
            playbooks=ST.playbooks_section(pbs, counts),
            solver=ST.solver_section(
                await self.store.solver_days(), await self.store.recent_runs(limit=ST.MAX_RECENT_RUNS),
                today=_utcnow().date(), daily_runs=c.daily_runs, daily_tokens=c.daily_tokens,
                enabled=self.cfg.solver_enabled,
            ),
            keys=ST.keys_section(await self.store.key_summary(), await self.store.key_labels()),
            outcomes=ST.outcomes_section(await self.store.outcome_days(), await self.store.fail_reasons()),
        )

    async def publish_status(self) -> dict:
        doc = await self.status_doc()
        await self.redis.set(
            ST.REDIS_KEY, json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=str),
            ex=ST.TTL_S,
        )
        return doc

    # ---- solving ---------------------------------------------------------

    async def _candidates(self) -> list:
        async with self._cands_lock:
            if self._cands is None or time.monotonic() - self._cands_at > self.cfg.candidates_ttl_s:
                self._cands = await self.store.candidates(reasons=self.sched_cfg.target_reasons)
                self._cands_at = time.monotonic()
            return self._cands

    async def manual_tick(self) -> str | None:
        """Take one requested run off the queue and carry it out. None when
        nobody asked. A request skips the scheduler's rules -- dormancy, the
        daily budget, "never produced a video" -- because a person chose the
        URL; it does not skip the replay test, so a playbook still has to
        reproduce on other URLs of the key."""
        try:
            raw = await self.redis.lpop(REQUESTS_KEY)
        except Exception:
            log.warning("request queue unreadable", exc_info=True)
            return None
        if not raw:
            return None
        try:
            req = json.loads(raw)
            url = str(req["url"]).strip()
            if not url.lower().startswith(("http://", "https://")):
                raise ValueError("not an http(s) URL")
        except Exception as e:
            log.warning("bad solve request %r: %s", str(raw)[:200], e)
            return "bad_request"
        rid = str(req.get("id") or "")
        klass = str(req.get("klass") or "")
        actions = req.get("actions")
        if actions is not None:
            why = compiler.check_manual_actions(actions)
            if why:
                log.warning("bad solve request %s: %s", url, why)
                return "bad_request"
        cand = await self.store.candidate_for_url(url, klass if klass in S.KNOWN_REASONS else "")
        playbooks = {p.key for p in await self.store.load_playbooks()}
        opened: list[str] = []        # the URLs THIS run opened (other runs go on meanwhile)
        if cand.key in playbooks:
            what = "has_playbook"
        elif cand.key in self._in_flight:
            what = "already_running"
        else:
            self._in_flight.add(cand.key)
            try:
                if actions is not None:
                    what = await self.try_actions(cand, actions, opened=opened)
                else:
                    what = await self.explore(cand, manual=True, opened=opened)
            finally:
                self._in_flight.discard(cand.key)
        log.info("manual solve %s (%s %s): %s", url, cand.host, cand.template, what)
        if rid:
            doc = {"id": rid, "url": url, "host": cand.host, "template": cand.template,
                   "klass": cand.klass, "result": what, "sessions": len(opened), "urls": opened,
                   "ts": time.time()}
            try:
                await self.redis.set(RESULT_KEY + rid, json.dumps(doc, ensure_ascii=False), ex=RESULT_TTL_S)
            except Exception:
                log.warning("could not store the result of request %s", rid, exc_info=True)
        return what

    async def solver_tick(self) -> str:
        """Run the Solver on the single most valuable key, if the budget
        allows. Returns what happened (for logs and tests). Several of
        these run at once (``solver_concurrency``)."""
        manual = await self.manual_tick()
        if manual is not None:
            return manual
        runs, tokens = await self.store.usage_today()
        if not S.budget_left(runs, tokens, self.sched_cfg):
            return "budget_exhausted"
        cands = await self._candidates()
        if not cands:
            return "no_candidates"
        playbooks = {p.key for p in await self.store.load_playbooks()}
        states, class_stats = await self.store.key_states(), await self.store.class_stats()
        # No await between reading the in-flight set and adding to it: two
        # runs never take the same key.
        ranked = S.rank(cands, states, class_stats, playbooks | self._in_flight,
                        now=_utcnow(), cfg=self.sched_cfg, rng=self.rng)
        if not ranked:
            return "nothing_eligible"
        _value, cand = ranked[0]
        self._in_flight.add(cand.key)
        try:
            return await self.explore(cand)
        finally:
            self._in_flight.discard(cand.key)

    async def explore(self, cand: S.KeyCandidate, *, manual: bool = False,
                      opened: list | None = None) -> str:
        all_urls = S.pick_urls(cand.urls, len(cand.urls))
        if len(all_urls) < 1 + self.cfg.gen_urls and not (manual and all_urls):
            return "too_few_urls"
        tag = "[manual] " if manual else ""
        # Open URLs of the key until one tells us something about the KEY.
        # "This video was deleted" is about that URL; "nothing to click" may
        # be too (the wall is not shown every time). Two URLs that both show
        # a page with no video at all are a verdict on the key.
        tryable = all_urls[: max(1, self.cfg.max_explore_urls)]
        dead: list[str] = []          # URLs not worth replaying on
        no_video = 0
        hard: dict = {}               # hard barrier -> URLs it was named on
        for i, explore_url in enumerate(tryable):
            if opened is not None:
                opened.append(explore_url)
            res = await V.solve(self.hub, self.llm, explore_url, cfg=self.solver_cfg, sleep=self._sleep,
                                notes=KLASS_NOTES.get(cand.klass, ""), vision=self.vision)
            again = False
            if res.result == V.GAVE_UP and res.barrier in V.URL_BARRIERS:
                dead.append(explore_url)
                again = True
            elif res.result == V.GAVE_UP and res.barrier == "no_video":
                dead.append(explore_url)
                no_video += 1
                again = no_video < 2
            elif res.result == V.GAVE_UP and res.barrier in V.HARD_BARRIERS:
                # "Needs a login" from one page is weak (the model says it of
                # any page that has a login link): look at a second URL.
                dead.append(explore_url)
                hard[res.barrier] = hard.get(res.barrier, 0) + 1
                again = hard[res.barrier] < 2
            elif res.result in (V.NO_ACTION, V.EXHAUSTED):
                again = True
            if not again or i == len(tryable) - 1:
                break
            await self._record(cand, explore_url, res, res.result, tag + _run_reason(res), None, {})
            log.info("solver %s %s: %s on %s -- trying another URL", cand.host, cand.template,
                     _run_reason(res), explore_url)
        result, reason, barrier = res.result, _run_reason(res), res.barrier
        if result == V.GAVE_UP and barrier == "no_video" and no_video >= 2 and cand.success_7d == 0:
            # Seen twice AND the ledger agrees: no fetch of this key got a
            # whole video in 7 days. A key that does produce videos is not
            # shelved on the model's word (it says "no video" of a player it
            # could not start).
            barrier = "not_video"     # a verdict on the key (scheduler.KEY_BARRIERS)
        long_sleep = barrier
        if barrier in V.HARD_BARRIERS and (hard.get(barrier, 0) < 2 or cand.success_7d > 0):
            # A month of silence needs the same barrier on two URLs and a
            # ledger that agrees (no whole video from this key in 7 days).
            long_sleep = ""
        actions = None
        pb_id = None
        replay: dict = {}
        urls = [explore_url] + [u for u in all_urls if u != explore_url and u not in dead][: self.cfg.gen_urls]

        if result == V.TRACE:
            actions, why = compiler.compile_trace(res.trace)
            if actions is None:
                result, reason = UNCOMPILABLE, why
            elif len(urls) < 1 + self.cfg.gen_urls:
                result, reason = REPLAY_FAIL, "too few live URLs to test the steps on"
            else:
                try:
                    replay = await self.replay(urls, actions, cand)
                except HubUnavailable as e:
                    result, reason = V.INFRA, f"replay: {e}"[:120]
                except HubError as e:
                    result, reason = REPLAY_FAIL, f"replay rejected: {e}"[:120]
                else:
                    result, reason = judge_replay(replay, explore_url, self.cfg.gen_urls)
                    if (result in (REPLAY_FAIL, GENERALIZE_FAIL) and "video_short" in reason
                            and not compiler.has_long_wait(actions)):
                        # The steps started the player, but what was saved is a
                        # short clip: in a plain fetch an advertisement plays
                        # first and the job ends before the main video loads.
                        # The Solver's own session often never shows that
                        # advertisement, so it had no reason to wait. Try the
                        # same steps once more with a wait behind them.
                        longer = compiler.with_ad_wait(actions)
                        try:
                            replay2 = await self.replay(urls, longer, cand)
                        except (HubUnavailable, HubError) as e:
                            reason = f"{reason}; retry with a wait: {e}"[:120]
                        else:
                            result2, reason2 = judge_replay(replay2, explore_url, self.cfg.gen_urls)
                            if result2 == SOLVED:
                                actions, replay, result, reason = longer, replay2, result2, reason2
                            else:
                                reason = f"{reason}; with a {compiler.AD_WAIT_S:.0f}s wait: {reason2}"[:120]
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

        await self._record(cand, explore_url, res, result, (tag + reason)[:120], pb_id, replay)
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
            until = _utcnow() + S.dormancy(streak, long_sleep)
        await self.store.update_key_state(
            host=cand.host, template=cand.template, intent=cand.intent, klass=cand.klass,
            solved=solved, dormant_until=until,
            # What the Solver concluded about the KEY ("gave_up:not_video").
            result=f"{result}:{barrier}" if result == V.GAVE_UP and barrier else result,
        )
        if solved:
            self.stats["solved"] += 1
        log.info("solver %s %s: %s (%s) clicks=%s tokens=%s pb=%s", cand.host, cand.template,
                 result, reason, res.clicks, res.tokens, pb_id)
        return result

    async def _record(self, cand: S.KeyCandidate, url: str, res, result: str, reason: str,
                      pb_id: int | None, replay: dict) -> None:
        """One Solver session = one row, whether or not it was the last
        URL tried for the key."""
        await self.store.record_run(
            host=cand.host, template=cand.template, intent=cand.intent, klass=cand.klass,
            url=url, result=result, reason=reason, steps=len(res.trace),
            llm_calls=res.llm_calls, tokens=res.tokens, duration_s=res.duration_s,
            pb_id=pb_id, manifest=self.manifest(),
            trace=res.trace + ([{"replay": replay}] if replay else []),
        )
        self.stats["runs"] += 1

    async def try_actions(self, cand: S.KeyCandidate, actions: list, *, opened: list | None = None) -> str:
        """Steps a person wrote, put through the same test as steps the
        Solver found: replayed without a model on the URL given and on other
        URLs of the key, judged by the Verifier, and only then a candidate
        playbook. No exploration, so no model is involved at all."""
        all_urls = S.pick_urls(cand.urls, len(cand.urls))
        urls = all_urls[: 1 + self.cfg.gen_urls]
        res = V.SolveResult(result=V.TRACE, trace=[{"action": "manual_actions", "n": len(actions)}])
        pb_id = None
        replay: dict = {}
        if opened is not None and urls:
            opened.append(urls[0])
        if len(urls) < 1 + self.cfg.gen_urls:
            result, reason = REPLAY_FAIL, "too few URLs of this key to test the steps on"
        else:
            try:
                replay = await self.replay(urls, actions, cand)
            except HubUnavailable as e:
                result, reason = V.INFRA, f"replay: {e}"[:120]
            except HubError as e:
                result, reason = REPLAY_FAIL, f"replay rejected: {e}"[:120]
            else:
                result, reason = judge_replay(replay, urls[0], self.cfg.gen_urls)
        if result == SOLVED:
            pb_id = await self.store.insert_candidate(
                host=cand.host, template=cand.template, intent=cand.intent, actions=actions,
                provenance={
                    "manifest": self.manifest(), "klass": cand.klass, "explore_url": urls[0],
                    "generalization": urls[1:], "replay": replay, "source": "manual",
                    "steps": compiler.summarize(actions),
                },
                holdout_pct=self.life_cfg.holdout_pct,
            )
        await self._record(cand, urls[0] if urls else "", res, result, ("[manual steps] " + reason)[:120],
                           pb_id, replay)
        if result in (V.INFRA, V.BAD_OUTPUT):
            self.stats["infra"] += 1
            return result
        solved = result == SOLVED
        until = None
        if not solved:
            streak = await self.store.fail_streak(cand.host, cand.template, cand.intent) + 1
            until = _utcnow() + S.dormancy(streak, "")
        await self.store.update_key_state(
            host=cand.host, template=cand.template, intent=cand.intent, klass=cand.klass,
            solved=solved, dormant_until=until, result=result,
        )
        if solved:
            self.stats["solved"] += 1
        log.info("manual steps %s %s: %s (%s) pb=%s", cand.host, cand.template, result, reason, pb_id)
        return result

    async def replay(self, urls: list[str], actions: list, cand: S.KeyCandidate) -> dict:
        """Run the compiled actions -- no model involved -- as ordinary
        fetch jobs and collect the Verifier's outcome for each URL.

        A job the hub reports finished whose outcome does not arrive is
        submitted again (``replay_resubmits`` times): losing the outcome is
        the hub's doing, and without this one lost event throws away steps
        that worked on every URL.

        A job whose video was not downloaded because the worker had no room
        for it (``_RERUN_REASONS``) is submitted again too
        (``replay_reruns`` times): the steps were not put to the test."""
        async def submit(i: int, u: str) -> str:
            return await self.hub.submit_fetch(
                u, actions=actions,
                learn={"arm": "solver", "role": "explore" if i == 0 else "gen"},
                download_video=(cand.intent == "video"),
            )

        jobs: dict[str, str] = {}
        for i, u in enumerate(urls):
            jobs[u] = await submit(i, u)
        deadline = time.monotonic() + self.cfg.replay_timeout_s
        out: dict[str, dict] = {u: {"job_id": j, "outcome": None, "reason": "timeout"}
                                for u, j in jobs.items()}
        finished_at: dict[str, float] = {}     # job_id -> when the hub first said it was over
        resubmits = {u: 0 for u in urls}
        reruns = {u: 0 for u in urls}
        while True:
            got = await self.store.outcomes(list(jobs.values()))
            pending = 0
            for i, (u, j) in enumerate(list(jobs.items())):
                oc = got.get(j)
                if oc is not None and oc[1] in _RERUN_REASONS and reruns[u] < self.cfg.replay_reruns:
                    try:
                        jobs[u] = await submit(i, u)
                    except (HubUnavailable, HubError):
                        pass                   # judged as it stands, below
                    else:
                        reruns[u] += 1
                        out[u] = {"job_id": jobs[u], "outcome": None, "reason": "timeout",
                                  "rerun": reruns[u]}
                        pending += 1
                        log.info("replay: %s (%s) was not downloaded (%s); submitted again as %s",
                                 j, u, oc[1], jobs[u])
                        continue
                if oc is not None and oc[1] not in _PENDING_REASONS:
                    out[u] = {"job_id": j, "outcome": oc[0], "reason": oc[1]}
                    if reruns[u]:
                        out[u]["rerun"] = reruns[u]
                    continue
                pending += 1
                if resubmits[u] >= self.cfg.replay_resubmits:
                    continue
                status_of = getattr(self.hub, "job_status", None)
                status = await status_of(j) if status_of else ""
                if status not in ("completed", "failed", "review"):
                    continue
                first = finished_at.setdefault(j, time.monotonic())
                if time.monotonic() - first < self.cfg.replay_lost_after_s:
                    continue
                try:
                    jobs[u] = await submit(i, u)
                except (HubUnavailable, HubError):
                    continue
                resubmits[u] += 1
                out[u] = {"job_id": jobs[u], "outcome": None, "reason": "timeout", "resubmitted": resubmits[u]}
                log.info("replay: the outcome of %s (%s) never arrived; submitted again as %s", j, u, jobs[u])
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
            # After the tick, and on its own: the status is for people, so
            # failing to write it must never cost a transition or a publish.
            try:
                await self.publish_status()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("status publish failed", exc_info=True)
            await _wait(stop, self.cfg.lifecycle_interval_s)

    async def _solver_loop(self, stop: asyncio.Event, start_after: float = 0.0) -> None:
        idle = ("no_candidates", "nothing_eligible", "budget_exhausted", "too_few_urls")
        await _wait(stop, start_after)
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
                if what in ("has_playbook", "already_running", "bad_request"):
                    delay = 1.0  # a request was answered without a run: next one
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("solver tick failed", exc_info=True)
                delay = self.cfg.infra_backoff_s
            await _wait(stop, delay)

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._lifecycle_loop(stop))]
        if self.cfg.solver_enabled:
            for i in range(max(1, self.cfg.solver_concurrency)):
                tasks.append(asyncio.create_task(self._solver_loop(stop, start_after=7.0 * i)))
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


def _run_reason(res) -> str:
    """The reason stored for a session: a give-up carries the barrier the
    model named, so a person (and the next query) can tell "deleted" from
    "needs a login" without reading the sentence."""
    if res.result == V.GAVE_UP and res.barrier:
        return f"[{res.barrier}] {res.reason}"[:120]
    return res.reason


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
