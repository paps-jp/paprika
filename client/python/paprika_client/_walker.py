"""High-level site walker primitive for paprika-client.

LLM-generated crawl scripts kept re-deriving the same brittle bookkeeping --
queue management, URL canonicalisation, duplicate elimination, dead-end-URL
filtering, off-domain redirect handling -- and getting it wrong in
different ways every retry. This module centralises that logic in tested
Python so scripts only have to write their *per-page* work.

Inspired by:

  * **Scrapy**'s Spider + RFP dupefilter + allowed_domains + LinkExtractor
    deny patterns: same separation of "framework owns queue, dedup,
    filter" / "operator owns parse(response)".
  * **Heritrix** / Nutch's BFS frontier model with depth-bounded crawl.
  * Lessons from production paprika failures (jobs ea25984276b7,
    8644b1c11303, 1f9e030b354d): RSS / sitemap dead-ends, cross-domain
    redirect cycles, "links[0] is the site logo back to home" oscillation,
    "modal blocked first outline".

Usage::

    import paprika_client as pap
    from paprika_client import async_paprika

    async with async_paprika.connect() as cli:
        async with cli.session(initial_url="https://example.com/") as page:
            # Always clear startup modals BEFORE walking.
            await page.agent("If a consent dialog appears, accept it.",
                              max_steps=2)

            async for visit in pap.walk(
                page,
                start_url="https://example.com/",
                target_pages=100,
                same_domain=True,
            ):
                print(f"[{visit.n}/{visit.target}] depth={visit.depth} "
                      f"{visit.url}")
                # ...per-page work here. Do NOT navigate; the walker owns
                # navigation. Reading + capturing is fine:
                await page.capture(f"page-{visit.n}")

The walker yields a :class:`Visit` only when a page actually landed in
scope (no off-domain redirect, no filtered URL, no load error). Failed
candidates are silently skipped.
"""
from __future__ import annotations

import asyncio
import random
import re
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Optional, Union
from urllib.parse import urldefrag, urljoin, urlparse


# Default deny patterns -- regexes applied to the URL's path (lowercase).
# Each entry is the kind of URL we've watched LLM crawlers land on and then
# bail out of because the page has no useful HTML links to harvest.
DEFAULT_DENY_PATTERNS = [
    # Non-HTML asset extensions. Outline of these returns nothing, so the
    # next iter exits "no more links to visit" -- the classic dead-end.
    r"\.(?:xml|rss|atom|json|jsonl|ndjson|pdf|zip|tar|tgz|gz|bz2|7z|"
    r"mp3|mp4|m4a|webm|avi|mov|mkv|wmv|flv|"
    r"jpg|jpeg|png|gif|webp|avif|svg|ico|"
    r"css|js|mjs|woff|woff2|ttf|otf|eot|"
    r"csv|tsv|xls|xlsx|doc|docx|ppt|pptx|"
    r"exe|dmg|deb|rpm|msi|apk|iso)(?:\?|$)",
    # Well-known machine-readable endpoints.
    r"/(?:rss|feed|atom|sitemap[^/]*|robots\.txt|favicon\.ico|"
    r"\.well-known|wp-json|graphql|api)(?:/|$|\.|\?)",
]


@dataclass
class Visit:
    """One successful landing during a walk.

    Yielded by :func:`walk` (and :meth:`Walker.__aiter__`) only when the
    page actually loaded on a URL that passes the walker's filters and
    domain checks. Failed candidates (load errors, off-domain redirects,
    filtered URLs) are silently skipped.

    Fields:
      n              1-based count of successful in-scope landings.
      target         The configured ``target_pages`` -- so callers can
                     render progress like ``[42/100]``.
      url            The actual URL the browser is on (after any
                     server-side redirects). May differ from
                     ``requested_url`` if the site redirected.
      requested_url  The URL the walker asked ``page.goto()`` to load.
      depth          Tree depth from ``start_url`` (start_url is depth 0).
      outline        The page outline text, pre-fetched by the walker
                     for link harvesting. Cached for free; use it from
                     the loop body instead of calling
                     ``page.outline()`` again.
      page           The :class:`paprika_client.Page` handle, for
                     convenience (so the loop body doesn't have to
                     capture ``page`` from its enclosing scope).
    """
    n: int
    target: int
    url: str
    requested_url: str
    depth: int
    outline: str = ""
    page: "object" = None  # typed as object to dodge the circular import


def _canonicalise_url(url: str) -> str:
    """Strip the URL fragment and trailing slash so we dedup
    consistently. ``a.com/x#frag`` and ``a.com/x`` map to the same key."""
    url, _frag = urldefrag(url)
    return url


def _normalise_netloc(netloc: str) -> str:
    """Strip 'www.' prefix and lowercase. Operators usually mean
    "the same site" when they write ``same_domain=True``, and
    ``www.example.com`` vs ``example.com`` shouldn't count as
    different sites."""
    nl = (netloc or "").lower()
    if nl.startswith("www."):
        nl = nl[4:]
    return nl


def _matches_recrawl_pattern(url: str, patterns) -> bool:
    """Glob-style match: ``*`` = any run of chars, ``?`` = one. Used
    by the walker's host-dedup path so frontier pages (site index,
    category listings) can be revisited each run even if they're in
    the host's visited set."""
    if not url or not patterns:
        return False
    for p in patterns:
        if not p:
            continue
        rx = re.escape(p).replace(r"\*", ".*").replace(r"\?", ".")
        try:
            if re.fullmatch(rx, url):
                return True
        except re.error:
            continue
    return False


class Walker:
    """The class behind :func:`walk`. Use the function form unless you
    need to inspect walker state (``walker.queue``, ``walker.crawled``)
    from outside the loop."""

    def __init__(
        self,
        page,
        *,
        start_url: Optional[str] = None,
        target_pages: int = 100,
        same_domain: bool = True,
        allowed_domains: Optional[Iterable[str]] = None,
        allow_paths: Optional[Iterable[str]] = None,
        deny_paths: Optional[Iterable[str]] = None,
        deny_defaults: bool = True,
        order: str = "bfs",
        max_depth: Optional[int] = None,
        per_page_timeout_s: float = 30.0,
        handle_modal_each_page: Optional[str] = None,
        persist_state: Union[bool, str] = True,
        host_dedup: bool = False,
        recrawl_patterns: Optional[Iterable[str]] = None,
        log=None,
    ) -> None:
        if order not in ("bfs", "dfs", "random"):
            raise ValueError("order must be 'bfs' | 'dfs' | 'random'")
        if target_pages < 1:
            raise ValueError("target_pages must be >= 1")

        self.page = page
        # Resolve start_url: fall back to page._url if known. ``start_url``
        # MUST be absolute -- relative wouldn't make sense.
        if start_url is None:
            start_url = getattr(page, "url", None) or getattr(page, "_url", None)
        if not start_url:
            raise ValueError(
                "start_url is required (or set the page to a URL first)"
            )
        self.start_url = _canonicalise_url(start_url)
        parsed = urlparse(self.start_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"start_url must be absolute: {start_url!r}")
        self._base_netloc = _normalise_netloc(parsed.netloc)

        self.target_pages = int(target_pages)
        self.same_domain = bool(same_domain)
        self.order = order
        self.max_depth = max_depth
        self.per_page_timeout_s = float(per_page_timeout_s)
        self.handle_modal_each_page = handle_modal_each_page
        self._log = log

        # Allowed-domains resolution: explicit list wins; else derive from
        # start_url when ``same_domain=True``; else unrestricted.
        if allowed_domains is not None:
            self._allowed_netlocs = {
                _normalise_netloc(d) for d in allowed_domains
            }
        elif self.same_domain:
            self._allowed_netlocs = {self._base_netloc}
        else:
            self._allowed_netlocs = None  # any host

        # Compile filter regexes.
        self._allow_rx = [re.compile(p) for p in (allow_paths or [])]
        deny_list = list(deny_paths or [])
        if deny_defaults:
            deny_list = DEFAULT_DENY_PATTERNS + deny_list
        self._deny_rx = [re.compile(p, re.IGNORECASE) for p in deny_list]

        # State -- all sets are keyed by canonicalised URL.
        # queue: front-of-walk frontier. (url, depth) pairs.
        # attempted: every URL we've EVER tried to navigate to (success or
        #   off-domain), so we never retry the same URL.
        # crawled: subset of attempted that landed successfully in scope.
        self.queue: deque = deque([(self.start_url, 0)])
        self.attempted: set = set()
        self.crawled: set = set()

        # Persistent-state key for resuming across attempts. ``True``
        # (default) -> "walker"; ``False`` -> disabled; or pass an
        # explicit key string. Only meaningful when the session is
        # bound to a parent_job_id (codegen-loop / rerun jobs are).
        if isinstance(persist_state, bool):
            self._state_key: Optional[str] = "walker" if persist_state else None
        else:
            self._state_key = persist_state or None

        # Host-wide dedup is **opt-in** (default: False). When enabled
        # explicitly the walker:
        #   * seeds ``attempted`` with the host's accumulated visited
        #     URL set at start (via GET /hosts/{host}/visited)
        #   * fetches the host's ``recrawl_patterns`` and bypasses
        #     dedup for URLs that match them (so frontier pages can
        #     be revisited each run)
        #   * POSTs each newly-crawled URL to the host registry as
        #     the crawl proceeds
        #
        # Why opt-in: the "I ran my job and got 0 new URLs because last
        # week's run visited them all" surprise was the #1 source of
        # operator confusion. The cron-style daily-crawl scenario
        # where dedup pays off is a minority of jobs; most are
        # one-shot exploratory or comparison runs where re-visiting is
        # actually wanted. Pass ``host_dedup=True`` to turn it on for
        # long-running multi-day crawls.
        self._host_dedup = bool(host_dedup)
        # Per-instance recrawl_patterns override (operator-supplied).
        # If None, we fetch from the host registry at start.
        self._explicit_recrawl_patterns: Optional[list[str]] = (
            list(recrawl_patterns) if recrawl_patterns is not None else None
        )
        # Populated by _load_host_dedup() before the main loop.
        self._recrawl_patterns: list[str] = []

    # ----- internal helpers --------------------------------------------

    def _say(self, msg: str) -> None:
        if self._log is not None:
            try:
                self._log(msg)
            except Exception:
                pass

    def _netloc_in_scope(self, netloc: str) -> bool:
        if self._allowed_netlocs is None:
            return True
        return _normalise_netloc(netloc) in self._allowed_netlocs

    def _url_passes_filters(self, url: str) -> bool:
        """Decide whether a candidate URL is worth enqueueing.
        Domain check + deny list + (optional) allow list. URLs already in
        ``self.attempted`` are filtered separately by the caller."""
        parsed = urlparse(url)
        if not parsed.scheme.startswith("http"):
            return False
        if not self._netloc_in_scope(parsed.netloc):
            return False
        path = parsed.path.lower() or "/"
        for rx in self._deny_rx:
            if rx.search(path):
                return False
        if self._allow_rx and not any(rx.search(path) for rx in self._allow_rx):
            return False
        return True

    def _pop_next(self):
        """Pop the next (url, depth) according to ``self.order``. Returns
        None when the frontier is empty."""
        if not self.queue:
            return None
        if self.order == "bfs":
            return self.queue.popleft()
        if self.order == "dfs":
            return self.queue.pop()
        # random
        i = random.randrange(len(self.queue))
        # deque doesn't support O(1) random access; rotate the picked
        # element to the front and popleft.
        self.queue.rotate(-i)
        item = self.queue.popleft()
        return item

    async def _harvest_links(self, current_url: str, outline_text: str,
                             current_depth: int) -> int:
        """Pull every in-scope candidate link off the current page and
        enqueue the ones we haven't already attempted. Returns the
        number of links actually added to the queue."""
        added = 0
        for line in outline_text.splitlines():
            # outline format: "[@N] tag "visible text" href=URL [visited=true]"
            # href can be relative ("/path"), protocol-relative ("//cdn..."),
            # or absolute ("https://..."). urljoin handles all three.
            m = re.search(r"href=(\S+)", line)
            if not m:
                continue
            href = m.group(1).rstrip('"').rstrip(",").rstrip(";")
            if not href:
                continue
            try:
                abs_url = urljoin(current_url, href)
            except Exception:
                continue
            abs_url = _canonicalise_url(abs_url)
            if abs_url in self.attempted:
                continue
            if not self._url_passes_filters(abs_url):
                continue
            # Avoid enqueueing the very URL we're already on.
            if abs_url == _canonicalise_url(current_url):
                continue
            self.queue.append((abs_url, current_depth + 1))
            added += 1
        return added

    # ----- iteration ----------------------------------------------------

    def __aiter__(self) -> AsyncIterator[Visit]:
        return self._walk()

    async def _load_saved_state(self) -> None:
        """If persist_state is on, try to resume from a saved snapshot.
        Best-effort -- a missing key (first attempt), corrupt JSON, or
        a session that isn't job-bound (no parent_job_id) just leaves
        us in the default fresh state."""
        if not self._state_key:
            return
        try:
            saved = await self.page.get_state(self._state_key)
        except Exception as e:
            self._say(f"[walker] state load skipped: {e}")
            return
        if not saved:
            return
        try:
            saved_attempted = set(saved.get("attempted") or [])
            saved_crawled = set(saved.get("crawled") or [])
            saved_queue = [(u, int(d)) for u, d in (saved.get("queue") or [])]
        except Exception as e:
            self._say(f"[walker] state load: malformed snapshot ({e}); ignoring")
            return
        # Merge: prefer previous progress as a floor. Re-enqueue saved
        # queue items that aren't already in our (fresh) frontier.
        self.attempted.update(saved_attempted)
        self.crawled.update(saved_crawled)
        fresh_urls = {u for u, _ in self.queue}
        for u, d in saved_queue:
            if u not in self.attempted and u not in fresh_urls:
                self.queue.append((u, d))
        self._say(
            f"[walker] resumed: {len(self.crawled)} crawled, "
            f"{len(self.attempted)} attempted, "
            f"{len(self.queue)} queue from previous attempt"
        )

    async def _save_state(self) -> None:
        """Persist current crawl state for the next attempt to resume."""
        if not self._state_key:
            return
        try:
            await self.page.set_state(self._state_key, {
                "attempted": sorted(self.attempted),
                "crawled": sorted(self.crawled),
                "queue": list(self.queue),
                "target_pages": self.target_pages,
                "base_netloc": self._base_netloc,
            })
        except Exception as e:
            # Don't fail the walk over a transient state-write error.
            self._say(f"[walker] state save failed: {e}")

    async def _load_host_dedup(self) -> None:
        """Pull the host-wide visited URL set + recrawl patterns from
        the hub and merge into the walker's per-instance state.

        Best-effort: a missing host record, transport failure, or a
        client that doesn't expose ``_json`` (mock / unit-test) just
        leaves us in the per-job-only state.

        ``host_dedup=False`` skips this entirely.
        """
        if not self._host_dedup:
            return
        client = getattr(self.page, "_client", None)
        if client is None or not hasattr(client, "_json"):
            return
        host = self._base_netloc
        if not host:
            return
        # 1. Pull the host record so we know its recrawl_patterns.
        #    Explicit kwarg wins over registry-stored.
        if self._explicit_recrawl_patterns is not None:
            self._recrawl_patterns = list(self._explicit_recrawl_patterns)
        else:
            try:
                rec = await client._json("GET", f"/hosts/{host}")
                self._recrawl_patterns = list(rec.get("recrawl_patterns") or [])
            except Exception:
                # 404 = host not yet registered: fine, recrawl_patterns
                # stays empty. Any other failure: same.
                self._recrawl_patterns = []
        # 2. Pull the visited URL set. Use a large limit to grab
        #    everything in one go -- the registry returns up to the
        #    server's hard cap (default 500) per request, so we page
        #    through if needed.
        try:
            visited: list[str] = []
            offset = 0
            page_size = 500
            while True:
                resp = await client._json(
                    "GET",
                    f"/hosts/{host}/visited?offset={offset}&limit={page_size}",
                )
                urls = [u.get("url") for u in (resp.get("urls") or [])]
                visited.extend(u for u in urls if u)
                total = resp.get("total") or 0
                offset += page_size
                if offset >= total:
                    break
        except Exception as e:
            self._say(f"[walker] host dedup load skipped: {e}")
            return
        if visited:
            # Merge into attempted. We don't add them to crawled
            # because the walker's per-yield count should reflect
            # NEW visits in this run, not the lifetime total.
            self.attempted.update(visited)
            self._say(
                f"[walker] host dedup: merged {len(visited)} previously-"
                f"visited URL(s) for {host} into attempted set"
            )
        if self._recrawl_patterns:
            self._say(
                f"[walker] host dedup: {len(self._recrawl_patterns)} "
                f"recrawl pattern(s) active for {host}"
            )

    async def _record_host_visit(self, url: str) -> None:
        """Tell the hub about a newly-crawled URL. Fire-and-forget --
        a failure here must not abort the walk."""
        if not self._host_dedup or not url:
            return
        client = getattr(self.page, "_client", None)
        if client is None or not hasattr(client, "_json"):
            return
        host = self._base_netloc
        if not host:
            return
        try:
            await client._json(
                "POST", f"/hosts/{host}/visited",
                json={"url": url},
            )
        except Exception as e:
            self._say(f"[walker] host visited record failed for {url}: {e}")

    async def _walk(self) -> AsyncIterator[Visit]:
        # Resume from previous attempt's snapshot if available.
        await self._load_saved_state()
        # Merge in the host-wide visited set + recrawl patterns.
        await self._load_host_dedup()
        n = 0
        while self.queue and n < self.target_pages:
            popped = self._pop_next()
            if popped is None:
                break
            url, depth = popped
            url = _canonicalise_url(url)

            # Depth bound -- we still process this one (it's already in
            # the queue), but won't harvest children deeper than max_depth.
            if self.max_depth is not None and depth > self.max_depth:
                continue
            # Dedup check. If the URL matches a recrawl pattern, we
            # bypass the attempted set entirely (frontier pages should
            # always be re-crawled on each run).
            force_recrawl = _matches_recrawl_pattern(url, self._recrawl_patterns)
            if url in self.attempted and not force_recrawl:
                continue
            if not self._url_passes_filters(url):
                continue

            if not force_recrawl:
                self.attempted.add(url)

            # --- navigate ------------------------------------------------
            try:
                await asyncio.wait_for(
                    self.page.goto(url),
                    timeout=self.per_page_timeout_s,
                )
            except asyncio.TimeoutError:
                self._say(f"[walker] timeout navigating to {url}")
                continue
            except Exception as e:
                self._say(f"[walker] goto failed for {url}: {type(e).__name__}: {e}")
                continue

            # --- where did we actually end up? ---------------------------
            actual_url = url
            try:
                state = await self.page.state()
                actual_url = _canonicalise_url(state.get("url") or url)
            except Exception:
                # If even state() fails we still proceed -- treat as if
                # we're on the requested URL.
                pass

            # Off-scope redirect: go back so the next iter starts from a
            # known good page, and skip the yield. The URL is already in
            # `attempted` so we won't pick it again.
            actual_parsed = urlparse(actual_url)
            if not self._netloc_in_scope(actual_parsed.netloc):
                self._say(
                    f"[walker] off-scope redirect: {url} -> {actual_url}, "
                    f"going back"
                )
                try:
                    await self.page.back()
                except Exception:
                    pass
                continue

            # Also filter the post-redirect URL against deny rules.
            if not self._url_passes_filters(actual_url):
                self._say(f"[walker] post-redirect URL hits deny list: {actual_url}")
                try:
                    await self.page.back()
                except Exception:
                    pass
                continue

            # Optional: clear modals on every page (some sites pop a
            # consent overlay per navigation). Default off.
            if self.handle_modal_each_page:
                try:
                    await self.page.agent(
                        self.handle_modal_each_page,
                        max_steps=2,
                    )
                except Exception:
                    pass

            # --- harvest links (cached outline goes to the visit) --------
            outline_text = ""
            try:
                outline_text = await self.page.outline() or ""
            except Exception as e:
                self._say(f"[walker] outline failed at {actual_url}: {e}")
            # Enqueue before yielding so even if the operator's body
            # raises mid-iteration, our frontier already has the next
            # candidates.
            try:
                added = await self._harvest_links(actual_url, outline_text, depth)
                if added:
                    self._say(f"[walker] enqueued {added} new links from {actual_url}")
            except Exception as e:
                self._say(f"[walker] harvest failed at {actual_url}: {e}")

            # --- yield ---------------------------------------------------
            self.crawled.add(actual_url)
            n += 1
            # Persist state BEFORE yielding so even if the body raises,
            # the next attempt picks up from this point.
            await self._save_state()
            # Tell the hub about this visit so future runs on the same
            # host (this job or any other) skip it. Fire-and-forget.
            await self._record_host_visit(actual_url)
            yield Visit(
                n=n,
                target=self.target_pages,
                url=actual_url,
                requested_url=url,
                depth=depth,
                outline=outline_text,
                page=self.page,
            )

        self._say(
            f"[walker] done: crawled={len(self.crawled)} attempted="
            f"{len(self.attempted)} queue_left={len(self.queue)}"
        )


def walk(
    page,
    *,
    start_url: Optional[str] = None,
    target_pages: int = 100,
    same_domain: bool = True,
    allowed_domains: Optional[Iterable[str]] = None,
    allow_paths: Optional[Iterable[str]] = None,
    deny_paths: Optional[Iterable[str]] = None,
    deny_defaults: bool = True,
    order: str = "bfs",
    max_depth: Optional[int] = None,
    per_page_timeout_s: float = 30.0,
    handle_modal_each_page: Optional[str] = None,
    persist_state: Union[bool, str] = True,
    host_dedup: bool = False,
    recrawl_patterns: Optional[Iterable[str]] = None,
    log=None,
) -> AsyncIterator[Visit]:
    """High-level site walker. Crawls breadth-first (default) from
    ``start_url`` until ``target_pages`` successful in-scope landings,
    yielding one :class:`Visit` per landing.

    The walker owns the brittle parts of crawling -- queue, in-job
    dedup, URL filtering, off-scope redirect handling -- so the loop
    body only has to do per-page work (capture, scrape, classify, etc.).

    Cross-job dedup (= "skip URLs visited by any previous job on this
    host") is **opt-in** via ``host_dedup=True``. Default is False so
    new jobs always crawl from scratch; this matches the common
    exploratory / comparison use case. Turn it on for cron-style
    incremental crawls where re-visiting last week's URLs is wasted
    work.

    Args:
      page: A :class:`paprika_client.Page` from an open session.
      start_url: Where to start. Defaults to the session's current
        URL if known. Must be absolute (``http(s)://...``).
      target_pages: Stop after this many successful landings.
      same_domain: Restrict crawl to the start_url's domain (www. is
        normalised). Default True. Set False for cross-site crawling.
      allowed_domains: Explicit set of netlocs (e.g. ``["example.com",
        "static.example.com"]``). Overrides ``same_domain``.
      allow_paths: Optional list of regex patterns. If set, only URLs
        whose path matches at least one are crawled.
      deny_paths: Extra regex patterns to skip. Appended to defaults
        (which already block .xml/.json/.rss/.pdf/feed/sitemap/etc.).
      deny_defaults: Whether to apply the built-in deny list. Default
        True. Turn off if you really want to crawl PDFs / feeds.
      order: ``"bfs"`` (default), ``"dfs"``, or ``"random"``.
      max_depth: Don't follow links beyond this tree depth from
        ``start_url`` (depth 0). ``None`` (default) = unbounded.
      per_page_timeout_s: Per-navigation timeout. Default 30s.
      handle_modal_each_page: If set, this string is sent to
        ``page.agent(..., max_steps=2)`` after every successful
        navigation. Use for sites that pop a consent dialog per page;
        most sites only need a once-only call before walking.
      host_dedup: Cross-job URL dedup. **Default False** (opt-in).
        When True, the walker:
          * seeds its visited set from /hosts/{host}/visited at start
          * skips URLs the host registry already records, except those
            matching ``recrawl_patterns`` (frontier pages)
          * posts each newly-crawled URL back to the registry
        Use for daily / cron-style incremental crawls where you don't
        want to re-fetch last week's pages. Leave at False for
        exploratory or comparison runs where a fresh sweep is what
        you actually want.
      recrawl_patterns: Glob patterns (e.g. ``["*/category/*",
        "*?page=*"]``) that *escape* the host_dedup check. URLs
        matching any pattern are always re-crawled even if the host
        registry has seen them. No effect when host_dedup=False.
      log: Optional ``callable(str) -> None`` for walker-internal
        messages (skipped URLs, redirects, etc.). Defaults to silent;
        pass ``print`` to see what's happening.

    Returns:
      An async iterator of :class:`Visit`. Use with
      ``async for visit in pap.walk(...): ...``.

    Example::

        async for visit in pap.walk(page, start_url="https://example.com/",
                                     target_pages=50, max_depth=3):
            print(f"[{visit.n}/{visit.target}] {visit.url}")
            await page.capture(f"page-{visit.n}")
    """
    return Walker(
        page,
        start_url=start_url,
        target_pages=target_pages,
        same_domain=same_domain,
        allowed_domains=allowed_domains,
        allow_paths=allow_paths,
        deny_paths=deny_paths,
        deny_defaults=deny_defaults,
        order=order,
        max_depth=max_depth,
        per_page_timeout_s=per_page_timeout_s,
        handle_modal_each_page=handle_modal_each_page,
        persist_state=persist_state,
        host_dedup=host_dedup,
        recrawl_patterns=recrawl_patterns,
        log=log,
    ).__aiter__()
