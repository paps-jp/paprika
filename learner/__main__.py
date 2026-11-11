"""``python -m learner`` -- run the outcome consumer.

Config is env-only (the learner must not read the hub's settings store:
that would be a second coupling):

    LEARNER_REDIS_URL          redis://host:6379/0          (required)
    LEARNER_MARIADB_HOST/PORT/USER/PASSWORD/DATABASE        (required)
    LEARNER_NAME               consumer name (default: hostname)
    LEARNER_MIN_VIDEO_S        verifier: minimum whole-video seconds (60)
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


async def _stats_loop(consumer: Consumer, stop: asyncio.Event) -> None:
    log = logging.getLogger("learner")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            log.info("stats %s", consumer.stats)


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LEARNER_LOG_LEVEL") or "INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
    tasks = [
        asyncio.create_task(consumer.run(stop)),
        asyncio.create_task(_prune_loop(ledger, stop)),
        asyncio.create_task(_stats_loop(consumer, stop)),
    ]
    try:
        await tasks[0]
    finally:
        stop.set()
        for t in tasks[1:]:
            t.cancel()
        await ledger.close()
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
