"""The post-fetch gap instrumentation: split the silence into two legs.

2026-08-16: joining a worker's docker log against the job store showed the
worker's visible work was p50 11s for a non-video job while the job stayed
"running" for another p50 21s (video: 18s vs 50s) with nothing logged in
between. That window contains the worker's serial upload chain AND the WS
queue plus this hub's handler backlog, and no existing signal separated them.

WorkerJobComplete now carries ``upload_ms`` (worker-measured) and ``sent_at``
(stamped as the message hits the socket); the hub derives transit on receipt.
The properties pinned here are the ones that decide whether the numbers can be
trusted: both fields are optional so an older worker degrades to "no sample"
rather than a wrong one, and a clock-skewed sample is dropped rather than
clamped into something that looks like a real measurement.
"""

import time

from server.hub import _complete_latency
from server.protocol import WorkerJobComplete


def _reset():
    _complete_latency._upload_ms.clear()
    _complete_latency._transit_ms.clear()


def test_fields_are_optional_on_the_wire():
    """A worker on an older build sends neither field. The model must still
    validate -- during a rolling deploy most of the fleet is that worker."""
    msg = WorkerJobComplete.model_validate(
        {"type": "complete", "job_id": "abc123", "result": {"job_id": "abc123", "status": "completed"}}
    )
    assert msg.sent_at is None
    assert msg.upload_ms is None


def test_missing_fields_contribute_no_sample():
    _reset()
    _complete_latency.record(None, None)
    s = _complete_latency.summary()
    assert s["upload_ms"]["n"] == 0 and s["upload_ms"]["p50"] is None
    assert s["transit_ms"]["n"] == 0 and s["transit_ms"]["p50"] is None


def test_percentiles_reflect_recorded_samples():
    _reset()
    for ms in (10.0, 20.0, 30.0, 40.0, 1000.0):
        _complete_latency.record(None, ms)
    s = _complete_latency.summary()
    assert s["upload_ms"]["n"] == 5
    assert s["upload_ms"]["p50"] == 30.0
    assert s["upload_ms"]["p90"] == 1000.0


def test_transit_is_derived_from_sent_at():
    _reset()
    _complete_latency.record(time.time() - 0.5, None)
    s = _complete_latency.summary()
    assert s["transit_ms"]["n"] == 1
    assert 400 <= s["transit_ms"]["p50"] <= 900


def test_clock_skew_is_dropped_not_clamped():
    """A worker clock ahead of the hub yields a negative delta, one behind
    yields hours. Either way the sample says the clocks disagree, not that
    transit was instant or enormous -- recording it would poison the p50 that
    the next fix gets judged on."""
    _reset()
    _complete_latency.record(time.time() + 60.0, None)      # worker ahead
    _complete_latency.record(time.time() - 86_400.0, None)  # worker a day behind
    assert _complete_latency.summary()["transit_ms"]["n"] == 0


def test_record_never_raises_on_junk():
    """This sits on the completion hot path; a bad value must not fail a job."""
    _reset()
    for bad in ("nope", object(), float("nan")):
        _complete_latency.record(bad, bad)
    assert _complete_latency.summary()["transit_ms"]["n"] == 0


def test_samples_are_bounded():
    _reset()
    for i in range(_complete_latency._MAXLEN + 500):
        _complete_latency.record(None, float(i))
    assert _complete_latency.summary()["upload_ms"]["n"] == _complete_latency._MAXLEN
