"""Event-loop lag sampler for the worker.

On 2026-08-15 a bug in the pull loop turned it into a spin: 99,768 claim
attempts in 0.35s, measured. The event loop stopped getting to anything else,
the hub's keepalive ping went unanswered, and websockets closed the connection
with 1011 -- failing every job that worker was running as "disconnected before
the job finished". Fleet success rate went 0.97 -> 0.50 in under an hour.

Every metric we collect stayed healthy through it. CPU was busy but workers are
supposed to be busy; memory was fine; the container never restarted. The only
signal was the 1011 itself -- a *consequence*, three layers downstream of the
cause, and one that reads like a network fault. Diagnosing it took reading
worker logs by hand on individual CTs.

Loop lag is the direct measurement. A task that sleeps a fixed interval and
reports how late it actually woke says, in one number, whether this process can
still answer anything. Python ships ``slow_callback_duration`` for the same
purpose but only under asyncio debug mode; aiodebug exists because the
measurement is wanted in production, always on. This is that, in ~40 lines and
with no dependency: one sleeping task, and a peak we hand to the heartbeat.

We report the PEAK since the last heartbeat, not the average. A 30-second stall
inside a 60-second window averages to a reassuring 500ms; the peak reports 30
seconds, which is the number that matters -- the ping timeout it blew through
is 120s, and a stall anywhere near that is the failure about to happen.
"""
from __future__ import annotations

import asyncio
import time

#: How often to probe. Short enough to catch a stall inside one heartbeat
#: window, long enough that the probe itself is free (2 wakeups/second).
_INTERVAL_S = 0.5


class _LoopLagMixin:
    """Adds ``_loop_lag_sampler`` and ``loop_lag_peak_ms``."""

    async def _loop_lag_sampler(self) -> None:
        """Sleep a known interval; record how much later we actually woke.

        Runs for the life of the process, not the life of a WebSocket: the
        stall we most need measured is the one happening while the connection
        is down.
        """
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(_INTERVAL_S)
            lag_ms = (time.monotonic() - t0 - _INTERVAL_S) * 1000.0
            if lag_ms > getattr(self, "_loop_lag_peak_ms", 0.0):
                self._loop_lag_peak_ms = lag_ms

    def loop_lag_peak_ms(self) -> float:
        """Peak lag since the last call, then reset.

        Read once per heartbeat. Resetting on read is what makes consecutive
        beats independent -- otherwise one bad second would pin the metric high
        for the rest of the process's life and stop meaning anything.
        """
        peak = float(getattr(self, "_loop_lag_peak_ms", 0.0))
        self._loop_lag_peak_ms = 0.0
        return round(peak, 1)
