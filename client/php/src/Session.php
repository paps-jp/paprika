<?php
declare(strict_types=1);

namespace Paprika\Client;

/**
 * A live browser session, bound to one Chrome lane.
 *
 * Phase 1: lifecycle only -- properties from the hub's ``/sessions``
 * response, plus :meth:`close` / :meth:`detach`. The Playwright-shape
 * page actions (goto / click / fill / locator / screenshot / state /
 * outline / ...) land in Phase 2 (port of paprika_client/_page.py).
 *
 * Typical use::
 *
 *     $cli  = Paprika::connect();
 *     $sess = $cli->openSession(initialUrl: 'https://example.com');
 *     try {
 *         // Phase 2: $sess->goto('https://...'); $sess->click('text=Login');
 *         // For now: read the URL the operator can view live
 *         echo "watch: {$sess->novncUrl}\n";
 *     } finally {
 *         $sess->close();
 *     }
 *
 * Or use the closure form for auto-close::
 *
 *     $cli->session('https://example.com', function (Session $sess) {
 *         // ... session is closed automatically when the closure returns
 *     });
 */
class Session
{
    /** Hub-assigned session id (e.g. "ses_xxxx"). */
    public readonly string $sessionId;
    /** Worker that owns the underlying Chrome lane. */
    public readonly ?string $workerId;
    /** Lane index within the worker (0-based). */
    public readonly ?int $laneIdx;
    /**
     * Hub-proxied noVNC URL for live operator viewing. While the
     * session is alive this is a hub-relative path
     * (``/sessions/{id}/novnc/?...``). Use {@see baseUrl} from the
     * client to form an absolute URL.
     */
    public readonly ?string $novncUrl;

    private bool $detached = false;
    private bool $closed = false;

    /**
     * Most callers should NOT construct this directly. Use
     * ``PaprikaClient::openSession()`` or ``PaprikaClient::session()``.
     *
     * @param array<string, mixed> $info  The hub's ``/sessions`` POST response.
     */
    public function __construct(
        private readonly PaprikaClient $client,
        public readonly array $info,
    ) {
        $this->sessionId = (string) ($info['session_id'] ?? '');
        $this->workerId  = isset($info['worker_id']) ? (string) $info['worker_id'] : null;
        $this->laneIdx   = isset($info['lane_idx']) ? (int) $info['lane_idx'] : null;
        $this->novncUrl  = isset($info['novnc_url']) ? (string) $info['novnc_url'] : null;
    }

    /** Has {@see close} already run (or been suppressed by {@see detach})? */
    public function isClosed(): bool
    {
        return $this->closed;
    }

    /** Has {@see detach} been called? Detached sessions skip auto-close. */
    public function isDetached(): bool
    {
        return $this->detached;
    }

    /**
     * Release the lane: DELETE /sessions/{id}.
     *
     * Idempotent. Best-effort -- if the session was already reaped by
     * the hub (TTL) or the worker is gone, this still completes without
     * raising. Repeat calls are no-ops.
     */
    public function close(): void
    {
        if ($this->closed || $this->detached) {
            $this->closed = true;
            return;
        }
        $this->closed = true;
        if ($this->sessionId === '') {
            return;
        }
        try {
            $this->client->_endSession($this->sessionId);
        } catch (\Throwable) {
            // best-effort cleanup -- session may already be gone
        }
    }

    /**
     * Hand the session off to the operator (or another script) and
     * SKIP the implicit close. Useful when ending a script that wants
     * the operator to keep driving the browser via noVNC.
     *
     * The closure form ({@see PaprikaClient::session}) honours the
     * detached flag -- the session is NOT auto-closed when the
     * closure returns after a detach() call.
     *
     * @return array<string, mixed>  The hub's detach response (typically
     *                              includes updated TTLs).
     */
    public function detach(): array
    {
        $this->detached = true;
        if ($this->sessionId === '') {
            return [];
        }
        return $this->client->_detachSession($this->sessionId);
    }

    public function __destruct()
    {
        // Safety net -- if the caller forgot to close() and didn't
        // detach(), free the lane. Wrapped because destructors run
        // during shutdown when sockets may already be gone.
        if (!$this->closed && !$this->detached) {
            try {
                $this->close();
            } catch (\Throwable) {
                // shutdown noise -- nothing useful we can do here
            }
        }
    }
}
