"""Dispatch CAS on the non-MariaDB job stores.

POST /jobs and _redrive call ``claim_queued_job`` / ``release_claimed_job`` /
``reclaim_stranded_queued_job`` unconditionally. They used to exist only on
MariaDBJobStore, so a hub running on Redis (or in-memory) raised
AttributeError and every job sat ``queued`` forever (standalone CT, 2026-11-16).

The Redis cases need a real Redis: set PAPRIKA_TEST_REDIS_URL (a throwaway
instance -- the test writes ``paprika:job:*`` keys and deletes them).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime

import pytest

from server.protocol import JobInfo, JobOptions, JobStatus
from server.store import InMemoryJobStore, RedisJobStore

_REDIS_URL = os.environ.get("PAPRIKA_TEST_REDIS_URL", "")


def _info(job_id: str, **kw) -> JobInfo:
    return JobInfo(
        job_id=job_id, url="https://example.com/", status=JobStatus.queued,
        options=JobOptions(), created_at=datetime.utcnow(), **kw,
    )


@pytest.fixture(params=["memory", "redis"])
async def store(request):
    if request.param == "memory":
        s = InMemoryJobStore()
        await s.initialize()
        yield s
        return
    if not _REDIS_URL:
        pytest.skip("PAPRIKA_TEST_REDIS_URL not set")
    s = RedisJobStore(_REDIS_URL)
    await s.initialize()
    s._test_ids = []
    yield s
    for jid in s._test_ids:
        await s.delete_job(jid)
    await s.close()


async def _new(store, **kw) -> str:
    jid = "test-cas-" + uuid.uuid4().hex[:12]
    if hasattr(store, "_test_ids"):
        store._test_ids.append(jid)
    await store.save_job_info(_info(jid, **kw))
    return jid


async def test_claim_flips_queued_to_running(store):
    jid = await _new(store)
    t = datetime(2026, 11, 16, 1, 2, 3)
    assert await store.claim_queued_job(jid, "w05", t) is True
    info = await store.get_job_info(jid)
    assert info.status == JobStatus.running
    assert info.worker_id == "w05"
    assert info.started_at == t
    # second claim loses: row is no longer queued
    assert await store.claim_queued_job(jid, "w06", t) is False
    assert (await store.get_job_info(jid)).worker_id == "w05"


async def test_claim_refuses_assigned_or_missing(store):
    jid = await _new(store, worker_id="w05")  # POST already handed it off
    assert await store.claim_queued_job(jid, "w06", datetime.utcnow()) is False
    assert await store.claim_queued_job("no-such-job", "w06", None) is False


async def test_release_reverts_running_only(store):
    jid = await _new(store)
    assert await store.release_claimed_job(jid) is False  # still queued
    await store.claim_queued_job(jid, "w05", datetime.utcnow())
    assert await store.release_claimed_job(jid) is True
    info = await store.get_job_info(jid)
    assert (info.status, info.worker_id, info.started_at) == (
        JobStatus.queued, None, None)
    # claimable again
    assert await store.claim_queued_job(jid, "w06", datetime.utcnow()) is True


async def test_reclaim_stranded_clears_matching_worker(store):
    jid = await _new(store, worker_id="w05")
    assert await store.reclaim_stranded_queued_job(jid, "w99") is False
    assert await store.reclaim_stranded_queued_job(jid, "w05") is True
    info = await store.get_job_info(jid)
    assert (info.status, info.worker_id) == (JobStatus.queued, None)
    assert await store.reclaim_stranded_queued_job(jid, "w05") is False


async def test_concurrent_claims_have_one_winner(store):
    jid = await _new(store)
    t = datetime.utcnow()
    wins = await asyncio.gather(
        *(store.claim_queued_job(jid, f"w{i:02d}", t) for i in range(16))
    )
    assert wins.count(True) == 1
    info = await store.get_job_info(jid)
    assert info.worker_id == f"w{wins.index(True):02d}"


async def test_claim_preserves_other_fields(store):
    jid = await _new(store, session_id="sess-1")
    await store.claim_queued_job(jid, "w05", datetime.utcnow())
    info = await store.get_job_info(jid)
    assert info.session_id == "sess-1"
    assert info.url == "https://example.com/"
