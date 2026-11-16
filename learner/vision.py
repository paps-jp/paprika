"""Where on the screen to click: the one question a vision model is asked.

The Solver's text model picks elements from a list. That list stops at the
edge of an embedded frame, and many players give their buttons no name at
all -- so "click the play button" has nothing to point at. A person looks
at the screen instead. This module does that: one screenshot in, one point
out.

What the model is trusted with is narrow, because a live test showed where
it is reliable and where it is not (qwen3-vl-4b, 14 pages, 2026-11-14):

  * it finds the play button, or the small close button of an advertisement
    over the player, to within a few pixels -- also inside an embedded
    frame, where the button is not in the middle;
  * it cannot tell a playing video from a paused one (it is shown a still
    image), it picked a secondary video on one page and an advertisement on
    another, and the reasons it writes are not to be relied on.

So the answer is only ever a PROPOSAL for a point. Which element is the
player, whether the point is inside it, whether the video already plays and
whether the click did anything are all decided by the Solver in code
(``learner/solver.py``). An engine that is down is not an error either: the
Solver clicks the middle of the player, as it did before.

Points come back as ``point_2d`` in thousandths of the image (0..1000 on
both axes, whatever the image size); here they leave as fractions 0..1.
"""
from __future__ import annotations

import asyncio
import base64

from learner.llm import LLMBadOutput, chat_url, count, parse_json_object

PROMPT_VERSION = 1

PROMPT = (
    "This is a screenshot of a web page that has a video player. Goal: make the MAIN video start playing.\n"
    "Decide the single next mouse click.\n"
    "- If an advertisement, popup or overlay covers the player, choose its close (x) or skip button.\n"
    "- Otherwise choose the play button of the main player.\n"
    "- If the main video is already playing, or nothing useful can be clicked, answer none.\n"
    'Output JSON only: {"target": "close" | "play" | "none", "point_2d": [x, y], "why": "<short reason>"}'
)

TARGETS = ("close", "play")


class VisionUnavailable(Exception):
    """No engine could answer. Infra: the caller falls back to the middle."""


def parse_click(text: str) -> dict | None:
    """The model's reply -> {"target", "x", "y", "why"} with x, y in 0..1, or
    None when it proposes no click (or nothing usable). Pure."""
    try:
        obj = parse_json_object(text)
    except LLMBadOutput:
        return None
    target = str(obj.get("target") or "").strip().lower()
    if target not in TARGETS:
        return None
    pt = obj.get("point_2d")
    if not isinstance(pt, (list, tuple)) or len(pt) < 2:
        return None
    try:
        x, y = float(pt[0]), float(pt[1])
    except (TypeError, ValueError):
        return None
    if not (0.0 <= x <= 1000.0 and 0.0 <= y <= 1000.0):
        return None
    return {"target": target, "x": x / 1000.0, "y": y / 1000.0, "why": str(obj.get("why") or "")[:160]}


class Vision:
    """An OpenAI-compatible vision engine, with a spare.

    ``endpoints`` are tried in turn, starting with a different one each
    call, so two engines share the load and either may be down.
    """

    def __init__(self, *, endpoints: list[str], model: str, api_key: str = "", client=None,
                 timeout_s: float = 45.0, max_tokens: int = 200) -> None:
        self._endpoints = [e.strip() for e in endpoints if e and e.strip()]
        self.model = model
        self._api_key = api_key
        self._client = client
        self._own_client = client is None
        self._timeout = timeout_s
        self._max_tokens = int(max_tokens)
        self._next = 0
        self.calls = 0
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return bool(self._endpoints and self.model)

    async def _http(self):
        if self._client is None:
            import httpx  # lazy: tests inject a fake client

            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def next_click(self, png: bytes) -> dict | None:
        """Ask where to click on this screenshot. ``parse_click``'s result;
        raises VisionUnavailable when no engine answered."""
        if not self.enabled:
            raise VisionUnavailable("no vision engine configured")
        body = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
                {"type": "text", "text": PROMPT},
            ]}],
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        client = await self._http()
        start = self._next
        self._next = (self._next + 1) % len(self._endpoints)
        last = ""
        for i in range(len(self._endpoints)):
            endpoint = self._endpoints[(start + i) % len(self._endpoints)]
            self.calls += 1
            count(calls=1)
            try:
                resp = await client.post(chat_url(endpoint), json=body, headers=headers, timeout=self._timeout)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                continue
            if int(resp.status_code) != 200:
                last = f"HTTP {resp.status_code}"
                continue
            try:
                data = resp.json()
                count(tokens=int((data.get("usage") or {}).get("total_tokens") or 0))
                content = data["choices"][0]["message"].get("content") or ""
            except Exception as e:
                last = f"malformed completion: {type(e).__name__}"
                continue
            return parse_click(content)
        self.failures += 1
        raise VisionUnavailable(last[:160])
