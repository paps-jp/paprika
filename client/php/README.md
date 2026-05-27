# paprika-client (PHP)

Synchronous PHP client for the [paprika](../../README.md) browser fleet.
Mirrors the surface of the Python client (`client/python/paprika_client`)
so calls translate 1:1 between the two languages.

- **Synchronous** — blocking call style, the way most PHP code is written.
- **No Composer dependencies** — `ext-curl` + `ext-json` only.
- **PHP 8.1+**.

## Install

```bash
composer require paprika/client
```

For local development inside this repo:

```bash
cd client/php
composer install
```

## Quick start

```php
<?php
require 'vendor/autoload.php';

use Paprika\Client\Paprika;

$cli = Paprika::connect();   // reads PAPRIKA_HUB env, falls back to http://localhost:8000

// Submit a Fetch-mode job and wait for it.
$job = $cli->fetch('https://example.com');
echo "job {$job['job_id']} -> {$job['status']}\n";

// List captured images
foreach ($cli->jobImages($job['job_id']) as $imageUrl) {
    echo $imageUrl . "\n";
}

// Or download them all to a directory
$saved = $cli->downloadJobAssets($job['job_id'], './downloads', kind: 'image');
```

Override the hub URL explicitly:

```php
$cli = Paprika::connect('http://paprika.lan:8000');
$cli = Paprika::connect(token: 'my-bearer-token');
```

## What's implemented

### Phase 1 — this release

`PaprikaClient` — job + asset APIs:

| Method | Hub endpoint |
|---|---|
| `health()` | `GET /health` |
| `listWorkers()` | `GET /workers` |
| `listSessions()` | `GET /sessions` |
| `createJob($url, $options)` | `POST /jobs` |
| `getJob($jobId)` | `GET /jobs/{id}` |
| `listJobs()` | `GET /jobs` |
| `jobResult($jobId)` | `GET /jobs/{id}/result` |
| `cancelJob($jobId)` | `POST /jobs/{id}/cancel` |
| `deleteJob($jobId)` | `DELETE /jobs/{id}` |
| `waitJob($jobId)` | polls `GET /jobs/{id}` |
| `fetch($url)` | `createJob` + `waitJob`, mode=fetch |
| `jobAssets($jobId)`, `jobImages($jobId)` | `GET /jobs/{id}/assets.json` |
| `downloadJobAssets($jobId, $destDir)` | download every asset to disk |
| `openSession(...)`, `session(url, $closure, ...)` | `POST /sessions` |

`Session` — lifecycle only:

- read-only properties: `sessionId`, `workerId`, `laneIdx`, `novncUrl`
- `close()` — `DELETE /sessions/{id}`
- `detach()` — `POST /sessions/{id}/detach`

Errors: `PaprikaError` (HTTP / transport failures, with `statusCode`)
and `PaprikaActionError` (page action failures — populated in Phase 2).

### Phase 2 — next

Page / Locator / page actions (Playwright shape). Once landed you'll write:

```php
$cli->session('https://example.com', function (Session $sess) {
    $sess->goto('https://news.ycombinator.com');
    $sess->locator('.athing .titleline > a')->first()->click();
    $sess->screenshot('hn.png');
    $state = $sess->state();
    echo $state['url'], "\n";
});
```

### Phase 3 — later

`Walker` (site crawling helper), oneshot helpers (`outline`, `run`,
`snapshot`, `state`), and a `bin/paprika` CLI.

## Running the examples

```bash
cd client/php
composer install                              # only generates the autoloader (no deps)
php examples/fetch.php https://example.com
php examples/job_assets.php https://example.com
php examples/session.php https://example.com
```

Point at a non-default hub via env:

```bash
PAPRIKA_HUB=http://paprika.lan:8000 php examples/fetch.php
```

## Hub URL resolution

The constructor resolves the hub URL in this order:

1. explicit `$baseUrl` argument to `Paprika::connect(...)`
2. `PAPRIKA_HUB` environment variable (set inside paprika-runner sandboxes)
3. `http://localhost:8000` (local-dev fallback)

For scripts that run **both** on your laptop and inside a paprika-runner
container, prefer the no-argument form:

```php
$cli = Paprika::connect();
```

Hardcoded LAN names like `paprika.lan` won't resolve inside the runner;
the in-Docker hub name is `http://hub:8000`. The client surfaces a
diagnostic hint in connect errors that points this out.

## License

PolyForm Noncommercial 1.0.0 — noncommercial use only.
See [LICENSE](LICENSE).
