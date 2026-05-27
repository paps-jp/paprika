"""PaprikaFlare adapter — paprika-native Cloudflare bypass plugin.

Strategy:
    1. POST /sessions  with initial_url = the CF-protected target.
       This boots a real Chrome inside a Worker, which auto-handles
       the standard "Checking your browser" JS challenge in ~5-8 s.
    2. Wait ``wait_s`` seconds for the navigation + CF settle to finish.
    3. (Optional) call /sessions/{sid}/agent with a one-shot vision
       instruction: "if you see a Cloudflare verification button or
       checkbox, click it; otherwise return done". This handles
       Turnstile-style challenges where a checkbox needs a click.
    4. GET /sessions/{sid}/cookies?host=<host> — snapshot the cookie
       jar filtered to the target host. cf_clearance + __cf_bm + any
       site-set cookies land in here.
    5. DELETE /sessions/{sid} — release the lane.

The returned cookies are scoped to the Worker that ran the solve.
Because all paprika Workers share the same LAN NAT egress IP, those
cookies remain valid when injected into the next job dispatched to
the same Worker pool.

Reasons this beats the off-the-shelf options for paprika:
    * cloudscraper / curl-cffi: pure-Python -- can't pass modern CF.
    * FlareSolverr: separate container, ~500 MB, runs from Hub IP
                    (not Worker IP); for IP-pinned cookies this is
                    a mismatch.
    * PaprikaFlare: uses the existing Worker Chrome, IP-matched,
                    no extra infrastructure.
"""

from __future__ import annotations

import os
import time
import httpx
from urllib.parse import urlparse


_HUB_URL = os.environ.get(
    "PAPRIKA_HUB_URL_FOR_PLUGINS",
    os.environ.get("PAPRIKA_HUB_URL", "http://hub:8000"),
)


def _hub() -> httpx.Client:
    return httpx.Client(base_url=_HUB_URL, timeout=60.0)


def get_cookies(
    *,
    url: str,
    wait_s: int = 10,
    use_vision_agent: bool = True,
    use_profile: str | None = None,
) -> dict:
    """Solve CF for ``url`` via a paprika Worker session and return cookies."""
    t0 = time.time()
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    sid = ""
    worker_id = ""

    with _hub() as h:
        # 1. Boot a session on a Worker.
        create_payload = {
            "initial_url": url,
        }
        if use_profile:
            create_payload["use_profile"] = use_profile
        r = h.post("/sessions", json=create_payload)
        r.raise_for_status()
        ses = r.json()
        sid = ses.get("session_id") or ""
        worker_id = ses.get("worker_id") or ""
        if not sid:
            raise RuntimeError(f"Hub /sessions did not return session_id: {ses}")

        try:
            # 2. Let Chrome navigate + CF auto-clear.
            time.sleep(max(1, wait_s))

            # 3. Optional: try clicking a visible CF challenge widget.
            #    This is paprika's secret weapon: real Chrome alone can't
            #    *click* an "I'm not a robot" checkbox, but combined with
            #    page.agent(engine="cogagent") it can.
            if use_vision_agent:
                try:
                    h.post(
                        f"/sessions/{sid}/agent",
                        json={
                            "goal": (
                                "If you see a Cloudflare verification challenge — "
                                "a checkbox labelled 'I'm not a robot' or 'Verify you "
                                "are human', or a 'Verify' button — click it. "
                                "If the page already shows real content, return done."
                            ),
                            "max_steps": 3,
                            "engine": "auto",
                        },
                        timeout=45.0,
                    )
                    # Give CF a moment to issue cf_clearance after the click.
                    time.sleep(5)
                except Exception:
                    # Vision step is best-effort; cookies may still be set
                    # from the simple navigation alone.
                    pass

            # 4. Snapshot the cookies the browser ended up with.
            r = h.get(
                f"/sessions/{sid}/cookies",
                params={"host": host} if host else {},
            )
            r.raise_for_status()
            cookie_payload = r.json()
            cookies_list = cookie_payload.get("cookies") or []
            cookies_flat: dict[str, str] = {}
            for c in cookies_list:
                n = c.get("name")
                v = c.get("value")
                if n and v is not None:
                    cookies_flat[n] = v

            return {
                "cookies":     cookies_flat,
                "status_code": 200,
                "elapsed_ms":  int((time.time() - t0) * 1000),
                "worker_id":   worker_id,
                "session_id":  sid,
                "final_url":   cookie_payload.get("current_url") or url,
                "total_in_browser": cookie_payload.get("total_in_browser") or len(cookies_list),
            }
        finally:
            # 5. Always release the lane.
            try:
                h.delete(f"/sessions/{sid}", timeout=10.0)
            except Exception:
                pass
