"""The learner's view of the hub: its public HTTP API, nothing else.

The Solver drives a browser through the same session API an SDK client
uses, and tests a compiled playbook by submitting ordinary fetch jobs. The
learner therefore depends on the hub's documented surface only -- no
imports from the hub's internals, no shared state (design §15.2).

Failures are split in two, because only one of them says anything about
the site being worked on:

    HubUnavailable   the fleet / hub could not serve the request right now
                     (no free lane, 5xx, timeout, connection error). Infra:
                     never counted against the URL or the key.
    HubError         the hub answered and said no (4xx).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


class HubUnavailable(Exception):
    """Infrastructure could not serve the call. Not a learning signal."""


class HubError(Exception):
    """The hub rejected the call."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class Hub:
    def __init__(self, base_url: str, *, client=None, token: str | None = None,
                 timeout_s: float = 60.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token or ""
        self._timeout = timeout_s
        self._client = client
        self._own_client = client is None

    async def _http(self):
        if self._client is None:
            import httpx  # lazy: tests inject a fake client

            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, path: str, *, json_body: dict | None = None,
                    params: dict | None = None, timeout: float | None = None) -> Any:
        client = await self._http()
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            resp = await client.request(
                method, self._base + path, json=json_body, params=params,
                headers=headers, timeout=timeout or self._timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # connection refused / reset / timeout
            raise HubUnavailable(f"{method} {path}: {type(e).__name__}: {e}") from e
        code = int(resp.status_code)
        if code >= 500 or code == 429:
            raise HubUnavailable(f"{method} {path}: HTTP {code}: {_short(resp)}")
        if code >= 400:
            raise HubError(f"{method} {path}: HTTP {code}: {_short(resp)}", code)
        try:
            return resp.json()
        except Exception:
            return None

    # ---- sessions (the Solver's hands) ---------------------------------

    async def open_session(self, url: str, *, idle_ttl_s: int = 180,
                           absolute_ttl_s: int = 900, retries: int = 5,
                           retry_delay_s: float = 4.0) -> str:
        """Open a browser session. A 503 here usually means "the hub that
        answered has no free lane" rather than "the fleet is full" (a hub
        only looks at its own workers plus one peer), so it is retried a
        few times -- each attempt lands on another hub -- before the call
        is reported as infrastructure trouble."""
        last: Exception | None = None
        for i in range(max(1, retries)):
            try:
                out = await self._call("POST", "/sessions", json_body={
                    "initial_url": url, "idle_ttl_s": idle_ttl_s, "absolute_ttl_s": absolute_ttl_s,
                })
            except HubUnavailable as e:
                last = e
                if i + 1 < retries:
                    await asyncio.sleep(retry_delay_s)
                continue
            sid = (out or {}).get("session_id")
            if not sid:
                raise HubUnavailable(f"POST /sessions returned no session_id: {str(out)[:120]}")
            return str(sid)
        raise HubUnavailable(str(last))

    async def close_session(self, sid: str) -> None:
        try:
            await self._call("DELETE", f"/sessions/{sid}", timeout=30.0)
        except (HubUnavailable, HubError):
            pass  # the session's own TTL reaps it

    async def outline(self, sid: str) -> str:
        out = await self._call("GET", f"/sessions/{sid}/outline")
        res = (out or {}).get("result")
        return res if isinstance(res, str) else ""

    async def evaluate(self, sid: str, expression: str) -> Any:
        """Run JS; a JSON string result is decoded."""
        out = await self._call("POST", f"/sessions/{sid}/evaluate",
                               json_body={"expression": expression})
        res = (out or {}).get("result")
        if isinstance(res, str):
            try:
                return json.loads(res)
            except Exception:
                return res
        return res

    async def click(self, sid: str, selector: str) -> dict:
        return await self._call("POST", f"/sessions/{sid}/click",
                                json_body={"selector": selector}) or {}

    async def scroll(self, sid: str, direction: str = "down", pixels: int = 800) -> dict:
        return await self._call("POST", f"/sessions/{sid}/scroll",
                                json_body={"direction": direction, "pixels": pixels}) or {}

    # ---- jobs (replaying a compiled playbook) --------------------------

    async def submit_fetch(self, url: str, *, actions: list | None, learn: dict,
                           download_video: bool = True, retries: int = 20) -> str:
        """Submit one fetch job that replays ``actions`` (None = plain
        fetch, no recipe of any kind). Retries the fleet-at-capacity 503,
        and the 409 the hub answers while the same URL is already queued or
        running for someone else (its same-URL dedup): that says "wait",
        nothing about the site, so it ends as HubUnavailable -- never as a
        failed replay charged to the key.

        The 503 is common and per hub: on a busy day more than half of all
        submissions get it while other hubs still have free lanes (seen
        live 2026-11-12: six in a row threw away a finished Solver trace).
        Every attempt lands on another hub, so retry often and for long --
        up to about eight minutes; a replay is never urgent."""
        options: dict = {"mode": "fetch", "download_video": bool(download_video), "learn": learn}
        if actions:
            options["fetch_recipe"] = {
                "pattern": "*", "description": "learner replay test",
                "actions": actions, "created_by": "learner",
            }
        else:
            options["fetch_strategy"] = "normal"
        delay = 3.0
        last: Exception | None = None
        for _ in range(max(1, retries)):
            try:
                out = await self._call("POST", "/jobs", json_body={"url": url, "options": options})
                jid = (out or {}).get("job_id")
                if jid:
                    return str(jid)
                raise HubUnavailable(f"POST /jobs returned no job_id: {str(out)[:120]}")
            except HubUnavailable as e:
                last = e
            except HubError as e:
                if e.status != 409:
                    raise
                last = e
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 30.0)
        raise HubUnavailable(str(last))


def _short(resp) -> str:
    try:
        return str(resp.text)[:160]
    except Exception:
        return ""
