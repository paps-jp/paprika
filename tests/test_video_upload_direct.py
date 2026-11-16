"""A video is uploaded straight to the video store, never through the hub.

Operator rule, 2026-11-16: "SSDには動画ファイルを書き込まない。これは絶対。"

The worker side of the rule keeps a download on the ramdisk (see
test_video_never_on_disk.py). This is the other half: the upload. The hub
writes every asset it is handed to ``<storage>/<job>/assets/`` before mirroring
it to the object store, and that directory is on an NVMe SSD -- so a video sent
through the hub was written to an SSD on its way to the RAM-backed store.
Measured on one production hub: 8,347 asset POSTs in 10 minutes, no direct
upload at all, and 46-91 MB mp4 files minutes old on the disk.

So a video goes worker -> store with a signed URL, the hub only signs and
records. What is pinned here is that no failure of that path leads back to the
hub's disk.
"""
import asyncio
import types

import httpx
import pytest

from server.protocol import ASSET_CAPTURE_MARKER, HubAssignJob, JobOptions, WorkerJobLog
from server.worker.agent import _mix_uploads
from server.worker.agent._core import WorkerAgent

MB = 1024 * 1024
JOB = "0a1b2c3d4e5f"
BASE = f"http://hub.test/jobs/{JOB}/assets"
STORE = "http://store.test:9000/paprika/jobs/" + JOB + "/assets/"


@pytest.fixture(autouse=True)
def production(monkeypatch):
    """Production settings: nothing set, waiting costs no wall-clock time."""
    for k in (
        "PAPRIKA_VIDEO_ALLOW_DISK", "PAPRIKA_WORKER_MINIO_DIRECT",
        "PAPRIKA_WORKER_MINIO_DIRECT_VIDEO", "PAPRIKA_WORKER_MINIO_DIRECT_VIDEO_MAX_MB",
        "PAPRIKA_WORKER_VIDEO_DIRECT_TRIES", "PAPRIKA_WORKER_VIDEO_FULL_RETRY_S",
    ):
        monkeypatch.delenv(k, raising=False)
    naps = []

    async def _sleep(n):
        naps.append(n)

    monkeypatch.setattr(_mix_uploads.asyncio, "sleep", _sleep)
    return naps


class _Http:
    """The worker's HTTP client: records every request, answers from a script.

    ``script[kind]`` is a list consumed one answer per request of that kind
    (an int status, or an exception to raise); when it runs out, 200."""

    def __init__(self, **script):
        self.calls = []
        self.script = {k: list(v) for k, v in script.items()}

    def _answer(self, kind, method, url):
        todo = self.script.get(kind) or []
        ans = todo.pop(0) if todo else 200
        if isinstance(ans, BaseException):
            raise ans
        body = {"put_url": STORE + url.rsplit("/", 1)[-1] + "?X-Amz-Signature=x"} \
            if kind == "presign" else {"ok": True}
        return httpx.Response(ans, json=body, request=httpx.Request(method, url))

    async def post(self, url, **kw):
        if url.endswith("/presign"):
            kind = "presign"
        elif url.endswith("/complete"):
            kind = "complete"
        else:
            kind = "hub"            # the bytes-through-hub POST
            f = (kw.get("files") or {}).get("file")
            if f:
                f[1].read()
        self.calls.append((kind, url, kw))
        return self._answer(kind, "POST", url)

    async def put(self, url, content=None, headers=None, timeout=None):
        n = 0
        async for chunk in content:
            n += len(chunk)
        self.calls.append(("put", url, {"bytes": n, "headers": headers or {}}))
        return self._answer("put", "PUT", url)

    def kinds(self):
        return [c[0] for c in self.calls]


def _agent(http):
    a = WorkerAgent.__new__(WorkerAgent)
    a.worker_id = "w-test"
    a.worker_secret = None
    a._http = http
    a.sent = []

    async def _send(m):
        a.sent.append(m)

    a._send = _send
    a._abandoned_job_ids = set()
    a._force_complete_job_ids = set()
    return a


def _assign():
    return HubAssignJob(
        job_id=JOB, url="https://site.test/watch/1", options=JobOptions(),
        asset_upload_base=BASE,
    )


def _video(tmp_path, name="clip.mp4", size=3 * MB):
    p = tmp_path / name
    p.write_bytes(b"\0" * size)
    return p


def _lines(a):
    return [m.line for m in a.sent if isinstance(m, WorkerJobLog)]


def _up(a, path, **kw):
    return asyncio.run(a._upload_asset(_assign(), path, path.name, mime="video/mp4", **kw))


# --- the normal case -----------------------------------------------------------


def test_a_video_goes_to_the_store_and_the_hub_gets_no_bytes(tmp_path):
    http = _Http()
    a = _agent(http)
    p = _video(tmp_path)

    assert _up(a, p) is True
    assert http.kinds() == ["presign", "put", "complete"]
    put = http.calls[1]
    assert put[1].startswith(STORE)                       # the store, not the hub
    assert put[2]["bytes"] == 3 * MB
    assert put[2]["headers"]["Content-Length"] == str(3 * MB)
    assert http.calls[2][2]["json"]["size"] == 3 * MB
    assert ASSET_CAPTURE_MARKER in _lines(a)              # the gallery still refreshes


def test_it_is_on_without_any_setting(monkeypatch):
    a = _agent(_Http())
    assert a._minio_direct_video_enabled() is True
    assert a._minio_direct_enabled() is False             # images: unchanged, off
    monkeypatch.setenv("PAPRIKA_WORKER_MINIO_DIRECT_VIDEO", "0")
    assert a._minio_direct_video_enabled() is False


@pytest.mark.parametrize("name", ["a.mp4", "A.MP4", "b.webm", "c.ts", "d.mkv", "e.m3u8", "f.mov"])
def test_every_video_kind_the_hub_routes_to_the_video_store(tmp_path, name):
    http = _Http()
    a = _agent(http)
    p = _video(tmp_path, name=name, size=1024)
    assert asyncio.run(a._upload_asset(_assign(), p, name)) is True
    assert "hub" not in http.kinds()


def test_an_image_still_goes_through_the_hub(tmp_path):
    http = _Http()
    a = _agent(http)
    p = tmp_path / "thumb.jpg"
    p.write_bytes(b"x" * 2048)
    assert asyncio.run(a._upload_asset(_assign(), p, "thumb.jpg", mime="image/jpeg")) is True
    assert http.kinds() == ["hub"]                        # one request per image, as before


# --- failures never lead back to the hub ---------------------------------------


def test_a_store_that_cannot_be_reached_is_retried_then_given_up(tmp_path, production):
    http = _Http(put=[httpx.ConnectError("no route")] * 3)
    a = _agent(http)
    st = {}
    assert _up(a, _video(tmp_path), status_out=st) is False
    assert http.kinds() == ["presign", "put"] * 3
    assert "hub" not in http.kinds()
    assert "status" not in st                              # a network error has no status
    assert len(production) == 2                            # waited between the tries
    assert any("video upload failed" in l and "not sent through the hub" in l for l in _lines(a))


def test_a_passing_error_costs_one_retry_not_the_video(tmp_path):
    http = _Http(put=[httpx.ReadTimeout("slow"), 200])
    a = _agent(http)
    assert _up(a, _video(tmp_path)) is True
    assert http.kinds() == ["presign", "put", "presign", "put", "complete"]


def test_a_full_store_is_reported_not_retried_here(tmp_path, production):
    http = _Http(presign=[507])
    a = _agent(http)
    st = {}
    assert _up(a, _video(tmp_path), status_out=st) is False
    assert st == {"status": 507}                           # the backoff caller reads this
    assert http.kinds() == ["presign"]
    assert production == []


def test_a_full_store_is_waited_out_by_the_backoff(tmp_path, production):
    http = _Http(presign=[507, 507, 200])
    a = _agent(http)
    p = _video(tmp_path)
    ok = asyncio.run(a._upload_video_with_backoff(_assign(), p, p.name, mime="video/mp4"))
    assert ok is True
    assert http.kinds() == ["presign", "presign", "presign", "put", "complete"]
    assert len(production) == 2
    assert "hub" not in http.kinds()


def test_a_job_the_hub_no_longer_knows_is_not_retried(tmp_path, production):
    http = _Http(presign=[404])
    a = _agent(http)
    st = {}
    assert _up(a, _video(tmp_path), status_out=st) is False
    assert st == {"status": 404}
    assert http.kinds() == ["presign"]


def test_an_old_status_does_not_outlive_a_later_network_error(tmp_path):
    http = _Http(presign=[503, 200, 200], put=[httpx.ConnectError("x"), httpx.ConnectError("y")])
    a = _agent(http)
    st = {}
    assert _up(a, _video(tmp_path), status_out=st) is False
    assert "status" not in st


def test_a_video_too_big_for_one_upload_is_not_sent_anywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPRIKA_WORKER_MINIO_DIRECT_VIDEO_MAX_MB", "1")
    http = _Http()
    a = _agent(http)
    assert _up(a, _video(tmp_path, size=2 * MB)) is False
    assert http.calls == []
    assert any("video not uploaded" in l and "never sent through the hub" in l for l in _lines(a))


def test_an_empty_file_is_not_sent(tmp_path):
    http = _Http()
    a = _agent(http)
    assert _up(a, _video(tmp_path, size=0)) is False
    assert http.calls == []


def test_a_job_the_hub_gave_up_on_stops_the_retries(tmp_path):
    http = _Http(put=[httpx.ConnectError("x")] * 3)
    a = _agent(http)
    a._abandoned_job_ids.add(JOB)
    assert _up(a, _video(tmp_path)) is False
    assert http.kinds() == ["presign", "put"]


def test_a_restarting_hub_does_not_cost_a_second_upload(tmp_path):
    http = _Http(complete=[503, httpx.ConnectError("restarting"), 200])
    a = _agent(http)
    assert _up(a, _video(tmp_path)) is True
    assert http.kinds() == ["presign", "put", "complete", "complete", "complete"]


def test_a_recorded_upload_that_never_confirms_fails(tmp_path):
    http = _Http(complete=[409] * 9)
    a = _agent(http)
    st = {}
    assert _up(a, _video(tmp_path), status_out=st) is False
    assert "hub" not in http.kinds()
    assert st == {"status": 409}


# --- the two ways a video may still reach the hub, both deliberate --------------


def test_a_development_box_may_fall_back_to_the_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPRIKA_VIDEO_ALLOW_DISK", "1")
    http = _Http(presign=[503] * 3)                        # no object store on a dev stack
    a = _agent(http)
    assert _up(a, _video(tmp_path)) is True
    assert http.kinds() == ["presign"] * 3 + ["hub"]


def test_the_emergency_switch_sends_videos_through_the_hub_again(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPRIKA_WORKER_MINIO_DIRECT_VIDEO", "0")
    http = _Http()
    a = _agent(http)
    assert _up(a, _video(tmp_path)) is True
    assert http.kinds() == ["hub"]


# --- a session's videos take the same road --------------------------------------


def _session():
    return types.SimpleNamespace(asset_upload_base=BASE, uploaded_assets=set(), job_id=JOB)


def test_a_session_video_goes_to_the_store(tmp_path):
    http = _Http()
    a = _agent(http)
    s = _session()
    p = _video(tmp_path, name="session.mp4")
    ok = asyncio.run(a._upload_one_session_asset(
        s, p, mime="video/mp4", source_url="https://cdn.test/v.mp4", page_url="https://site.test/",
    ))
    assert ok is True
    assert http.kinds() == ["presign", "put", "complete"]
    assert http.calls[0][1] == BASE + "/presign"
    assert http.calls[0][2]["json"]["source_url"] == "https://cdn.test/v.mp4"
    assert s.uploaded_assets == {"session.mp4"}


def test_a_session_video_that_cannot_be_sent_is_not_posted_to_the_hub(tmp_path):
    http = _Http(put=[httpx.ConnectError("x")] * 3)
    a = _agent(http)
    s = _session()
    ok = asyncio.run(a._upload_one_session_asset(s, _video(tmp_path), mime="video/mp4"))
    assert ok is False
    assert "hub" not in http.kinds()
    assert s.uploaded_assets == set()


def test_a_session_screenshot_still_goes_through_the_hub(tmp_path):
    http = _Http()
    a = _agent(http)
    s = _session()
    p = tmp_path / "shot.png"
    p.write_bytes(b"x" * 100)
    assert asyncio.run(a._upload_one_session_asset(s, p, mime="image/png")) is True
    assert http.kinds() == ["hub"]
