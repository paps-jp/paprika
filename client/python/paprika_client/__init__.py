"""paprika-client: Playwright-shape async API for a paprika hub.

Usage::

    import asyncio
    from paprika_client import async_paprika

    async def main():
        async with async_paprika.connect() as cli:
            async with cli.session(initial_url="https://example.com") as page:
                await page.goto("https://news.ycombinator.com")
                await page.locator(".athing .titleline > a").click()
                state = await page.state()
                print(state.url, state.title)
                png = await page.screenshot(path="hn.png")

    asyncio.run(main())

Prefer async, but a synchronous facade is available for callers that
can't use async/await (legacy sync code, quick scripts, notebooks)::

    from paprika_client import sync_paprika

    with sync_paprika.connect() as cli:
        with cli.session(initial_url="https://example.com") as page:
            page.goto("https://news.ycombinator.com")
            page.locator(".athing .titleline > a").first.click()
            print(page.state().url)

It bridges every call onto a background asyncio loop, so it is a 1:1
blocking mirror of the async surface (see _sync.py).

The package is a thin async HTTP wrapper around the hub's ``/sessions``
endpoints (see RFC-001 §6). It does NOT speak CDP itself; everything
flows through the hub, which then forwards to a worker, which calls
``browser_ops`` on a Chrome.

The action surface mirrors Playwright's so existing browser-automation
intuition transfers. Some Playwright methods that depend on
``page.evaluate`` (text_content, eval_on_selector, …) are not yet
implemented because the hub deliberately defers ``/evaluate`` to V2
for security reasons. Use :py:meth:`Page.outline` to read element
labels in the meantime.
"""
from __future__ import annotations

from ._client import PaprikaClient, PaprikaError, async_paprika
from ._page import (
    Candidate,
    HandoffInfo,
    Locator,
    Page,
    PaprikaActionError,
    Session,
    response_of,
)
from ._oneshot import outline, run, snapshot, state
from ._walker import DEFAULT_DENY_PATTERNS, Visit, Walker, walk
from ._sync import SyncClient, sync_paprika
from . import _actions as act

__all__ = [
    "PaprikaClient",
    "async_paprika",
    # Synchronous facade -- a blocking 1:1 mirror of the async API for
    # callers that can't / don't want to use async/await (legacy sync
    # code, simple scripts, notebooks). Bridges every call onto a
    # background asyncio loop. See _sync.py.
    "sync_paprika",
    "SyncClient",
    "Page",
    "Session",
    "HandoffInfo",
    "Locator",
    # LLM-proposed action object returned by Page.observe(). Passed
    # back to Page.click() / Page.fill() to execute the proposal.
    "Candidate",
    # Playwright-compatible HTTP Response accessor for nav-action
    # replies (page.goto / back / forward / reload / history_first).
    # Always returns a dict with {url, status, status_text, ok, headers,
    # mime}; status == 0 means "response not captured".
    "response_of",
    # Exception types -- exported here so LLM-generated scripts can
    # write `except pap.PaprikaActionError` / `except pap.PaprikaError`
    # without reaching into the private `_page` / `_client` submodules.
    # Job 6bd8209a8d15 attempt 3 died on
    #   AttributeError: module 'paprika_client' has no attribute
    #                   'PaprikaActionError'
    # because previously only the underscore-prefixed modules exposed
    # them. Re-exporting is zero-cost and makes the obvious code work.
    "PaprikaActionError",
    "PaprikaError",
    # High-level site walker (Layer 4): handles queue / dedup / filter
    # / off-scope-redirect so LLM scripts don't have to re-derive them.
    # Strongly preferred over hand-rolled crawl loops for any "visit N
    # pages of site X" task. See _walker.py for full docs.
    "walk",
    "Walker",
    "Visit",
    "DEFAULT_DENY_PATTERNS",
    # One-shot helpers (Layer 3, RFC-001 §10)
    "snapshot",
    "outline",
    "state",
    "run",
    "act",
]
__version__ = "0.1.0"
