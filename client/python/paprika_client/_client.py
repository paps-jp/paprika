"""PaprikaClient + session context manager."""
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any, Optional

import httpx


# When the script is being run by paprika-runner under a codegen-loop
# job, the orchestrator sets PAPRIKA_JOB_ID. The SDK forwards it to
# the hub so the resulting Session shows up in /jobs/{id}/sessions
# (admin UI live panel). Locally-run scripts leave it unset.
_AMBIENT_PARENT_JOB_ID = os.environ.get("PAPRIKA_JOB_ID") or None

from ._page import Page

# Forward reference for the "Session" annotations in _SessionHandle /
# open_session. We can't import Session at module top because Session
# is a subclass of Page that lives in _page.py, and _page.py uses
# constructs that benefit from PaprikaClient being available -- the
# real import happens lazily inside open_session(). The TYPE_CHECKING
# guard lets static analysers (ruff F821, mypy) resolve the string
# annotation without a runtime cost.
if TYPE_CHECKING:
    from ._page import Session


class _AttrDict(dict):
    """A ``dict`` subclass that exposes keys as attributes.

    Existing ``a['name']`` and ``a.get('source_url')`` keep working
    (full dict protocol) while also allowing ``a.name`` / ``a.source_url``
    for a cleaner look in scripts and docs.  Attribute access is purely
    syntactic sugar; all mutations still go through the normal dict API
    so ``json.dumps(a)`` / ``{**a}`` work unchanged.
    """
    __slots__ = ()

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value):
        self[name] = value

    def __delattr__(self, name: str):
        try:
            del self[name]
        except KeyError:
            raise AttributeError(name) from None


def _as_attr(obj):
    """Recursively wrap dicts (including nested ones) as ``_AttrDict``
    so ``resp.progress.assets_saved`` works, not just ``resp.status``.
    Lists are walked but non-dict elements are left untouched."""
    if isinstance(obj, dict):
        return _AttrDict({k: _as_attr(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_as_attr(v) for v in obj]
    return obj


class PaprikaError(Exception):
    """Raised when the hub returns an error or an unexpected payload.

    Attributes:
      status_code: The HTTP status code from the hub response, when
        the error came from an HTTP-level failure. ``None`` for
        transport errors (network drop, connection refused, etc.)
        and for client-side validation errors raised before any
        request went out. Lets retry logic branch on the response
        kind without parsing the message string::

            try:
                async with cli.session(...) as page:
                    ...
            except pap.PaprikaError as e:
                if e.status_code == 502:
                    # transient -- retry
                ...
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code: Optional[int] = status_code


class _SessionHandle:
    """Dual-mode return value of ``PaprikaClient.session(url, ...)``.

    Both forms work and end up calling :meth:`PaprikaClient.open_session`
    with the same kwargs::

        # Form A: ``async with`` (auto-close on block exit)
        async with cli.session("https://x.com") as sess:
            ...

        # Form B: direct ``await`` (no auto-close)
        sess = await cli.session("https://x.com")
        ...
        await sess.close()   # or sess.detach()

    The dual-mode is achieved by implementing both
    ``__await__`` (returns the Session) and ``__aenter__`` /
    ``__aexit__`` (also returns the Session, plus auto-close unless
    detached). Either path runs the same SDK code; only the cleanup
    semantics differ.
    """

    __slots__ = ("_client", "_kwargs", "_session")

    def __init__(self, client: "PaprikaClient", kwargs: dict) -> None:
        self._client = client
        self._kwargs = kwargs
        self._session: Optional["Session"] = None  # type: ignore[name-defined]

    def __await__(self):
        # Form B: ``sess = await cli.session(...)``. Plain awaitable;
        # no cleanup is attached by the SDK so the operator owns the
        # session's lifetime.
        return self._client.open_session(**self._kwargs).__await__()

    async def __aenter__(self):
        # Form A: ``async with cli.session(...) as sess:``. Stash the
        # opened session so __aexit__ can close it on block exit.
        self._session = await self._client.open_session(**self._kwargs)
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        sess = self._session
        self._session = None
        if sess is None:
            return
        # Skip cleanup when detach() flipped the flag mid-block --
        # operator's responsibility now.
        if sess._detached:
            return
        try:
            await sess.close()
        except Exception:
            # Best-effort cleanup; session may already be gone if the
            # worker disconnected mid-operation.
            pass


class PaprikaClient:
    """An async HTTP client bound to one paprika hub.

    Use as an async context manager so the underlying ``httpx`` client
    is cleaned up cleanly::

        async with async_paprika.connect() as cli:
            async with cli.session() as page:
                ...

    Or manually::

        cli = async_paprika.connect()
        await cli.__aenter__()
        try:
            page = await cli.open_session(initial_url="...")
            ...
            await page.close()
        finally:
            await cli.__aexit__(None, None, None)

    The hub URL resolves from PAPRIKA_HUB env var (set by the runner
    sandbox) or falls back to http://localhost:8000. Pass an explicit
    URL only when you need to target a hub other than the default --
    e.g. ``async_paprika.connect("http://paprika.lan")``.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        token: Optional[str] = None,
        timeout: float = 180.0,
    ) -> None:
        # Resolve the hub URL in this order:
        #   1. explicit base_url argument
        #   2. PAPRIKA_HUB environment variable
        #   3. http://localhost:8000  (local-dev fallback)
        # The middle step is the important one: paprika-runner sandboxes
        # always have PAPRIKA_HUB injected by the orchestrator
        # (server/hub/runner.py spawns runner containers with
        # ``-e PAPRIKA_HUB=http://hub:8000``), so a script that just
        # says ``async_paprika.connect()`` -- no argument -- Just Works
        # in both runner and local-dev contexts. This is the strongly
        # recommended form for scripts that get reused across both
        # places: hardcoded URLs are the single biggest source of
        # "connect failed" bugs in rerun-mode jobs.
        if base_url is None:
            base_url = (
                os.environ.get("PAPRIKA_HUB")
                or "http://localhost:8000"
            )
        self._base_url = base_url.rstrip("/")
        # Resolve the bearer token in the same spirit as base_url:
        #   1. explicit token argument
        #   2. PAPRIKA_API_KEY / PAPRIKA_TOKEN environment variable
        #   3. None (anonymous — works against hubs in auth_mode off/optional)
        # So a script / runner sandbox / CLI invocation picks up a key from
        # the env automatically and keeps working once a hub flips to
        # auth_mode=enforce, without any code change. The token is sent as
        # ``Authorization: Bearer <token>`` (see __aenter__).
        if token is None:
            token = (
                os.environ.get("PAPRIKA_API_KEY")
                or os.environ.get("PAPRIKA_TOKEN")
                or None
            )
        self._token = token
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None

    # -- context manager ----------------------------------------------------

    async def __aenter__(self) -> "PaprikaClient":
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=headers,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- public API ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    async def health(self) -> dict:
        """GET /health -- handy for smoke tests."""
        return await self._json("GET", "/health")

    async def list_workers(self) -> list[dict]:
        data = await self._json("GET", "/workers")
        return data.get("workers", [])

    async def list_sessions(self) -> list[dict]:
        data = await self._json("GET", "/sessions")
        return data.get("sessions", [])

    async def jobs_summary(self) -> dict:
        """GET /jobs/summary — dashboard-shaped overview of the job store.

        Returns a dict with::

            {
              "as_of":     "2026-06-01T22:45:00Z",   # ISO UTC
              "total":     3154,                      # all jobs in store
              "by_status": {
                "queued":    1,
                "running":   6,
                "completed": 1254,
                "failed":    1881,
                "cancelled": 12,
              },
              "by_mode": {
                "fetch":         3010,
                "codegen-loop":   103,
                "rerun":            4,
              },
              "recent_1h":  {"created": N, "by_status": {...}, "success_rate": 0.93},
              "recent_24h": {"created": M, "by_status": {...}, "success_rate": 0.91},
              "active": {
                "queued":  1,
                "running": 6,
                "running_preview": [
                  {"job_id": "...", "url": "...", "mode": "fetch",
                   "worker_id": "...", "lane_idx": 0,
                   "started_at": "...", "age_s": 412.3},
                  ...up to 5 rows
                ],
              },
            }

        ``success_rate`` is computed over terminal jobs only
        (completed + failed); pending / running jobs aren't counted
        in the denominator. ``None`` when no terminal jobs in the
        window. Typical uses::

            s = await cli.jobs_summary()
            if s["by_status"]["queued"] > 100:
                # queue backpressure: pause submission
                ...
            recent = s["recent_1h"]
            if recent["created"] > 5 and (recent["success_rate"] or 1.0) < 0.7:
                # alert: failure rate spike
                ...

        Cheap: the hub computes everything from store-side aggregates
        (SQL ``GROUP BY status`` when MariaDB-backed) in <50ms at
        100k+ rows, plus a 2-second server memo cache. Safe to poll
        every few seconds for live dashboards.
        """
        return await self._json("GET", "/jobs/summary")

    # -- jobs ---------------------------------------------------------------
    #
    # The session API (above) drives a live browser. These wrap the hub's
    # *job* surface instead: submit a fetch / codegen job, poll it, read
    # its results & captured assets. Job artifacts outlive the run, so
    # this is how you grab images after a one-shot crawl rather than from
    # an interactive session.

    async def create_job(self, url: str, **options: Any) -> dict:
        """POST /jobs -- submit a job and return the initial ``JobInfo``.

        ``options`` are merged into ``JobOptions`` verbatim, e.g.
        ``mode="fetch"`` (default server-side), ``scroll=True``,
        ``use_profile="..."``, ``goal="..."`` (codegen / vision modes).
        Returns immediately; the job runs async on a worker. Use
        :meth:`wait_job` (or :meth:`fetch`) to block until it finishes.
        """
        body: dict[str, Any] = {"url": url}
        if options:
            body["options"] = options
        return await self._json("POST", "/jobs", json=body)

    async def get_job(self, job_id: str) -> dict:
        """GET /jobs/{id} -- the current ``JobInfo`` (status, progress, …)."""
        return await self._json("GET", f"/jobs/{job_id}")

    async def list_jobs(
        self,
        *,
        offset: int = 0,
        limit: int = 0,
        status: Optional[str] = None,
        mode: Optional[str] = None,
        q: Optional[str] = None,
    ) -> dict:
        """GET /jobs -- list jobs with optional pagination and filtering.

        Args:
          offset:  Skip this many entries (default 0).
          limit:   Max entries to return (0 = all, max 500 server-side).
          status:  Filter by status. Comma-separated for multiple,
                   e.g. ``"completed,failed"``.
          mode:    Filter by job mode, e.g. ``"fetch"`` or
                   ``"codegen-loop"``.
          q:       Case-insensitive substring match against job URL.

        Returns an ``_AttrDict`` with the paginated envelope::

            {
                "total":  457,
                "count":  50,
                "offset": 0,
                "limit":  50,
                "jobs":   [...]   # list of JobInfo dicts
            }

        Access as ``resp.jobs``, ``resp.total``, etc.
        """
        params: dict[str, Any] = {}
        if offset:
            params["offset"] = offset
        if limit:
            params["limit"] = limit
        if status:
            params["status"] = status
        if mode:
            params["mode"] = mode
        if q:
            params["q"] = q
        data = await self._json("GET", "/jobs", params=params)
        # Backward compat: if hub is old and returns a bare list, wrap it
        if isinstance(data, list):
            return _as_attr({"total": len(data), "count": len(data),
                             "offset": 0, "limit": 0, "jobs": data})
        return data

    async def job_result(self, job_id: str) -> dict:
        """GET /jobs/{id}/result -- the ``JobResult`` (assets list, links,
        final url, …). 404s until the job has produced a result."""
        return await self._json("GET", f"/jobs/{job_id}/result")

    async def cancel_job(self, job_id: str) -> dict:
        """POST /jobs/{id}/cancel -- stop an in-flight job. Idempotent."""
        return await self._json("POST", f"/jobs/{job_id}/cancel")

    async def delete_job(self, job_id: str) -> dict:
        """DELETE /jobs/{id} -- remove the job and its on-disk artifacts."""
        return await self._json("DELETE", f"/jobs/{job_id}")

    async def wait_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ) -> dict:
        """Poll GET /jobs/{id} until it reaches a terminal state
        (``completed`` / ``failed`` / ``cancelled``) and return the final
        ``JobInfo``. Raises ``TimeoutError`` if it doesn't finish in time."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            info = await self.get_job(job_id)
            if info.get("status") in ("completed", "failed", "cancelled"):
                return info
            if loop.time() > deadline:
                raise TimeoutError(
                    f"job {job_id} did not finish within {timeout}s "
                    f"(last status: {info.get('status')})"
                )
            await asyncio.sleep(poll_interval)

    async def fetch(
        self,
        url: str,
        *,
        wait: bool = True,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
        scroll: bool = True,
        download_video: bool = False,
        **options: Any,
    ) -> dict:
        """Convenience: submit a fetch-mode job and (by default) wait for
        it to finish. Returns the final ``JobInfo`` dict.

        ``scroll`` defaults to ``True`` so lazy-loaded images fire. Extra
        kwargs flow into ``JobOptions`` (``use_profile=`` / ``cookies_from=``
        for login-gated sites, ``scroll_max=`` for long pages). Pair with
        :meth:`job_images` to collect the captured images::

            job = await cli.fetch("https://example.com/article")
            imgs = await cli.job_images(job.job_id)

        ``download_video`` opts into the video-download flow: the worker
        enables iframe + nested-iframe network tracing from session start
        so HLS/DASH manifest URLs are reliably captured, and a final
        yt-dlp pass runs if the operator (or generated script) calls
        ``page.download_video()``. Default ``False`` -- keep video-DL
        machinery dormant for cheap data-extraction crawls.
        """
        info = await self.create_job(
            url,
            mode="fetch",
            scroll=scroll,
            download_video=download_video,
            **options,
        )
        if not wait:
            return info
        return await self.wait_job(
            info.job_id, poll_interval=poll_interval, timeout=timeout,
        )

    # -- assets (image / media retrieval) -----------------------------------

    async def job_assets(
        self,
        job_id: str,
        *,
        kind: Optional[str] = None,
        absolute: bool = True,
        details: bool = False,
    ):
        """GET /jobs/{id}/assets.json -- the assets captured by a job.

        Args:
          kind:     Filter by ``"image"`` / ``"video"`` / ``"audio"`` /
                    ``"other"``; ``None`` (default) returns every kind.
          absolute: ``True`` -> ready-to-GET URLs; ``False`` -> relative
                    ``href``.
          details:  ``False`` -> ``list[str]`` of URLs; ``True`` ->
                    ``list[_AttrDict]`` with full metadata (size /
                    source_url / mime / …).  Each row supports both
                    ``a['name']`` and ``a.name`` access.
        """
        data = await self._json("GET", f"/jobs/{job_id}/assets.json")
        items = [
            it for it in (data.get("items") or [])
            if kind is None or it.get("kind") == kind
        ]
        prefix = self._base_url if absolute else ""
        if details:
            out = []
            for it in items:
                row = _AttrDict(it)
                row.url = prefix + it.href
                out.append(row)
            return out
        return [prefix + it.href for it in items]

    async def job_images(self, job_id: str, **kwargs: Any):
        """Shorthand for :meth:`job_assets` with ``kind="image"``."""
        kwargs.setdefault("kind", "image")
        return await self.job_assets(job_id, **kwargs)

    async def download_job_assets(
        self,
        job_id: str,
        dest_dir: str,
        *,
        kind: Optional[str] = "image",
    ) -> list[str]:
        """Download a job's captured assets to ``dest_dir`` and return the
        written file paths. Defaults to images only."""
        rows = await self.job_assets(job_id, kind=kind, absolute=False, details=True)
        os.makedirs(dest_dir, exist_ok=True)
        paths: list[str] = []
        for it in rows:
            blob = await self._bytes("GET", it.href)
            dest = os.path.join(dest_dir, it.name)
            with open(dest, "wb") as f:
                f.write(blob)
            paths.append(dest)
        return paths

    async def open_session(
        self,
        initial_url: Optional[str] = None,
        *,
        worker_id: Optional[str] = None,
        lane_hint: Optional[int] = None,
        idle_ttl_s: Optional[int] = None,
        absolute_ttl_s: Optional[int] = None,
        parent_job_id: Optional[str] = None,
        use_profile: Optional[str] = None,
        auto_reopen: bool = True,
    ) -> "Session":
        """Reserve a Lane and return a :class:`Session` bound to it.

        ``initial_url`` accepts both positional and keyword form so
        ``cli.open_session("https://example.com")`` reads naturally
        alongside ``cli.session("https://example.com")``.

        ``parent_job_id`` defaults to the ``PAPRIKA_JOB_ID`` env var so
        scripts run under paprika-runner automatically tag their
        sessions with the parent job. Pass ``parent_job_id=""`` to
        opt out explicitly. Locally-run scripts can leave it alone.

        Returns a :class:`Session` (which is-a :class:`Page`, so every
        single-tab call style keeps working). Don't forget to
        ``await session.close()`` when done, or prefer the
        :meth:`session` factory which is both ``await``-able and
        usable as an ``async with`` context manager.
        """
        body: dict[str, Any] = {}
        if initial_url is not None:
            body["initial_url"] = initial_url
        if worker_id is not None:
            body["worker_id"] = worker_id
        if lane_hint is not None:
            body["lane_hint"] = lane_hint
        if idle_ttl_s is not None:
            body["idle_ttl_s"] = idle_ttl_s
        if absolute_ttl_s is not None:
            body["absolute_ttl_s"] = absolute_ttl_s
        effective_pjid = parent_job_id if parent_job_id is not None else _AMBIENT_PARENT_JOB_ID
        if effective_pjid:
            body["parent_job_id"] = effective_pjid
        if use_profile:
            # Name of a Chrome profile previously uploaded to the hub
            # via ``paprika-client upload-profile``. The hub fetches
            # the tarball into the lane's user-data-dir before the
            # browser starts so the session opens with the operator's
            # cookies / logins / localStorage already in place.
            body["use_profile"] = use_profile

        info = await self._json("POST", "/sessions", json=body)
        # Import here to avoid the _page <-> _client circular at module
        # import time.
        from ._page import Session

        # Stash the open args on the Session so its auto-reopen-on-404
        # path (see _SessionReopenProxy in _page.py) can recreate the
        # same shape of session after a hub/worker restart: same
        # profile, same parent job (= same gallery), same initial URL,
        # same lane pin if any. ``parent_job_id`` re-uses the
        # resolved value (env fallback or explicit) so a reopen
        # doesn't suddenly land in a different gallery.
        open_kwargs: dict = {}
        if initial_url is not None:
            open_kwargs["initial_url"] = initial_url
        if worker_id is not None:
            open_kwargs["worker_id"] = worker_id
        if lane_hint is not None:
            open_kwargs["lane_hint"] = lane_hint
        if idle_ttl_s is not None:
            open_kwargs["idle_ttl_s"] = idle_ttl_s
        if absolute_ttl_s is not None:
            open_kwargs["absolute_ttl_s"] = absolute_ttl_s
        if effective_pjid:
            open_kwargs["parent_job_id"] = effective_pjid
        if use_profile:
            open_kwargs["use_profile"] = use_profile
        return Session(
            self, info,
            open_kwargs=open_kwargs,
            auto_reopen=auto_reopen,
        )

    def session(self, initial_url: Optional[str] = None, **kwargs) -> "_SessionHandle":
        """Open a paprika session against an available Lane.

        Dual-mode: the returned handle is both ``await``-able and
        usable as an ``async with`` context manager. Pick whichever
        matches your cleanup intent::

            # Auto-close on block exit (recommended for transient scripts).
            async with cli.session("https://example.com") as sess:
                await sess.capture("snap")

            # Direct await -- session stays alive until you close /
            # detach it explicitly. Useful when handing off to an
            # operator via noVNC at end of script.
            sess = await cli.session("https://example.com")
            try:
                await sess.capture("snap")
                handoff = await sess.detach()    # bumps TTL, no auto-close
            except Exception:
                await sess.close()
                raise

        ``initial_url`` accepts both positional and keyword form.

        All other kwargs (``worker_id`` / ``lane_hint`` /
        ``idle_ttl_s`` / ``absolute_ttl_s`` / ``parent_job_id``) are
        forwarded to :meth:`open_session`.
        """
        if initial_url is not None:
            kwargs["initial_url"] = initial_url
        return _SessionHandle(self, kwargs)

    # -- internal -----------------------------------------------------------

    async def _json(self, method: str, path: str, **kwargs) -> dict:
        if self._http is None:
            raise PaprikaError(
                "PaprikaClient is not entered; use `async with` or call "
                "__aenter__() manually before issuing requests."
            )
        try:
            r = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            # When the failure is a low-level connect / DNS error,
            # the operator usually wants to know two things FAST:
            #   * which URL the SDK was actually pointed at
            #   * whether the env var the orchestrator sets agrees
            # The default httpx error ("All connection attempts
            # failed") on its own gives neither, and the common
            # failure mode -- a script hardcoding the LAN-side hub
            # name (e.g. ``https://paprika.lan``) and then running
            # inside a paprika-runner Docker container that can only
            # see ``http://hub:8000`` -- looks like a generic outage
            # if you don't already suspect it. Inline the diagnostic.
            if isinstance(e, httpx.ConnectError):
                env_hub = os.environ.get("PAPRIKA_HUB")
                hint = (
                    f" (configured base_url={self._base_url!r}"
                    + (f", PAPRIKA_HUB env={env_hub!r}" if env_hub else "")
                    + "). If this script is running inside a "
                    f"paprika-runner sandbox, the hub is reachable "
                    f"only on the Docker network -- typically "
                    f"http://hub:8000. LAN / mDNS names like "
                    f"paprika.lan won't resolve inside the runner; "
                    f"either hardcode http://hub:8000 or call "
                    f"async_paprika.connect(os.environ['PAPRIKA_HUB'])."
                )
                raise PaprikaError(
                    f"{method} {path}: transport error: {e}{hint}",
                ) from e
            raise PaprikaError(f"{method} {path}: transport error: {e}") from e
        if r.status_code >= 400:
            # Surface the hub's JSON error body if there is one.
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise PaprikaError(
                f"{method} {path}: HTTP {r.status_code}: {body}",
                status_code=r.status_code,
            )
        if r.headers.get("content-type", "").startswith("application/json"):
            return _as_attr(r.json())
        return _AttrDict({"raw": r.content})

    async def _bytes(self, method: str, path: str, **kwargs) -> bytes:
        if self._http is None:
            raise PaprikaError("PaprikaClient is not entered.")
        try:
            r = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise PaprikaError(f"{method} {path}: transport error: {e}") from e
        if r.status_code >= 400:
            raise PaprikaError(
                f"{method} {path}: HTTP {r.status_code}",
                status_code=r.status_code,
            )
        return r.content


class _AsyncPaprikaNamespace:
    """Module-level entry point: ``async_paprika.connect(...)``.

    Exists so the module-level call style mirrors ``playwright.async_api``
    where you do ``async with async_playwright() as p:``. paprika is
    simpler -- the connect call is sync and returns the client; the
    HTTP setup happens in ``__aenter__``.
    """

    @staticmethod
    def connect(
        base_url: Optional[str] = None,
        *,
        token: Optional[str] = None,
        timeout: float = 180.0,
    ) -> PaprikaClient:
        # ``base_url`` is now optional. None -> read PAPRIKA_HUB env
        # (set by paprika-runner) -> http://localhost:8000 fallback.
        # See PaprikaClient.__init__ for the full resolution chain.
        # Recommended call style for scripts that run in BOTH runner
        # and local-dev contexts:
        #     async with async_paprika.connect() as cli:
        #         ...
        return PaprikaClient(base_url, token=token, timeout=timeout)


async_paprika = _AsyncPaprikaNamespace()
