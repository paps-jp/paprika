<?php
declare(strict_types=1);

namespace Paprika\Client;

/**
 * Raised when the hub returns an error or an unexpected payload.
 *
 * - $statusCode: HTTP status from the hub (e.g. 404, 502). null for
 *   transport-level failures (network drop, connection refused, DNS) and
 *   for client-side validation errors raised before a request went out.
 *   Lets retry logic branch on the response kind without parsing the
 *   message string:
 *
 *     try {
 *         $cli->openSession(initialUrl: 'https://x.com');
 *     } catch (PaprikaError $e) {
 *         if ($e->statusCode === 502) {
 *             // transient -- retry
 *         }
 *     }
 */
class PaprikaError extends \RuntimeException
{
    public function __construct(
        string $message,
        public readonly ?int $statusCode = null,
        ?\Throwable $previous = null,
    ) {
        parent::__construct($message, 0, $previous);
    }
}
