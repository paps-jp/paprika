#!/usr/bin/env python3
"""paprika-mcp-server — Full Paprika Hub API as MCP tools.

Auto-discovers endpoints from the Hub's OpenAPI schema at startup,
so every REST endpoint is callable from Claude Code (or any MCP client).

Setup (Claude Code):
    Add to .claude/settings.json → mcpServers:

    "paprika": {
      "command": "uv",
      "args": ["run", "--with", "mcp", "--with", "httpx",
               "python", "client/mcp/paprika_mcp_server.py"],
      "env": {"PAPRIKA_HUB": "http://paprika.lan:8000"}
    }

    Or if deps are already installed:

    "paprika": {
      "command": "python",
      "args": ["client/mcp/paprika_mcp_server.py"],
      "env": {"PAPRIKA_HUB": "http://paprika.lan:8000"}
    }

Env vars:
    PAPRIKA_HUB          Hub base URL  (default: http://localhost:8000)
    PAPRIKA_MCP_TIMEOUT   Per-request timeout in seconds (default: 120)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from typing import Any

import httpx
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# ── Config ────────────────────────────────────────────────────────────

HUB_URL = os.environ.get("PAPRIKA_HUB", "http://localhost:8000")
REQUEST_TIMEOUT = float(os.environ.get("PAPRIKA_MCP_TIMEOUT", "120"))

# ── Endpoint filters ──────────────────────────────────────────────────

# Paths containing these substrings are skipped (UI / static / internal)
_SKIP_PATH_CONTAINS = (
    "/ui/",
    "/static/",
    "worker-source.tar.gz",
    "/screencast/",
    "/novnc/",
    "/icon.svg",
    "/favicon.ico",
    "/screenshots",       # HTML screenshot gallery page
)

# Specific (method, path) combos to skip (binary upload, passthrough)
_SKIP_EXACT: set[tuple[str, str]] = {
    ("post", "/jobs/{job_id}/assets"),       # multipart binary upload
    ("post", "/jobs/{job_id}/files/{kind}"), # multipart binary upload
    ("post", "/profiles/{name}"),            # tar.gz profile upload
    ("get", "/profiles/{name}"),             # tar.gz download
    ("get", "/{full_url}"),                  # passthrough proxy
    ("get", "/"),                            # admin UI HTML
    ("get", "/extensions/{slug}/download"),  # binary extension download
}

# ── OpenAPI schema state ──────────────────────────────────────────────

_openapi: dict[str, Any] = {}
_tools: dict[str, dict[str, Any]] = {}

# ── Schema resolution ─────────────────────────────────────────────────


def _resolve_ref(node: Any, depth: int = 0) -> Any:
    """Recursively resolve $ref pointers in an OpenAPI schema.

    Caps recursion at 12 levels to guard against circular refs.
    """
    if depth > 12:
        return node
    if isinstance(node, dict):
        if "$ref" in node:
            ref_path = node["$ref"].lstrip("#/").split("/")
            target: Any = _openapi
            for part in ref_path:
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return _resolve_ref(target, depth + 1)
        return {k: _resolve_ref(v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_ref(v, depth + 1) for v in node]
    return node


# ── operationId → clean tool name ────────────────────────────────────


def _fastapi_path_slug(path: str) -> str:
    """Reproduce the path slug that FastAPI appends to operationId.

    /jobs/{job_id}/cancel  →  jobs__job_id__cancel
    /jobs                  →  jobs
    """
    parts = path.strip("/").split("/")
    slugs: list[str] = []
    for p in parts:
        if p.startswith("{") and p.endswith("}"):
            slugs.append(f"_{p[1:-1]}_")
        else:
            slugs.append(p)
    return "_".join(slugs)


def _normalise_slug(s: str) -> str:
    """Normalise a slug for comparison.

    FastAPI's operationId replaces dots and hyphens in paths with
    underscores, so we do the same when matching the path slug
    against the operationId.
    """
    return re.sub(r"[^a-zA-Z0-9_]", "_", s)


def _extract_func_name(op_id: str, method: str, path: str) -> str:
    """Extract the Python function name from a FastAPI operationId.

    FastAPI auto-generates: {func_name}_{path_slug}_{method}
    We strip the trailing _{path_slug}_{method} to recover the function name.
    """
    # Strip trailing _{method}
    suffix = f"_{method}"
    base = op_id[: -len(suffix)] if op_id.endswith(suffix) else op_id

    # Try exact slug match first, then normalised match
    slug_raw = _fastapi_path_slug(path)
    slug_norm = _normalise_slug(slug_raw)

    for slug in (slug_raw, slug_norm):
        slug_suffix = f"_{slug}"
        if base.endswith(slug_suffix):
            name = base[: -len(slug_suffix)]
            if name:
                return re.sub(r"[^a-zA-Z0-9_]", "_", name)

    # Heuristic fallback: the path components appear at the end of the
    # operationId.  Walk backwards and find where they start.
    # e.g. "download_video_for_job_jobs__job_id__download_video"
    #       func = "download_video_for_job"
    #       slug = "jobs__job_id__download_video" (normalised)
    # Split the base on the first path component to isolate the func name.
    first_component = path.strip("/").split("/")[0]
    # Find the last occurrence of _{first_component}_ in base
    marker = f"_{first_component}_"
    idx = base.rfind(marker)
    if idx > 0:
        name = base[:idx]
        if name:
            return re.sub(r"[^a-zA-Z0-9_]", "_", name)

    # Fallback: if stripping left us empty, use the whole operationId
    return re.sub(r"[^a-zA-Z0-9_]", "_", base if base else op_id)


# ── Tool registration from OpenAPI ────────────────────────────────────


def _simplify_schema_for_desc(schema: dict) -> str:
    """Produce a compact one-line hint of an object schema for descriptions."""
    if schema.get("type") != "object" or "properties" not in schema:
        return json.dumps(schema, ensure_ascii=False)[:300]
    props = schema["properties"]
    parts: list[str] = []
    req = set(schema.get("required", []))
    for pname, pschema in list(props.items())[:15]:
        t = pschema.get("type", "any")
        default = pschema.get("default")
        star = "*" if pname in req else ""
        d = f"={json.dumps(default)}" if default is not None else ""
        parts.append(f"{pname}{star}:{t}{d}")
    extra = len(props) - 15
    if extra > 0:
        parts.append(f"…+{extra}")
    return "{ " + ", ".join(parts) + " }"


def _build_input_schema(operation: dict, path: str) -> dict[str, Any]:
    """Build a JSON Schema for a tool's input parameters.

    Merges path params, query params, and request body properties
    into a single flat object schema.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    # Path and query parameters
    for param in operation.get("parameters", []):
        pname: str = param["name"]
        pin: str = param.get("in", "query")
        pschema = _resolve_ref(param.get("schema", {"type": "string"}))

        prop: dict[str, Any] = {}
        for key in ("type", "enum", "default", "items"):
            if key in pschema:
                prop[key] = pschema[key]
        if not prop.get("type"):
            # anyOf nullable pattern → pick the first non-null type
            any_of = pschema.get("anyOf", [])
            for branch in any_of:
                if branch.get("type") and branch["type"] != "null":
                    prop["type"] = branch["type"]
                    break

        desc_parts: list[str] = []
        if param.get("description"):
            desc_parts.append(param["description"])
        if pin == "path":
            desc_parts.append("(path)")
        prop["description"] = " ".join(desc_parts) if desc_parts else pname

        properties[pname] = prop
        if param.get("required") or pin == "path":
            required.append(pname)

    # Request body (application/json only)
    req_body = operation.get("requestBody", {})
    content = req_body.get("content", {}).get("application/json", {})
    body_schema = content.get("schema")
    if body_schema:
        resolved = _resolve_ref(body_schema)
        if resolved.get("type") == "object" and "properties" in resolved:
            # Merge body properties directly into the tool schema
            for prop_name, prop_schema in resolved["properties"].items():
                if prop_name in properties:
                    continue  # path/query param takes precedence
                properties[prop_name] = prop_schema
            for r in resolved.get("required", []):
                if r not in required:
                    required.append(r)
        else:
            # Non-object body → wrap as _body
            properties["_body"] = {
                "description": "Request body (JSON): "
                + json.dumps(resolved, ensure_ascii=False)[:500],
            }

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _register_tools_from_openapi(spec: dict[str, Any]) -> None:
    """Parse the full OpenAPI spec and populate the _tools registry."""
    global _openapi
    _openapi = spec

    seen_names: dict[str, int] = {}

    for path, path_item in spec.get("paths", {}).items():
        if any(skip in path for skip in _SKIP_PATH_CONTAINS):
            continue

        for method in ("get", "post", "put", "delete"):
            operation = path_item.get(method)
            if not operation:
                continue
            if (method, path) in _SKIP_EXACT:
                continue

            op_id: str = operation.get("operationId", "")
            if op_id:
                tool_name = _extract_func_name(op_id, method, path)
            else:
                slug = (
                    path.strip("/")
                    .replace("/", "_")
                    .replace("{", "")
                    .replace("}", "")
                )
                tool_name = f"{method}_{slug}"

            # Deduplicate: append _N if name collision
            if tool_name in seen_names:
                seen_names[tool_name] += 1
                tool_name = f"{tool_name}_{seen_names[tool_name]}"
            else:
                seen_names[tool_name] = 0

            # Build description
            summary = operation.get("summary", "")
            desc_text = operation.get("description", "")
            tag = (operation.get("tags") or [""])[0]

            lines: list[str] = []
            if summary:
                lines.append(summary)
            lines.append(f"`{method.upper()} {path}`")
            if desc_text and desc_text != summary:
                # Truncate long descriptions
                if len(desc_text) > 500:
                    desc_text = desc_text[:500] + "…"
                lines.append(desc_text)

            full_desc = "\n\n".join(lines)

            input_schema = _build_input_schema(operation, path)

            _tools[tool_name] = {
                "method": method.upper(),
                "path_template": path,
                "description": full_desc,
                "input_schema": input_schema,
                "tag": tag,
                "operation": operation,
            }


# ── HTTP request dispatch ─────────────────────────────────────────────


async def _hub_request_raw(
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[Any, str | None]:
    """Send an HTTP request and return ``(parsed_data, error_prefix)``.

    *parsed_data* is the decoded JSON (list / dict / scalar) when the
    response has a JSON content-type, or a plain string otherwise.
    *error_prefix* is ``"HTTP 4xx"`` on error responses, else ``None``.
    """
    async with httpx.AsyncClient(
        base_url=HUB_URL, timeout=REQUEST_TIMEOUT
    ) as client:
        resp = await client.request(
            method, path, params=query or None, json=body or None
        )
        err: str | None = None
        if resp.status_code >= 400:
            err = f"HTTP {resp.status_code}"

        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            try:
                return resp.json(), err
            except Exception:
                return resp.text, err
        if "text" in ct or "xml" in ct:
            return resp.text, err
        return f"[Binary response: {ct}, {len(resp.content)} bytes]", err


_MAX_RESPONSE_CHARS = 80_000


def _format_response(
    data: Any,
    err: str | None,
    page: int = 1,
    per_page: int = 50,
) -> str:
    """Serialise *data* to JSON text, applying pagination for arrays.

    If the paginated slice still exceeds ``_MAX_RESPONSE_CHARS`` the
    page size is halved automatically until it fits, so the response
    is always valid JSON.
    """
    if isinstance(data, list):
        total = len(data)
        if total > per_page or page > 1:
            # Try with requested per_page; shrink if the slice is too big
            effective_pp = per_page
            while effective_pp >= 1:
                total_pages = max(1, (total + effective_pp - 1) // effective_pp)
                clamped_page = max(1, min(page, total_pages))
                start = (clamped_page - 1) * effective_pp
                end = start + effective_pp
                payload = {
                    "items": data[start:end],
                    "_pagination": {
                        "page": clamped_page,
                        "per_page": effective_pp,
                        "total_items": total,
                        "total_pages": total_pages,
                    },
                }
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                if len(text) <= _MAX_RESPONSE_CHARS:
                    break
                effective_pp = max(1, effective_pp // 2)
            else:
                # Even 1 item is too big — fall back to truncated text
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                text = text[: _MAX_RESPONSE_CHARS - 100] + (
                    f"\n\n… [truncated single item, {len(text)} chars]"
                )
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2)
    elif isinstance(data, dict):
        text = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        text = str(data)

    if err:
        text = f"{err}\n{text}"

    # Safety net for non-array responses
    if len(text) > _MAX_RESPONSE_CHARS:
        text = (
            text[: _MAX_RESPONSE_CHARS - 100]
            + f"\n\n… [truncated, total {len(text)} chars]"
        )
    return text


async def _hub_request(
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    page: int = 1,
    per_page: int = 50,
) -> str:
    """Send an HTTP request to the Hub and return formatted text."""
    data, err = await _hub_request_raw(
        method, path, query=query, body=body
    )
    return _format_response(data, err, page=page, per_page=per_page)


def _separate_params(
    arguments: dict[str, Any], operation: dict[str, Any], path_template: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Split tool arguments into (url_path, query_params, body_params).

    Path parameters are substituted into the URL template.
    Query parameters (declared "in": "query") go to the query dict.
    Everything else goes to the body dict.
    """
    args = dict(arguments)  # copy
    path = path_template

    # Substitute path params
    path_param_names = set(re.findall(r"\{(\w+)\}", path_template))
    for pp in path_param_names:
        val = args.pop(pp, "")
        path = path.replace(f"{{{pp}}}", str(val))

    # Identify query param names from the operation spec
    query_param_names = {
        p["name"]
        for p in operation.get("parameters", [])
        if p.get("in") == "query"
    }

    query: dict[str, Any] = {}
    body: dict[str, Any] = {}
    for key, val in args.items():
        if key.startswith("_"):
            continue
        if key in query_param_names:
            query[key] = val
        else:
            body[key] = val

    # Explicit _body overrides
    if "_body" in args and isinstance(args["_body"], dict):
        body = args["_body"]

    return path, query, body


# ── Convenience tools ─────────────────────────────────────────────────

_FETCH_AND_WAIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "The URL to fetch.",
        },
        "mode": {
            "type": "string",
            "enum": ["fetch", "codegen-loop"],
            "description": "Job mode. Default: fetch",
            "default": "fetch",
        },
        "poll_interval": {
            "type": "number",
            "description": "Seconds between status polls (default: 3)",
            "default": 3,
        },
        "timeout": {
            "type": "number",
            "description": "Max seconds to wait for completion (default: 300)",
            "default": 300,
        },
        "wait_seconds": {
            "type": "integer",
            "description": "Fetch wait_seconds option (default: 20)",
            "default": 20,
        },
        "cookies_from": {
            "type": "string",
            "description": "Host name to inject cookies from (optional)",
        },
        "download_video": {
            "type": "boolean",
            "description": "Run yt-dlp video download (default: false)",
            "default": False,
        },
        "goal": {
            "type": "string",
            "description": "Goal text for codegen-loop mode (optional)",
        },
    },
    "required": ["url"],
}


async def _fetch_and_wait(arguments: dict[str, Any]) -> str:
    """Submit a job and poll until completion, returning the full result."""
    url = arguments["url"]
    mode = arguments.get("mode", "fetch")
    poll_interval = arguments.get("poll_interval", 3)
    timeout = arguments.get("timeout", 300)

    options: dict[str, Any] = {"mode": mode}
    for opt_key in (
        "wait_seconds",
        "cookies_from",
        "download_video",
        "goal",
    ):
        if opt_key in arguments and arguments[opt_key] is not None:
            options[opt_key] = arguments[opt_key]

    # Submit job
    submit_resp = await _hub_request(
        "POST", "/jobs", body={"url": url, "options": options}
    )
    try:
        job = json.loads(submit_resp)
    except json.JSONDecodeError:
        return f"Failed to submit job:\n{submit_resp}"

    job_id = job.get("job_id")
    if not job_id:
        return f"No job_id in response:\n{submit_resp}"

    # Poll for completion
    deadline = time.time() + timeout
    status = job.get("status", "queued")
    while status in ("queued", "running") and time.time() < deadline:
        await asyncio.sleep(poll_interval)
        info_text = await _hub_request("GET", f"/jobs/{job_id}")
        try:
            info = json.loads(info_text)
            status = info.get("status", status)
        except json.JSONDecodeError:
            pass

    # Gather result
    parts: list[str] = [f"## Job {job_id} — {status}\n"]

    result_text = await _hub_request("GET", f"/jobs/{job_id}/result")
    parts.append(f"### Result\n{result_text}\n")

    assets_text = await _hub_request("GET", f"/jobs/{job_id}/assets.json")
    parts.append(f"### Assets\n{assets_text}\n")

    if status not in ("completed", "failed", "cancelled"):
        parts.append(
            f"\n⚠ Job did not finish within {timeout}s (status: {status})"
        )

    return "\n".join(parts)


_GENERIC_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method": {
            "type": "string",
            "enum": ["GET", "POST", "PUT", "DELETE"],
            "description": "HTTP method",
        },
        "path": {
            "type": "string",
            "description": "Request path, e.g. /jobs or /hosts/example.com",
        },
        "query": {
            "type": "object",
            "description": "Query parameters as key-value pairs (optional)",
        },
        "body": {
            "type": "object",
            "description": "JSON request body (optional, for POST/PUT)",
        },
    },
    "required": ["method", "path"],
}


# ── MCP server ────────────────────────────────────────────────────────

server = Server("paprika")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """Return all auto-generated tools plus convenience tools."""
    tools: list[types.Tool] = []

    # Convenience: fetch_and_wait
    tools.append(
        types.Tool(
            name="paprika_fetch_and_wait",
            description=(
                "Submit a job (fetch or codegen-loop) and wait for it to complete.\n\n"
                "This is the main high-level tool: give it a URL and it returns the "
                "full result including assets list. Equivalent to POST /jobs + polling "
                "GET /jobs/{id} + GET /jobs/{id}/result + GET /jobs/{id}/assets.json."
            ),
            inputSchema=_FETCH_AND_WAIT_SCHEMA,
        )
    )

    # Convenience: generic request (catch-all)
    tools.append(
        types.Tool(
            name="paprika_request",
            description=(
                "Send an arbitrary HTTP request to the Paprika Hub API.\n\n"
                "Use this for endpoints not covered by the auto-generated tools, "
                "or when you need full control over the request. "
                f"Hub URL: {HUB_URL}"
            ),
            inputSchema=_GENERIC_REQUEST_SCHEMA,
        )
    )

    # Convenience: get schema for discovery
    tools.append(
        types.Tool(
            name="paprika_schema",
            description=(
                "Return the OpenAPI schema for the Paprika Hub API.\n\n"
                "Use this to discover available endpoints, request/response formats, "
                "and parameter details. Filter by path substring to narrow results."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path_filter": {
                        "type": "string",
                        "description": (
                            "Filter to paths containing this string "
                            "(e.g. '/jobs', '/sessions', '/hosts')"
                        ),
                    },
                    "components_only": {
                        "type": "boolean",
                        "description": "Return only component schemas (models)",
                        "default": False,
                    },
                },
            },
        )
    )

    # Auto-generated tools from OpenAPI
    for name, info in _tools.items():
        tools.append(
            types.Tool(
                name=name,
                description=info["description"],
                inputSchema=info["input_schema"],
            )
        )

    return tools


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    """Dispatch a tool call."""
    arguments = arguments or {}

    # Extract pagination meta-params (available on every tool)
    _page = int(arguments.pop("_page", 1))
    _per_page = int(arguments.pop("_per_page", 50))

    try:
        # ── Convenience tools ──
        if name == "paprika_fetch_and_wait":
            text = await _fetch_and_wait(arguments)
            return [types.TextContent(type="text", text=text)]

        if name == "paprika_request":
            text = await _hub_request(
                arguments.get("method", "GET"),
                arguments.get("path", "/"),
                query=arguments.get("query"),
                body=arguments.get("body"),
                page=_page,
                per_page=_per_page,
            )
            return [types.TextContent(type="text", text=text)]

        if name == "paprika_schema":
            path_filter = arguments.get("path_filter", "")
            if arguments.get("components_only"):
                data = _openapi.get("components", {}).get("schemas", {})
                text = json.dumps(data, ensure_ascii=False, indent=2)
            elif path_filter:
                filtered = {
                    p: v
                    for p, v in _openapi.get("paths", {}).items()
                    if path_filter in p
                }
                text = json.dumps(
                    {"paths": filtered}, ensure_ascii=False, indent=2
                )
            else:
                # Return a summary, not the full spec (can be huge)
                paths = _openapi.get("paths", {})
                summary: dict[str, list[str]] = {}
                for p, methods in paths.items():
                    summary[p] = [
                        m.upper()
                        for m in ("get", "post", "put", "delete")
                        if m in methods
                    ]
                text = json.dumps(
                    {
                        "info": _openapi.get("info", {}),
                        "paths_summary": summary,
                        "component_schemas": list(
                            _openapi.get("components", {})
                            .get("schemas", {})
                            .keys()
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            if len(text) > 80_000:
                text = text[:80_000] + "\n\n… [truncated]"
            return [types.TextContent(type="text", text=text)]

        # ── Auto-generated API tools ──
        info = _tools.get(name)
        if not info:
            return [
                types.TextContent(
                    type="text",
                    text=f"Unknown tool: {name}. Use paprika_schema to discover endpoints.",
                )
            ]

        path, query, body = _separate_params(
            arguments, info["operation"], info["path_template"]
        )
        # For GET/DELETE, move leftover body params to query
        # (some FastAPI endpoints accept query params not declared
        # in the OpenAPI spec, e.g. ?limit=10)
        if info["method"] in ("GET", "DELETE") and body:
            query.update(body)
            body = {}

        text = await _hub_request(
            info["method"], path, query=query, body=body or None,
            page=_page, per_page=_per_page,
        )
        return [types.TextContent(type="text", text=text)]

    except httpx.TimeoutException:
        return [
            types.TextContent(
                type="text",
                text=f"⚠ Request timed out after {REQUEST_TIMEOUT}s",
            )
        ]
    except httpx.ConnectError as exc:
        return [
            types.TextContent(
                type="text",
                text=f"⚠ Cannot connect to Hub at {HUB_URL}: {exc}",
            )
        ]
    except Exception as exc:
        return [
            types.TextContent(type="text", text=f"⚠ Error: {type(exc).__name__}: {exc}")
        ]


# ── Main entry point ──────────────────────────────────────────────────


async def _init_and_run() -> None:
    """Fetch the OpenAPI spec from the Hub, register tools, run MCP."""
    # Fetch OpenAPI spec
    try:
        async with httpx.AsyncClient(
            base_url=HUB_URL, timeout=30
        ) as client:
            resp = await client.get("/openapi.json")
            resp.raise_for_status()
            spec = resp.json()
        _register_tools_from_openapi(spec)
        print(
            f"[paprika-mcp] Loaded {len(_tools)} tools from {HUB_URL}/openapi.json",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"[paprika-mcp] WARNING: Failed to load OpenAPI spec from {HUB_URL}: {exc}",
            file=sys.stderr,
        )
        print(
            "[paprika-mcp] Starting with convenience tools only "
            "(paprika_request, paprika_fetch_and_wait).",
            file=sys.stderr,
        )

    # Run the MCP stdio server
    async with stdio_server() as streams:
        await server.run(
            streams[0],
            streams[1],
            server.create_initialization_options(),
        )


def main() -> None:
    """Entry point."""
    asyncio.run(_init_and_run())


if __name__ == "__main__":
    main()
