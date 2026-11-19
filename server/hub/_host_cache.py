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

The same loop paces the per-host job counters under ``host_knowledge/``
(``distiller_light``): outcomes are counted in memory and added to a host's
file at most once per ``host_stats_flush_s``. Those counters live only in
this hub's files, MariaDB or not -- a hub that is killed loses at most one
interval of counts, which feed a log line and the Knowledge tab.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("paprika.host_cache")

_TICK_S = 30.0
_DEFAULT_FLUSH_S = 3600
_STATS_SHUTDOWN_BUDGET_S = 3.0


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


def wanted_stats_interval_s() -> float:
    """Seconds a host's job outcomes may be counted in memory before they
    are added to its knowledge file; 0 writes every outcome at once."""
    from server.hub._state import state

    raw = _DEFAULT_FLUSH_S
    try:
        if state.settings is not None:
            raw = state.settings.get("host_stats_flush_s", _DEFAULT_FLUSH_S)
    except Exception:
        raw = _DEFAULT_FLUSH_S
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(_DEFAULT_FLUSH_S)


async def tick_stats() -> None:
    from server.hub import distiller_light

    want = wanted_stats_interval_s()
    if distiller_light.write_interval_s() != want:
        was = distiller_light.write_interval_s()
        written = await asyncio.to_thread(distiller_light.set_write_interval, want)
        log.info(
            "host stats: job outcomes wait up to %.0fs before reaching disk "
            "(was %.0fs); wrote %d waiting file(s)",
            want, was, written,
        )
    if want > 0:
        await asyncio.to_thread(distiller_light.flush, only_due=True)


async def tick() -> None:
    from server.hub._state import state

    try:
        await tick_stats()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("host stats: tick failed", exc_info=True)

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

    written = 0
    reg = state.hosts
    if reg is not None and hasattr(reg, "flush"):
        try:
            written = reg.flush()
        except Exception:
            log.warning("host cache: flush on shutdown failed", exc_info=True)

    # Job counters second: they are the ones that can be lost without harm.
    # No fsync and a time limit -- shutdown has a few seconds in all.
    try:
        from server.hub import distiller_light

        n = distiller_light.flush(durable=False, budget_s=_STATS_SHUTDOWN_BUDGET_S)
        left = distiller_light.pending_count()
        if n or left:
            log.info(
                "host stats: wrote %d waiting file(s) on shutdown, %d not written",
                n, left,
            )
    except Exception:
        log.warning("host stats: flush on shutdown failed", exc_info=True)
    return written
