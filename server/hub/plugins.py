"""Plugin Registry — dynamic tool invocation.

v2 Phase 7 of the architecture (see internal/v2-architecture.html).

Decouples external tools (cloudscraper, yt-dlp, ffmpeg, ...) from
paprika core code. Tools live under ``data/tools/installed/`` with a
self-describing ``plugin.json`` that declares:

  * which actions the tool exposes
  * how to invoke them (subprocess / python_lib in a venv / http service)
  * which paprika capabilities the tool maps to (cloudflare_challenge,
    video_extraction, etc.)

Paprika core calls ``invoke_plugin(name, action, params)`` and the
registry resolves the adapter. Adding a new tool means dropping files
into ``data/tools/installed/{name}/`` and editing ``registry.json``;
no paprika code changes.

Audit: every invocation appends to ``data/tools/invocations.jsonl``
so operators (and the auditor in ぱっぷす's governance) can answer
"what did this tool do and when".

Currently used by:
  * cloudscraper pre-flight on Cloudflare-barriered hosts (Phase 7a)
  * yt-dlp download (migration target -- still inline in fetcher.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tools directory resolution.
#
# Hub-side: $PAPRIKA_TOOLS_DIR or /opt/paprika/tools (bind-mounted into
#           hub container as /data/tools when running in compose).
# Worker-side: /data/tools (mounted from /opt/paprika/tools on the
#           worker host by docker-compose-worker.yml).
# Both use the same registry.json layout.
# ---------------------------------------------------------------------------


def _default_tools_dir() -> Path:
    env = os.environ.get("PAPRIKA_TOOLS_DIR")
    if env:
        return Path(env)
    for candidate in (Path("/data/tools"), Path("/opt/paprika/tools")):
        if candidate.is_dir():
            return candidate
    # Local dev fallback -- relative to project root.
    return Path(__file__).resolve().parents[2] / "data" / "tools"


TOOLS_DIR: Path = _default_tools_dir()


# ---------------------------------------------------------------------------
# Plugin descriptor (parsed from plugin.json).
# ---------------------------------------------------------------------------


@dataclass
class PluginAction:
    name: str
    description: str = ""
    entry: str = ""                       # interpretation depends on kind
    args_template: list[str] = field(default_factory=list)
    timeout_s: float = 60.0
    stdout_parser: str | None = None
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)


@dataclass
class PluginRecord:
    name: str
    version: str
    kind: str                              # python_lib | subprocess | http_service
    dir: Path
    capabilities: list[str] = field(default_factory=list)
    actions: dict[str, PluginAction] = field(default_factory=dict)
    endpoint: str = ""                     # http_service only
    disabled: bool = False
    notes: str = ""


class PluginNotAvailable(Exception):
    """Raised when ``invoke_plugin`` is called for an absent / disabled tool."""


class PluginInvocationError(Exception):
    """Adapter-level failure (process exited non-zero, HTTP 5xx, JSON parse, ...)."""


# ---------------------------------------------------------------------------
# Registry (lazy-loaded, refreshable).
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, PluginRecord] | None = None
_REGISTRY_LOADED_AT: float = 0.0
_REGISTRY_TTL_S: float = 30.0


def _parse_plugin_json(path: Path) -> PluginRecord | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _log.info("[plugins] cannot read %s: %s", path, e)
        return None
    name = d.get("name") or ""
    if not name:
        return None
    actions: dict[str, PluginAction] = {}
    for an, av in (d.get("actions") or {}).items():
        if not isinstance(av, dict):
            continue
        actions[an] = PluginAction(
            name=an,
            description=str(av.get("description") or ""),
            entry=str(av.get("entry") or ""),
            args_template=list(av.get("args_template") or []),
            timeout_s=float(av.get("timeout_s") or 60.0),
            stdout_parser=av.get("stdout_parser"),
            input_schema=dict(av.get("input_schema") or {}),
            output_schema=dict(av.get("output_schema") or {}),
        )
    return PluginRecord(
        name=name,
        version=str(d.get("version") or "?"),
        kind=str(d.get("kind") or "subprocess"),
        dir=path.parent,
        capabilities=list(d.get("capabilities") or []),
        actions=actions,
        endpoint=str(d.get("endpoint") or ""),
        disabled=bool(d.get("disabled") or False),
        notes=str(d.get("notes") or ""),
    )


def load_registry(force: bool = False) -> dict[str, PluginRecord]:
    """Read every ``plugin.json`` under TOOLS_DIR/installed/.

    Cached for ``_REGISTRY_TTL_S`` so a busy hub doesn't stat the
    filesystem on every job dispatch. Pass ``force=True`` to bust the
    cache (admin UI calls this after install/uninstall).
    """
    global _REGISTRY, _REGISTRY_LOADED_AT
    if (
        _REGISTRY is not None
        and not force
        and (time.monotonic() - _REGISTRY_LOADED_AT) < _REGISTRY_TTL_S
    ):
        return _REGISTRY

    out: dict[str, PluginRecord] = {}
    installed = TOOLS_DIR / "installed"
    if installed.is_dir():
        for sub in sorted(installed.iterdir()):
            if not sub.is_dir():
                continue
            pj = sub / "plugin.json"
            if not pj.is_file():
                continue
            rec = _parse_plugin_json(pj)
            if rec is not None:
                out[rec.name] = rec

    _REGISTRY = out
    _REGISTRY_LOADED_AT = time.monotonic()
    return out


def list_plugins() -> list[dict]:
    """Public API: a list of plugin summaries for the admin UI."""
    out = []
    for p in load_registry().values():
        out.append({
            "name": p.name,
            "version": p.version,
            "kind": p.kind,
            "capabilities": list(p.capabilities),
            "actions": list(p.actions.keys()),
            "disabled": p.disabled,
            "notes": p.notes,
        })
    return out


# ---------------------------------------------------------------------------
# Plugin catalog -- the list of plugins KNOWN to paprika (whether installed
# or not). Drives the "Available plugins" view in the admin UI. Source of
# truth is data/tools/catalog.json (operator-editable); a missing or
# corrupt file degrades to "catalog is empty, but installed plugins still
# show up via list_plugins()".
# ---------------------------------------------------------------------------


def load_catalog() -> dict:
    """Read ``data/tools/catalog.json``. Returns ``{"plugins": []}`` on miss."""
    catalog_path = TOOLS_DIR / "catalog.json"
    if not catalog_path.is_file():
        return {"version": 1, "plugins": []}
    try:
        d = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception as e:
        _log.info("[plugins] catalog.json unparseable: %s", e)
        return {"version": 1, "plugins": [], "_error": str(e)[:200]}
    # Tolerate slight schema drift -- ensure ``plugins`` is a list.
    if not isinstance(d.get("plugins"), list):
        d["plugins"] = []
    return d


def merged_catalog() -> list[dict]:
    """Return the catalog joined with current install status.

    Each entry from catalog.json is augmented with:
      * ``installed: bool``   -- does data/tools/installed/{name}/ exist
      * ``installed_version`` -- version string from installed plugin.json
      * ``actions``           -- action names from installed plugin (empty if not installed)

    Installed plugins NOT in the catalog are appended at the end with
    ``in_catalog: false`` so operators can still see locally-dropped
    plugins. This keeps both "advertised" and "rogue" plugins visible.
    """
    cat = load_catalog()
    catalog_entries = list(cat.get("plugins") or [])
    installed = load_registry()
    installed_names = set(installed.keys())

    out: list[dict] = []
    for entry in catalog_entries:
        name = (entry or {}).get("name") or ""
        if not name:
            continue
        merged = dict(entry)
        merged["installed"] = name in installed_names
        merged["in_catalog"] = True
        if merged["installed"]:
            inst = installed[name]
            merged["installed_version"] = inst.version
            merged["actions"] = list(inst.actions.keys())
        else:
            merged["installed_version"] = None
            merged["actions"] = []
        out.append(merged)

    # Append installed-but-uncatalogued plugins so operators see them too.
    catalog_names = {(e or {}).get("name") for e in catalog_entries}
    for name, inst in installed.items():
        if name in catalog_names:
            continue
        out.append({
            "name": name,
            "version": inst.version,
            "kind": inst.kind,
            "category": "uncategorized",
            "summary": (inst.notes or "")[:200],
            "capabilities": list(inst.capabilities),
            "source": "local-only",
            "default": False,
            "installed": True,
            "in_catalog": False,
            "installed_version": inst.version,
            "actions": list(inst.actions.keys()),
        })

    return out


def find_for_capability(capability: str) -> PluginRecord | None:
    """Return the first non-disabled plugin that claims this capability."""
    for p in load_registry().values():
        if p.disabled:
            continue
        if capability in p.capabilities:
            return p
    return None


# ---------------------------------------------------------------------------
# Adapters.
# ---------------------------------------------------------------------------


async def _adapter_python_lib(
    plugin: PluginRecord, action: PluginAction, params: dict,
) -> dict:
    """Invoke a Python plugin via its isolated venv.

    The plugin's ``entry`` is ``module:function``. We run a tiny
    bootstrap in the venv's python, which:
      * adds the plugin dir to sys.path,
      * imports ``module``,
      * calls ``function(**params)``,
      * prints the return value as JSON.

    The bootstrap runs out-of-process so the plugin's dependencies
    never enter the hub/worker's main interpreter.
    """
    if ":" not in action.entry:
        raise PluginInvocationError(
            f"plugin '{plugin.name}' action '{action.name}' entry must be 'module:function', got {action.entry!r}"
        )
    module, func = action.entry.split(":", 1)

    venv_python = plugin.dir / "venv" / "bin" / "python"
    if not venv_python.is_file():
        # Fallback for "no venv, use system python" plugins. Use the
        # *current* interpreter -- container Python is at
        # /usr/local/bin/python3 (slim debian image), not /usr/bin/python3.
        import sys as _sys
        venv_python = Path(_sys.executable)

    # Bootstrap: plugin.dir on sys.path (so ``adapter.py`` resolves),
    # plus ``lib/`` subdir if present (where ``pip install --target lib``
    # places third-party deps -- the recommended layout for plugins
    # without a full venv). System python finds the adapter; the lib
    # path supplies the actual library code.
    bootstrap = (
        "import sys, json, os\n"
        f"sys.path.insert(0, {str(plugin.dir)!r})\n"
        f"_lib = os.path.join({str(plugin.dir)!r}, 'lib')\n"
        "if os.path.isdir(_lib): sys.path.insert(0, _lib)\n"
        f"from {module} import {func} as _fn\n"
        "_params = json.loads(sys.stdin.read() or '{}')\n"
        "_out = _fn(**_params)\n"
        "print(json.dumps(_out, default=str))\n"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            str(venv_python), "-c", bootstrap,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise PluginInvocationError(f"venv python not found: {e}") from e

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(params).encode("utf-8")),
            timeout=action.timeout_s,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise PluginInvocationError(
            f"plugin '{plugin.name}.{action.name}' timed out after {action.timeout_s}s"
        )

    if proc.returncode != 0:
        raise PluginInvocationError(
            f"plugin '{plugin.name}.{action.name}' exited {proc.returncode}: "
            f"{stderr.decode(errors='replace')[:800]}"
        )

    text = stdout.decode(errors="replace").strip()
    if not text:
        raise PluginInvocationError(
            f"plugin '{plugin.name}.{action.name}' produced no output (stderr: {stderr.decode(errors='replace')[:400]})"
        )
    # The bootstrap may print log lines before the final JSON; grab the
    # last newline-delimited block that parses as JSON.
    for candidate in reversed(text.splitlines()):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise PluginInvocationError(
        f"plugin '{plugin.name}.{action.name}' output is not JSON: {text[:400]!r}"
    )


async def _adapter_subprocess(
    plugin: PluginRecord, action: PluginAction, params: dict,
) -> dict:
    """Invoke a CLI tool. ``entry`` is path relative to plugin dir."""
    bin_path = plugin.dir / action.entry
    if not bin_path.is_file():
        # Maybe absolute path or PATH-resolved binary
        bin_path = Path(action.entry)
    cmd = [str(bin_path)]
    # Fill {placeholder} from params.
    for arg in action.args_template:
        try:
            cmd.append(arg.format(**params))
        except KeyError as e:
            raise PluginInvocationError(
                f"plugin '{plugin.name}.{action.name}' arg template missing param {e}"
            ) from e

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise PluginInvocationError(f"plugin entry binary not found: {e}") from e

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=action.timeout_s,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise PluginInvocationError(
            f"plugin '{plugin.name}.{action.name}' timed out after {action.timeout_s}s"
        )

    out_text = stdout.decode(errors="replace")
    err_text = stderr.decode(errors="replace")

    if action.stdout_parser:
        # Pluggable parser hook -- currently unused; reserved for
        # tools whose output is non-JSON (yt-dlp's progress lines).
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": out_text[-4000:],
            "stderr": err_text[-2000:],
        }

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": out_text[-4000:],
        "stderr": err_text[-2000:],
    }


async def _adapter_http_service(
    plugin: PluginRecord, action: PluginAction, params: dict,
) -> dict:
    """Invoke a tool that runs as a separate HTTP service."""
    import httpx

    base = plugin.endpoint.rstrip("/")
    if not base:
        raise PluginInvocationError(
            f"plugin '{plugin.name}' http_service has no endpoint"
        )
    url = f"{base}/{action.entry or action.name}"
    try:
        async with httpx.AsyncClient(timeout=action.timeout_s) as client:
            r = await client.post(url, json=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise PluginInvocationError(
            f"plugin '{plugin.name}.{action.name}' HTTP call failed: {e}"
        ) from e


_ADAPTERS = {
    "python_lib":   _adapter_python_lib,
    "subprocess":   _adapter_subprocess,
    "http_service": _adapter_http_service,
}


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


async def invoke_plugin(
    name: str,
    action: str,
    params: dict | None = None,
    *,
    audit_context: dict | None = None,
) -> dict:
    """Invoke a plugin action and return the parsed result.

    Raises ``PluginNotAvailable`` if the tool isn't installed; raises
    ``PluginInvocationError`` on adapter-level failures. The caller is
    expected to handle both (e.g., fall back to "no cookies" when
    cloudscraper isn't there).

    ``audit_context`` is logged into invocations.jsonl alongside the
    invocation -- pass ``{"job_id": ..., "host": ...}`` so operators
    can trace back which job triggered which tool call.
    """
    params = params or {}
    audit_context = audit_context or {}

    registry = load_registry()
    plugin = registry.get(name)
    if plugin is None:
        raise PluginNotAvailable(f"plugin '{name}' not installed")
    if plugin.disabled:
        raise PluginNotAvailable(f"plugin '{name}' is disabled")
    act = plugin.actions.get(action)
    if act is None:
        raise PluginNotAvailable(
            f"plugin '{name}' has no action '{action}' "
            f"(available: {list(plugin.actions.keys())})"
        )
    adapter = _ADAPTERS.get(plugin.kind)
    if adapter is None:
        raise PluginInvocationError(f"plugin '{name}' has unknown kind '{plugin.kind}'")

    t0 = time.monotonic()
    err = None
    result: dict | None = None
    try:
        result = await adapter(plugin, act, params)
        return result
    except Exception as e:
        err = e
        raise
    finally:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        try:
            _audit(
                name=name,
                action=action,
                params=params,
                result_ok=(err is None),
                error=str(err) if err else "",
                elapsed_ms=elapsed_ms,
                context=audit_context,
            )
        except Exception:
            pass  # never let auditing break the call path


def _audit(
    *,
    name: str,
    action: str,
    params: dict,
    result_ok: bool,
    error: str,
    elapsed_ms: int,
    context: dict,
) -> None:
    from datetime import datetime
    path = TOOLS_DIR / "invocations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Redact obvious secret-looking values from params for the audit log.
    safe_params: dict = {}
    for k, v in params.items():
        sk = k.lower()
        if any(s in sk for s in ("password", "secret", "token", "auth")):
            safe_params[k] = "<redacted>"
        else:
            safe_params[k] = v
    entry = {
        "at":         datetime.utcnow().isoformat() + "Z",
        "plugin":     name,
        "action":     action,
        "ok":         result_ok,
        "elapsed_ms": elapsed_ms,
        **({"error": error[:300]} if error else {}),
        "params":     safe_params,
        **context,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
