<?php
declare(strict_types=1);

namespace Paprika\Client;

/**
 * Top-level factory: ``Paprika::connect(...)``.
 *
 * Mirrors Python's ``sync_paprika.connect(...)`` / module-level
 * Playwright-style entry point. Returns a ready-to-use
 * {@see PaprikaClient}.
 *
 * Usage::
 *
 *     use Paprika\Client\Paprika;
 *
 *     $cli  = Paprika::connect();                     // PAPRIKA_HUB or localhost
 *     $cli  = Paprika::connect('http://paprika.lan'); // explicit host
 *     $cli  = Paprika::connect(token: 'abc');         // bearer auth
 */
final class Paprika
{
    /**
     * @param string|null $baseUrl  Hub URL. When null, reads PAPRIKA_HUB
     *                              env var; falls back to
     *                              http://localhost:8000.
     * @param string|null $token    Optional bearer token for the
     *                              Authorization header.
     * @param float       $timeout  Per-request timeout (seconds). Long
     *                              by default because fetch / codegen
     *                              jobs can legitimately take minutes.
     */
    public static function connect(
        ?string $baseUrl = null,
        ?string $token = null,
        float $timeout = 180.0,
    ): PaprikaClient {
        return new PaprikaClient($baseUrl, $token, $timeout);
    }

    private function __construct()
    {
        // Static class -- prevent instantiation.
    }
}
