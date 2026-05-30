"""Session-action plugin package.

Each ``handlers/*.py`` module defines free-function handlers
``async def _act_<kind>(agent, ctx)`` decorated with ``@_session_action``;
importing this package auto-imports every handler module so the registry
is populated. Drop a new file in ``handlers/`` -- no edit here needed."""
from __future__ import annotations

import importlib as _importlib
import pkgutil as _pkgutil

from server.worker.session_actions._registry import (
    _ActionCtx,
    _ActionSpec,
    _SESSION_ACTIONS,
    _session_action,
)
from server.worker.session_actions import handlers as _handlers_pkg

for _m in _pkgutil.iter_modules(_handlers_pkg.__path__):
    _importlib.import_module(f"{_handlers_pkg.__name__}.{_m.name}")

__all__ = ["_ActionCtx", "_ActionSpec", "_SESSION_ACTIONS", "_session_action"]
