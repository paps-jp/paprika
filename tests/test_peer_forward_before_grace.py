"""Regression: a locally-full hub must forward to an idle peer, not wait.

2026-08-14 throughput investigation, stage 3. After the dispatch-log fix, the
remaining cost in POST /jobs was worker selection: **73.9% of measured time,
p50 4.05s, p90 6.69s**.

Root cause was a mismatch, not a shortage:

  * nginx round-robins submits evenly over the 7 hubs;
  * workers pin to one hub by consistent hash, so lanes are split very
    unevenly -- measured hub-40 with 10 workers / 20 lanes / **0 free** while
    the fleet as a whole had **146 lanes idle**;
  * ``_fleet_has_spare_capacity()`` looks fleet-wide and says yes, then
    ``pick_worker()`` only sees LOCAL workers and finds nothing.

So every submit routed to a full hub sat in the 8s ``JOB_DISPATCH_GRACE_S``
loop (0.5s polls -> the observed 4s p50) even though a peer one hop away was
idle. Grace-window hits over 15 minutes: hub-40 201, hub-36 139, hub-38 122,
hub-37 115, and 0 on the three hubs that had free lanes.

The fix tries the peer forward BEFORE the grace loop. This file pins the two
properties that make that safe:

  1. ordering -- the forward is attempted before any grace sleeping, and the
     grace loop still runs when no peer has capacity (that loop exists for the
     post-restart window where the WS registry is momentarily empty, which is
     a real transient, so it must not be deleted);
  2. no orphans -- the job row is already persisted by the time dispatch runs
     and the peer issues its own job_id, so the forward must delete the local
     placeholder. Leaving it queued feeds the redrive a duplicate crawl of the
     same URL.
"""

import asyncio
import inspect

import pytest

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub.routes.jobs import lifecycle as lc


class _Store:
    def __init__(self, fail_delete: bool = False):
        self.deleted: list[str] = []
        self.fail_delete = fail_delete

    async def delete_job(self, job_id):
        if self.fail_delete:
            raise RuntimeError("db down")
        self.deleted.append(job_id)
        return True


class _Sessions:
    def __init__(self):
        self.removed: list[str] = []

    def remove(self, sid):
        self.removed.append(sid)


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


@pytest.fixture
def wired(monkeypatch):
    """state + peer lookup + proxy hop, all stubbed."""
    store, sessions = _Store(), _Sessions()
    calls = {"peer": 0, "proxy": []}

    monkeypatch.setattr(lc.state, "store", store, raising=False)
    monkeypatch.setattr(lc.state, "sessions", sessions, raising=False)
    monkeypatch.setattr(lc.state, "hubs", object(), raising=False)

    def _set_peer(peer):
        async def _p():
            calls["peer"] += 1
            return peer
        monkeypatch.setattr(lc, "_peer_hub_with_spare_capacity", _p)

    async def _proxy(peer, request, timeout):
        calls["proxy"].append(peer)
        return _Resp(200)

    import server.hub.routes.sessions as sess_mod
    monkeypatch.setattr(sess_mod, "_proxy_request_to_hub", _proxy, raising=False)
    monkeypatch.setattr(sess_mod, "_FWD_MARK", "x-paprika-fwd", raising=False)
    return store, sessions, calls, _set_peer, _proxy


def test_forward_is_attempted_before_any_grace_sleeping():
    """Source-level ordering: the whole point is not waiting first."""
    src = inspect.getsource(lc.create_job)
    assert src.index("_forward_to_peer_hub") < src.index("_grace_deadline")


def test_grace_loop_still_exists_for_the_no_peer_case():
    """It covers the post-restart window where the WS registry is empty and
    no peer has capacity either -- deleting it would turn that transient into
    an immediate 503."""
    src = inspect.getsource(lc.create_job)
    assert "JOB_DISPATCH_GRACE_S" in src and "_grace_deadline" in src


@pytest.mark.asyncio
async def test_forward_drops_the_local_placeholder_row(wired):
    store, _sessions, calls, set_peer, _ = wired
    set_peer("hub-37")
    resp = await lc._forward_to_peer_hub(_Req(), "job-abc", fetch_sid=None)
    assert resp is not None and resp.status_code == 200
    assert calls["proxy"] == ["hub-37"]
    # Without this the redrive would re-dispatch the row and crawl the URL
    # a second time while the peer is already running it.
    assert store.deleted == ["job-abc"]


@pytest.mark.asyncio
async def test_forward_also_rolls_back_an_eager_session(wired):
    store, sessions, _calls, set_peer, _ = wired
    set_peer("hub-38")
    await lc._forward_to_peer_hub(_Req(), "job-def", fetch_sid="ses_x")
    assert sessions.removed == ["ses_x"]
    assert store.deleted == ["job-def"]


@pytest.mark.asyncio
async def test_no_peer_means_no_forward_and_no_deletion(wired):
    store, _sessions, calls, set_peer, _ = wired
    set_peer(None)
    assert await lc._forward_to_peer_hub(_Req(), "job-ghi", fetch_sid=None) is None
    assert calls["proxy"] == []
    assert store.deleted == []   # caller keeps dispatching locally


@pytest.mark.asyncio
async def test_peer_503_falls_back_to_local_dispatch(wired, monkeypatch):
    store, _sessions, _calls, set_peer, _ = wired
    set_peer("hub-40")
    import server.hub.routes.sessions as sess_mod

    async def _full(peer, request, timeout):
        return _Resp(503)
    monkeypatch.setattr(sess_mod, "_proxy_request_to_hub", _full, raising=False)
    assert await lc._forward_to_peer_hub(_Req(), "job-jkl", fetch_sid=None) is None
    assert store.deleted == []   # row must survive -- we still need it locally


@pytest.mark.asyncio
async def test_already_forwarded_request_does_not_bounce_again(wired):
    store, _sessions, calls, set_peer, _ = wired
    set_peer("hub-39")
    req = _Req({"x-paprika-fwd": "1"})
    assert await lc._forward_to_peer_hub(req, "job-mno", fetch_sid=None) is None
    assert calls["peer"] == 0 and calls["proxy"] == []


@pytest.mark.asyncio
async def test_delete_failure_still_returns_the_peer_response(wired):
    """The peer is already doing the work -- a failed cleanup must not turn a
    successful dispatch into an error. It logs and moves on."""
    store, _sessions, _calls, set_peer, _ = wired
    store.fail_delete = True
    set_peer("hub-41")
    resp = await lc._forward_to_peer_hub(_Req(), "job-pqr", fetch_sid=None)
    assert resp is not None and resp.status_code == 200


@pytest.mark.asyncio
async def test_peer_lookup_failure_is_not_fatal(wired, monkeypatch):
    store, _sessions, _calls, _set_peer, _ = wired

    async def _boom():
        raise RuntimeError("redis timeout")
    monkeypatch.setattr(lc, "_peer_hub_with_spare_capacity", _boom)
    assert await lc._forward_to_peer_hub(_Req(), "job-stu", fetch_sid=None) is None
    assert store.deleted == []


# --------------------------------------------------------------------------
# the forward must not collide with its own placeholder
# --------------------------------------------------------------------------

def test_forwarded_requests_skip_the_dedup():
    """2026-08-15 incident. The forwarding hub persists a `queued` row for the
    URL, then forwards the SAME request; the peer's dedup found that row and
    409'd every forward -- hub-37 forwarded 1,611 requests in 30 minutes while
    nginx returned 1,751 409s. The submitter reads 409 as "paprika already has
    it", marks the crawl row done, and the URL is never crawled: silent data
    loss on top of the wasted slot."""
    src = inspect.getsource(lc.create_job)
    head = src[:src.index("At-capacity gate")]
    assert "_is_forwarded" in head
    assert "not _is_forwarded" in head


@pytest.mark.asyncio
async def test_peer_409_is_never_treated_as_a_successful_placement(wired, monkeypatch):
    """Defence in depth: if a peer 409s anyway, keep the row and dispatch
    locally. Passing it back would delete a good row and drop the URL."""
    store, _sessions, _calls, set_peer, _ = wired
    set_peer("hub-36")
    import server.hub.routes.sessions as sess_mod

    async def _conflict(peer, request, timeout):
        return _Resp(409)
    monkeypatch.setattr(sess_mod, "_proxy_request_to_hub", _conflict, raising=False)
    assert await lc._forward_to_peer_hub(_Req(), "job-409", fetch_sid=None) is None
    assert store.deleted == [], "a 409 must not delete the local placeholder"
