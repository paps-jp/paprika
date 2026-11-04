"""The pull list must track the job lifecycle, not drift from it.

Design D7. ``BLPOP`` is a hard pop, so a worker that dies between popping an
id and claiming it takes the id with it while the row stays ``queued`` — that
is the one recovery window pull adds, and the redrive is what notices. The
other direction matters too: a job cancelled or deleted while its id is still
in the list would be popped by a worker, 404 the claim, and bounce a lane for
nothing.

Pinned here:

  1. under pull the redrive re-queues instead of picking a worker itself —
     otherwise it reintroduces exactly the hub-local choosing that pull exists
     to remove;
  2. the re-queue removes before pushing, so a job still waiting in the list
     doesn't accumulate a copy per pass — duplicates there mean one URL
     crawled twice;
  3. a Redis failure falls back to inline dispatch rather than stranding
     the job;
  4. cancel and delete drop the id.
"""

import inspect

import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
from server.hub import _pull_queue, _redrive
from server.hub.routes.jobs import lifecycle as lc


def test_redrive_requeues_under_pull():
    src = inspect.getsource(_redrive._redrive_dispatch_one)
    head = src[:src.index("pick_worker")]
    assert "_pull_queue.ENABLED" in head, "the pull branch must come first"
    assert "_pull_queue.repush_if_absent" in head


def test_requeue_never_reorders_or_duplicates():
    """A queued job may still be sitting in the list unpopped. The redrive must
    not move it (remove+RPUSH sent every waiting job to the back on every pass
    of every hub -- 2026-11-03) nor stack a second copy: it only re-pushes an
    id that is absent, once per cooldown."""
    src = inspect.getsource(_redrive._redrive_dispatch_one)
    assert "_pull_queue.remove" not in src
    assert "_pull_queue.push(" not in src


class _FakeRedis:
    def __init__(self, items=()):
        self.items = list(items)
        self.keys = set()

    async def lpos(self, _k, v):
        return self.items.index(v) if v in self.items else None

    async def set(self, k, _v, nx=False, ex=None):
        if nx and k in self.keys:
            return None
        self.keys.add(k)
        return True

    async def lpush(self, _k, v):
        self.items.insert(0, v)


def _repush(fake, job_id):
    import asyncio
    from unittest import mock

    with mock.patch.object(_pull_queue, "_redis", return_value=fake):
        return asyncio.run(_pull_queue.repush_if_absent(job_id))


def test_repush_leaves_a_waiting_id_where_it_is():
    fake = _FakeRedis(["a", "b", "c"])
    assert _repush(fake, "b") == "present"
    assert fake.items == ["a", "b", "c"]


def test_repush_once_per_cooldown_and_to_the_front():
    fake = _FakeRedis(["a", "b"])
    assert _repush(fake, "z") == "pushed"
    assert fake.items == ["z", "a", "b"]
    fake.items.remove("z")  # popped again and lost
    assert _repush(fake, "z") == "cooldown"  # another hub just did it
    assert fake.items == ["a", "b"]


def test_redrive_falls_back_to_inline_dispatch_on_redis_failure():
    src = inspect.getsource(_redrive._redrive_dispatch_one)
    branch = src[src.index("_pull_queue.ENABLED"):src.index("pick_worker")]
    assert "except Exception" in branch
    # No early return on failure -- control must reach the existing path.
    assert "falling back" in branch


def test_pull_branch_skips_hub_orchestrated_modes():
    """codegen-loop / rerun run in-process on the hub and never touch a
    worker lane, so they must not enter the worker queue."""
    src = inspect.getsource(_redrive._redrive_dispatch_one)
    assert src.index('mode in ("codegen-loop", "rerun")') < src.index("_pull_queue.ENABLED")


def test_cancel_drops_the_id_from_the_list():
    assert "_pull_queue.remove" in inspect.getsource(lc.cancel_job)


def test_delete_drops_the_id_from_the_list():
    assert "_pull_queue.remove" in inspect.getsource(lc.delete_job)


def test_remove_clears_every_copy():
    """Belt and braces for the idempotent re-queue above: even if a duplicate
    ever lands, one removal clears it."""
    src = inspect.getsource(_pull_queue.remove)
    assert "lrem" in src and ", 0, " in src   # count=0 == all occurrences
