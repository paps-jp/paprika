"""arq worker — picks up jobs from Redis, runs `core.fetcher.fetch`,
persists results back to the store, streams logs over Pub/Sub.

This module exposes:

- `run_fetch_job(ctx, job_id)` — the arq task.
- `make_worker_settings(redis_url, data_dir)` — builds an arq WorkerSettings
  class. arq inspects the class attributes (`functions`, `redis_settings`,
  `on_startup`, etc.) — it doesn't instantiate it.
- `run_inproc_worker(...)` — start an arq Worker inside an existing event loop
  (used by `--mode all`).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path

from arq.connections import RedisSettings
from arq.worker import Worker

log = logging.getLogger(__name__)

from core.fetcher import FetchOptions, clone_chrome_profile, fetch
from server.protocol import (
    AssetInfo,
    JobInfo,
    JobResult,
    JobStatus,
    YtdlpResult,
)
from server.store import JobStore, make_store

# Sentinel used on the log Pub/Sub channel to tell subscribers "all done"
DONE_SENTINEL = "__JOB_DONE__"


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _norm_optional_str(v) -> str | None:
    if v is None or not isinstance(v, str):
        return v
    s = v.strip()
    if not s or s.lower() == "string":
        return None
    return s


def _resolve_attach(spec: str) -> tuple[str, int]:
    spec = spec.strip()
    if ":" in spec:
        host_str, port_str = spec.rsplit(":", 1)
        host = host_str or "127.0.0.1"
    else:
        host = "127.0.0.1"
        port_str = spec
    try:
        port = int(port_str)
    except ValueError:
        raise ValueError(f"--attach: expected PORT or HOST:PORT (got '{spec}')")
    return host, port


def _build_fetch_options(
    job_id: str,
    info: JobInfo,
    data_dir: Path,
    log,
    cloned_dir_holder: list[Path | None],
) -> FetchOptions:
    opts = info.options

    attach = _norm_optional_str(opts.attach)
    clone_profile = _norm_optional_str(opts.clone_chrome_profile)
    cookies_from = _norm_optional_str(opts.cookies_from)
    referer = _norm_optional_str(opts.referer)

    attach_host: str | None = None
    attach_port: int | None = None
    user_data_dir: Path | None = None

    if attach:
        attach_host, attach_port = _resolve_attach(attach)
    elif clone_profile:
        cloned = clone_chrome_profile(clone_profile, log=log)
        cloned_dir_holder[0] = cloned
        user_data_dir = cloned

    job_dir = data_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = (job_dir / "assets") if opts.capture_assets else None

    return FetchOptions(
        url=info.url,
        wait_seconds=opts.wait_seconds,
        settle_seconds=opts.settle_seconds,
        idle_seconds=opts.idle_seconds,
        max_wait_seconds=opts.max_wait_seconds,
        scroll=opts.scroll,
        scroll_step=opts.scroll_step,
        scroll_max=opts.scroll_max,
        scroll_early_after=opts.scroll_early_after,
        post_click_seconds=opts.post_click_seconds,
        download_video=bool(getattr(opts, "download_video", False)),
        cookies_from=cookies_from,
        referer=referer,
        user_data_dir=user_data_dir,
        attach_host=attach_host,
        attach_port=attach_port,
        keep_open=False,
        headless=opts.headless,
        assets_dir=assets_dir,
        log=log,
    )


def _make_log_callback(
    store: JobStore,
    job_id: str,
    job_dir: Path,
):
    """Build a log function that:
    1) writes to local data/jobs/{id}/log.txt
    2) appends to the store (LIST in Redis)
    3) publishes to Pub/Sub for live WebSocket subscribers
    4) also prints to stderr for operator visibility
    """
    log_path = job_dir / "log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fp = open(log_path, "a", encoding="utf-8", buffering=1)

    def _log(msg: str) -> None:
        line = msg.rstrip()
        fp.write(line + "\n")
        log.info("%s", line)
        # store ops are async — schedule them
        loop = asyncio.get_event_loop()
        asyncio.ensure_future(store.append_log_line(job_id, line), loop=loop)
        asyncio.ensure_future(store.publish_log(job_id, line), loop=loop)

    _log._fp = fp  # type: ignore[attr-defined]
    return _log


# ----------------------------------------------------------------------------
# arq task
# ----------------------------------------------------------------------------


async def run_fetch_job(ctx: dict, job_id: str) -> dict:
    """Run a fetch job referenced by ID. Job spec lives in the store."""
    store: JobStore = ctx["store"]
    data_dir: Path = ctx["data_dir"]

    info = await store.get_job_info(job_id)
    if info is None:
        return {"ok": False, "error": "job not found in store"}

    job_dir = data_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    log = _make_log_callback(store, job_id, job_dir)
    cloned_dir_holder: list[Path | None] = [None]

    try:
        info.status = JobStatus.running
        info.started_at = datetime.utcnow()
        info.progress.phase = "running"
        await store.save_job_info(info)

        fetch_opts = _build_fetch_options(job_id, info, data_dir, log, cloned_dir_holder)
        result = await fetch(fetch_opts)

        # Persist HTML
        html_path = job_dir / "page.html"
        html_path.write_text(result.html, encoding="utf-8")

        # Build AssetInfo list. page_url is the same for every entry in
        # fetch mode -- the single URL the operator gave us. Matches the
        # worker-side path in server/worker/agent.py.
        page_url_for_assets = info.url or None
        # urllib.parse.quote with safe="" percent-encodes ``#`` (and
        # everything else non-alphanumeric) -- without it, asset names
        # containing ``#`` (common in scraped video titles) silently
        # truncate at the browser fragment delimiter, 404'ing in the
        # gallery. See server/hub/app.py:_asset_href for the full note.
        from urllib.parse import quote as _quote

        assets = []
        for a in result.assets_saved:
            assets.append(
                AssetInfo(
                    name=a["name"],
                    size=a["size"],
                    mime=a.get("mime"),
                    url=a.get("url"),
                    page_url=page_url_for_assets,
                    href=f"/jobs/{_quote(job_id, safe='')}/assets/{_quote(a['name'], safe='')}",
                )
            )

        # Finalize
        info.status = JobStatus.completed
        info.progress.phase = "completed"
        info.progress.assets_saved = len(assets)
        info.progress.assets_failed = result.assets_failed
        info.completed_at = datetime.utcnow()
        await store.save_job_info(info)

        job_result = JobResult(
            job_id=job_id,
            status=JobStatus.completed,
            html_href=f"/jobs/{job_id}/page.html",
            log_href=f"/jobs/{job_id}/log.txt",
            assets=assets,
            assets_failed=result.assets_failed,
            video_detection=result.video_detection,
            video_urls_seen=list(result.video_urls_seen),
            iframe_srcs=list(result.iframe_srcs),
            ytdlp_results=[YtdlpResult(**r) for r in result.ytdlp_results],
            error=None,
        )
        await store.save_job_result(job_result)
        return {"ok": True}

    except asyncio.CancelledError:
        info.status = JobStatus.cancelled
        info.progress.phase = "cancelled"
        info.error = "cancelled"
        info.completed_at = datetime.utcnow()
        try:
            await store.save_job_info(info)
        except Exception:
            pass
        raise
    except Exception as e:
        info.status = JobStatus.failed
        info.progress.phase = "failed"
        info.error = f"{type(e).__name__}: {e}"
        info.completed_at = datetime.utcnow()
        log(f"  !! job failed: {info.error}")
        try:
            await store.save_job_info(info)
            await store.save_job_result(
                JobResult(
                    job_id=job_id,
                    status=JobStatus.failed,
                    error=info.error,
                    log_href=f"/jobs/{job_id}/log.txt",
                )
            )
        except Exception:
            pass
        return {"ok": False, "error": info.error}
    finally:
        # Clean up cloned profile
        if cloned_dir_holder[0] is not None:
            try:
                shutil.rmtree(cloned_dir_holder[0], ignore_errors=True)
            except Exception:
                pass
        # Tell live subscribers we're done
        try:
            await store.publish_log(job_id, DONE_SENTINEL)
        except Exception:
            pass
        # Close log file
        try:
            log._fp.close()  # type: ignore[attr-defined]
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Worker lifecycle
# ----------------------------------------------------------------------------


async def on_startup(ctx: dict) -> None:
    store, kind = await make_store(ctx.get("_redis_url"))
    ctx["store"] = store
    ctx["data_dir"] = ctx.get("_data_dir", Path("./data/jobs"))
    ctx["data_dir"].mkdir(parents=True, exist_ok=True)
    log.info(
        "startup: store=%s, data_dir=%s", kind, ctx["data_dir"].resolve()
    )


async def on_shutdown(ctx: dict) -> None:
    store: JobStore | None = ctx.get("store")
    if store is not None:
        try:
            await store.close()
        except Exception:
            pass
    log.info("shutdown complete")


def make_worker_settings(
    redis_url: str,
    data_dir: Path,
    max_jobs: int = 2,
) -> type:
    """Build a WorkerSettings class for arq.

    arq inspects this class for `functions`, `redis_settings`, `on_startup`,
    `on_shutdown`, `max_jobs`. It does NOT instantiate it.

    Closure state (redis_url, data_dir) is smuggled into ctx via `on_startup`
    by attaching it to `ctx` before make_store is called. We do that with a
    wrapper.
    """

    async def _startup(ctx: dict) -> None:
        ctx["_redis_url"] = redis_url
        ctx["_data_dir"] = data_dir
        await on_startup(ctx)

    class Settings:
        functions = [run_fetch_job]
        redis_settings = RedisSettings.from_dsn(redis_url)
        on_startup = staticmethod(_startup)
        on_shutdown = staticmethod(on_shutdown)
        max_jobs_attr = max_jobs

    # arq expects `max_jobs` as a class attribute
    Settings.max_jobs = max_jobs  # type: ignore[attr-defined]
    return Settings


async def run_inproc_worker(
    redis_url: str,
    data_dir: Path,
    max_jobs: int = 2,
) -> None:
    """Run an arq Worker inside the current event loop.

    Used by --mode all to keep hub + worker in one process during development.
    Blocks until cancelled.
    """
    settings = make_worker_settings(redis_url, data_dir, max_jobs)
    worker = Worker(
        functions=settings.functions,
        redis_settings=settings.redis_settings,
        on_startup=settings.on_startup,
        on_shutdown=settings.on_shutdown,
        max_jobs=max_jobs,
        handle_signals=False,  # parent process owns signals
    )
    await worker.async_run()
