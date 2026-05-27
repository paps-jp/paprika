"""Engine registry: pluggable AI backends (LLM / VLM / VLA).

Holds the operator-managed list of AI engines that paprika can call
when a script uses ``page.agent(engine="X")`` or ``page.ask()``.

Each record is one external service ── could be a hosted vendor
(OpenAI, Anthropic, Google), a self-hosted vLLM / Ollama instance,
or paprika's own bundled services (the in-compose ``agent_service``
that wraps Qwen).

Storage layout::

    {data_dir}/engines/<slug>.json

One file per engine, contents are the JSON of :class:`EngineRecord`.

There is no auto-seeding -- operators add each engine explicitly via
the admin UI. Typical entries:

  * ``qwen``      ── kind=vision-chat, protocol=agent-service
                    (AGENT_URL agent_service wrapper, the eye)
  * ``qwen-chat`` ── kind=chat,        protocol=openai
                    (raw AGENT_LLM_URL for translation + page.ask)
  * ``deepseek-r1`` ── kind=reasoning, protocol=openai
                    (R1 brain: judge, distiller, strategist)

Built-in records carry ``builtin=True`` and are read-only in the
admin UI (operators can promote / demote them but not delete or
rename). Users can add as many extra engines as they like via
``PUT /engines/{slug}`` -- typically OpenAI-compat chat backends
(OpenAI, OpenRouter, LiteLLM-proxied Claude, etc.) for ``page.ask``
and codegen, or self-hosted vision models for visual agent tasks.

API keys are NOT stored in the JSON; the record's ``api_key_env``
field names an environment variable that the worker resolves at
request time. This way the registry stays free of secrets even when
the data dir is backed up, shared, or NAS-mounted.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional


# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------

# Capability category of an engine: what kind of tasks it can do.
#
#   chat        ── pure text in -> text out. Used by page.ask(),
#                  codegen-loop, JP->EN translation.
#   vision-chat ── text + image in -> text out. Used by page.agent()
#                  on the qwen / GPT-4V / Claude-vision path. Also the
#                  v2 "eye" — Qwen-VL produces PerceptionResult here.
#   reasoning   ── chat-shaped, but the model produces a long internal
#                  <think>...</think> block before its answer. Used by
#                  v2 architecture's R1 Strategist / Judge / Distiller.
#                  Slower & pricier than plain "chat"; consumers should
#                  use it sparingly (post-job decisions, not per-step).
#
# NOTE: the v1 "gui-agent" kind (CogAgent / pixel-space action loops)
# was removed in the v2 cleanup. Vision-grounded actions now flow
# through the "vision-chat" + plugin auto-invocation path.
EngineKind = Literal["chat", "vision-chat", "reasoning"]

# HTTP API the endpoint speaks. Picks which adapter the worker uses.
#
#   openai        ── POST {endpoint}/v1/chat/completions
#                    Covers OpenAI, vLLM, Ollama, LM Studio, OpenRouter,
#                    LiteLLM proxy (= Claude / Gemini / Bedrock through).
#   anthropic     ── POST {endpoint}/v1/messages (native Claude API).
#                    Reserved for v2 -- v1 routes Claude through LiteLLM.
#   agent-service ── paprika's bundled agent_service wrapper: POST /act
#                    with the worker's outline + history. Used by the
#                    seed "qwen" engine for backward compat.
#
# NOTE: the "cogagent" protocol was retired alongside gui-agent (v2).
EngineProtocol = Literal["openai", "anthropic", "agent-service"]


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def normalise_slug(s: str) -> str:
    """Coerce arbitrary text to a safe identifier (lowercase, max 80
    chars). Allows ``a-z 0-9 . _ -`` so version-suffixed slugs like
    ``claude-3.5-sonnet`` / ``gpt-4o_mini`` survive readably. Empty
    input becomes ``unnamed``."""
    s = (s or "").strip().lower()
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-._")
    s = s[:80]
    return s or "unnamed"


@dataclass
class EngineRecord:
    """One AI engine the operator made available to scripts.

    The shape is deliberately flat so the admin UI can render it as
    a single form. ``slug`` is the primary key and what scripts pass
    to ``engine="<slug>"``. Built-in records are seeded with
    ``builtin=True`` and shown but not deletable in the UI.

    Two ways to provide an API key:
      * ``api_key_env`` -- name of an environment variable on the hub
        container (e.g. ``OPENAI_API_KEY``). The key never lands in
        the JSON. Preferred for production / .env-style deploys.
      * ``api_key`` -- the literal key, stored on disk inside the
        engine record JSON. Convenient for one-off testing without
        editing .env + restarting the container. **The hub redacts
        this field from all GET responses** so it isn't accidentally
        exposed via the admin UI; only the test/use path reads it.
    """
    slug: str
    name: str                              # human-readable
    kind: EngineKind                       # chat / vision-chat / reasoning
    protocol: EngineProtocol               # openai / anthropic / agent-service
    endpoint: str                          # base URL (no trailing slash needed)
    model: str = ""                        # model name passed to the API. "" if N/A
    api_key_env: str = ""                  # env var NAME (not value). "" = no auth
    api_key: str = ""                      # direct value -- redacted from GET responses
    headers: dict = field(default_factory=dict)  # extra HTTP headers
    timeout_s: int = 60
    promoted: bool = False                 # use first when engine="auto" of this kind
    # OpenAI-style function-calling support. Toggles whether codegen
    # may attach a ``tools`` array (currently just ``web_search`` via
    # SearXNG) to requests routed through this engine. Modern OpenAI /
    # Anthropic / vLLM-with-tool-calling = True. Plain text-completion
    # endpoints or older models = False; the LLM then sees no tools and
    # codegen falls back to a one-shot completion. Defaults to True
    # because every engine kind we currently ship supports it; the
    # operator can flip it off in the admin UI if a custom endpoint
    # rejects the tools field.
    supports_tools: bool = True
    # Whether this engine appears in the Submit form's "コード生成 LLM"
    # selector. Operator opt-in: only engines with this flag set are
    # offered for codegen-loop. Previously this was an implicit filter
    # (kind in chat/vision-chat AND protocol=openai). For backward compat,
    # from_json() derives the default from that legacy rule when the
    # JSON predates this field; explicit values in saved records win.
    use_for_codegen: bool = False
    # Daily quota caps. Each is independently enforced before every
    # LLM call routed through this engine; the call is rejected with
    # a clear error when the limit would be exceeded. Set to 0 to
    # disable that specific cap (the default). Counters reset at UTC
    # midnight. See EngineUsageRegistry.
    #
    # Why daily and not per-minute: protects the operator's OpenAI /
    # Anthropic invoice from a runaway codegen-loop without throttling
    # legitimate bursts. A separate rate-limit layer (nginx /
    # Cloudflare) handles the per-second case.
    daily_token_budget: int = 0            # 0 = no cap. counts prompt+completion
    daily_request_budget: int = 0          # 0 = no cap. counts /chat/completions calls
    notes: str = ""                        # operator memo
    builtin: bool = False                  # True = seeded, UI shows as read-only
    created_at: str = ""
    updated_at: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "EngineRecord":
        return cls(
            slug=normalise_slug(str(d.get("slug") or "")),
            name=str(d.get("name") or ""),
            kind=str(d.get("kind") or "chat"),  # type: ignore[arg-type]
            protocol=str(d.get("protocol") or "openai"),  # type: ignore[arg-type]
            endpoint=str(d.get("endpoint") or ""),
            model=str(d.get("model") or ""),
            api_key_env=str(d.get("api_key_env") or ""),
            api_key=str(d.get("api_key") or ""),
            headers=dict(d.get("headers") or {}),
            timeout_s=int(d.get("timeout_s") or 60),
            promoted=bool(d.get("promoted") or False),
            # ``supports_tools`` defaults True for back-compat: engines
            # written before this field existed should keep getting the
            # web_search tool. Operator can turn it off explicitly.
            supports_tools=bool(d.get("supports_tools", True)),
            # Back-compat: legacy records (pre-use_for_codegen) get the
            # field derived from the old Submit-form filter so the
            # dropdown stays populated until the operator picks
            # explicitly via the Engines tab checkbox.
            use_for_codegen=(
                bool(d["use_for_codegen"])
                if "use_for_codegen" in d
                else (
                    str(d.get("kind") or "chat") in ("chat", "vision-chat")
                    and str(d.get("protocol") or "openai") == "openai"
                )
            ),
            daily_token_budget=int(d.get("daily_token_budget") or 0),
            daily_request_budget=int(d.get("daily_request_budget") or 0),
            notes=str(d.get("notes") or ""),
            builtin=bool(d.get("builtin") or False),
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
        )


# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------


class EngineRegistry:
    """File-backed CRUD over ``{data_dir}/engines/<slug>.json``.

    Same shape as :class:`HostRegistry` / :class:`PresetRegistry`:
    operations are single-file read / write, no Redis. Operators
    create every engine explicitly from the admin UI -- there is no
    auto-seeding of "built-in" entries. (The old seeder added a
    fixed set of entries on first start but its endpoints pointed
    at compose-internal hostnames that were wrong on most deploys;
    operators ended up editing every field by hand anyway, so the
    seed produced more confusion than convenience and was removed.)
    """

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "engines"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, slug: str) -> Path:
        return self.dir / f"{normalise_slug(slug)}.json"

    def list_all(self) -> list[EngineRecord]:
        records: list[EngineRecord] = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                records.append(EngineRecord.from_json(
                    json.loads(p.read_text(encoding="utf-8"))
                ))
            except Exception:
                # Corrupt file -- skip silently so the operator at
                # least sees the other engines in the UI.
                pass
        # Sort: built-in first, then by kind, then alphabetically.
        records.sort(key=lambda r: (not r.builtin, r.kind, r.slug))
        return records

    def get(self, slug: str) -> Optional[EngineRecord]:
        p = self._path(slug)
        if not p.exists():
            return None
        try:
            return EngineRecord.from_json(
                json.loads(p.read_text(encoding="utf-8"))
            )
        except Exception:
            return None

    def upsert(self, rec: EngineRecord) -> EngineRecord:
        if not rec.slug.strip():
            raise ValueError("engine slug cannot be empty")
        rec.slug = normalise_slug(rec.slug)
        existing = self.get(rec.slug)
        now = _utcnow_iso()
        rec.created_at = (
            existing.created_at if existing and existing.created_at else now
        )
        rec.updated_at = now
        # Operators cannot toggle "builtin" themselves -- only the
        # seeder code path sets it. Keep the existing flag if any.
        if existing is not None:
            rec.builtin = existing.builtin
        self._write(rec)
        return rec

    def delete(self, slug: str) -> bool:
        rec = self.get(slug)
        if rec is None:
            return False
        # Built-in engines can be deleted too. The re-seed guard
        # (``__init__``) only fires when the engines dir is COMPLETELY
        # empty -- so removing a single builtin (qwen-chat / qwen /
        # deepseek-r1) while at least one other engine file remains keeps
        # the deletion permanent across hub restarts. Operators who
        # later need it back can recreate via the "new engine" form,
        # or wipe the dir to trigger a full re-seed.
        p = self._path(slug)
        try:
            p.unlink()
            return True
        except Exception:
            return False

    def set_promoted(self, slug: str, promoted: bool) -> Optional[EngineRecord]:
        rec = self.get(slug)
        if rec is None:
            return None
        rec.promoted = bool(promoted)
        rec.updated_at = _utcnow_iso()
        self._write(rec)
        return rec

    def pick_for_kind(self, kind: str) -> Optional[EngineRecord]:
        """Find the engine that should serve ``engine="auto"`` for
        the given kind. Picks the first promoted entry of the
        requested kind, falling back to the first non-promoted, or
        None if no match."""
        matches = [r for r in self.list_all() if r.kind == kind]
        promoted = [r for r in matches if r.promoted]
        return (promoted or matches or [None])[0]

    def _write(self, rec: EngineRecord) -> None:
        p = self._path(rec.slug)
        # Write to a temp file + rename so a crashed Python doesn't
        # leave a half-written record on disk.
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(rec.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)


# ----------------------------------------------------------------------------
# Per-engine daily usage tracking + quota check
# ----------------------------------------------------------------------------
#
# One JSON file at {data_dir}/engines/_usage.json with a flat
# date -> slug -> {prompt, completion, requests} layout. We keep
# rolling 14-day history so the admin UI can show a sparkline; older
# dates are pruned on every write to keep the file bounded.

_USAGE_FILE_NAME = "_usage.json"
_USAGE_HISTORY_DAYS = 14


def _today_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


@dataclass
class QuotaCheck:
    """Outcome of EngineUsageRegistry.check_quota(). ``allowed``
    True means the caller may proceed; False means refuse with
    ``reason`` (operator-visible). On allow, ``warning`` may carry
    a "you're at 90% of today's cap" hint for surfacing in logs."""
    allowed: bool
    reason: str = ""
    warning: str = ""


class EngineUsageRegistry:
    """Per-engine daily token + request counter.

    Counts are incremented after each successful LLM call (callers
    use ``record(slug, prompt, completion)``). Before each call, the
    caller asks ``check_quota(slug)`` and bails if the response says
    refused. Counters live in a single JSON file:

        {data_dir}/engines/_usage.json
        {
          "2026-05-24": {
            "chatgpt51": {"prompt": 12345, "completion": 6789, "requests": 50},
            "qwen":      {"prompt":   200, "completion": 1500, "requests": 30}
          },
          "2026-05-23": {...}
        }

    Writes are atomic (write to .tmp then rename) so a crash during
    counter update doesn't corrupt the history. No locking needed
    because the file is single-writer (hub process).

    Resets are implicit: today's bucket gets keyed by the current UTC
    date, and a new day starts a fresh bucket without manual
    intervention. Yesterday's data is preserved for the
    ``_USAGE_HISTORY_DAYS`` window so the admin UI can show a chart.
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "engines" / _USAGE_FILE_NAME

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        # Atomic replace: write to a sibling .tmp then rename.
        # Prune dates older than the history window in the same pass
        # to keep the file from growing without bound.
        keep_dates = sorted(data.keys())[-_USAGE_HISTORY_DAYS:]
        pruned = {d: data[d] for d in keep_dates}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(pruned, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get_today(self, slug: str) -> dict:
        """Return today's counters for ``slug``: ``{prompt, completion, requests}``.
        Zero-valued dict when no calls yet today."""
        data = self._read()
        today = _today_utc()
        bucket = (data.get(today) or {}).get(slug) or {}
        return {
            "prompt": int(bucket.get("prompt", 0) or 0),
            "completion": int(bucket.get("completion", 0) or 0),
            "requests": int(bucket.get("requests", 0) or 0),
        }

    def get_history(self, slug: str) -> dict:
        """Return per-date counters for ``slug`` across the kept
        history window. Keyed by date string. Useful for sparklines
        / debugging traffic spikes."""
        data = self._read()
        out: dict = {}
        for date_str, by_slug in sorted(data.items()):
            row = (by_slug or {}).get(slug)
            if row:
                out[date_str] = {
                    "prompt": int(row.get("prompt", 0) or 0),
                    "completion": int(row.get("completion", 0) or 0),
                    "requests": int(row.get("requests", 0) or 0),
                }
        return out

    def record(self, slug: str, prompt: int, completion: int) -> None:
        """Increment today's counters by (prompt, completion) tokens
        and 1 request. Best-effort: errors during write are logged
        (via the operator's stderr) but never raised -- counter loss
        is preferable to killing an in-flight job."""
        if not slug:
            return
        slug = normalise_slug(slug)
        data = self._read()
        today = _today_utc()
        day = data.setdefault(today, {})
        cur = day.setdefault(slug, {"prompt": 0, "completion": 0, "requests": 0})
        cur["prompt"] = int(cur.get("prompt", 0) or 0) + max(0, int(prompt or 0))
        cur["completion"] = int(cur.get("completion", 0) or 0) + max(0, int(completion or 0))
        cur["requests"] = int(cur.get("requests", 0) or 0) + 1
        try:
            self._write(data)
        except Exception as e:
            import sys
            print(
                f"[engines] usage record write failed: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    def check_quota(self, rec: EngineRecord) -> QuotaCheck:
        """Pre-call check. Returns QuotaCheck(allowed=False, reason=...)
        when the engine has hit its daily limit, allowed=True otherwise.

        Both daily_token_budget and daily_request_budget are checked
        independently; either being 0 means that limb is disabled.
        """
        if rec is None or not rec.slug:
            return QuotaCheck(allowed=True)
        usage = self.get_today(rec.slug)
        used_tokens = usage["prompt"] + usage["completion"]
        used_requests = usage["requests"]
        # Token check
        if rec.daily_token_budget > 0:
            if used_tokens >= rec.daily_token_budget:
                return QuotaCheck(
                    allowed=False,
                    reason=(
                        f"engine '{rec.slug}' hit daily token budget: "
                        f"{used_tokens} / {rec.daily_token_budget} used "
                        f"(resets at UTC midnight)"
                    ),
                )
            if used_tokens >= int(rec.daily_token_budget * 0.9):
                return QuotaCheck(
                    allowed=True,
                    warning=(
                        f"engine '{rec.slug}' at {used_tokens}/"
                        f"{rec.daily_token_budget} tokens today (>=90%)"
                    ),
                )
        # Request check
        if rec.daily_request_budget > 0:
            if used_requests >= rec.daily_request_budget:
                return QuotaCheck(
                    allowed=False,
                    reason=(
                        f"engine '{rec.slug}' hit daily request budget: "
                        f"{used_requests} / {rec.daily_request_budget} "
                        f"requests (resets at UTC midnight)"
                    ),
                )
        return QuotaCheck(allowed=True)
