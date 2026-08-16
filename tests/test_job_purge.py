"""Regression: POST /jobs/purge -- "the consumer is done, leave nothing".

A job's output lives in four places: the hub's local /data/jobs cache, the hot
object tier, the spill tier it overflowed to under RAM pressure, and the DB
row. The pipeline's image-pull can only reach ONE of them (it deletes the
jobs/{id}/ prefix off the hot store once it has taken the images), so until
this endpoint existed "consumed" left the spilled copies and the local cache
dir behind for the 5-day retention pass to find.

Pinned here:
  * every store is hit, and the reported numbers are the ones reclaimed
  * the DB row SURVIVES by default -- image-pull's pending-GC reads a 404 from
    GET /jobs/{id} as "orphan, reclaim it", so silently dropping rows would
    change how a retry classifies a job (and empty the #jobs dashboard)
  * ...but a caller that asks for the row to go gets it
  * unknown / already-purged ids report zeros instead of failing, so a
    consumer can retry a whole batch freely
"""

import pytest

import server.hub.app  # noqa: F401  (route packages import in app order)
from server.hub import objstore
from server.hub._state import state
from server.hub.routes.jobs import lifecycle


class _FakeStore:
    def __init__(self):
        self.deleted = []

    async def delete_job(self, jid):
        self.deleted.append(jid)
        return True


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A hub whose storage dir is tmp_path and whose object store records the
    prefixes it was asked to delete."""
    store = _FakeStore()
    monkeypatch.setattr(state, "store", store, raising=False)
    monkeypatch.setattr(lifecycle, "get_storage_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "server.hub.routes.jobs._base.get_storage_dir", lambda: tmp_path,
        raising=False,
    )
    deleted_prefixes = []

    async def _delete_prefix(job_id, **kw):
        deleted_prefixes.append(job_id)
        # Two tiers' worth: delete_prefix unions every configured store, which
        # is how the spilled copies go with the primary.
        return {"objects": 4, "bytes": 4096, "deleted": True}

    monkeypatch.setattr(objstore, "enabled", lambda: True)
    monkeypatch.setattr(objstore, "delete_prefix", _delete_prefix)
    return {
        "dir": tmp_path,
        "store": store,
        "prefixes": deleted_prefixes,
    }


def _job_dir(root, job_id, nbytes=1024):
    d = root / job_id
    (d / "assets").mkdir(parents=True)
    (d / "page.html").write_bytes(b"x" * nbytes)
    (d / "assets" / "1.jpg").write_bytes(b"y" * nbytes)
    return d


async def test_purge_clears_every_store(hub):
    d = _job_dir(hub["dir"], "ab12ef34")

    out = await lifecycle.purge_jobs({"job_ids": ["ab12ef34"]})

    assert out["purged"] == 1
    assert not d.exists()                       # local cache dir
    assert hub["prefixes"] == ["ab12ef34"]      # hot + spill tiers
    assert out["minio_objects"] == 4
    assert out["local_bytes"] == 2048
    assert out["failed"] == []


async def test_the_row_survives_by_default(hub):
    _job_dir(hub["dir"], "ab12ef34")

    out = await lifecycle.purge_jobs({"job_ids": ["ab12ef34"]})

    assert hub["store"].deleted == []
    assert out["records_deleted"] == 0


async def test_the_row_goes_when_asked(hub):
    _job_dir(hub["dir"], "ab12ef34")

    out = await lifecycle.purge_jobs(
        {"job_ids": ["ab12ef34"], "delete_record": True},
    )

    assert hub["store"].deleted == ["ab12ef34"]
    assert out["records_deleted"] == 1


async def test_purging_a_job_twice_is_harmless(hub):
    _job_dir(hub["dir"], "ab12ef34")

    await lifecycle.purge_jobs({"job_ids": ["ab12ef34"]})
    out = await lifecycle.purge_jobs({"job_ids": ["ab12ef34", "nosuchjob"]})

    assert out["purged"] == 2
    assert out["local_bytes"] == 0
    assert out["failed"] == []


async def test_batch_purges_every_id(hub):
    for jid in ("j1", "j2", "j3"):
        _job_dir(hub["dir"], jid, nbytes=512)

    out = await lifecycle.purge_jobs({"job_ids": ["j1", "j2", "j3"]})

    assert out["purged"] == 3
    assert sorted(hub["prefixes"]) == ["j1", "j2", "j3"]
    assert out["local_bytes"] == 3 * 1024
    assert not any((hub["dir"] / j).exists() for j in ("j1", "j2", "j3"))


async def test_a_bad_body_is_rejected(hub):
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await lifecycle.purge_jobs({"job_ids": "not-a-list"})
    with pytest.raises(HTTPException):
        await lifecycle.purge_jobs({"job_ids": [f"j{i}" for i in range(1001)]})
