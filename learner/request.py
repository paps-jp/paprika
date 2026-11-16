"""Ask the running learner to work on one URL now.

    docker exec learner-learner-1 python -m learner.request URL [--klass K] [--no-wait]

The request goes on a Redis list the autopilot reads before it picks
anything itself (``learner/autopilot.py`` ``manual_tick``); the run is the
ordinary Solver run -- same limits in code, same replay test on other URLs
of the key before anything becomes a playbook -- only the choice of URL is
yours. It skips dormancy, the daily budget and the "this key never produced
a video" filter.

Every session of the run is a row of ``learn_solver_runs`` (reason starts
with ``[manual]``) and shows on the admin page (#ai -> 学習ループ).

With ``--actions FILE`` you hand in the steps yourself (you worked out by
hand what the page needs). No model runs: the steps are replayed on the URL
and on two other URLs of its key, and become a candidate playbook only when
the Verifier calls all three a success -- the same bar as for steps the
Solver finds. From there the trial against a control group decides.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid

from learner.autopilot import REQUESTS_KEY, RESULT_KEY
from learner.compiler import check_manual_actions
from learner.scheduler import KNOWN_REASONS


def build_request(url: str, klass: str = "", *, actions: list | None = None,
                  now: float | None = None, rid: str | None = None) -> dict:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    if klass and klass not in KNOWN_REASONS:
        raise ValueError(f"--klass must be one of {', '.join(KNOWN_REASONS)}")
    req = {"id": rid or uuid.uuid4().hex[:12], "url": url, "klass": klass,
           "ts": float(now if now is not None else time.time())}
    if actions is not None:
        why = check_manual_actions(actions)
        if why:
            raise ValueError(why)
        req["actions"] = actions
    return req


async def submit(redis, req: dict) -> None:
    await redis.rpush(REQUESTS_KEY, json.dumps(req, ensure_ascii=False))


async def wait_result(redis, rid: str, *, timeout_s: float, poll_s: float = 3.0, sleep=asyncio.sleep) -> dict | None:
    deadline = time.monotonic() + timeout_s
    while True:
        raw = await redis.get(RESULT_KEY + rid)
        if raw:
            return json.loads(raw)
        if time.monotonic() >= deadline:
            return None
        await sleep(poll_s)


async def _main() -> int:
    ap = argparse.ArgumentParser(prog="python -m learner.request", description=__doc__.split("\n\n")[0])
    ap.add_argument("url")
    ap.add_argument("--klass", default="", help="what ordinary fetches of this page run into: "
                    + ", ".join(KNOWN_REASONS) + " (default: taken from the ledger)")
    ap.add_argument("--actions", default="", metavar="FILE",
                    help="a JSON file with steps you wrote ('-' = stdin): a list of "
                         '{"kind": "wait", "seconds": N} / {"kind": "click", "selector": CSS} / '
                         '{"kind": "evaluate", "expression": JS}. The Solver is skipped; the steps go '
                         "straight to the replay test on this URL and two others of its key, and become "
                         "a candidate playbook only if all three succeed")
    ap.add_argument("--no-wait", action="store_true", help="queue the request and return")
    ap.add_argument("--timeout", type=float, default=2700.0, help="seconds to wait for the result (2700)")
    args = ap.parse_args()
    actions = None
    if args.actions:
        try:
            text = sys.stdin.read() if args.actions == "-" else open(args.actions, encoding="utf-8").read()
            actions = json.loads(text)
        except (OSError, ValueError) as e:
            print(f"error: cannot read --actions: {e}")
            return 2
    try:
        req = build_request(args.url, args.klass, actions=actions)
    except ValueError as e:
        print(f"error: {e}")
        return 2
    import redis.asyncio as aioredis

    r = aioredis.from_url(os.environ["LEARNER_REDIS_URL"], decode_responses=True)
    try:
        await submit(r, req)
        print(f"queued: id={req['id']} url={req['url']}")
        if args.no_wait:
            return 0
        print("waiting for the learner (a run takes from seconds to minutes; a replay test up to 30 more) ...")
        res = await wait_result(r, req["id"], timeout_s=args.timeout)
        if res is None:
            print("no result yet; the request stays queued. See #ai -> 学習ループ for the run.")
            return 1
        print(f"result: {res['result']}   key: {res['host']} {res['template']}   "
              f"class: {res['klass']}   sessions: {res['sessions']}")
        for u in res.get("urls") or []:
            print(f"  opened: {u}")
        return 0
    finally:
        await r.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
