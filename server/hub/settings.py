"""Hub-wide runtime settings.

A small JSON file at ``{data_dir}/settings.json`` stores the handful
of knobs that benefit from being mutable at runtime via the admin UI
(skill / convention auto-extract toggles, skill retrieval top-K).

Things that aren't here:
  * LLM URLs / model names -- env-controlled, require deploy to swap.
  * Per-Submit-form defaults -- those live in the operator's
    browser localStorage (one operator = one preference set).
  * Per-host things -- HostRegistry has them.

The registry exposes a dict-like API with a typed schema and
sensible env-derived defaults so an unset key falls back to the
deploy-time configuration.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# Default schema. Keys map to (default, "type"). The type label is
# advisory -- it shapes how the UI renders the field.
_SCHEMA: dict[str, tuple[Any, str]] = {
    # Auto-extraction toggles -- whether codegen-loop SUCCESS triggers
    # the skill / convention distillation LLM calls.
    "skill_auto_extract_enabled": (True, "bool"),
    "convention_auto_extract_enabled": (True, "bool"),
    # How many skills the retriever picks per new job before injection.
    "skill_retrieval_top_k": (3, "int"),
    # Minimum byte size for a captured asset. Anything smaller than
    # this is dropped on the floor instead of written to the gallery.
    # Set to 0 (default) to disable -- save everything. Useful values:
    #   1024  -- skip 1KB-and-under "decorative" icons / 1px trackers
    #   4096  -- skip small SVG icons + favicons
    #   10240 -- skip thumbnails too
    # Applied by both core.fetcher (Fetch mode) and the worker session
    # asset capture (Code / LLM modes via paprika-runner sessions).
    "min_asset_size_bytes": (0, "int"),
    # ---- Fetch defaults --------------------------------------------------
    # Mirrors of FetchOptions / JobOptions knobs. The hub overlays these
    # onto JobOptions on dispatch for any field the client didn't set
    # explicitly (Pydantic model_fields_set). Applies primarily to
    # Fetch mode -- Code / LLM modes don't go through core.fetcher.
    #
    # Pydantic JobOptions defaults are reproduced here so a fresh
    # SettingsRegistry matches existing behaviour byte-for-byte.
    "fetch_wait_seconds": (20, "int"),
    "fetch_settle_seconds": (0.0, "float"),
    "fetch_idle_seconds": (3.0, "float"),
    "fetch_max_wait_seconds": (60.0, "float"),
    "fetch_scroll": (False, "bool"),
    "fetch_scroll_step": (50, "int"),
    "fetch_scroll_max": (3000, "int"),
    "fetch_scroll_early_after": (5.0, "float"),
    "fetch_post_click_seconds": (5.0, "float"),
    # ---- Codegen web_search tool (SearXNG-backed) ------------------------
    # When ``searxng_url`` is non-empty AND the Coder's engine has
    # supports_tools=True, the hub attaches a ``web_search`` OpenAI tool
    # to the request so the LLM can look up external facts (third-party
    # API shapes, unfamiliar site selectors). Empty URL -> feature off.
    # Both knobs fall back to SEARXNG_URL / SEARXNG_TIMEOUT_S env vars
    # via _env_default; see server/hub/web_search.py.
    "searxng_url": ("", "str"),
    "searxng_timeout_s": (15.0, "float"),
    # Per-attempt cap on how many web_search calls the LLM may make
    # inside one generate_script. The Coder usually needs 0-2; the cap
    # bounds token cost / latency when a confused model keeps re-
    # searching. 0 -> tool effectively off (no calls allowed) even when
    # SearXNG is reachable.
    "web_search_max_calls": (5, "int"),
    # ---- Storage: alternative data directory ----------------------------
    # When non-empty, job artifact directories ({job_id}/, assets/,
    # page.html, log.txt, …) are written to this path instead of the
    # default ``data_dir``.  Designed for mounting an SMB share so large
    # captures live on a NAS while hub metadata (skills, conventions,
    # hosts, engines, settings.json) stays on fast local storage.
    # Empty string (default) = use ``data_dir`` as before.
    "storage_dir": ("", "str"),
    # ---- SMB connection settings -----------------------------------------
    # Full SMB share connection parameters. When configured and mounted,
    # ``storage_dir`` is automatically set to ``smb_mount_point``.
    "smb_server": ("", "str"),           # e.g. "192.168.1.100"
    "smb_share": ("", "str"),            # e.g. "paprika"
    "smb_username": ("", "str"),         # e.g. "guest"
    "smb_password": ("", "str"),         # SMB password (stored in settings.json)
    "smb_mount_point": ("/mnt/paprika", "str"),  # local mount path
    "smb_mount_options": ("", "str"),    # extra mount -o options (e.g. "vers=3.0")
    # When True (default) the hub mounts the configured SMB share at
    # startup and a background watchdog re-mounts it within ~30s if the
    # mount drops (host/container restart, NAS reboot, network blip).
    # Set False to manage the mount entirely by hand -- the manual
    # /settings/smb/unmount endpoint flips this off so the watchdog
    # doesn't fight a deliberate unmount; /settings/smb/mount flips it
    # back on. See server/hub/smb_mount.py.
    "smb_auto_mount": (True, "bool"),
    # ---- Windows portable: Chrome headless ------------------------------
    # When True, the bundled Chromium starts with ``--headless=new`` so
    # the operator's physical desktop isn't taken over by paprika's job
    # browser. Lane preview thumbnails (CDP screenshot) still work; the
    # live noVNC viewer doesn't render anything (= no physical pixels to
    # capture) and is hidden by the platform=windows label gate in
    # routes/novnc.py.
    #
    # Takes effect on the NEXT paprika.exe start. (Chrome is launched
    # once at boot; toggling this setting at runtime doesn't migrate
    # the running Chromium.) fleet 版の Linux worker は ``--headless``
    # を使わない設計 (Xvfb 仮想 display + lane VNC で見るので) なの
    # で、この knob は実質 Windows portable 専用。
    "worker_chrome_headless": (False, "bool"),
}


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _env_default(key: str, fallback: Any) -> Any:
    """Resolve a default from env first, then fallback to the static
    schema. Lets a deploy-time env continue to seed initial values
    even after the settings file exists."""
    # Map a few key env names that pre-existed.
    env_map = {
        "skill_auto_extract_enabled": ("SKILL_AUTO_EXTRACT_ENABLED", "bool"),
        "convention_auto_extract_enabled": ("CONVENTION_AUTO_EXTRACT_ENABLED", "bool"),
        "skill_retrieval_top_k": ("SKILL_RETRIEVAL_TOP_K", "int"),
        # Storage: alternative data directory for job artifacts.
        "storage_dir": ("STORAGE_DIR", "str"),
        "smb_server": ("SMB_SERVER", "str"),
        "smb_share": ("SMB_SHARE", "str"),
        "smb_username": ("SMB_USERNAME", "str"),
        "smb_password": ("SMB_PASSWORD", "str"),
        "smb_mount_point": ("SMB_MOUNT_POINT", "str"),
        # Codegen web_search: settings.json -> env vars -> static default.
        "searxng_url": ("SEARXNG_URL", "str"),
        "searxng_timeout_s": ("SEARXNG_TIMEOUT_S", "float"),
        "web_search_max_calls": ("WEB_SEARCH_MAX_CALLS", "int"),
    }
    info = env_map.get(key)
    if not info:
        return fallback
    env_name, kind = info
    raw = os.environ.get(env_name)
    if raw is None:
        return fallback
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            return fallback
    if kind == "float":
        try:
            return float(raw)
        except ValueError:
            return fallback
    if kind == "str":
        # No coercion needed -- env values are already strings. Strip
        # to drop trailing whitespace on .env edits.
        return raw.strip()
    return raw


class SettingsRegistry:
    """File-backed dict of hub-wide settings."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "settings.json"
        # Cached state. Lazily loaded.
        self._cache: dict | None = None

    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        data: dict = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        self._cache = data
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data["_updated_at"] = _utcnow_iso()
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self._cache = data

    def schema(self) -> dict:
        """The merged default-set: env > static fallback. Used by the
        UI to know what keys exist + their default values."""
        out = {}
        for k, (fb, kind) in _SCHEMA.items():
            out[k] = {
                "default": _env_default(k, fb),
                "type": kind,
            }
        return out

    def all(self) -> dict:
        """Return the full effective settings (file value or
        env-default-or-schema fallback per key)."""
        stored = self._load()
        out = {}
        for k, (fb, kind) in _SCHEMA.items():
            if k in stored:
                out[k] = stored[k]
            else:
                out[k] = _env_default(k, fb)
        # Include metadata fields too (e.g. _updated_at).
        for k, v in stored.items():
            if k.startswith("_"):
                out[k] = v
        return out

    def is_set(self, key: str) -> bool:
        """True iff ``key`` was explicitly written to settings.json
        (NOT counting the env-fallback default). Used by first-run
        dialogs to detect "operator hasn't been asked yet"."""
        return key in self._load()

    def get(self, key: str, default: Any = None) -> Any:
        """Single-key getter. Falls back to env / schema default
        when the key isn't in the persisted file."""
        if key.startswith("_"):
            return self._load().get(key, default)
        stored = self._load()
        if key in stored:
            return stored[key]
        if key in _SCHEMA:
            fb, _ = _SCHEMA[key]
            return _env_default(key, fb)
        return default

    def _coerce(self, kind: str, v: Any, fallback: Any) -> Any:
        """Best-effort coerce ``v`` to the schema's declared type."""
        if kind == "bool":
            return bool(v)
        if kind == "int":
            try:
                return int(v)
            except (TypeError, ValueError):
                return fallback
        if kind == "float":
            try:
                return float(v)
            except (TypeError, ValueError):
                return fallback
        if kind == "str":
            # Coerce + strip. None / missing -> empty string so the
            # admin UI sees a stable value (and the env-fallback path
            # gets a chance via _env_default when this is later read).
            if v is None:
                return ""
            return str(v).strip()
        return v

    def replace_all(self, new_values: dict) -> dict:
        """Replace the entire persisted settings dict (operator did a
        full save from the admin UI). Returns the effective view.
        Validates each key against the schema; unknown keys are
        dropped silently."""
        cleaned: dict = {}
        for k, (fb, kind) in _SCHEMA.items():
            if k not in new_values:
                continue
            cleaned[k] = self._coerce(kind, new_values[k], fb)
        self._write(cleaned)
        return self.all()

    def update(self, partial: dict) -> dict:
        """Partial update -- merge ``partial`` into the persisted
        dict, leaving other keys alone."""
        merged = dict(self._load())
        for k, (fb, kind) in _SCHEMA.items():
            if k not in partial:
                continue
            merged[k] = self._coerce(kind, partial[k], fb)
        # Drop the metadata key before writing -- _write re-stamps it.
        merged.pop("_updated_at", None)
        self._write(merged)
        return self.all()
