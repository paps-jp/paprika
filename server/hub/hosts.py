"""Per-host cookie registry.

Stores cookies (and operator notes) keyed by hostname so that a job
targeting example.com (or any other site that needs login) can have
its session pre-populated automatically -- no script-side
``set_cookies(...)`` boilerplate needed.

Storage layout::

    {data_dir}/hosts/<safe-host>.json

The file content is the JSON form of :class:`HostRecord`. One file
per host. Filenames are sanitised but the canonical key remains the
www-stripped lowercase host (so ``example.com`` and ``www.example.com``
collapse to the same record).

Auto-injection flow:

  1. Operator registers cookies for ``example.com`` via
     ``PUT /hosts/example.com``.
  2. A codegen-loop / rerun / direct session asks the hub to open
     ``cli.session(initial_url="https://www.example.com/")``.
  3. Hub's ``create_session`` extracts the host from ``initial_url``,
     looks it up in this registry, and attaches the cookie list to the
     ``HubSessionStart`` message it sends to the worker.
  4. Worker, before navigating to ``initial_url``, calls CDP
     ``Network.setCookies`` so the very first request carries them.
  5. Hub bumps ``last_used_at`` on the record.

Timestamps:
  - ``created_at``: first save (immutable)
  - ``updated_at``: every save / edit
  - ``last_used_at``: when the hub last auto-injected the cookies into
    a session_start. ``None`` until first use.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from server.hub._jsonstore import JsonRecordRegistry


def _normalise_host(host: str) -> str:
    """Lowercase + strip ``www.`` prefix + strip whitespace.

    So ``example.com`` and ``www.example.com`` map to the same record.
    Operators usually mean "the site" when they type either form."""
    h = (host or "").strip().lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def _safe_filename(host: str) -> str:
    """Make a host safe to use as a filename. The registry caps it at
    120 chars so weird inputs can't blow up the filesystem."""
    return re.sub(r"[^A-Za-z0-9.-]", "_", host)[:120]


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


@dataclass
class HostRecord:
    """One host's registered cookies + metadata.

    The ``cookies`` field is a list of CDP CookieParam-shaped dicts.
    Minimum useful entry: ``{"name": "foo", "value": "bar",
    "domain": ".example.com", "path": "/"}``. ``expires`` is optional
    (session cookies omit it). Other CDP-allowed fields:
    ``url``, ``secure``, ``httpOnly``, ``sameSite``, ``priority``,
    ``sameParty``, ``sourceScheme``, ``sourcePort``, ``partitionKey``.
    """

    host: str
    cookies: list[dict] = field(default_factory=list)
    notes: str | None = None
    # URL patterns (fnmatch / glob-style with ``*``) that should ALWAYS
    # be re-crawled, even when they appear in the host's visited set.
    # Used by ``pap.walk(host_dedup=True)`` so frontier pages
    # (site indexes, category listings, sitemaps) can be revisited each
    # run while still skipping individual content pages already seen.
    # Examples::
    #   "https://www.example.com/"                  # exact match
    #   "https://www.example.com/category/*"        # any URL under /category/
    #   "https://*.example.com/"                    # any subdomain
    recrawl_patterns: list[str] = field(default_factory=list)
    # How the worker's tab-killer treats popups (new tabs) opened by
    # this host's pages.
    #   "kill"   -- default. Close the popup. Redirect the main tab
    #               to the popup URL ONLY when same netloc.
    #   "follow" -- Close the popup. Redirect the main tab to the
    #               popup URL regardless of netloc. Use for sites
    #               that surface their real content (video pages,
    #               etc.) via a window.open to a different
    #               subdomain or short-lived redirect host (e.g.
    #               video-site.example -> embed.video-site.example/v/XXX).
    popup_policy: str = "kill"
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str | None = None
    # ---- auto re-login recipe (optional) ----------------------------
    # When set, the hub can re-authenticate this host automatically
    # so a login-gated fetch keeps working past the session-cookie
    # expiry. See _ensure_host_login() in app.py.
    #
    #   login_url    -- where to drive the login. Usually a page that
    #                   redirects to the login form when logged out
    #                   (e.g. a gated item URL) or the login page
    #                   itself. The agent runs against whatever this
    #                   resolves to.
    #   login_goal   -- the natural-language page.agent() goal that
    #                   performs the login. Contains the credentials
    #                   inline (email / password / etc.). Stored
    #                   plaintext (LAN-trusted, same model as cookies).
    #   login_check  -- substring that, if present in the post-navigate
    #                   URL **or** page title, means "still logged
    #                   out" (e.g. "login." or "Log in"). Used both to
    #                   decide whether a re-login is needed and to
    #                   confirm it succeeded.
    #   login_refresh_ttl_s -- pre-fetch staleness gate: re-login
    #                   before a fetch only when the last successful
    #                   login is older than this. 0 = re-check every
    #                   fetch. Default 900s (15 min), comfortably under
    #                   a typical PHP session idle timeout.
    #   last_login_at -- timestamp of the last successful auto-login.
    login_url: str | None = None
    login_goal: str | None = None
    login_check: str | None = None
    login_refresh_ttl_s: int = 900
    last_login_at: str | None = None
    # ---- pre-baked per-host fetch recipes ----------------------------
    # Each recipe is a "playbook" that the worker runs right after the
    # initial navigation (before scroll / asset capture). Used for the
    # site-specific stuff that doesn't belong in a generic Fetch:
    # cookie-banner dismissal, age-gate click, play-button kick, etc.
    #
    # Per RFC notes, the goal/code variants are deferred to Phase 2
    # (they involve LLM coordination during fetch). Phase 1 ships
    # ``actions`` only -- a deterministic list of click/fill/press/
    # wait/etc. dicts that browser_ops.execute() can run.
    #
    # Lookup: HostRecord.pick_recipe(url) glob-matches the URL's path
    # against each recipe's ``pattern`` (longest literal prefix wins,
    # ``*`` is wildcard). Returns the best match or None.
    fetch_recipes: list = field(default_factory=list)
    # ---- Phase 2b tenancy -------------------------------------------------
    # Hosts are keyed by hostname (one record per host). ``owner_id`` is the
    # tenant that pushed the cookies ("default" = shared tenant / auth
    # off/optional); ``shared`` = the cookies may ride onto ANY tenant's job
    # (the pre-tenancy ambient behaviour). Pre-tenancy records backfill to
    # owner=default / shared=True, so the worker-dispatch cookie gate
    # (auth.owner_can_use) is a no-op until enforce + a non-admin owner.
    owner_id: str = "default"
    shared: bool = True
    # Operator-registered fact: this host has NO video content. When True a
    # ``download_video`` fetch on this host is NOT auto-escalated into the AI
    # codegen-loop -- we don't spend GPU/AI hunting a video that's confirmed
    # absent. Curated / operator-asserted (set via PUT /hosts/{host} or the
    # #hosts edit modal). Read by server/hub/_escalate.py classify_completed.
    no_video: bool = False

    def to_json(self) -> dict:
        return asdict(self)

    def pick_recipe(self, url: str) -> "HostRecipe | None":
        """Return the best-matching :class:`HostRecipe` for ``url`` or
        None if no recipe applies. ``url`` is the full URL; we match
        on its path component using fnmatch-style globs. Among matches,
        the one with the longest literal prefix wins (most-specific
        first); ties broken by most recent ``last_success_at``."""
        if not self.fetch_recipes:
            return None
        from urllib.parse import urlparse
        try:
            path = (urlparse(url).path or "/") or "/"
        except Exception:
            return None
        import fnmatch
        candidates: list[tuple[int, str, HostRecipe]] = []
        for raw in self.fetch_recipes:
            r = raw if isinstance(raw, HostRecipe) else HostRecipe.from_json(raw)
            pat = (r.pattern or "*").strip()
            if not pat:
                pat = "*"
            # Normalise: prepend "/" when pattern looks path-shaped but
            # operator omitted it. Plain "*" stays "*" (match-anything).
            if pat != "*" and not pat.startswith("/") and not pat.startswith("*"):
                pat = "/" + pat
            if fnmatch.fnmatchcase(path, pat) or pat == "*":
                # Specificity score: literal char count before first wildcard.
                lit = 0
                for ch in pat:
                    if ch in "*?":
                        break
                    lit += 1
                candidates.append((lit, r.last_success_at or "", r))
        if not candidates:
            return None
        candidates.sort(key=lambda t: (-t[0], -ord(t[1][0]) if t[1] else 0))
        return candidates[0][2]

    @property
    def has_login_recipe(self) -> bool:
        return bool((self.login_goal or "").strip())

    @classmethod
    def from_json(cls, d: dict) -> HostRecord:
        pp = (d.get("popup_policy") or "kill").strip().lower()
        if pp not in ("kill", "follow"):
            pp = "kill"
        try:
            ttl = int(d.get("login_refresh_ttl_s") or 900)
        except (TypeError, ValueError):
            ttl = 900
        return cls(
            host=d.get("host", "") or "",
            cookies=list(d.get("cookies") or []),
            notes=d.get("notes"),
            recrawl_patterns=list(d.get("recrawl_patterns") or []),
            popup_policy=pp,
            created_at=d.get("created_at") or "",
            updated_at=d.get("updated_at") or "",
            last_used_at=d.get("last_used_at"),
            login_url=d.get("login_url"),
            login_goal=d.get("login_goal"),
            login_check=d.get("login_check"),
            login_refresh_ttl_s=ttl,
            last_login_at=d.get("last_login_at"),
            fetch_recipes=[
                HostRecipe.from_json(r) if isinstance(r, dict) else r
                for r in (d.get("fetch_recipes") or [])
                if r
            ],
            owner_id=str(d.get("owner_id") or "default"),
            shared=bool(d.get("shared", True)),
            no_video=bool(d.get("no_video") or False),
        )


@dataclass
class HostRecipe:
    """One per-host playbook: a glob-matched URL pattern + a list of
    deterministic actions to run right after Fetch's initial navigation
    (before scroll / asset capture).

    Phase 1 supports ``actions`` only -- a list of
    ``{"kind": "click|fill|press|scroll|wait|type|navigate|evaluate",
       ...kind-specific fields}`` dicts that the worker dispatches via
    browser_ops.execute(). ``goal`` and ``code`` are reserved for
    later phases (they require LLM coordination / sandbox execution
    during fetch which the Phase 1 scope explicitly excludes).
    """
    pattern: str = "*"
    description: str = ""
    actions: list = field(default_factory=list)  # Phase 1: primary execution mode
    goal: str | None = None                       # Phase 2: agent goal
    code: str | None = None                       # Phase 3: Python snippet
    engine: str = "auto"                          # for goal: which engine
    max_steps: int = 5                            # for goal: agent step cap
    timeout_s: float = 30.0
    created_by: str = "operator"                  # "operator" / "ai"
    created_from_job: str | None = None
    success_count: int = 0
    failure_count: int = 0
    created_at: str = ""
    last_success_at: str | None = None
    last_failure_at: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "HostRecipe":
        return cls(
            pattern=str(d.get("pattern") or "*"),
            description=str(d.get("description") or ""),
            actions=list(d.get("actions") or []),
            goal=d.get("goal"),
            code=d.get("code"),
            engine=str(d.get("engine") or "auto"),
            max_steps=int(d.get("max_steps") or 5),
            timeout_s=float(d.get("timeout_s") or 30.0),
            created_by=str(d.get("created_by") or "operator"),
            created_from_job=d.get("created_from_job"),
            success_count=int(d.get("success_count") or 0),
            failure_count=int(d.get("failure_count") or 0),
            created_at=str(d.get("created_at") or ""),
            last_success_at=d.get("last_success_at"),
            last_failure_at=d.get("last_failure_at"),
        )


# CDP CookieParam fields we'll forward. Anything else in the operator's
# JSON is silently dropped so a stray ``id`` / ``size`` / ``session``
# field copied from devtools doesn't blow up Network.setCookies.
_CDP_COOKIE_FIELDS = {
    "name",
    "value",
    "url",
    "domain",
    "path",
    "secure",
    "httpOnly",
    "sameSite",
    "expires",
    "priority",
    "sameParty",
    "sourceScheme",
    "sourcePort",
    "partitionKey",
}


def pattern_from_url(url: str, *, generalize: bool = True) -> str:
    """Return a sensible fnmatch glob for ``url``'s path, suitable as
    a HostRecipe.pattern seed.

    Phase 2c policy (vendor-neutral -- NO host names ever):

      * Drop the scheme + host + query + fragment; keep only the path.
      * Empty / "/" path  -> "/*"  (match everything on this host)
      * Trailing path segment that looks like an id (purely digits, or
        16+ hex chars) -> replace with ``*``. Cap at one such swap per
        path so a multi-segment path like /a/b/c/123 collapses to
        /a/b/c/* but /posts/12345/comments stays /posts/12345/comments
        (the LAST segment is the one we generalise).
      * Path ending in / gets "/*" appended.
      * Otherwise: append "*" so "/frame" matches "/frame?pi=..." too
        (query is stripped before matching; the trailing * also covers
        path-extended variants like "/frame/v2").

    Operators are expected to EDIT this in the UI before saving when
    the heuristic guesses wrong -- it's a seed, not a final pattern.
    """
    if not url:
        return "*"
    from urllib.parse import urlparse
    try:
        p = urlparse(url).path or "/"
    except Exception:
        return "*"
    if p in ("", "/"):
        return "/*"
    if not generalize:
        return p
    import re as _re
    segs = [s for s in p.split("/") if s]
    if segs:
        last = segs[-1]
        is_pure_digit = last.isdigit()
        is_long_hex = bool(_re.fullmatch(r"[0-9a-fA-F]{16,}", last))
        if is_pure_digit or is_long_hex:
            segs[-1] = "*"
            return "/" + "/".join(segs)
    if p.endswith("/"):
        return p + "*"
    return p + "*"


def cookies_for_cdp(cookies: list[dict]) -> list[dict]:
    """Project + sanitise a stored cookie list into a form
    Network.setCookies will accept. Drops unknown keys, coerces obvious
    types, and skips entries missing the required ``name`` / ``value``
    pair."""
    out: list[dict] = []
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        clean: dict = {k: v for k, v in c.items() if k in _CDP_COOKIE_FIELDS}
        if "name" not in clean or "value" not in clean:
            continue
        # expires may arrive as int or str; CDP wants float seconds.
        if "expires" in clean and not isinstance(clean["expires"], (int, float)):
            try:
                clean["expires"] = float(clean["expires"])
            except (TypeError, ValueError):
                clean.pop("expires", None)
        # Keep installed cookies alive client-side for >= 3 days: bump each
        # cookie's expiry to max(original, now+3d). Short-lived ones
        # (cf_clearance / PHPSESSID) and session cookies (no ``expires``)
        # would otherwise be dropped by Chrome during/after the fetch, and
        # the post-fetch dump-back would then persist their stale/expired
        # state back into the host registry. max() never SHORTENS a
        # longer-lived cookie. Server-side expiry is unaffected -- this only
        # stops Chrome from discarding the cookie early and keeps the
        # registry copy fresh across fetches.
        import time as _t
        try:
            _cur_exp = float(clean.get("expires") or 0)
        except (TypeError, ValueError):
            _cur_exp = 0.0
        clean["expires"] = max(_cur_exp, _t.time() + 3 * 86400)
        # CDP requires either url OR domain. If only domain is given and
        # it lacks the leading dot some browsers expect, leave it as-is
        # -- CDP is lenient.
        out.append(clean)
    return out


def cookies_to_netscape(cookies: list[dict], fallback_host: str = "") -> str:
    """Render a stored cookie list as a Netscape cookies.txt blob for
    ``yt-dlp --cookies``.

    Netscape line format (TAB-separated)::

        domain  include_subdomains  path  secure  expiry  name  value

    * ``domain``: from the cookie's ``domain`` field; falls back to
      ``fallback_host`` when absent.
    * ``include_subdomains``: ``TRUE`` when the domain begins with a
      dot (Netscape convention for "applies to subdomains"), else
      ``FALSE``.
    * HttpOnly cookies get a ``#HttpOnly_`` prefix on the domain --
      yt-dlp / curl understand it.
    * Session cookies (no expiry) are written with expiry ``0``.

    Returns "" when there are no usable cookies, so the caller can
    skip writing an empty file.
    """
    lines = ["# Netscape HTTP Cookie File"]
    n = 0
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if name is None or value is None:
            continue
        domain = (c.get("domain") or fallback_host or "").strip()
        if not domain:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        # expiry: accept expires (epoch float) or expirationDate.
        # Clamp session / negative expiries to 0 -- a negative value
        # in Netscape format reads as "expired in 1969" and yt-dlp /
        # curl silently drop the cookie. 0 = session cookie (kept for
        # the run).
        exp = c.get("expires", c.get("expirationDate", 0))
        try:
            exp_i = int(float(exp)) if exp else 0
        except (TypeError, ValueError):
            exp_i = 0
        if exp_i < 0:
            exp_i = 0
        dom_field = ("#HttpOnly_" + domain) if c.get("httpOnly") else domain
        lines.append(
            "\t".join(
                [
                    dom_field,
                    include_sub,
                    path,
                    secure,
                    str(exp_i),
                    str(name),
                    str(value),
                ]
            )
        )
        n += 1
    if n == 0:
        return ""
    return "\n".join(lines) + "\n"


class HostRegistry(JsonRecordRegistry[HostRecord]):
    """File-backed CRUD over the per-host cookie store. Inherits the
    generic list / get / delete / atomic-write from
    :class:`JsonRecordRegistry`; only the host-specific (de)serialisation
    + the cookie / recipe / auto-login helpers live here. Operations are
    O(1) (single-file read/write); list is O(N) over registered hosts,
    fine at the typical scale (tens, not millions)."""

    subdir = "hosts"

    # ---- JsonRecordRegistry hooks -----------------------------------------

    def _slug(self, key: str) -> str:
        return _safe_filename(_normalise_host(key))

    def _key_of(self, rec: HostRecord) -> str:
        return rec.host

    def _to_json(self, rec: HostRecord) -> dict:
        return rec.to_json()

    def _from_json(self, d: dict) -> HostRecord:
        return HostRecord.from_json(d)

    def upsert(
        self,
        host: str,
        cookies: list[dict],
        notes: str | None = None,
        recrawl_patterns: list[str] | None = None,
        popup_policy: str | None = None,
        login_url: str | None = None,
        login_goal: str | None = None,
        login_check: str | None = None,
        login_refresh_ttl_s: int | None = None,
        last_login_at: str | None = None,
        fetch_recipes: list | None = None,
        owner_id: str | None = None,
        shared: bool | None = None,
        no_video: bool | None = None,
    ) -> HostRecord:
        h = _normalise_host(host)
        if not h:
            raise ValueError("host cannot be empty")
        now = _utcnow_iso()
        existing = self.get(h)
        # recrawl_patterns / popup_policy: explicit None = preserve
        # existing. Lets a caller update cookies without wiping the
        # other host-level fields.
        if recrawl_patterns is None:
            merged_patterns = list(existing.recrawl_patterns) if existing else []
        else:
            merged_patterns = list(recrawl_patterns)
        if popup_policy is None:
            merged_popup = existing.popup_policy if existing else "kill"
        else:
            pp = (popup_policy or "").strip().lower()
            merged_popup = pp if pp in ("kill", "follow") else "kill"

        # Login-recipe fields: explicit None = preserve existing, so a
        # cookie-only re-save (the rolling-refresh path) never wipes a
        # configured login recipe. Empty string clears the field.
        # fetch_recipes merge: explicit None = preserve existing, so
        # a cookie-only re-save (the rolling-refresh path) never wipes
        # an operator's recipe set. Empty list ([]) clears.
        if fetch_recipes is None:
            merged_fetch_recipes = (
                list(existing.fetch_recipes) if existing else []
            )
        else:
            # Coerce dict members to HostRecipe so storage stays
            # uniform (HostRecord.to_json walks them with asdict).
            merged_fetch_recipes = [
                (
                    r
                    if isinstance(r, HostRecipe)
                    else HostRecipe.from_json(r)
                )
                for r in fetch_recipes
                if r
            ]

        def _keep(new, old):
            return old if new is None else new

        merged_login_url = _keep(login_url, existing.login_url if existing else None)
        merged_login_goal = _keep(login_goal, existing.login_goal if existing else None)
        merged_login_check = _keep(login_check, existing.login_check if existing else None)
        if login_refresh_ttl_s is None:
            merged_ttl = existing.login_refresh_ttl_s if existing else 900
        else:
            try:
                merged_ttl = int(login_refresh_ttl_s)
            except (TypeError, ValueError):
                merged_ttl = 900
        merged_last_login = _keep(
            last_login_at,
            existing.last_login_at if existing else None,
        )

        # Phase 2b: ownership is sticky — explicit None preserves the existing
        # owner/shared (cookie-only re-saves & auto-saves never change it), so
        # only an explicit ``/hosts/{host}`` push by a new tenant reassigns it.
        if owner_id is None:
            merged_owner = existing.owner_id if existing else "default"
        else:
            merged_owner = owner_id or "default"
        if shared is None:
            merged_shared = existing.shared if existing else True
        else:
            merged_shared = bool(shared)
        # no_video: explicit None preserves existing (cookie-only / auto-save
        # re-saves never clear the operator's "this host has no video" flag).
        if no_video is None:
            merged_no_video = existing.no_video if existing else False
        else:
            merged_no_video = bool(no_video)

        rec = HostRecord(
            host=h,
            cookies=list(cookies or []),
            notes=notes,
            recrawl_patterns=merged_patterns,
            popup_policy=merged_popup,
            created_at=(existing.created_at if existing and existing.created_at else now),
            updated_at=now,
            last_used_at=(existing.last_used_at if existing else None),
            login_url=merged_login_url,
            login_goal=merged_login_goal,
            login_check=merged_login_check,
            login_refresh_ttl_s=merged_ttl,
            last_login_at=merged_last_login,
            fetch_recipes=merged_fetch_recipes,
            owner_id=merged_owner,
            shared=merged_shared,
            no_video=merged_no_video,
        )
        self._write(rec)
        return rec

    def append_recipe(self, host: str, recipe: dict) -> HostRecord:
        """Append a single :class:`HostRecipe` to ``host``'s recipe
        list. Creates the host record if it doesn't exist yet (a
        codegen-loop investigation can target an unregistered host;
        the recipe IS the registration in that case).

        ``recipe`` is the raw dict form -- this method parses it via
        HostRecipe.from_json (so it accepts the same shape as PUT
        /hosts/{host} accepts in ``fetch_recipes``) and copies it onto
        the existing list. Existing recipes are NOT touched.

        Stamps ``created_at`` with the current UTC timestamp when the
        caller left it blank, so the disk record always has a sortable
        creation time.
        """
        h = _normalise_host(host)
        if not h:
            raise ValueError("host cannot be empty")
        if not isinstance(recipe, dict):
            raise ValueError("recipe must be a dict")
        # Stamp created_at when missing -- the UI shows recipes in
        # chronological order and a blank timestamp confuses the sort.
        if not recipe.get("created_at"):
            recipe = dict(recipe)
            recipe["created_at"] = _utcnow_iso()
        parsed = HostRecipe.from_json(recipe)
        existing = self.get(h)
        if existing is None:
            # Phase 2c: an AI-investigated host might not have any
            # cookies yet -- create a bare HostRecord so the recipe
            # has somewhere to live.
            now = _utcnow_iso()
            rec = HostRecord(
                host=h,
                cookies=[],
                created_at=now,
                updated_at=now,
                fetch_recipes=[parsed],
            )
        else:
            rec = HostRecord(
                host=existing.host,
                cookies=list(existing.cookies),
                notes=existing.notes,
                recrawl_patterns=list(existing.recrawl_patterns),
                popup_policy=existing.popup_policy,
                created_at=existing.created_at,
                updated_at=_utcnow_iso(),
                last_used_at=existing.last_used_at,
                login_url=existing.login_url,
                login_goal=existing.login_goal,
                login_check=existing.login_check,
                login_refresh_ttl_s=existing.login_refresh_ttl_s,
                last_login_at=existing.last_login_at,
                fetch_recipes=list(existing.fetch_recipes) + [parsed],
                owner_id=existing.owner_id,
                shared=existing.shared,
            )
        self._write(rec)
        return rec

    def touch_used(self, host: str) -> HostRecord | None:
        """Bump ``last_used_at`` to now. Returns the updated record,
        or None if no record exists for ``host``. Called by the hub
        when a session starts that auto-injected this host's cookies."""
        rec = self.get(host)
        if rec is None:
            return None
        rec.last_used_at = _utcnow_iso()
        self._write(rec)
        return rec

    def touch_login(self, host: str) -> HostRecord | None:
        """Bump ``last_login_at`` to now -- called after a successful
        auto re-login so the pre-fetch staleness gate knows the
        session is fresh. Returns the updated record or None."""
        rec = self.get(host)
        if rec is None:
            return None
        rec.last_login_at = _utcnow_iso()
        self._write(rec)
        return rec

    def is_login_stale(self, host: str) -> bool:
        """True when the host has a login recipe AND the last
        successful login is older than ``login_refresh_ttl_s`` (or has
        never happened). Used as the pre-fetch gate: only pay the
        re-login cost when the session is plausibly expired. Hosts
        with no recipe always return False (nothing to refresh)."""
        rec = self.get(host)
        if rec is None or not rec.has_login_recipe:
            return False
        if not rec.last_login_at:
            return True
        ttl = rec.login_refresh_ttl_s or 0
        if ttl <= 0:
            return True
        try:
            last = datetime.fromisoformat(rec.last_login_at.rstrip("Z"))
        except (ValueError, AttributeError):
            return True
        return (datetime.utcnow() - last).total_seconds() > ttl

