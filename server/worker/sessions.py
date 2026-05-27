"""Worker-side session state.

A SessionState bundles everything the worker needs to keep alive
between ``HubSessionStart`` and ``HubSessionEnd`` for one session:

  * the :class:`Lane` it reserved
  * the nodriver ``browser`` (CDP connection) and ``tab`` (page) it
    attached
  * a per-session :class:`asyncio.Lock` so concurrent
    ``HubSessionAction`` messages serialise
  * the visited-URL set used to mark ``✓`` in outlines
  * the assets directory for ``capture()`` outputs

Each ``WorkerAgent`` holds a ``dict[session_id, SessionState]`` and
routes incoming session messages to the right entry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionState:
    """In-process state for one active session on a worker.

    Multi-tab model (Phase 1):
      A session now holds **N tabs** (= nodriver ``Tab`` objects),
      keyed by short string ids. ``state.pages[pid] -> Tab``. One of
      them is the *default* page (``state.default_page_id``) and the
      backwards-compatible ``state.tab`` property reads / writes it.

      The existing dispatcher code paths (click / fill / outline /
      goto / ...) all reference ``state.tab`` and therefore keep
      operating on the default page without changes. Phase 2 adds
      operator-facing ``page_id`` routing on top of this same dict.
    """

    session_id: str
    # The reserved Lane (server.worker.lanes.Lane). Typed as Any to avoid
    # a circular import; the field is concretely a Lane.
    lane: Any
    # nodriver ``browser`` (CDP connection). Single Chrome instance per
    # lane; each tab in this session lives inside it.
    browser: Any = None
    # All tabs owned by this session, keyed by short id (``p_default``,
    # ``p_<uuid4hex8>``, ...). Populated by session_start (default tab)
    # and (in Phase 2) by ``page.new_tab()`` / popup-event handlers.
    pages: dict[str, Any] = field(default_factory=dict)
    # Per-page asyncio.Lock so two actions on the SAME tab serialise,
    # while actions on different tabs in the same session run in
    # parallel. Mirrors ``state.lock`` (session-wide) but at tab grain.
    page_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Which page is the "current" / default. Dispatcher primitives that
    # don't carry an explicit page_id target this one. session_start
    # sets it to ``"p_default"``; ``page.switch()`` would mutate it in
    # the future operator API (Phase 2).
    default_page_id: str | None = None
    # Assets directory for this session's captures. Created during
    # session_start under a tmp path; uploaded to the hub per-capture
    # when ``asset_upload_base`` is set (codegen-loop / session jobs).
    assets_dir: Path | None = None
    # If set, after each capture the worker POSTs the newly-written
    # files to this URL so they land in the parent job's /assets/ dir
    # on the hub (and the gallery picks them up). When None (e.g. a
    # legacy bare /sessions/* caller) captures stay on the worker
    # tempdir and disappear at session_end.
    asset_upload_base: str | None = None
    # Names of asset files we've already uploaded -- so re-uploads are
    # cheap (the hub overwrites by name) and we can skip files an
    # earlier capture already shipped.
    uploaded_assets: set[str] = field(default_factory=set)
    # URLs the passive asset listener has already saved (or queued).
    # Used to dedup across page navigations: revisiting the same image
    # URL on multiple pages of one session won't produce foo.png +
    # foo_1.png + foo_2.png in the gallery -- the second sighting is
    # short-circuited at the on_response handler before we even try
    # to read the response body.
    seen_asset_urls: set[str] = field(default_factory=set)
    # Network log: every HTTP response the browser loads while this
    # session is alive, tracked by the CDP Network listeners installed
    # by ``install_session_asset_capture``. Each entry is a dict:
    #   {url, mime, size, saved, timestamp, document_url}
    # The Live panel's "Network" tab reads this via the ``kind="network"``
    # session action so the operator can inspect traffic and selectively
    # add items to the job's asset gallery.
    network_log: list[dict] = field(default_factory=list)
    # Canonicalised URLs the session has been on, in arrival order.
    # Powers the visited=true marker in outline().
    visited_urls: set[str] = field(default_factory=set)
    visited_urls_ordered: list[str] = field(default_factory=list)
    # Serialise concurrent action handling for one session. Different
    # sessions on the same worker still run in parallel.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # When True this session is owned by a running fetch job and the
    # tab/browser handles are managed by ``core.fetcher.fetch()``. The
    # action dispatcher rejects write-style operations (click / fill /
    # navigate / scroll / press_key / back) for these sessions so the
    # operator can inspect cookies / outline / screenshot without
    # racing the fetch loop. The session goes away automatically when
    # the fetch returns (worker on_browser_closing callback unregisters
    # it from ``self._sessions``) -- EXCEPT in keep_session=True mode,
    # where the worker flips this back to False right after fetch and
    # leaves the session alive for operator interaction.
    is_fetch_owned: bool = False
    # Parent job_id that owns this session (set by both session_start
    # and the fetch keep_session path). The fetch_refresh dispatcher
    # uses this to build the per-job upload URLs (page.html + assets).
    job_id: str | None = None
    # Worker-side tempdir to rmtree on session_end. Set by the fetch
    # keep_session path so the workdir (which holds assets_dir) lives
    # past the normal fetch cleanup. None for session_start-style
    # sessions (those manage their own tmpdir).
    workdir: Path | None = None

    # ----- video downloader (session-wide) ------------------------------
    # Closures from agent._make_video_downloader, created once per
    # session at session_open and shared across page.agent() / passive
    # m3u8 detection / explicit page.download_video() calls. Wired so:
    #
    # * ``video_downloader(url, referer)``: sync; fires yt-dlp (HLS/DASH)
    #   or direct GET (mp4/webm/...) in the background if the URL shape
    #   matches a video. Idempotent on the same URL via an internal set.
    #   Called by:
    #     - install_session_asset_capture's on_response when a .m3u8 /
    #       .mpd response is seen (passive auto-detect from the moment
    #       the session starts -- no wait for the LLM / SDK caller).
    #     - the page-navigation video detector in _handle_session_agent
    #       (URL changes to .mp4 between agent steps).
    #
    # * ``video_drainer()``: async; awaits all pending background tasks
    #   the downloader spawned. Called from _teardown_session_state
    #   BEFORE the tab is closed so HLS merges aren't killed mid-ffmpeg.
    #
    # Both are None until session_open wires them up. Code paths that
    # might run before session_open (or in fetch_owned sessions) must
    # tolerate None and fall back to creating a local downloader.
    video_downloader: Any | None = None
    video_drainer: Any | None = None

    # ----- multi-tab compat shim ----------------------------------------
    # Existing dispatcher code reads ``state.tab`` everywhere. Expose it
    # as a property that proxies to the default page so we don't have
    # to hunt down ~16 call sites for Phase 1. Phase 2's operator API
    # ``page.new_tab() / page.switch()`` will move on top of the same
    # ``state.pages`` dict.

    @property
    def tab(self) -> Any:
        if self.default_page_id is None:
            return None
        return self.pages.get(self.default_page_id)

    @tab.setter
    def tab(self, value: Any) -> None:
        # Legacy setter -- assigning to ``state.tab`` updates the entry
        # under the current default_page_id, creating one if missing.
        # session_start does ``state.tab = await chrome.get(...)`` once;
        # most other code just READS .tab.
        if self.default_page_id is None:
            self.default_page_id = "p_default"
        self.pages[self.default_page_id] = value
        # Ensure a lock exists for this page id.
        if self.default_page_id not in self.page_locks:
            self.page_locks[self.default_page_id] = asyncio.Lock()

    def note_url(self, canon: str) -> None:
        if canon and canon not in self.visited_urls:
            self.visited_urls.add(canon)
            self.visited_urls_ordered.append(canon)
