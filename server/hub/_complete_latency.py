"""Where a finished job's wall time actually goes, measured at the hub.

Motivation (2026-08-16): a fleet-wide slowdown showed up as jobs taking 2.7x
longer on IDENTICAL hosts, with CPU, memory, disk and egress bandwidth all
idle. Joining a worker's docker log against the job store showed why the
worker log was no help: the visible work (navigate, capture, save) was p50 11s
for a non-video job, but the job stayed "running" for another p50 21s AFTER
the worker's last log line -- and 50s for a video job. Nothing logged in that
window, so it had never been attributed.

That gap has three parts, and only the hub can see all three:

    worker's last log line
      |-- upload_ms ------> the serial POST chain in _upload_files
      |                     (log.txt, page.html, sidecars, straggler assets)
      |-- transit_ms -----> WS queue + network + this hub's handler backlog
      +-- (hub stamps completed_at)

The worker reports the first leg on ``WorkerJobComplete.upload_ms`` and stamps
``sent_at`` as it hands the message to the socket; the hub derives the second
leg on receipt. Both are optional -- a worker on an older build simply doesn't
contribute a sample, so this degrades to "no data" rather than wrong data.

Deliberately in-process and bounded: this is a diagnostic to tell us WHICH leg
to attack, not a metrics pipeline. Per-hub numbers are fine because every hub
serves /health and the deploy script already polls all of them.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque

# ~20 min of samples at the observed steady-state completion rate. Bounded so
# a hub that runs for weeks can't grow this without limit.
_MAXLEN = 2000

_upload_ms: Deque[float] = deque(maxlen=_MAXLEN)
_transit_ms: Deque[float] = deque(maxlen=_MAXLEN)


def record(sent_at: float | None, upload_ms: float | None) -> None:
    """Fold one WorkerJobComplete's timings in. Never raises: this sits on the
    hot completion path and must never be able to fail a job."""
    try:
        if upload_ms is not None and upload_ms >= 0:
            _upload_ms.append(float(upload_ms))
        if sent_at is not None and sent_at > 0:
            delta = (time.time() - float(sent_at)) * 1000.0
            # Guard against worker/hub clock skew: a negative or absurd delta
            # says the clocks disagree, not that transit was instant/hours.
            # Dropping the sample is honest; clamping would invent a number.
            if 0 <= delta <= 600_000:
                _transit_ms.append(delta)
    except Exception:
        pass


def _pct(seq: Deque[float], frac: float) -> float | None:
    if not seq:
        return None
    ordered = sorted(seq)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * frac))], 1)


def summary() -> dict:
    """The /health view: p50/p90 of each leg, plus sample counts so a reader
    can tell "fast" from "no data yet"."""
    return {
        "upload_ms": {
            "n": len(_upload_ms),
            "p50": _pct(_upload_ms, 0.5),
            "p90": _pct(_upload_ms, 0.9),
        },
        "transit_ms": {
            "n": len(_transit_ms),
            "p50": _pct(_transit_ms, 0.5),
            "p90": _pct(_transit_ms, 0.9),
        },
    }
