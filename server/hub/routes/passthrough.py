"""URL pass-through catch-all: GET /https://example.com -> POST /jobs.

Quick one-line job submission from a browser address bar or curl::

    GET /https://example.com?scroll=1&play_videos=1&max_wait=120

Equivalent to a JSON-bodied POST /jobs with those options. Lives in
its own module because the catch-all ``/{full_url:path}`` route MUST
be registered LAST -- otherwise it shadows every other route. app.py
mounts this router at the very end (after include_router calls for
every other module + after _apply_route_tags has run).

NOTE: if the target URL itself has a query string, URL-encode it
(``/https://x.com/y%3Ffoo%3Dbar``) or use POST /jobs with a JSON body.
"""

from __future__ import annotations

import re as _re

from fastapi import APIRouter, HTTPException, Request

from server.protocol import JobOptions, JobRequest

router = APIRouter(tags=["System"])


_PASSTHRU_BOOLS = {"scroll", "headless"}
_PASSTHRU_INTS = {"wait_seconds", "scroll_step", "scroll_max"}
_PASSTHRU_FLOATS = {
    "settle_seconds",
    "idle_seconds",
    "max_wait_seconds",
    "scroll_early_after",
    "post_click_seconds",
}
_PASSTHRU_STRS = {
    "cookies_from",
    "referer",
    "attach",
    "clone_chrome_profile",
}
# Friendly short-name aliases for query params
_PASSTHRU_ALIASES = {
    "wait": "wait_seconds",
    "settle": "settle_seconds",
    "idle": "idle_seconds",
    "max_wait": "max_wait_seconds",
    "post_click": "post_click_seconds",
    "scroll_early": "scroll_early_after",
}


def _truthy(v: str) -> bool:
    return v.lower() in ("1", "true", "yes", "on")


@router.get("/{full_url:path}")
async def quick_fetch(full_url: str, request: Request):
    # Accept either /https://x  or /https:/x  (some clients/proxies collapse //)
    m = _re.match(r"^(https?):/+(.*)$", full_url)
    if not m:
        raise HTTPException(404, "not found")
    target = f"{m.group(1)}://{m.group(2)}"

    # Build options from query params
    qp = request.query_params
    opts_data: dict = {}
    for raw_key, value in qp.multi_items():
        key = _PASSTHRU_ALIASES.get(raw_key, raw_key)
        if key in _PASSTHRU_BOOLS:
            opts_data[key] = _truthy(value)
        elif key in _PASSTHRU_INTS:
            try:
                opts_data[key] = int(value)
            except ValueError:
                pass
        elif key in _PASSTHRU_FLOATS:
            try:
                opts_data[key] = float(value)
            except ValueError:
                pass
        elif key in _PASSTHRU_STRS:
            opts_data[key] = value

    job_req = JobRequest(url=target, options=JobOptions(**opts_data))
    # create_job moved to routes/jobs.py in #2B-G3. Lazy import keeps
    # the catch-all router cleanly mountable last (jobs router is
    # mounted earlier in app.py).
    from server.hub.routes.jobs import create_job

    return await create_job(job_req, request)
