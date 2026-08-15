"""Event-loop lag must be measured, and must survive the whole mirror chain.

A heartbeat field is not one edit. It is copied through eight places -- the
protocol model, the scheduler dataclass, the update signature, the assignment,
the local row builder, the cross-hub Redis snapshot, the snapshot restore, and
the hub route that unpacks the message -- and a field that reaches seven of
them is invisible in exactly the situation it was added for. That happened on
2026-08-14 with ``mem_anon_rate_mb_min``: one of four sites was wired, the
test asserted on that one site, it passed, and the metric read 0 on all 170
workers.

So the mirror test here is structural rather than a list of line numbers:
whatever set of files carries one heartbeat metric must carry every heartbeat
metric. It fails for the NEXT field someone adds too.

Why this metric: on 2026-08-15 a spin in the pull loop (99,768 claim attempts
in 0.35s, measured) starved the event loop until the hub's keepalive ping went
unanswered; websockets closed with 1011 and every job those workers were
running failed as "disconnected before the job finished". Fleet success rate
went 0.97 -> 0.50. CPU, memory, container uptime and job counts all looked
normal throughout -- the only signal was the 1011, three layers downstream and
easily read as a network fault.
"""

import asyncio
import inspect
import time
from pathlib import Path

import pytest

from server import protocol, scheduler
from server.worker.agent._core import WorkerAgent
from server.worker.agent._loop_lag import _LoopLagMixin

_ROOT = Path(__file__).resolve().parents[1]

#: Every file that mirrors a per-heartbeat worker metric.
_MIRROR_FILES = [
    "server/protocol.py",
    "server/scheduler.py",
    "server/hub/routes/workers.py",
    "server/worker/agent/_mix_run.py",
]

#: A field known to be wired end to end, used as the reference shape.
_REFERENCE = "mem_anon_rate_mb_min"


def _text(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- the mirror chain -------------------------------------------------------

@pytest.mark.parametrize("rel", _MIRROR_FILES)
def test_the_field_reaches_every_file_the_reference_reaches(rel):
    src = _text(rel)
    assert _REFERENCE in src, f"{rel} no longer mirrors heartbeat metrics"
    assert "loop_lag_ms" in src, (
        f"{rel} carries {_REFERENCE} but not loop_lag_ms -- a partially wired "
        f"metric reads 0 on every worker and looks healthy"
    )


def test_the_field_reaches_every_site_within_the_scheduler():
    """scheduler.py alone holds five of the eight: the dataclass, the update
    signature, the assignment, the local row builder and the snapshot
    restore. Counting them is what catches "wired the dataclass, forgot the
    row builder" -- which is precisely how the metric stays invisible."""
    src = _text("server/scheduler.py")
    assert src.count("loop_lag_ms") >= src.count(_REFERENCE) - 1, (
        "loop_lag_ms appears at fewer scheduler sites than the reference field"
    )


def test_the_protocol_carries_it_with_a_safe_default():
    """Older workers do not send it; the field must not break their beat."""
    hb = protocol.WorkerHeartbeat.model_fields["loop_lag_ms"]
    assert hb.default == 0.0


def test_the_scheduler_worker_holds_it():
    assert "loop_lag_ms" in {f.name for f in
                             __import__("dataclasses").fields(scheduler.ConnectedWorker)}


def test_the_hub_route_reads_it_defensively():
    """getattr with a default: a worker mid-rollout sends a beat without it."""
    src = _text("server/hub/routes/workers.py")
    assert 'getattr(msg, "loop_lag_ms", 0.0)' in src


# --- the measurement itself -------------------------------------------------

class _Agent(_LoopLagMixin):
    pass


@pytest.mark.asyncio
async def test_a_stalled_loop_is_measured():
    """THE test. Block the loop the way a spin does and the peak must show it.
    Nothing else we collect moves when this happens."""
    a = _Agent()
    task = asyncio.create_task(a._loop_lag_sampler())
    await asyncio.sleep(0.05)
    # Must outlast the sampler's own 0.5s interval, or it wakes on time and
    # there is genuinely no lag to report -- a block that fits inside the gap
    # between two probes is a block the loop absorbed.
    time.sleep(0.9)              # synchronous: the loop cannot run
    await asyncio.sleep(0.6)
    task.cancel()
    assert a.loop_lag_peak_ms() > 100, "a 900ms stall went unmeasured"


@pytest.mark.asyncio
async def test_a_healthy_loop_reads_near_zero():
    """It has to be quiet when things are fine, or nobody will trust it."""
    a = _Agent()
    task = asyncio.create_task(a._loop_lag_sampler())
    await asyncio.sleep(1.2)
    task.cancel()
    assert a.loop_lag_peak_ms() < 100


def test_the_peak_resets_on_read():
    """Without a reset one bad second pins the metric high forever and the
    next beat says nothing about the next second."""
    a = _Agent()
    a._loop_lag_peak_ms = 4200.0
    assert a.loop_lag_peak_ms() == 4200.0
    assert a.loop_lag_peak_ms() == 0.0


def test_peak_not_average():
    """A 30s stall inside a 60s window averages to a reassuring 500ms. The
    number that matters is the one that nearly blew the 120s ping timeout."""
    src = inspect.getsource(_LoopLagMixin._loop_lag_sampler)
    assert ">" in src and "_loop_lag_peak_ms" in src
    assert "mean" not in src and "/ 2" not in src


# --- lifecycle --------------------------------------------------------------

def test_the_sampler_is_composed_into_the_agent():
    assert _LoopLagMixin in WorkerAgent.__mro__
    assert hasattr(WorkerAgent, "loop_lag_peak_ms")


def test_the_sampler_outlives_the_websocket():
    """Started once and never cancelled with the connection: the stall most
    worth measuring is the one happening while the link is down. The pull loop
    right above it IS cancelled -- the difference is deliberate."""
    src = inspect.getsource(
        __import__("server.worker.agent._mix_run", fromlist=["x"])._RunMixin
        ._handshake_and_loop
    )
    assert "_loop_lag_sampler()" in src
    assert "_lag_task", "the task must be held so it is not garbage collected"
    lag_at = src.index("_loop_lag_sampler()")
    assert "cancel()" not in src[lag_at:lag_at + 200]


def test_the_heartbeat_sends_it_freshly_read():
    """Send the accessor, not a stored attribute: reading is what resets the
    window, so a stale read would report one peak forever."""
    src = _text("server/worker/agent/_mix_run.py")
    assert "loop_lag_ms=self.loop_lag_peak_ms()" in src
