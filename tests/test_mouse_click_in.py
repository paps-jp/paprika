"""A real mouse click inside an element (``browser_ops.click_in``).

``el.click()`` is not a user gesture and never reaches the document inside
an ``<iframe>``; a player embedded from another site can only be started
with a click that goes through the mouse. These tests pin down how that
click is asked for -- by a session action, and by a replayed recipe step --
and that it lands where it was asked to. No browser: the tab is a fake.
"""
import asyncio

import pytest

from server.worker import browser_ops
from server.worker.browser_ops import mouse, response


class FakeTab:
    def __init__(self, answer):
        self.answer = answer
        self.scripts = []

    async def evaluate(self, js):
        self.scripts.append(js)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _log(_line):
    return None


@pytest.fixture
def clicks(monkeypatch):
    """Record what would have gone to the mouse instead of sending CDP."""
    seen = []

    async def fake_click_at(tab, x, y, log, **_kw):
        seen.append((x, y))
        return "OK"

    monkeypatch.setattr(mouse, "click_at", fake_click_at)
    return seen


def test_the_click_goes_to_the_point_the_page_reports(clicks):
    tab = FakeTab("XY:726,552")
    out = asyncio.run(mouse.click_in(tab, "#video_embed_player", _log, dy=162))
    assert out == "OK" and clicks == [(726, 552)]
    js = tab.scripts[0]
    # The middle of the box, moved 162 px down -- and never outside the box.
    assert '"#video_embed_player"' in js and "fx=0.5,fy=0.5,dx=0,dy=162;" in js
    assert "r.bottom-1" in js and "r.left+1" in js
    # The point is computed in the page from the element's own box, scrolled into view when needed.
    assert "getBoundingClientRect" in js and "scrollIntoView" in js


def test_nothing_is_clicked_when_the_element_is_missing_or_not_drawn(clicks):
    for answer in ("NO_MATCH", "NO_BOX", "ERR: boom", RuntimeError("tab closed"), None, ""):
        out = asyncio.run(mouse.click_in(FakeTab(answer), "#p", _log))
        assert out != "OK" and (out in ("NO_MATCH", "NO_BOX") or out.startswith("ERR:"))
    assert clicks == []
    assert asyncio.run(mouse.click_in(FakeTab("XY:1,1"), "", _log)) == "ERR: empty selector"
    assert asyncio.run(mouse.click_in(FakeTab("XY:a,b"), "#p", _log)).startswith("ERR: bad position")


def test_fractions_outside_the_box_are_pulled_back_and_junk_means_the_middle(clicks):
    assert mouse._fraction(7) == 1.0 and mouse._fraction(-1) == 0.0
    assert mouse._fraction(None) == 0.5 and mouse._fraction("left") == 0.5 and mouse._fraction(float("nan")) == 0.5
    assert mouse._offset("12.6") == 13 and mouse._offset(None) == 0 and mouse._offset("x") == 0
    assert mouse._offset(1e9) == 5000 and mouse._offset(float("nan")) == 0
    tab = FakeTab("XY:10,10")
    asyncio.run(mouse.click_in(tab, "#p", _log, fx=None, fy="x", dx="y", dy=None))
    assert "fx=0.5,fy=0.5,dx=0,dy=0;" in tab.scripts[0]


def test_a_session_action_asks_for_it_with_mouse_true(monkeypatch):
    calls = []

    async def fake_click_in(tab, sel, log, *, fx=None, fy=None, dx=None, dy=None):
        calls.append(("mouse", sel, fx, fy, dx, dy))
        return "OK"

    async def fake_click(tab, sel, log):
        calls.append(("js", sel))
        return "OK"

    monkeypatch.setattr(response, "click_in", fake_click_in)
    monkeypatch.setattr(response, "click", fake_click)
    run = lambda action: asyncio.run(browser_ops.execute(None, action, _log))  # noqa: E731
    assert run({"kind": "click", "selector": "#a", "mouse": True, "fx": 0.5, "fy": 0.8, "dy": 162}) == "OK"
    assert run({"kind": "click", "selector": "#a", "mouse": True}) == "OK"
    assert run({"kind": "click", "selector": "#a"}) == "OK"             # unchanged: el.click()
    assert run({"kind": "click", "selector": "#a", "mouse": False}) == "OK"
    assert calls == [("mouse", "#a", 0.5, 0.8, None, 162), ("mouse", "#a", None, None, None, None),
                     ("js", "#a"), ("js", "#a")]


def test_a_replayed_recipe_step_carries_the_mouse_click_through(monkeypatch):
    from server.worker.agent import recipe

    seen = []

    async def fake_execute(tab, action, log):
        seen.append(action)
        return "OK"

    monkeypatch.setattr(recipe.browser_ops, "execute", fake_execute)
    rep = asyncio.run(recipe._apply_fetch_recipe(None, {"pattern": "*", "actions": [
        {"kind": "click", "selector": '[data-pb-t="p1"]', "mouse": True, "dx": 0, "dy": 162},
        {"kind": "click", "selector": "#plain", "dy": 5},
    ]}, _log))
    assert rep["ok"] == 2 and rep["errors"] == []
    assert seen == [
        {"kind": "click", "selector": '[data-pb-t="p1"]', "mouse": True, "dx": 0, "dy": 162},
        {"kind": "click", "selector": "#plain"},                 # an offset without mouse means nothing
    ]


def test_the_session_api_passes_the_request_on_and_rejects_bad_fractions(monkeypatch):
    from fastapi import HTTPException

    import server.hub.app  # noqa: F401  (import first: routes.* has a cycle)
    from server.hub.routes.sessions import actions

    sent = []

    async def fake_send(session_id, action, **_kw):
        sent.append(action)
        return {"status": "OK"}

    monkeypatch.setattr(actions, "_send_session_action", fake_send)
    click = lambda body: asyncio.run(actions.session_click("ses_1", body))  # noqa: E731
    click({"selector": "#p"})
    click({"selector": "#p", "mouse": True})
    click({"selector": "#p", "mouse": True, "fx": 0.5, "fy": "0.78", "dy": 162, "page_id": "pg2"})
    click({"selector": "#p", "fx": 0.2})                                 # fx without mouse: ignored
    assert sent == [
        {"kind": "click", "selector": "#p"},
        {"kind": "click", "selector": "#p", "mouse": True},
        {"kind": "click", "selector": "#p", "mouse": True, "fx": 0.5, "fy": 0.78, "dy": 162.0, "page_id": "pg2"},
        {"kind": "click", "selector": "#p"},
    ]
    for bad in ({"selector": "#p", "mouse": True, "fx": 1.5}, {"selector": "#p", "mouse": True, "fy": "low"},
                {"selector": "#p", "mouse": True, "dx": 99999}, {"selector": "#p", "mouse": True, "dy": "nan"},
                {"mouse": True}):
        with pytest.raises(HTTPException) as e:
            click(bad)
        assert e.value.status_code == 400


class FrontTab(FakeTab):
    """Also records the CDP commands sent and what happened in which order."""

    def __init__(self, answer, *, send_error=None):
        super().__init__(answer)
        self.order = []
        self.send_error = send_error

    async def send(self, cmd):
        self.order.append("send")
        if self.send_error:
            raise self.send_error

    async def evaluate(self, js):
        self.order.append("evaluate")
        return await super().evaluate(js)


def test_the_tab_is_brought_to_the_front_before_the_click(clicks):
    # An advertisement opened by an earlier click sits in front of our tab; Chrome gives a hidden page no input.
    tab = FrontTab("XY:5,5")
    assert asyncio.run(mouse.click_in(tab, "#p", _log)) == "OK"
    assert tab.order == ["send", "evaluate"] and clicks == [(5, 5)]
    # Failing to do so is not a reason to give up on the click.
    tab = FrontTab("XY:5,5", send_error=RuntimeError("no such target"))
    assert asyncio.run(mouse.click_in(tab, "#p", _log)) == "OK"
    assert asyncio.run(mouse.bring_to_front(tab)) is False


def test_a_click_chrome_never_answers_ends_as_an_error(monkeypatch):
    async def never(*_a, **_kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(mouse, "click_at", never)
    monkeypatch.setattr(mouse, "_CLICK_TIMEOUT_S", 0.05)
    out = asyncio.run(mouse.click_in(FrontTab("XY:5,5"), "#p", _log))
    assert out.startswith("ERR: mouse click timed out")
