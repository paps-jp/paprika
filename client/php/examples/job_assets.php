<?php
/**
 * Example: fetch a URL, then download every captured image into ./downloads.
 *
 *   php examples/job_assets.php https://example.com
 */
declare(strict_types=1);

require __DIR__ . '/../vendor/autoload.php';

use Paprika\Client\Paprika;

$url = $argv[1] ?? 'https://example.com';

$cli = Paprika::connect();
$job = $cli->fetch($url);
echo "job {$job['job_id']} -> {$job['status']}\n";

$destDir = __DIR__ . '/downloads';
$saved = $cli->downloadJobAssets($job['job_id'], $destDir, kind: 'image');
echo "saved " . count($saved) . " file(s) to $destDir\n";
foreach ($saved as $p) {
    echo "  $p\n";
}
