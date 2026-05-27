<?php
/**
 * Example: reserve a live browser session.
 *
 * Phase 1 has session lifecycle only -- page actions (goto/click/fill)
 * land in Phase 2. The session does open Chrome on a worker, though,
 * so this is useful for "hand the operator a noVNC link" workflows.
 *
 *   php examples/session.php https://example.com
 */
declare(strict_types=1);

require __DIR__ . '/../vendor/autoload.php';

use Paprika\Client\Paprika;
use Paprika\Client\Session;

$url = $argv[1] ?? 'https://example.com';
$cli = Paprika::connect();

// Closure form: the session is auto-closed when the closure returns,
// unless you call $sess->detach() to hand it off.
$cli->session($url, function (Session $sess) use ($cli) {
    echo "session  : {$sess->sessionId}\n";
    echo "worker   : " . ($sess->workerId ?? '?') . "\n";
    echo "lane     : " . ($sess->laneIdx ?? '?') . "\n";
    echo "noVNC URL: " . ($sess->novncUrl !== null ? $cli->baseUrl() . $sess->novncUrl : '(none)') . "\n";

    // Hold the session for ~5 s so the operator can watch in noVNC.
    echo "watching for 5 s...\n";
    sleep(5);
});
echo "session closed.\n";
