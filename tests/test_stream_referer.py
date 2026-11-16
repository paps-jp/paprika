"""The referer a detected video stream is downloaded with.

A browser sends a request made inside an embedded frame with that FRAME as
its referer, and stream hosts check it. The downloader used to present the
fetched page instead; for a player sitting in a frame (or in a frame inside
a frame) that is a different site, and the host answered 403 -- while the
same URL with the frame as referer returned the playlist (2026-11-14).
"""
from core import fetcher as F
from server.worker.browser_ops import media

PAGE = "https://site.example/watch/1"
FRAME = "https://player.other.example/e/abc"
STREAM = "https://cdn.far.example/v/master.m3u8"


def test_a_stream_is_downloaded_with_the_document_that_asked_for_it():
    docs: dict = {}
    F.note_stream_document(docs, STREAM, FRAME)
    assert F.stream_referer(docs, STREAM, PAGE) == FRAME
    # Nothing known about a URL: the page, as before.
    assert F.stream_referer(docs, "https://cdn.far.example/other.m3u8", PAGE) == PAGE
    assert F.stream_referer({}, STREAM, None) is None


def test_the_first_requesting_document_is_kept_and_only_real_documents_count():
    docs: dict = {}
    F.note_stream_document(docs, STREAM, FRAME)
    F.note_stream_document(docs, STREAM, PAGE)                       # a later request for the same URL
    assert docs == {STREAM: FRAME}
    for doc in ("", None, "about:blank", "about:srcdoc", "blob:https://x/1", 7):
        F.note_stream_document(docs, "https://cdn.far.example/a.mp4", doc)
    assert "https://cdn.far.example/a.mp4" not in docs
    # Only video-like URLs are remembered: pages request thousands of other things.
    F.note_stream_document(docs, "https://cdn.far.example/logo.png", FRAME)
    F.note_stream_document(docs, "", FRAME)
    F.note_stream_document(docs, "https://cdn.far.example/seg-1.ts?t=9", FRAME)
    assert set(docs) == {STREAM, "https://cdn.far.example/seg-1.ts?t=9"}


def test_the_table_of_documents_is_bounded():
    docs = {f"https://c.example/{i}.ts": FRAME for i in range(F._STREAM_DOCS_MAX)}
    F.note_stream_document(docs, STREAM, FRAME)
    assert STREAM not in docs and len(docs) == F._STREAM_DOCS_MAX


def test_the_in_page_hook_reports_the_document_it_runs_in():
    # The hook is installed in every frame; location.href there is the requesting document.
    js = media._URL_CAPTURE_HOOK_JS
    assert js.count("doc: String(location.href)") == 2               # fetch() and XMLHttpRequest
