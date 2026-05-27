<?php
declare(strict_types=1);

namespace Paprika\Client;

/**
 * Raised when a page-level action (click / fill / wait) fails on the
 * worker side. A subtype of {@see PaprikaError} so blanket catches
 * keep working.
 *
 * Phase 2 (Page/Locator) populates these; in Phase 1 it just exists
 * so callers can write the catch ahead of time.
 */
class PaprikaActionError extends PaprikaError
{
}
