# paprika-client

Playwright-shape async Python client for the [paprika](https://github.com/paps-jp/paprika) browser fleet.

```python
import asyncio
from paprika_client import async_paprika

async def main():
    async with async_paprika.connect() as cli:
        async with cli.session(initial_url="https://news.ycombinator.com") as page:
            await page.locator(".athing .titleline > a").click()
            state = await page.state()
            print(state.url, "->", state.title)
            await page.back()
            await page.screenshot(path="hn.png")

asyncio.run(main())
```

## What is this?

paprika is a hub-and-spoke browser fleet: a hub orchestrates many
workers, each of which runs N pre-warmed Chrome lanes. This client
opens a **Session** against the hub — the hub reserves a Lane on
some Worker, attaches CDP, and the client drives the Chrome
action-by-action over HTTP.

The action surface mirrors Playwright's so existing browser-automation
intuition transfers. You don't get a local Chrome — you get a shared
fleet, viewable over noVNC for free.

## Install

From the paprika source tree:

```
pip install -e ./client/python
```

Or pull just this directory into your own project. The only runtime
dependency is `httpx`.

## API

### Connect

```python
cli = async_paprika.connect()                    # PAPRIKA_HUB env -> localhost:8000
async with cli:                                  # opens an httpx.AsyncClient
    ...

# Or pass an explicit URL:
cli = async_paprika.connect("http://hub.lan")
```

### Open a Session

A session reserves one Lane (one Chrome process) for the lifetime of
the `async with` block.

```python
async with cli.session(initial_url="https://example.com") as page:
    ...
```

Or manually:

```python
page = await cli.open_session(initial_url="https://example.com")
try:
    ...
finally:
    await page.close()
```

### Actions

| Playwright | paprika |
|---|---|
| `page.goto(url)` | `page.goto(url)` |
| `page.click(selector)` | `page.click(selector)` |
| `page.fill(selector, value)` | `page.fill(selector, value)` |
| `page.press(key)` | `page.press(key)` |
| `page.go_back()` | `page.back()` (alias `go_back`) |
| `page.screenshot(path=...)` | `page.screenshot(path=...)` |
| `page.title()` | `page.title()` |
| `page.url` | `page.url` (sync, cached) |
| `page.locator(sel)` | `page.locator(sel)` |
| `page.get_by_role(role)` | `page.get_by_role(role)` |
| `page.get_by_text(text)` | `page.get_by_text(text)` |

paprika-only:

```python
await page.outline()       # text view with [@N] ids
await page.visited_urls()  # list of canonical URLs the session opened
await page.capture("snap") # persist HTML + PNG + outline server-side
await page.assets()        # URLs of images captured on this page (see below)
await page.save_assets("out/")  # download those captured images to disk
await page.cookies()       # current cookie jar (CDP-shaped), host-filtered
await page.save_cookies_to_host()  # promote those cookies into the Host registry
await page.network()       # media network log (image/audio/video responses)
await page.close_popups()  # close all non-default tabs (after a popup-spawning click)
page.novnc_url             # live noVNC URL for this session's lane
```

#### Captured assets (`page.assets` / `page.save_assets`)

paprika passively records every resource the page loads (images, video,
…) — the same machinery Fetch mode uses. `page.assets()` flushes the
worker's capture buffer and lists what was captured; it's the one-call
equivalent of wiring up Playwright's `page.on("response")` yourself.

```python
async with cli.session(
    "https://example.com/article",
    parent_job_id="my-crawl",        # assets need a job dir to land in
) as page:
    await page.scroll()              # trigger lazy-loaded images
    srcs = await page.assets()       # -> ["http://hub/jobs/.../img_001.jpg", ...]
    rows = await page.assets(details=True)  # -> list of dicts w/ size, source_url, mime
    await page.save_assets("out/images")    # download them to disk
```

| arg | default | meaning |
|---|---|---|
| `kind` | `"image"` | `image` / `video` / `audio` / `other`, or `None` for all |
| `absolute` | `True` | absolute URLs (`False` -> hub-relative `href`) |
| `refresh` | `True` | flush newly-captured assets off the worker first |
| `details` | `False` | return full metadata dicts instead of URL strings |

> **Job-bound session required.** Like `get_state` / `set_state`, the
> passive capture needs a parent job to store assets under. Scripts run
> by paprika-runner are bound automatically (`PAPRIKA_JOB_ID`); a raw
> `cli.session(...)` must pass `parent_job_id=...`, else `page.assets()`
> raises `PaprikaActionError`.

> **Multi-tab gotcha.** `await sess[-1].close()` is NOT how you close a
> popup tab. Popups spawned by worker-side clicks are not in the SDK's
> local `_pages` cache, so `sess[-1]` resolves to `sess` itself, and
> `Session.close()` is unconditional — it kills the whole session.
> Use `await sess.close_popups()` instead.

### Jobs & assets (non-session API)

The session API drives a live browser. The **job** API is the other half:
submit a one-shot fetch / codegen job, poll it, and read its captured
assets after the fact. These are methods on the client, not the page:

```python
async with async_paprika.connect() as cli:
    # fetch() = submit a fetch-mode job + wait for it to finish
    job = await cli.fetch("https://example.com/article", scroll=True)
    print(job.status)                          # "completed"

    # collect the captured images (assets.json, kind=image)
    imgs = await cli.job_images(job.job_id)    # -> [url, url, ...]
    rows = await cli.job_assets(job.job_id, details=True)  # full metadata
    await cli.download_job_assets(job.job_id, "out/images")
```

| method | endpoint | purpose |
|---|---|---|
| `cli.create_job(url, **opts)` | `POST /jobs` | submit (returns immediately) |
| `cli.fetch(url, wait=True, **opts)` | `POST /jobs` (+poll) | submit fetch + wait |
| `cli.get_job(id)` / `cli.list_jobs()` | `GET /jobs[/{id}]` | status / listing |
| `cli.wait_job(id)` | poll `GET /jobs/{id}` | block until terminal |
| `cli.job_result(id)` | `GET /jobs/{id}/result` | final JobResult |
| `cli.cancel_job(id)` / `cli.delete_job(id)` | `POST cancel` / `DELETE` | lifecycle |
| `cli.job_assets(id, kind=, details=)` | `GET /jobs/{id}/assets.json` | captured assets |
| `cli.job_images(id)` | (assets, `kind="image"`) | shorthand |
| `cli.download_job_assets(id, dir)` | `GET /jobs/{id}/assets/*` | save to disk |

`**opts` flow into `JobOptions` (`mode=`, `scroll=`, `scroll_max=`,
`use_profile=`, `cookies_from=`, `goal=` for codegen / vision modes, …).

> Anything not wrapped here (hosts / profiles / engines / settings / …)
> is still reachable via `await cli._json("GET", "/hosts")` etc. — the
> same thin HTTP helper every wrapper uses.

### Keep the session alive after the script exits

Call `page.keepalive()` (alias `detach`) before leaving the `async
with` block. The hub keeps the lane held and the browser open so a
human can take over via noVNC; the session auto-closes after
`idle_ttl_s` seconds of no operator activity (mouse / key / clipboard
through the noVNC viewer).

```python
async with cli.session(initial_url="https://example.com") as page:
    await page.get_by_text("Login").click()
    await page.fill("input[name=user]", "alice")
    await page.keepalive(idle_ttl_s=120)   # default: 120s
    # leaving the `with` block here no longer kills the session.
```

The hub's screenshot grid shows three states:

- **RUNNING** (red) — a script action or noVNC interaction is in flight
- **KEEPALIVE** (orange) — alive but nobody is touching it
- **IDLE** — closed and reaped (lane freed)

Fetch jobs submitted with `options.keep_session=true` (server-side
crawl + human handoff) use a 60 s default idle TTL; SDK `keepalive()`
defaults to 120 s and can be set per-call.

### Locators

`page.locator(selector)` returns a `Locator`. Like Playwright, it's
lazy — the selector is resolved each time you call `.click()` etc:

```python
btn = page.locator("button.primary")
await btn.click()
```

`get_by_text("…")` walks the current page outline to find the first
interactive element with that visible label, then clicks the matching
`[data-paprika-id="N"]`:

```python
await page.get_by_text("Login").click()
```

## Errors

| Exception | When |
|---|---|
| `PaprikaError` | HTTP-level error (404, 5xx, network) |
| `PaprikaActionError` | The hub returned 200 but the action returned `NO_MATCH` or `ERR: ...` |

`PaprikaActionError.status` carries the raw string from `browser_ops`.

### Now implemented (built on `page.evaluate`)

`page.evaluate(js)` landed and the DOM surface is built on top of it
(see [Guides — DOM 取得・待機・入力](https://paps-jp.github.io/paprika/guides.html#dom)): `wait_for_selector`,
`text_content` / `inner_text` / `get_attribute` / `input_value` /
`count` / `is_visible` / `is_checked` / …, the JS-dispatched inputs
(`hover` / `dblclick` / `select_option` / `check` / `uncheck` / `focus`),
`set_input_files` (CDP), `cookies()` (read), and the locator chain
(`first` / `last` / `nth` / `all` / `count`) plus `get_by_test_id` /
`get_by_placeholder` / `get_by_title` / `get_by_alt_text`.

### Still deferred (V1 → V2)

| Feature | Why deferred |
|---|---|
| `page.wait_for_url`, `wait_for_load_state` | Navigation-event hooks not wired |
| `locator.bounding_box()`, element `screenshot()` | Need a geometry/CDP path |
| `page.context.add_cookies()` | Use the Host registry + `use_profile` / `cookies_from` |
| iframe / `frame_locator` / multiple `BrowserContext` | Worker is single-frame, 1 session = 1 context |
| Real (trusted) input events / `route()` interception | Synthetic events + `network()` polling are the V1 stand-ins |
| Sync API (`sync_paprika`) | Async-first; sync wrapper can come later |

> ⚠️ Note: `page.evaluate` runs arbitrary JS in the browser. paprika is
> LAN-trusted (same model as cookie injection / profile upload), so it's
> exposed without the RFC-001 §12 auth gate.

## See also

- **[API リファレンス](https://paps-jp.github.io/paprika/api.html)** — 全公開関数の API リファレンス（関数ごとの引数・戻り値・例）
- **[ガイド](https://paps-jp.github.io/paprika/guides.html)** — 画像・動画取得の実践レシピ
  （単発ページ取得 / Recent jobs からの取得 / 動画 / ログイン必須サイト / walk によるサイト巡回）
- [RFC-001](../../docs/rfc-001-sessions.md) for the protocol design
- The paprika hub admin UI at `http://hub:8000/` for live VNC viewers
