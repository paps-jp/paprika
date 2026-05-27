"""Action dict builders for :func:`paprika_client.run`.

Each helper returns a small dict in the same shape the worker's
``browser_ops.execute`` understands, so a recipe is just a list of
dicts that can be JSON-serialised, stored, replayed, etc.

The names mirror Playwright's where there is an equivalent. Wait is
sleep-based (not Playwright-shape wait_for_*); that primitive lands
when the hub exposes a polling endpoint (V2).
"""
from __future__ import annotations

from typing import Any


def goto(url: str) -> dict[str, Any]:
    return {"kind": "navigate", "url": url}


def navigate(url: str) -> dict[str, Any]:  # alias
    return goto(url)


def click(selector: str) -> dict[str, Any]:
    return {"kind": "click", "selector": selector}


def fill(selector: str, value: str) -> dict[str, Any]:
    # Wire field is `text` on the worker side -- browser_ops.execute
    # reads action.text and forwards to browser_ops.fill(value=).
    return {"kind": "type", "selector": selector, "text": value}


def press(key: str) -> dict[str, Any]:
    return {"kind": "press_key", "key": key}


def scroll(direction: str = "down", pixels: int = 800) -> dict[str, Any]:
    return {"kind": "scroll", "direction": direction, "amount": pixels}


def back() -> dict[str, Any]:
    return {"kind": "back"}


def wait(seconds: float = 2.0) -> dict[str, Any]:
    """Sleep on the worker. Useful between an action and the next
    observation when you don't have a wait-for-selector primitive yet."""
    return {"kind": "wait", "seconds": float(seconds)}


def capture(label: str = "capture", *, step: int = 0) -> dict[str, Any]:
    return {"kind": "capture", "label": label, "step": step}


# Inspection ops (paprika-specific; not part of execute() but allowed
# inside a recipe -- run() looks at the kind and dispatches to the
# right SDK method).
def outline() -> dict[str, Any]:
    return {"kind": "outline"}


def state() -> dict[str, Any]:
    return {"kind": "state"}


def screenshot() -> dict[str, Any]:
    return {"kind": "screenshot"}
