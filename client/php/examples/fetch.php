<?php
/**
 * Example: submit a Fetch-mode job and list the captured images.
 *
 *   php examples/fetch.php
 *   php examples/fetch.php https://example.com
 *
 * Set PAPRIKA_HUB env to point at a hub other than http://localhost:8000:
 *   PAPRIKA_HUB=http://paprika.lan:8000 php examples/fetch.php
 */
declare(strict_types=1);

require __DIR__ . '/../vendor/autoload.php';

use Paprika\Client\Paprika;
use Paprika\Client\PaprikaError;

$url = $argv[1] ?? 'https://example.com';

try {
    $cli = Paprika::connect();
    echo "hub: {$cli->baseUrl()}\n";

    echo "submitting fetch for $url ...\n";
    $job = $cli->fetch($url);
    echo "  job_id   = {$job['job_id']}\n";
    echo "  status   = {$job['status']}\n";

    $images = $cli->jobImages($job['job_id']);
    echo "captured " . count($images) . " image(s):\n";
    foreach ($images as $u) {
        echo "  $u\n";
    }
} catch (PaprikaError $e) {
    fwrite(STDERR, "ERR: " . $e->getMessage() . "\n");
    if ($e->statusCode !== null) {
        fwrite(STDERR, "     (HTTP {$e->statusCode})\n");
    }
    exit(1);
}
