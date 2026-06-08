#!/usr/bin/env python3
"""Capacity-aware batch fetch -- paprika-client example.

Fetches a list of URLs through the paprika fleet, AUTOMATICALLY throttled to the
fleet's *recommended* concurrency (so you don't saturate it / hit 503s), then
prints how many images each page yielded.

The point of this sample is the pair:

    cap = await cli.capacity()              # ask the fleet how busy it can get
    results = await cli.fetch_many(urls)    # fan out, capped at recommended_concurrency

Run:

    pip install -e ./client/python
    export PAPRIKA_HUB=http://10.10.50.34:8000        # or pass --hub
    python client/python/examples/capacity_fetch.py https://example.com https://en.wikipedia.org/wiki/Cat
    # no URLs given -> a small built-in demo list

    # override the auto cap, or just print the recommended number:
    python .../capacity_fetch.py --concurrency 20 <urls...>
    python .../capacity_fetch.py --show-capacity
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from paprika_client import async_paprika

DEMO_URLS = [
    "https://en.wikipedia.org/wiki/Web_scraping",
    "https://en.wikipedia.org/wiki/Cat",
    "https://en.wikipedia.org/wiki/Tokyo",
]


async def run(hub: str | None, urls: list[str], concurrency: int | None) -> int:
    async with async_paprika.connect(hub) as cli:
        # 1) Ask the fleet how many concurrent fetches it can comfortably take.
        cap = await cli.capacity()
        print(
            "fleet capacity:"
            f"  max={cap['max_concurrent']}"
            f"  recommended={cap['recommended_concurrency']}"
            f"  (load_factor={cap['load_factor']})"
            f"  available_now={cap['available']}"
            f"  running={cap['running']}"
            f"  util={cap['utilization_pct']}%"
        )

        cap_n = concurrency if concurrency is not None else cap["recommended_concurrency"]
        print(
            f"\nfetching {len(urls)} url(s), concurrency={cap_n}"
            f"{' (= recommended)' if concurrency is None else ' (override)'}\n"
        )

        # 2) Progress callback -- fired as each fetch finishes (any order).
        done = 0

        def on_result(url: str, result) -> None:
            nonlocal done
            done += 1
            if isinstance(result, Exception):
                print(f"  [{done}/{len(urls)}] FAIL       {url}  ({result})")
            else:
                print(f"  [{done}/{len(urls)}] {str(result.get('status') or '?'):10s} {url}")

        # 3) Batch fetch. concurrency=None -> auto = recommended_concurrency.
        #    Extra kwargs flow straight into cli.fetch() (scroll=, use_profile=,
        #    download_video=, timeout=, ...). Failures are returned, not raised.
        results = await cli.fetch_many(
            urls,
            concurrency=concurrency,   # None => auto-throttle to the fleet
            on_result=on_result,
            scroll=True,
            timeout=300.0,
        )

        # 4) Collect image assets for everything that completed.
        print("\nresults:")
        ok = 0
        for url, res in zip(urls, results):
            if isinstance(res, Exception):
                print(f"  FAIL  {url}: {res}")
                continue
            if res.get("status") != "completed":
                print(f"  {res.get('status')}  {url}")
                continue
            images = await cli.job_assets(res.get("job_id"), kind="image")
            ok += 1
            print(f"  OK    {url}  -> {len(images)} image(s)")

        print(f"\n{ok}/{len(urls)} completed.")
    return 0


async def show_capacity(hub: str | None) -> int:
    async with async_paprika.connect(hub) as cli:
        import json

        print(json.dumps(await cli.capacity(), indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Capacity-aware batch fetch demo.")
    ap.add_argument("urls", nargs="*", help="URLs to fetch (default: a small demo list).")
    ap.add_argument(
        "--hub",
        default=os.environ.get("PAPRIKA_HUB"),
        help="Hub base URL (default: $PAPRIKA_HUB or http://localhost:8000).",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Override parallelism (default: the fleet's recommended_concurrency).",
    )
    ap.add_argument(
        "--show-capacity",
        action="store_true",
        help="Just print GET /workers/capacity and exit.",
    )
    args = ap.parse_args()

    if args.show_capacity:
        return asyncio.run(show_capacity(args.hub))
    return asyncio.run(run(args.hub, args.urls or DEMO_URLS, args.concurrency))


if __name__ == "__main__":
    sys.exit(main())
