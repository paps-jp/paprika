"""The claim must be addressed to the worker, and nginx must route it stickily.

Only the hub holding a worker's WebSocket can answer that worker's claim: it
alone has the worker in its registry and can deliver the assignment. But the
worker reaches the fleet through the nginx front (``HUB_URL`` is the router,
not a hub), and nginx picks an upstream from the request PATH -- it never sees
the body.

Addressed as ``POST /jobs/{job_id}/claim`` with ``{"worker_id": ...}`` in the
body, the claim matched no sticky location, fell through to ``location /`` and
round-robined across all seven hubs. Six in seven landed on a hub that had
never heard of the worker and returned 409 "not connected to this hub". Pull
dispatch ran for hours and delivered ZERO jobs.

Nothing looked wrong, which is the part worth defending against: the rows
stayed ``queued``, the redrive picked them up and dispatched them the old way,
every job completed, and throughput did not move. Measured on 2026-08-15:
~1,900 claim attempts across three pull workers, 0 successes, and the jobs
those workers popped were executed by w51191 / w51169 / w51178 / w51153
instead. It was the third time in this migration that a correct fallback hid a
feature that was entirely inert (``state.redis`` being None was the first).

So these tests pin the three pieces that only work together:

  1. the route exists with worker_id in the PATH;
  2. the worker calls that shape (and puts job_id in the body);
  3. nginx routes it to the sticky upstream -- the same hash as the WS.

Any one of them alone is a no-op that reports success.
"""

import inspect
import re
from pathlib import Path

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub.routes.jobs import lifecycle
from server.worker.agent import _mix_pull

_NGINX = Path(__file__).resolve().parents[1] / "deploy" / "nginx.conf"


# --- 1. the hub route -------------------------------------------------------

def _claim_routes() -> list[str]:
    paths = []
    for route in server.hub.app.app.routes:
        path = getattr(route, "path", "")
        if path.endswith("/claim") and "POST" in (getattr(route, "methods", None) or ()):
            paths.append(path)
    return paths


def test_a_claim_route_carries_worker_id_in_the_path():
    """The whole fix. worker_id must be in the path or nginx cannot route."""
    assert "/workers/{worker_id}/claim" in _claim_routes()


def test_the_job_addressed_claim_still_exists():
    """Kept for callers that already know which hub to talk to (tests, and
    anything reaching a hub's :8100 directly, bypassing nginx)."""
    assert "/jobs/{job_id}/claim" in _claim_routes()


def test_the_worker_addressed_route_delegates_rather_than_duplicating():
    """Two claim paths must not mean two claim implementations: the CAS is the
    arbiter of ownership and there has to be exactly one of it."""
    src = inspect.getsource(lifecycle.claim_job_as_worker)
    assert "claim_job(" in src
    assert "claim_queued_job" not in src


@pytest.mark.asyncio
async def test_missing_job_id_is_a_400_not_a_confusing_404():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        await lifecycle.claim_job_as_worker("w511", {}, None)
    assert e.value.status_code == 400


# --- 2. the worker side -----------------------------------------------------

def test_the_worker_claims_on_the_worker_path():
    src = inspect.getsource(_mix_pull._PullMixin._pull_claim)
    assert "/workers/{self.worker_id}/claim" in src
    assert "/jobs/{job_id}/claim" not in src


def test_the_worker_sends_job_id_in_the_body():
    src = inspect.getsource(_mix_pull._PullMixin._pull_claim)
    assert 'json={"job_id": job_id}' in src


def test_a_successful_claim_is_logged():
    """The only honest signal that pull dispatch is doing anything. Absent it,
    'queue depth is draining' reads as success while the redrive does the work
    -- exactly how this bug survived its first production run."""
    src = inspect.getsource(_mix_pull._PullMixin._pull_claim)
    ok_branch = src[src.index("status_code == 200:"):src.index("status_code == 404")]
    assert re.search(r"log\.info\(.*-> ok", ok_branch)


def test_a_rejected_claim_logs_the_reason():
    """'not connected to this hub' (routing is broken) and 'already running'
    (lost a race) are the same 409 and demand opposite responses."""
    src = inspect.getsource(_mix_pull._PullMixin._pull_claim)
    assert "resp.text" in src


# --- 3. nginx ---------------------------------------------------------------

def _nginx() -> str:
    return _NGINX.read_text(encoding="utf-8")


def test_nginx_has_a_sticky_location_for_the_claim():
    assert re.search(
        r"location\s+~\s+\^/workers/\(\?<worker_id>\[\^/\]\+\)/claim",
        _nginx(),
    ), "without this the claim round-robins and pull dispatch is a no-op"


def test_the_claim_uses_the_same_upstream_as_the_websocket():
    """Same upstream => same consistent hash => the claim provably reaches the
    hub holding this worker's WS. A separate upstream would drift."""
    conf = _nginx()
    claim = conf[conf.index("/claim"):]
    assert "hubs_sticky" in claim[:claim.index("location /")]
    assert "hash $worker_id consistent;" in conf


def test_the_claim_location_precedes_the_catch_all():
    """nginx tries regex locations in order; after `location /` it would never
    be reached."""
    conf = _nginx()
    assert conf.index("/claim") < conf.index("location / {")


def test_nginx_conf_is_deployed_by_hand():
    """deploy-from-34.sh syncs server/ and core/ only. A reader who assumes
    the watcher ships this file will 'fix' pull dispatch and see no change."""
    conf = _nginx()
    assert "not auto-deployed" in inspect.getsource(lifecycle.claim_job_as_worker)
    assert "nginx.conf" in inspect.getsource(lifecycle.claim_job_as_worker)
    assert "hubs_sticky" in conf


# --- 4. the forward that makes routing exact --------------------------------
#
# Sticky routing is necessary but NOT sufficient. nginx's consistent hash
# decides where a NEW connection goes; an existing WebSocket stays on whatever
# hub it landed on, so a worker that reconnected during a rolling restart sits
# on the failover hub while its claims keep hashing to the canonical one.
# Measured 2026-08-15, minutes apart: w511 19/20 correct, w5111 0/20 -- same
# config, same nginx, opposite outcome. The hash cannot be the guarantee.

def test_the_claim_forwards_to_the_hub_that_holds_the_worker():
    src = inspect.getsource(lifecycle.claim_job_as_worker)
    assert "_hub_holding_worker" in src
    assert "_proxy_request_to_hub" in src


def test_the_owner_is_read_from_the_snapshot_not_derived():
    """The only authority on where a WS actually lives."""
    src = inspect.getsource(lifecycle._hub_holding_worker)
    assert "stats_async" in src


def test_the_forward_is_skipped_when_we_hold_the_worker():
    """The common case must not pay for a lookup or a hop."""
    src = inspect.getsource(lifecycle.claim_job_as_worker)
    i = src.index("_hub_holding_worker")
    assert "connections.get(worker_id) is None" in src[:i]


def test_the_forward_cannot_loop():
    """A forwarded claim must be handled or refused, never bounced onward --
    two hubs each thinking the other owns the worker would ping-pong."""
    src = inspect.getsource(lifecycle.claim_job_as_worker)
    assert "_FWD_MARK_HDR()" in src[:src.index("_hub_holding_worker")]


def test_a_snapshot_failure_serves_the_stale_map():
    """The snapshot is a cross-hub aggregation and does fail (redis timeouts
    are why /workers grew a last-GOOD cache). A 5s-stale hub id beats none."""
    src = inspect.getsource(lifecycle._hub_holding_worker)
    tail = src[src.index("except Exception"):]
    assert "c[1].get(worker_id)" in tail


def test_the_lookup_is_cached():
    """Uncached, every misrouted claim would trigger a fleet-wide aggregation
    -- 1.5s at 152 workers, on the dispatch path."""
    assert lifecycle._WORKER_HUB_TTL_S > 0
    src = inspect.getsource(lifecycle._hub_holding_worker)
    assert "_worker_hub_cache" in src and "_worker_hub_lock" in src


# --- 5. a disconnected worker must not drain the queue ----------------------

def test_no_claim_without_a_live_websocket():
    """The assignment arrives over the WS, so without one a pop can only be
    discarded -- and the popped id is gone from Redis, leaving the row to wait
    out the redrive. w5110 did exactly this with alive=False on 2026-08-15."""
    src = inspect.getsource(_mix_pull._PullMixin._pull_should_ask)
    assert '"_ws"' in src
