<?php
declare(strict_types=1);

namespace Paprika\Client;

use Paprika\Client\Internal\HttpClient;

/**
 * Synchronous PHP client bound to one paprika hub.
 *
 * The hub URL resolves in this order:
 *   1. explicit $baseUrl argument to the constructor / {@see Paprika::connect}
 *   2. PAPRIKA_HUB environment variable (set by paprika-runner)
 *   3. http://localhost:8000 (local-dev fallback)
 *
 * For scripts that may run BOTH locally and inside a paprika-runner
 * sandbox, the recommended call is the no-argument form::
 *
 *     $cli = Paprika::connect();
 *
 * Hardcoded URLs are the single biggest source of "connect failed"
 * bugs in rerun-mode jobs (paprika.lan doesn't resolve inside the
 * runner; the in-Docker name is http://hub:8000).
 *
 * Phase status:
 *   - Phase 1 (this release): job APIs + Session lifecycle
 *   - Phase 2 (next): Page / Locator / page actions (Playwright shape)
 *   - Phase 3: Walker, oneshot helpers, CLI
 */
class PaprikaClient
{
    private readonly string $baseUrl;
    private readonly HttpClient $http;

    public function __construct(
        ?string $baseUrl = null,
        ?string $token = null,
        float $timeout = 180.0,
    ) {
        if ($baseUrl === null) {
            $baseUrl = \getenv('PAPRIKA_HUB') ?: 'http://localhost:8000';
        }
        $this->baseUrl = \rtrim($baseUrl, '/');
        $this->http = new HttpClient($this->baseUrl, $token, $timeout);
    }

    /** The hub URL this client points at, without trailing slash. */
    public function baseUrl(): string
    {
        return $this->baseUrl;
    }

    // ------------------------------------------------------------------
    // generic
    // ------------------------------------------------------------------

    /** GET /health -- handy for smoke tests. */
    public function health(): array
    {
        return $this->http->requestJson('GET', '/health');
    }

    /** GET /workers -- live worker inventory. Returns an array of worker dicts. */
    public function listWorkers(): array
    {
        $data = $this->http->requestJson('GET', '/workers');
        return $data['workers'] ?? [];
    }

    /** GET /sessions -- list active hub sessions. */
    public function listSessions(): array
    {
        $data = $this->http->requestJson('GET', '/sessions');
        return $data['sessions'] ?? [];
    }

    // ------------------------------------------------------------------
    // jobs
    // ------------------------------------------------------------------
    //
    // The session API (below) drives a live browser. These wrap the
    // hub's *job* surface instead: submit a fetch / codegen job, poll
    // it, read its results & captured assets. Job artifacts outlive
    // the run, so this is how you grab images after a one-shot crawl
    // rather than from an interactive session.

    /**
     * POST /jobs -- submit a job and return the initial JobInfo dict.
     *
     * @param array<string, mixed> $options  Merged into JobOptions verbatim,
     *        e.g. ['mode' => 'fetch', 'scroll' => true, 'use_profile' => 'foo'].
     * @return array<string, mixed>  JobInfo (includes 'job_id', 'status', ...).
     */
    public function createJob(string $url, array $options = []): array
    {
        $body = ['url' => $url];
        if ($options !== []) {
            $body['options'] = $options;
        }
        return $this->http->requestJson('POST', '/jobs', $body);
    }

    /** GET /jobs/{id} -- the current JobInfo (status, progress, ...). */
    public function getJob(string $jobId): array
    {
        return $this->http->requestJson('GET', '/jobs/' . \rawurlencode($jobId));
    }

    /**
     * GET /jobs -- every job the hub knows about (newest first as the
     * hub orders them). Returns a sequential array of JobInfo dicts.
     *
     * @return array<int, array<string, mixed>>
     */
    public function listJobs(): array
    {
        $data = $this->http->requestJson('GET', '/jobs');
        // /jobs returns a bare JSON array; HttpClient decodes it as a
        // sequential PHP array which is_array() == true. The fallback
        // path covers a future wrap-in-object change without breaking.
        if (\array_is_list($data)) {
            return $data;
        }
        return $data['jobs'] ?? [];
    }

    /**
     * GET /jobs/{id}/result -- the JobResult (assets list, links, final
     * url, ...). 404s until the job has produced a result.
     */
    public function jobResult(string $jobId): array
    {
        return $this->http->requestJson('GET', '/jobs/' . \rawurlencode($jobId) . '/result');
    }

    /** POST /jobs/{id}/cancel -- stop an in-flight job. Idempotent. */
    public function cancelJob(string $jobId): array
    {
        return $this->http->requestJson('POST', '/jobs/' . \rawurlencode($jobId) . '/cancel');
    }

    /** DELETE /jobs/{id} -- remove the job and its on-disk artifacts. */
    public function deleteJob(string $jobId): array
    {
        return $this->http->requestJson('DELETE', '/jobs/' . \rawurlencode($jobId));
    }

    /**
     * Poll GET /jobs/{id} until it reaches a terminal state
     * (completed / failed / cancelled) and return the final JobInfo.
     *
     * @throws PaprikaError when the job does not finish within $timeout
     *                     seconds (statusCode is null -- it's not an HTTP error).
     */
    public function waitJob(
        string $jobId,
        float $pollInterval = 2.0,
        float $timeout = 600.0,
    ): array {
        $deadline = \microtime(true) + $timeout;
        while (true) {
            $info = $this->getJob($jobId);
            $status = $info['status'] ?? '';
            if (\in_array($status, ['completed', 'failed', 'cancelled'], true)) {
                return $info;
            }
            if (\microtime(true) > $deadline) {
                throw new PaprikaError(
                    "job {$jobId} did not finish within {$timeout}s "
                    . "(last status: " . ($status !== '' ? $status : '?') . ')'
                );
            }
            \usleep((int) ($pollInterval * 1_000_000));
        }
    }

    /**
     * Convenience: submit a fetch-mode job and (by default) wait for it
     * to finish. Returns the final JobInfo.
     *
     * ``$scroll`` defaults to true so lazy-loaded images fire. Pair
     * with {@see jobImages} to collect captured images.
     *
     *     $job = $cli->fetch('https://example.com/article');
     *     $imgs = $cli->jobImages($job['job_id']);
     *
     * @param array<string, mixed> $options  Extra JobOptions
     *        (use_profile, cookies_from, scroll_max, headless, ...).
     */
    public function fetch(
        string $url,
        array $options = [],
        bool $wait = true,
        float $pollInterval = 2.0,
        float $timeout = 600.0,
        bool $scroll = true,
    ): array {
        $opts = $options + ['mode' => 'fetch', 'scroll' => $scroll];
        $info = $this->createJob($url, $opts);
        if (!$wait) {
            return $info;
        }
        return $this->waitJob($info['job_id'], $pollInterval, $timeout);
    }

    // ------------------------------------------------------------------
    // assets (image / media retrieval)
    // ------------------------------------------------------------------

    /**
     * GET /jobs/{id}/assets.json -- assets captured by a job.
     *
     * @param string|null $kind   "image" / "video" / "audio" / "other"
     *                           or null to return everything.
     * @param bool $absolute     true -> ready-to-GET URLs (baseUrl prefixed);
     *                           false -> relative hrefs.
     * @param bool $details      false -> array of URL strings;
     *                           true  -> array of dicts with full metadata.
     * @return array<int, mixed> List of URLs (string) or rows (array).
     */
    public function jobAssets(
        string $jobId,
        ?string $kind = null,
        bool $absolute = true,
        bool $details = false,
    ): array {
        $data  = $this->http->requestJson('GET', '/jobs/' . \rawurlencode($jobId) . '/assets.json');
        $items = $data['items'] ?? [];
        if ($kind !== null) {
            $items = \array_values(\array_filter(
                $items,
                static fn($it) => ($it['kind'] ?? null) === $kind,
            ));
        }
        $prefix = $absolute ? $this->baseUrl : '';
        if ($details) {
            return \array_map(static function ($it) use ($prefix) {
                $row = $it;
                $row['url'] = $prefix . ($it['href'] ?? '');
                return $row;
            }, $items);
        }
        return \array_map(static fn($it) => $prefix . ($it['href'] ?? ''), $items);
    }

    /** Shorthand for {@see jobAssets} with kind='image'. */
    public function jobImages(string $jobId, bool $absolute = true, bool $details = false): array
    {
        return $this->jobAssets($jobId, 'image', $absolute, $details);
    }

    /**
     * Download a job's captured assets to ``$destDir`` and return the
     * written file paths. Defaults to images only.
     *
     * @return array<int, string>  Filesystem paths of the saved files.
     */
    public function downloadJobAssets(
        string $jobId,
        string $destDir,
        ?string $kind = 'image',
    ): array {
        $rows = $this->jobAssets($jobId, $kind, absolute: false, details: true);
        if (!\is_dir($destDir) && !\mkdir($destDir, 0777, true) && !\is_dir($destDir)) {
            throw new PaprikaError("could not create destination directory: {$destDir}");
        }
        $paths = [];
        foreach ($rows as $it) {
            $href = (string) ($it['href'] ?? '');
            $name = (string) ($it['name'] ?? \basename(\parse_url($href, PHP_URL_PATH) ?: 'asset.bin'));
            if ($href === '') {
                continue;
            }
            $blob = $this->http->requestRaw('GET', $href);
            $dest = \rtrim($destDir, "/\\") . DIRECTORY_SEPARATOR . $name;
            \file_put_contents($dest, $blob);
            $paths[] = $dest;
        }
        return $paths;
    }

    // ------------------------------------------------------------------
    // sessions (live browser)
    // ------------------------------------------------------------------

    /**
     * POST /sessions -- reserve a Lane and return a {@see Session}
     * bound to it.
     *
     * ``$parentJobId`` defaults to PAPRIKA_JOB_ID env var so scripts
     * run under paprika-runner automatically tag their sessions with
     * the parent job. Pass an empty string ('') to opt out explicitly.
     */
    public function openSession(
        ?string $initialUrl = null,
        ?string $workerId = null,
        ?int $laneHint = null,
        ?int $idleTtlS = null,
        ?int $absoluteTtlS = null,
        ?string $parentJobId = null,
        ?string $useProfile = null,
    ): Session {
        $body = [];
        if ($initialUrl !== null)    $body['initial_url']     = $initialUrl;
        if ($workerId !== null)      $body['worker_id']       = $workerId;
        if ($laneHint !== null)      $body['lane_hint']       = $laneHint;
        if ($idleTtlS !== null)      $body['idle_ttl_s']      = $idleTtlS;
        if ($absoluteTtlS !== null)  $body['absolute_ttl_s']  = $absoluteTtlS;

        $effectivePjid = $parentJobId ?? (\getenv('PAPRIKA_JOB_ID') ?: null);
        if ($effectivePjid !== null && $effectivePjid !== '') {
            $body['parent_job_id'] = $effectivePjid;
        }
        if ($useProfile !== null && $useProfile !== '') {
            // Name of a Chrome profile previously uploaded to the hub.
            // The hub fetches the tarball into the lane's user-data-dir
            // before the browser starts so the session opens with the
            // operator's cookies / logins / localStorage in place.
            $body['use_profile'] = $useProfile;
        }

        $info = $this->http->requestJson('POST', '/sessions', $body);
        return new Session($this, $info);
    }

    /**
     * Closure form: open a session, run a callback with it, auto-close.
     *
     * Mirrors Python's ``async with cli.session(...) as page:`` block
     * but using a PHP closure for scope. Returns whatever the closure
     * returns. The session is closed when the closure returns OR throws,
     * UNLESS the closure called {@see Session::detach}.
     *
     *     $cli->session('https://example.com', function (Session $sess) {
     *         // Phase 2: drive the page via $sess->goto(...) etc.
     *     });
     *
     * If $fn is null, this is identical to {@see openSession} (no
     * auto-close), kept for parity.
     *
     * @param array<string, mixed> $kwargs  Same kwargs as openSession()
     *                                      using the snake_case keys
     *                                      (worker_id, lane_hint, ...).
     * @return Session|mixed  The Session when $fn is null; otherwise
     *                        whatever the closure returned.
     */
    public function session(
        ?string $initialUrl = null,
        ?callable $fn = null,
        array $kwargs = [],
    ): mixed {
        $session = $this->openSession(
            initialUrl:    $initialUrl,
            workerId:      $kwargs['worker_id']      ?? null,
            laneHint:      $kwargs['lane_hint']      ?? null,
            idleTtlS:      $kwargs['idle_ttl_s']     ?? null,
            absoluteTtlS:  $kwargs['absolute_ttl_s'] ?? null,
            parentJobId:   $kwargs['parent_job_id']  ?? null,
            useProfile:    $kwargs['use_profile']    ?? null,
        );
        if ($fn === null) {
            return $session;
        }
        try {
            return $fn($session);
        } finally {
            if (!$session->isDetached()) {
                $session->close();
            }
        }
    }

    // ------------------------------------------------------------------
    // internal -- session lifecycle calls. Underscore prefix marks them
    // as not-public; Session::close() / ::detach() invoke them.
    // ------------------------------------------------------------------

    /** @internal */
    public function _endSession(string $sessionId): array
    {
        return $this->http->requestJson('DELETE', '/sessions/' . \rawurlencode($sessionId));
    }

    /** @internal */
    public function _detachSession(string $sessionId): array
    {
        return $this->http->requestJson('POST', '/sessions/' . \rawurlencode($sessionId) . '/detach');
    }
}
