"""Decides when the host registry may hold cookie-only saves in memory.

The files under ``{data_dir}/hosts/`` are the hub's only copy of the host
records when there is no MariaDB, and a cache of MariaDB when there is one.
Only in the second case is it safe to let a file trail behind: every save is
written through to MariaDB, and a restart mirrors MariaDB back onto the files.

This loop keeps ``HostRegistry``'s write-behind in step with that:

  * MariaDB connected  -> write-behind on, interval = ``host_cache_flush_s``
  * MariaDB not there  -> write-behind off; anything waiting is written out
  * setting changed    -> picked up on the next tick (30 s), no restart

and, on every tick, writes out the hosts that have waited a full interval.
The registry itself decides per save what may wait (cookies and timestamps
only) -- see ``HostRegistry._write``.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("paprika.host_cache")

_TICK_S = 30.0
_DEFAULT_FLUSH_S = 3600


def wanted_interval_s() -> float:
    """Seconds a cookie-only save may wait before reaching its file; 0 when
    the file has to be current (no MariaDB behind it, or switched off)."""
    from server.hub._state import state

    if getattr(state, "mariadb_pool", None) is None:
        return 0.0
    raw = _DEFAULT_FLUSH_S
    try:
        if state.settings is not None:
            raw = state.settings.get("host_cache_flush_s", _DEFAULT_FLUSH_S)
    except Exception:
        raw = _DEFAULT_FLUSH_S
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(_DEFAULT_FLUSH_S)


async def tick() -> None:
    from server.hub._state import state

    reg = state.hosts
    if reg is None or not hasattr(reg, "set_write_behind"):
        return
    want = wanted_interval_s()
    if reg.write_behind_s != want:
        was = reg.write_behind_s
        written = await asyncio.to_thread(reg.set_write_behind, want)
        log.info(
            "host cache: cookie-only saves wait up to %.0fs before reaching "
            "disk (was %.0fs); wrote %d waiting record(s)",
            want, was, written,
        )
    if want > 0:
        await asyncio.to_thread(reg.flush, only_due=True)


async def _host_cache_flush_loop() -> None:
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("host cache: tick failed", exc_info=True)
        await asyncio.sleep(_TICK_S)


def flush_on_shutdown() -> int:
    """Write out everything still waiting. Best-effort: if the hub is killed
    before this runs, the next start mirrors the records back from MariaDB."""
    from server.hub._state import state

    reg = state.hosts
    if reg is None or not hasattr(reg, "flush"):
        return 0
    try:
        return reg.flush()
    except Exception:
        log.warning("host cache: flush on shutdown failed", exc_info=True)
        return 0
