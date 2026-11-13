"""``python -m learner`` -- run the outcome consumer.

Config is env-only (the learner must not read the hub's settings store:
that would be a second coupling):

    LEARNER_REDIS_URL          redis://host:6379/0          (required)
    LEARNER_MARIADB_HOST/PORT/USER/PASSWORD/DATABASE        (required)
    LEARNER_NAME               consumer name (default: hostname)
    LEARNER_MIN_VIDEO_S        verifier: minimum whole-video seconds (60)

The autopilot (playbook life cycle + Solver, learner/autopilot.py) is off
unless asked for:

    LEARNER_AUTOPILOT=1        run the playbook life cycle and publish the
                               applicable playbooks for the hubs
    LEARNER_SOLVER_ENABLED=1   also let the Solver look for new playbooks
    LEARNER_HUB_URL            hub API base URL (required for the Solver)
    LEARNER_HUB_TOKEN          bearer token, if the hub enforces auth
    LEARNER_LLM_ENDPOINT/_MODEL/_API_KEY
                               an OpenAI-compatible engine; when no endpoint
                               is given the hub's engine registry is asked
                               (LEARNER_LLM_KIND, default vision-chat;
                               LEARNER_WORKER_SECRET if the hub has one)
    LEARNER_LLM_THINKING       reasoning models: 1 / 0 = ask for thinking
                               on / off, empty = the engine's default
    LEARNER_LLM_MAX_TOKENS     cap per reply, thinking included (4000)
    LEARNER_TARGET_REASONS     Verifier failure reasons the Solver works on,
                               comma separated (default: all three
                               known: overlay_wall, video_not_captured,
                               video_short)
    LEARNER_SOLVER_DAILY_RUNS  Solver sessions per UTC day (1000)
    LEARNER_SOLVER_DAILY_TOKENS  model tokens per UTC day (50000000)
    LEARNER_SOLVER_CONCURRENCY Solver runs at the same time (3)
    LEARNER_MIN_VIDEO_7D       a key is worked on only if this many of its
                               jobs produced a playable video in 7 days
                               (1; 0 = work on every key)
    LEARNER_TRIAL_PCT          share of matching jobs a trial takes (20)
    LEARNER_HOLDOUT_PCT        share an active playbook is withheld from (5)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

from learner.consumer import Consumer
from learner.ledger import MariaLedger
from learner.verify import VERSION, VerifyConfig


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None or v == "":
        raise SystemExit(f"learner: missing required env {name}")
    return v


async def _prune_loop(ledger: MariaLedger, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await ledger.prune()
        except Exception:
            logging.getLogger("learner").warning("prune failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=6 * 3600)
        except asyncio.TimeoutError:
            pass


async def _stats_loop(consumer: Consumer, stop: asyncio.Event, autopilot=None) -> None:
    log = logging.getLogger("learner")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            if autopilot is not None:
                log.info("stats %s autopilot %s", consumer.stats, autopilot.stats)
            else:
                log.info("stats %s", consumer.stats)


def _on(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def build_autopilot(ledger: MariaLedger, r):
    """The autopilot and the things to close with it, or (None, [])."""
    if not _on("LEARNER_AUTOPILOT"):
        return None, []
    from learner.autopilot import Autopilot, AutopilotConfig
    from learner.hubapi import Hub
    from learner.llm import LLM
    from learner.playbook import LifecycleConfig
    from learner.scheduler import KNOWN_REASONS, TARGET_REASONS, SchedulerConfig
    from learner.store import Store

    solver_on = _on("LEARNER_SOLVER_ENABLED")
    hub_url = os.environ.get("LEARNER_HUB_URL") or ""
    if solver_on and not hub_url:
        raise SystemExit("learner: LEARNER_SOLVER_ENABLED needs LEARNER_HUB_URL")
    hub = Hub(hub_url or "http://hub-not-configured", token=os.environ.get("LEARNER_HUB_TOKEN"))
    llm = LLM(
        endpoint=os.environ.get("LEARNER_LLM_ENDPOINT") or "",
        model=os.environ.get("LEARNER_LLM_MODEL") or "",
        api_key=os.environ.get("LEARNER_LLM_API_KEY") or "",
        hub_url=hub_url, kind=os.environ.get("LEARNER_LLM_KIND") or "vision-chat",
        worker_secret=os.environ.get("LEARNER_WORKER_SECRET") or "",
        max_tokens=int(os.environ.get("LEARNER_LLM_MAX_TOKENS") or 4000),
        thinking=(None if not (os.environ.get("LEARNER_LLM_THINKING") or "").strip()
                  else _on("LEARNER_LLM_THINKING")),
    )
    reasons = tuple(
        x.strip() for x in (os.environ.get("LEARNER_TARGET_REASONS") or "").split(",") if x.strip()
    ) or TARGET_REASONS
    unknown = [x for x in reasons if x not in KNOWN_REASONS]
    if unknown:
        raise SystemExit(f"learner: unknown LEARNER_TARGET_REASONS {unknown}; known: {KNOWN_REASONS}")
    ap = Autopilot(
        store=Store(ledger._pool), hub=hub, llm=llm, redis=r,
        cfg=AutopilotConfig(
            solver_enabled=solver_on,
            solver_concurrency=int(os.environ.get("LEARNER_SOLVER_CONCURRENCY") or 3),
        ),
        life_cfg=LifecycleConfig(
            trial_pct=int(os.environ.get("LEARNER_TRIAL_PCT") or 20),
            holdout_pct=int(os.environ.get("LEARNER_HOLDOUT_PCT") or 5),
        ),
        sched_cfg=SchedulerConfig(
            target_reasons=reasons,
            daily_runs=int(os.environ.get("LEARNER_SOLVER_DAILY_RUNS") or 1000),
            daily_tokens=int(os.environ.get("LEARNER_SOLVER_DAILY_TOKENS") or 50_000_000),
            min_video_7d=int(os.environ.get("LEARNER_MIN_VIDEO_7D") or 1),
        ),
    )
    return ap, [hub, llm]


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LEARNER_LOG_LEVEL") or "INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # One line per HTTP call (several per Solver step) buries the lines
    # that matter; the Solver's own result line says what happened.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("learner")
    import redis.asyncio as aioredis

    r = aioredis.from_url(_env("LEARNER_REDIS_URL"), decode_responses=True)
    ledger = await MariaLedger.connect(
        host=_env("LEARNER_MARIADB_HOST"),
        port=int(os.environ.get("LEARNER_MARIADB_PORT") or 3306),
        user=_env("LEARNER_MARIADB_USER"),
        password=_env("LEARNER_MARIADB_PASSWORD"),
        db=_env("LEARNER_MARIADB_DATABASE"),
    )
    await ledger.ensure_schema()
    cfg = VerifyConfig(min_video_s=float(os.environ.get("LEARNER_MIN_VIDEO_S") or 60))
    consumer = Consumer(
        r, ledger, name=os.environ.get("LEARNER_NAME") or socket.gethostname(), cfg=cfg,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows dev box
            pass
    log.info("learner up: verifier v%s, min_video_s=%s", VERSION, cfg.min_video_s)
    autopilot, closers = build_autopilot(ledger, r)
    tasks = [
        asyncio.create_task(consumer.run(stop)),
        asyncio.create_task(_prune_loop(ledger, stop)),
        asyncio.create_task(_stats_loop(consumer, stop, autopilot)),
    ]
    if autopilot is not None:
        autopilot.consumer_stats = lambda: consumer.stats
        await autopilot.store.ensure_schema()
        log.info("autopilot on: solver=%s", autopilot.cfg.solver_enabled)
        tasks.append(asyncio.create_task(autopilot.run(stop)))
    try:
        await tasks[0]
    finally:
        stop.set()
        for t in tasks[1:]:
            t.cancel()
        for c in closers:
            await c.close()
        await ledger.close()
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
