"""Process-wide logging setup. Call :func:`setup_logging` exactly once
at the very top of every entry point (``python -m server`` modes,
worker agent, codegen-runner sandbox).

Goals:

* All paprika modules use ``logging.getLogger(__name__)``. The logger
  name therefore matches the dotted module path
  (``server.hub._jobrunner``, ``server.worker.agent`` ...). Operators
  filter with ``grep "server.hub.routes.jobs"`` instead of guessing at
  hand-written ``[hub]`` / ``[codegen X]`` prefixes.
* Format is consistent with uvicorn's own loggers — we reconfigure
  ``uvicorn``, ``uvicorn.error`` and ``uvicorn.access`` so the transcript
  reads as a single stream.
* Level knobs come from env so the operator can quiet noisy categories
  without code changes:

  ====================================  ==================  ========
  Variable                              Logger              Default
  ====================================  ==================  ========
  ``PAPRIKA_LOG_LEVEL``                 paprika app root    ``INFO``
  ``PAPRIKA_ACCESS_LOG_LEVEL``          ``uvicorn.access``  ``INFO``
  ``PAPRIKA_HTTPX_LOG_LEVEL``           ``httpx``/``httpcore`` ``WARNING``
  ====================================  ==================  ========
"""

from __future__ import annotations

import logging
import os
import sys

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def _level(env_name: str, default: str) -> int:
    name = os.environ.get(env_name, default).upper()
    return getattr(logging, name, getattr(logging, default))


_did_setup = False


def setup_logging() -> None:
    """Idempotent. Safe to call multiple times — only the first call
    installs handlers; subsequent calls are no-ops so unit tests and
    re-entrant uvicorn reloads don't end up with stacked StreamHandlers
    that print every record twice."""
    global _did_setup
    if _did_setup:
        return

    level = _level("PAPRIKA_LOG_LEVEL", "INFO")

    # ``force=True`` evicts any prior basicConfig that uvicorn or another
    # framework installed before our entry point got the first word
    # (uvicorn does this under --reload).
    logging.basicConfig(
        level=level,
        format=_DEFAULT_FORMAT,
        datefmt=_DEFAULT_DATEFMT,
        stream=sys.stderr,
        force=True,
    )

    # Align uvicorn's three loggers with ours. ``handlers.clear()`` drops
    # uvicorn's own StreamHandler (which uses its own colorized format),
    # ``propagate = True`` lets the records bubble up to the root handler
    # we configured above.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(level)

    access_level = _level("PAPRIKA_ACCESS_LOG_LEVEL", "INFO")
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = True
    access.setLevel(access_level)

    # httpx / httpcore log every request at DEBUG which would 10x our
    # transcript when the operator flips PAPRIKA_LOG_LEVEL=DEBUG to
    # investigate something paprika-side.
    httpx_level = _level("PAPRIKA_HTTPX_LOG_LEVEL", "WARNING")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(httpx_level)

    _did_setup = True
