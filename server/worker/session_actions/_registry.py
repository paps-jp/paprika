"""Session-action registry primitives (ctx, spec, the decorator, the
``_SESSION_ACTIONS`` dict). Leaf within the package: handlers import
``_session_action`` from here; the package ``__init__`` re-exports
``_ActionCtx`` / ``_SESSION_ACTIONS`` for the worker dispatcher."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

_logger = logging.getLogger("server.worker.session_actions")


@dataclass
class _ActionCtx:
    """Everything a session-action handler needs. Built once per action
    in ``_handle_session_action`` and passed to the matched handler."""

    state: Any                 # SessionState
    tab: Any                   # target nodriver Tab (None for session-level)
    action: dict
    reply: Any                 # WorkerSessionActionResult (handler mutates)
    cur: str                   # snapshotted current URL of the target tab
    slog: Callable[[str], None]
    t0: float
    msg: Any                   # HubSessionAction


@dataclass
class _ActionSpec:
    fn: Callable               # unbound: called as fn(self, ctx)
    read_only: bool
    session_level: bool


# kind -> spec. Populated by the @_session_action decorator at class-def time.
_SESSION_ACTIONS: dict[str, _ActionSpec] = {}


def _session_action(kind: str, *, read_only: bool = False, session_level: bool = False):
    """Register a WorkerAgent method as the handler for ``kind``.

    ``read_only`` marks kinds safe to run against a fetch-owned session
    (the fetch loop is driving the tab; a write mid-fetch would race).
    ``session_level`` marks kinds that act on the whole session, so they
    run under ``state.lock`` rather than the per-page lock. Both flags
    live on the resulting ``_ActionSpec`` and are the single source of
    truth: the fetch gate and the lock picker in
    ``_handle_session_action`` read them straight off the registry.
    """
    def deco(fn: Callable) -> Callable:
        _SESSION_ACTIONS[kind] = _ActionSpec(fn, read_only, session_level)
        return fn

    return deco
