"""Regression: POST /jobs/{id}/assets must STREAM the body to disk.

2026-08-31. The handler used to take ``file: UploadFile = File(...)``, which
puts the body on Starlette's ``SpooledTemporaryFile(max_size=1MB)``. Every
upload over 1 MB was therefore written to /tmp in full **before the handler
ran**, and then copied again to ``assets/<name>``. On the hub CTs /tmp is the
container overlayfs, i.e. the same consumer SSD as the asset store, so each
worker video (measured: 660 MB / 126 MB / 116 MB single spools) hit the disk
twice and the first copy was read back and thrown away.

Measured on hub-903 (75 s, write-fds only): /tmp spool 1,798 MB (76.8%) vs
assets/*.part 528 MB (22.6%). base's pve/data was doing 400 MB/s at util
91-100% and await 38.9 ms; the hub event loop stalled on fsync (POST /jobs
showed capacity=11,372 ms) and stats_async's 1.5 s wait_for fired, collapsing
the cross-hub worker aggregation to local-only.

These tests pin the properties the streaming rewrite rests on:
  1. no ``UploadFile`` in the signature  -- the spool cannot come back
  2. bytes land intact, even when the parser is fed in tiny chunks
     (boundaries split across chunk edges is the risky part)
  3. field order does not matter -- the final name is resolved after parsing
  4. metadata sidecar, response shape and .part cleanup are unchanged
  5. malformed / unauthenticated requests leave nothing behind
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os

import pytest
from fastapi import HTTPException

# server.hub.routes.jobs.uploads cannot be imported first: jobs/_base ->
# routes.sessions -> routes.hosts -> routes.sessions is a cycle that only
# resolves in the order server.hub.app establishes. Import that first.
import server.hub.app  # noqa: F401
from server.hub.routes.jobs import uploads


# --- helpers ---------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in for starlette Request: headers + a chunked stream."""

    def __init__(self, body: bytes, content_type: str, chunk: int = 64 * 1024):
        self.headers = {"content-type": content_type}
        self._body = body
        self._chunk = chunk

    async def stream(self):
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i:i + self._chunk]
        yield b""


def _multipart(parts: list[tuple], boundary: str = "----pytestboundary1234"):
    """Build a multipart body. Each part is either
    ("field", name, value) or ("file", name, filename, data)."""
    out = bytearray()
    b = boundary.encode()
    for p in parts:
        out += b"--" + b + b"\r\n"
        if p[0] == "field":
            _, name, value = p
            out += (
                b'Content-Disposition: form-data; name="%s"\r\n\r\n'
                % name.encode()
            )
            out += value.encode() + b"\r\n"
        else:
            _, name, filename, data = p
            out += (
                b'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (name.encode(), filename.encode())
            )
            out += b"Content-Type: application/octet-stream\r\n\r\n"
            out += data + b"\r\n"
    out += b"--" + b + b"--\r\n"
    return bytes(out), "multipart/form-data; boundary=" + boundary


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point the handler at a temp storage root and stub out its I/O deps."""

    async def _noop_resolve(job_id, require_subdir=None):
        return None

    async def _noop_mirror(path, job_id=None):
        return None

    monkeypatch.setattr(uploads, "_soft_resolve_job", _noop_resolve)
    monkeypatch.setattr(uploads, "get_storage_dir", lambda: tmp_path)
    monkeypatch.setattr(uploads.objstore, "mirror_file", _noop_mirror)
    monkeypatch.setattr(uploads.config, "worker_secret", None, raising=False)
    return tmp_path


def _run(job_id, body, ctype, chunk=64 * 1024):
    return asyncio.run(
        uploads.upload_asset(job_id, _FakeRequest(body, ctype, chunk))
    )


# --- 1. the spool must not come back ---------------------------------------


def test_handler_takes_no_uploadfile():
    """A ``UploadFile``/``File(...)`` parameter would reintroduce the /tmp
    spool: Starlette materialises the whole body before the handler runs."""
    sig = inspect.signature(uploads.upload_asset)
    annots = [str(p.annotation) for p in sig.parameters.values()]
    assert not any("UploadFile" in a for a in annots), annots
    defaults = [repr(p.default) for p in sig.parameters.values()]
    assert not any("File" in d and "Form" not in d for d in defaults), defaults
    assert "request" in sig.parameters


# --- 2. bytes land intact, including across chunk edges --------------------


@pytest.mark.parametrize("chunk", [7, 1024, 64 * 1024])
def test_roundtrip_exact_bytes(_isolate, chunk):
    """Feeding the parser 7 bytes at a time puts a boundary in the middle of
    almost every chunk -- the case a hand-rolled scanner gets wrong."""
    payload = os.urandom(300_000)
    body, ctype = _multipart([
        ("field", "asset_name", "clip.mp4"),
        ("field", "mime", "video/mp4"),
        ("file", "file", "clip.mp4", payload),
    ])
    res = _run("job1", body, ctype, chunk=chunk)
    saved = _isolate / "job1" / "assets" / "clip.mp4"
    assert saved.read_bytes() == payload
    assert res["size"] == len(payload)
    assert res["name"] == "clip.mp4"
    assert res["saved"] == str(saved)
    # no .part residue
    assert list((_isolate / "job1" / "assets").glob("*.part")) == []


def test_empty_file_part(_isolate):
    body, ctype = _multipart([
        ("field", "asset_name", "empty.bin"),
        ("file", "file", "empty.bin", b""),
    ])
    res = _run("job1", body, ctype)
    assert (_isolate / "job1" / "assets" / "empty.bin").read_bytes() == b""
    assert res["size"] == 0


# --- 3. field order must not matter ----------------------------------------


def test_fields_after_file_still_name_correctly(_isolate):
    """httpx sends data-then-files, but multipart guarantees no order. The
    final name is resolved after parsing and reached via rename."""
    payload = os.urandom(50_000)
    body, ctype = _multipart([
        ("file", "file", "wrong-name.bin", payload),
        ("field", "asset_name", "right-name.bin"),
    ])
    res = _run("job1", body, ctype, chunk=512)
    assets = _isolate / "job1" / "assets"
    assert (assets / "right-name.bin").read_bytes() == payload
    assert not (assets / "wrong-name.bin").exists()
    assert list(assets.glob("*.part")) == []
    assert res["name"] == "right-name.bin"


def test_filename_used_when_asset_name_absent(_isolate):
    body, ctype = _multipart([("file", "file", "from-filename.txt", b"hi")])
    res = _run("job1", body, ctype)
    assert res["name"] == "from-filename.txt"
    assert (_isolate / "job1" / "assets" / "from-filename.txt").read_bytes() == b"hi"


def test_name_is_sanitised(_isolate):
    body, ctype = _multipart([
        ("field", "asset_name", 'a/b\\c:d*e?f"g<h>i|j.png'),
        ("file", "file", "x.png", b"z"),
    ])
    res = _run("job1", body, ctype)
    assert "/" not in res["name"] and "\\" not in res["name"]
    assert (_isolate / "job1" / "assets" / res["name"]).read_bytes() == b"z"


# --- 4. sidecar metadata ----------------------------------------------------


def test_meta_sidecar(_isolate):
    body, ctype = _multipart([
        ("field", "asset_name", "pic.jpg"),
        ("field", "source_url", "https://ex.test/pic.jpg"),
        ("field", "page_url", "https://ex.test/page"),
        ("field", "mime", "image/jpeg"),
        ("file", "file", "pic.jpg", b"abcdef"),
    ])
    _run("job1", body, ctype)
    meta = _isolate / "job1" / "assets" / ".meta" / "pic.jpg.json"
    got = json.loads(meta.read_text(encoding="utf-8"))
    assert got == {
        "name": "pic.jpg",
        "source_url": "https://ex.test/pic.jpg",
        "page_url": "https://ex.test/page",
        "mime": "image/jpeg",
        "size": 6,
    }


def test_no_sidecar_without_metadata(_isolate):
    body, ctype = _multipart([
        ("field", "asset_name", "pic.jpg"),
        ("file", "file", "pic.jpg", b"abcdef"),
    ])
    _run("job1", body, ctype)
    assert not (_isolate / "job1" / "assets" / ".meta").exists()


# --- 5. rejects leave nothing behind ---------------------------------------


def test_non_multipart_rejected(_isolate):
    with pytest.raises(HTTPException) as e:
        _run("job1", b'{"a":1}', "application/json")
    assert e.value.status_code == 400


def test_missing_boundary_rejected(_isolate):
    with pytest.raises(HTTPException) as e:
        _run("job1", b"x", "multipart/form-data")
    assert e.value.status_code == 400


def test_missing_file_part_rejected(_isolate):
    body, ctype = _multipart([("field", "asset_name", "x.bin")])
    with pytest.raises(HTTPException) as e:
        _run("job1", body, ctype)
    assert e.value.status_code == 422


def test_oversized_form_field_rejected(_isolate):
    body, ctype = _multipart([
        ("field", "source_url", "u" * (uploads._MAX_FIELD_BYTES + 10)),
        ("file", "file", "x.bin", b"z"),
    ])
    with pytest.raises(HTTPException) as e:
        _run("job1", body, ctype, chunk=4096)
    assert e.value.status_code == 413


def test_bad_secret_rejected_and_no_file_left(_isolate, monkeypatch):
    monkeypatch.setattr(uploads.config, "worker_secret", "s3cret", raising=False)
    payload = os.urandom(20_000)
    body, ctype = _multipart([
        ("field", "secret", "wrong"),
        ("field", "asset_name", "x.bin"),
        ("file", "file", "x.bin", payload),
    ])
    with pytest.raises(HTTPException) as e:
        _run("job1", body, ctype, chunk=512)
    assert e.value.status_code == 401
    assets = _isolate / "job1" / "assets"
    assert not (assets / "x.bin").exists()
    assert list(assets.glob("*.part")) == []


def test_good_secret_accepted(_isolate, monkeypatch):
    monkeypatch.setattr(uploads.config, "worker_secret", "s3cret", raising=False)
    body, ctype = _multipart([
        ("field", "secret", "s3cret"),
        ("field", "asset_name", "x.bin"),
        ("file", "file", "x.bin", b"ok"),
    ])
    res = _run("job1", body, ctype)
    assert res["size"] == 2


def test_late_secret_still_enforced(_isolate, monkeypatch):
    """secret がファイルの後に来ても弾く (書いた分は消す)。"""
    monkeypatch.setattr(uploads.config, "worker_secret", "s3cret", raising=False)
    body, ctype = _multipart([
        ("field", "asset_name", "x.bin"),
        ("file", "file", "x.bin", os.urandom(10_000)),
        ("field", "secret", "wrong"),
    ])
    with pytest.raises(HTTPException) as e:
        _run("job1", body, ctype, chunk=256)
    assert e.value.status_code == 401
    assets = _isolate / "job1" / "assets"
    assert not (assets / "x.bin").exists()
    assert list(assets.glob("*.part")) == []


# --- 6. the wire format the worker actually sends ---------------------------


def test_matches_httpx_encoding(_isolate):
    """The worker posts via httpx ``files=``/``data=``. Encode a request with
    httpx itself and feed the real bytes through the handler."""
    httpx = pytest.importorskip("httpx")
    payload = os.urandom(200_000)
    req = httpx.Request(
        "POST", "http://h/jobs/job1/assets",
        files={"file": ("clip.mp4", payload, "video/mp4")},
        data={"asset_name": "clip.mp4", "mime": "video/mp4",
              "source_url": "https://ex.test/v"},
    )
    res = _run("job1", req.read(), req.headers["content-type"], chunk=8192)
    assert (_isolate / "job1" / "assets" / "clip.mp4").read_bytes() == payload
    assert res["size"] == len(payload)

# --- 7. concurrent uploads of the SAME name must not collide ---------------


def test_concurrent_same_name_uploads(_isolate):
    """2026-08-31 production regression: the temp file used to be
    ``<name>.part``, shared by every request for that name. With the body
    now streamed (rather than spooled to /tmp first) the write window spans
    the whole upload, so two workers posting the same asset name to the same
    job overlap: the first to finish renames the shared .part away and the
    second dies with FileNotFoundError (measured 1-7 per 5,000 uploads), and
    before that both were writing into the same file. The temp name is now
    unique per request."""
    payload_a = b"A" * 40_000
    payload_b = b"B" * 40_000
    body_a, ctype = _multipart([
        ("field", "asset_name", "same.jpg"),
        ("file", "file", "same.jpg", payload_a),
    ])
    body_b, _ = _multipart([
        ("field", "asset_name", "same.jpg"),
        ("file", "file", "same.jpg", payload_b),
    ])

    async def _both():
        return await asyncio.gather(
            uploads.upload_asset("job1", _FakeRequest(body_a, ctype, 256)),
            uploads.upload_asset("job1", _FakeRequest(body_b, ctype, 256)),
        )

    res = asyncio.run(_both())
    assert all(r["size"] == 40_000 for r in res), res
    assets = _isolate / "job1" / "assets"
    # last writer wins, but the bytes must be one payload or the other --
    # never an interleaving of both.
    got = (assets / "same.jpg").read_bytes()
    assert got in (payload_a, payload_b)
    assert list(assets.glob("*.part")) == []
