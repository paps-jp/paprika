<?php
declare(strict_types=1);

namespace Paprika\Client\Internal;

use Paprika\Client\PaprikaError;

/**
 * Small cURL-based HTTP client. Single dependency: ext-curl.
 *
 * - JSON in (assoc array) -> JSON out (assoc array) via requestJson()
 * - raw bytes via requestRaw() for asset downloads
 * - error mapping with the same diagnostic-on-connect-failure hint the
 *   Python client emits (mention PAPRIKA_HUB env, "use http://hub:8000
 *   inside paprika-runner" tip).
 *
 * Internal -- consumers should call PaprikaClient methods instead.
 */
final class HttpClient
{
    private string $baseUrl;

    public function __construct(
        string $baseUrl,
        private readonly ?string $token = null,
        private readonly float $timeout = 180.0,
    ) {
        $this->baseUrl = rtrim($baseUrl, '/');
    }

    public function baseUrl(): string
    {
        return $this->baseUrl;
    }

    /**
     * Issue an HTTP request, expect (and decode) a JSON response body.
     *
     * @param string $method  HTTP verb (GET / POST / PUT / DELETE / PATCH).
     * @param string $path    Path beginning with "/", appended to baseUrl.
     * @param array<mixed>|null $jsonBody  Encoded as Content-Type: application/json.
     * @param array<string, scalar>|null $query  Appended as ?key=val&...
     * @return array<mixed>   Decoded JSON. A bare JSON array (e.g. /jobs)
     *                       comes back as a sequential PHP array; a JSON
     *                       object as an associative array.
     * @throws PaprikaError on transport or HTTP failures.
     */
    public function requestJson(string $method, string $path, ?array $jsonBody = null, ?array $query = null): array
    {
        [$status, $body, $contentType] = $this->doRequest($method, $path, $jsonBody, $query);
        if ($status >= 400) {
            $this->raiseHttp($method, $path, $status, $body);
        }
        // Some endpoints return 204 No Content; treat as empty object.
        if ($body === '' || $body === null) {
            return [];
        }
        if (\str_starts_with($contentType, 'application/json')) {
            $decoded = \json_decode($body, true);
            if (\json_last_error() !== JSON_ERROR_NONE) {
                throw new PaprikaError(
                    "$method $path: invalid JSON response: " . \json_last_error_msg()
                );
            }
            return \is_array($decoded) ? $decoded : ['value' => $decoded];
        }
        // Non-JSON response on an endpoint we expected JSON for -- surface
        // the raw bytes under a 'raw' key (mirrors Python's behaviour).
        return ['raw' => $body];
    }

    /**
     * Issue an HTTP request, return the raw response body as a string.
     * Used for asset downloads (images / video / arbitrary blobs).
     *
     * @throws PaprikaError on transport or HTTP failures.
     */
    public function requestRaw(string $method, string $path, ?array $jsonBody = null, ?array $query = null): string
    {
        [$status, $body] = $this->doRequest($method, $path, $jsonBody, $query);
        if ($status >= 400) {
            $this->raiseHttp($method, $path, $status, $body);
        }
        return $body;
    }

    /**
     * @return array{0:int,1:string,2:string}  [statusCode, body, contentType]
     */
    private function doRequest(string $method, string $path, ?array $jsonBody, ?array $query): array
    {
        $url = $this->baseUrl . $path;
        if ($query) {
            $url .= (\str_contains($path, '?') ? '&' : '?') . \http_build_query($query);
        }

        $ch = \curl_init();
        if ($ch === false) {
            throw new PaprikaError("$method $path: failed to init cURL handle");
        }

        $headers = ['Accept: application/json'];
        if ($this->token !== null && $this->token !== '') {
            $headers[] = 'Authorization: Bearer ' . $this->token;
        }

        $opts = [
            CURLOPT_URL            => $url,
            CURLOPT_CUSTOMREQUEST  => $method,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_HEADER         => true,        // keep headers so we can read Content-Type
            CURLOPT_TIMEOUT        => (int) \ceil($this->timeout),
            CURLOPT_CONNECTTIMEOUT => 30,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_MAXREDIRS      => 5,
            // Hub LANs usually run plain HTTP; we don't disable cert
            // verification because users CAN front the hub with TLS and
            // expect verification to work.
        ];

        if ($jsonBody !== null) {
            $opts[CURLOPT_POSTFIELDS] = \json_encode(
                $jsonBody,
                JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE,
            );
            $headers[] = 'Content-Type: application/json';
        }

        $opts[CURLOPT_HTTPHEADER] = $headers;
        \curl_setopt_array($ch, $opts);

        $response = \curl_exec($ch);
        if ($response === false) {
            $err   = \curl_error($ch);
            $errno = \curl_errno($ch);
            \curl_close($ch);
            $this->raiseTransport($method, $path, $err, $errno);
        }

        /** @var string $response */
        $status      = (int) \curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        $headerSize  = (int) \curl_getinfo($ch, CURLINFO_HEADER_SIZE);
        $contentType = (string) (\curl_getinfo($ch, CURLINFO_CONTENT_TYPE) ?: '');
        \curl_close($ch);

        $body = \substr($response, $headerSize);

        return [$status, $body, $contentType];
    }

    private function raiseHttp(string $method, string $path, int $status, string $body): never
    {
        // Keep the message readable -- cap long error bodies (full HTML
        // 500 pages are noise).
        $snippet = \strlen($body) > 500 ? \substr($body, 0, 500) . '...' : $body;
        throw new PaprikaError(
            "$method $path: HTTP $status: $snippet",
            statusCode: $status,
        );
    }

    private function raiseTransport(string $method, string $path, string $err, int $errno): never
    {
        // Mirror the Python ConnectError diagnostic. The three errno
        // values are: COULDNT_CONNECT (7), COULDNT_RESOLVE_HOST (6),
        // OPERATION_TIMEDOUT (28). The hint is by far the most common
        // gotcha for paprika scripts: hardcoding 'paprika.lan' and
        // running inside a paprika-runner sandbox where only the
        // Docker-network name 'hub' resolves.
        $hint = '';
        if (\in_array($errno, [CURLE_COULDNT_CONNECT, CURLE_COULDNT_RESOLVE_HOST, 28], true)) {
            $envHub = \getenv('PAPRIKA_HUB') ?: null;
            $hint = " (configured base_url='{$this->baseUrl}'"
                . ($envHub !== null ? ", PAPRIKA_HUB env='{$envHub}'" : '')
                . '). If this script is running inside a paprika-runner '
                . 'sandbox, the hub is reachable only on the Docker '
                . 'network -- typically http://hub:8000. LAN / mDNS '
                . "names like paprika.lan won't resolve inside the "
                . 'runner; either hardcode http://hub:8000 or pass '
                . "getenv('PAPRIKA_HUB') to Paprika::connect().";
        }
        throw new PaprikaError("$method $path: transport error: $err$hint");
    }
}
