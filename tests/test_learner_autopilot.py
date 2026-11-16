"""Solver, Compiler, scheduler and the unattended loop (learner/).

No browser, no model, no database: the hub, the engine and the store are
small in-memory fakes. What is being pinned down is the behaviour that
makes the loop safe to leave alone -- what counts as evidence, what is
refused, and what happens to a key after each kind of result.
"""
import asyncio
import datetime as dt
import json
import random
import re

from learner import autopilot as A
from learner import request as R
from learner import compiler as C
from learner import playbook as P
from learner import scheduler as S
from learner import solver as V
from learner.hubapi import HubError, HubUnavailable
from learner.llm import LLMBadOutput, LLMUnavailable, chat_url, count, parse_json_object

NOW = dt.datetime(2026, 11, 20, 12, 0, 0)


def _utc():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


async def _nosleep(_s):
    return None


# ---- fakes -----------------------------------------------------------------

class FakePage:
    """An age gate over a video. Element @2 dismisses it."""

    def __init__(self):
        self.gate = True
        self.url = "https://x.example/v/1/"
        self.password = False
        self.elements = {
            1: {"tag": "a", "text": "Home", "cands": ["a.nav-home"], "external": False, "editable": False},
            2: {"tag": "button", "text": "I am 18 - Enter", "cands": ["button.age-yes", "button.btn-8842117"],
                "external": False, "editable": False},
            3: {"tag": "input", "text": "", "cands": ["input[name=\"q\"]"], "external": False, "editable": True},
            4: {"tag": "a", "text": "Hot singles", "cands": ["a.ad"], "external": True, "editable": False},
            5: {"tag": "a", "text": "ログイン", "cands": ["a.login"], "external": False, "editable": False},
            6: {"tag": "a", "text": "Continue with Google", "cands": ["a.oauth"], "external": False, "editable": False},
            7: {"tag": "a", "text": "Watch on our partner", "cands": ["a.go"], "external": False, "editable": False},
            8: {"tag": "button", "text": "Members area", "cands": ["button.members"], "external": False,
                "editable": False},
        }

    def outline(self):
        return "\n".join(f'[@{i}] {e["tag"]} "{e["text"]}"' for i, e in self.elements.items())

    def probe(self):
        return {
            "url": self.url, "title": "A video",
            "videos": [] if self.gate else [{"src": "blob:x", "dur": 600, "paused": False}],
            "iframes": 0, "overlay": {"cover": 0.95 if self.gate else 0.0, "text": "Are you 18?"},
            "password_field": self.password,
        }


class FakeHub:
    def __init__(self, page=None, *, open_error=None, click_error=None):
        self.page = page or FakePage()
        self.open_error, self.click_error = open_error, click_error
        self.clicked, self.closed, self.submitted = [], [], []
        self.player_looks, self.mouse = [], []           # player lookups / real mouse clicks (dx, dy)

    async def open_session(self, url, **_kw):
        if self.open_error:
            raise self.open_error
        return "ses_1"

    async def close_session(self, sid):
        self.closed.append(sid)

    async def outline(self, sid):
        return self.page.outline()

    async def evaluate(self, sid, expr):
        m = re.search(r'data-paprika-id="(\d+)"\]\'\);', expr)
        if m and "cands" in expr:
            return self.page.elements.get(int(m.group(1)))
        if m:                                            # gone_js: did the clicked element vanish?
            return int(m.group(1)) == 2 and not self.page.gate
        if "largest('video, iframe'" in expr and "data-pb-t" in expr:
            self.player_looks.append(expr)
            player = getattr(self.page, "player", {"found": False})
            m = re.search(r"hint = \[(-?\d+), (-?\d+)\]", expr)
            if m and player.get("found"):
                # What the page does with a proposed point: is it inside the player?
                hx, hy = int(m.group(1)), int(m.group(2))
                inside = (player["x"] <= hx <= player["x"] + player["w"]
                          and player["y"] <= hy <= player["y"] + player["h"])
                return dict(player, hinted=inside,
                            dx=hx - (player["x"] + player["w"] // 2) if inside else 0,
                            dy=hy - (player["y"] + player["h"] // 2) if inside else 0)
            return player
        assert "password_field" in expr
        return self.page.probe()

    async def click(self, sid, selector, *, mouse=False, dx=None, dy=None):
        if self.click_error:
            raise self.click_error
        if "data-pb-t" in selector:                      # the video player
            self.clicked.append("player")
            self.mouse.append((dx, dy) if mouse else None)
            self.page.player_clicked()
            return {"status": "OK"}
        ref = int(re.search(r'data-paprika-id="(\d+)"', selector).group(1))
        self.clicked.append(ref)
        if ref == 2:
            self.page.gate = False
        if ref == 7:                                     # a same-origin link that redirects off-site
            self.page.url = "https://accounts.other-site.test/signin"
        if ref == 8:
            self.page.password = True
        return {}

    async def scroll(self, sid, direction="down", pixels=800):
        return {}

    async def screenshot(self, sid):
        return b"PNG fake"

    async def network(self, sid, since=0.0):
        return [e for e in getattr(self.page, "net", []) if e.get("timestamp", 0) > since]

    async def submit_fetch(self, url, *, actions, learn, download_video=True, retries=6):
        jid = f"job{len(self.submitted) + 1}"
        self.submitted.append({"job_id": jid, "url": url, "actions": actions, "learn": learn,
                               "download_video": download_video})
        return jid


class FakeLLM:
    model = "fake-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.tokens = 0
        self.prompts = []

    async def chat_json(self, system, user, **_kw):
        self.calls += 1
        self.tokens += 100
        count(calls=1, tokens=100)      # what the real client does
        self.prompts.append(user)
        r = self.replies.pop(0) if self.replies else {"action": "done"}
        if isinstance(r, Exception):
            raise r
        return r


class FakeRedis:
    def __init__(self):
        self.kv, self.lists = {}, {}

    async def set(self, k, v, ex=None):
        self.kv[k] = v

    async def get(self, k):
        return self.kv.get(k)

    async def rpush(self, k, v):
        self.lists.setdefault(k, []).append(v)

    async def lpop(self, k):
        q = self.lists.get(k) or []
        return q.pop(0) if q else None


class FakeStore:
    def __init__(self, cands=None, outcomes=None):
        self.cands = cands or []
        self._outcomes = outcomes or {}
        self.playbooks: list[P.Playbook] = []
        self.runs, self.key_updates, self.state_changes = [], [], []
        self.states, self.counts, self.recent, self.idle = {}, {}, {}, {}
        self.base = {"n_success": 0, "n_fail": 50}
        self.usage = (0, 0)
        self.streaks = {}

    async def usage_today(self):
        return self.usage

    async def candidates(self, reasons=None):
        self.asked_reasons = reasons
        return self.cands

    async def load_playbooks(self, include_retired=False):
        return [p for p in self.playbooks if include_retired or p.state != P.RETIRED]

    async def key_states(self):
        return self.states

    async def candidate_for_url(self, url, klass=""):
        self.asked_url = (url, klass)
        return _cand(klass=klass or "video_short", video_7d=0, fails_7d=0,
                     urls=[url] + [u for u in self.manual_others if u != url])

    manual_others = [f"https://x.example/v/{i}/" for i in range(1, 6)]

    async def class_stats(self):
        return {}

    async def insert_candidate(self, **kw):
        pb = P.Playbook(id=len(self.playbooks) + 1, host=kw["host"], template=kw["template"],
                        intent=kw["intent"], state=P.CANDIDATE, actions=kw["actions"],
                        holdout_pct=kw["holdout_pct"], provenance=kw["provenance"],
                        state_changed_at=NOW)
        self.playbooks.append(pb)
        return pb.id

    async def record_run(self, **kw):
        self.runs.append(kw)
        return len(self.runs)

    async def fail_streak(self, host, template, intent):
        return self.streaks.get((host, template, intent), 0)

    async def update_key_state(self, **kw):
        self.key_updates.append(kw)

    async def outcomes(self, job_ids):
        return {j: self._outcomes[j] for j in job_ids if j in self._outcomes}

    async def playbook_counts(self, ids, since_days=None):
        src = self.recent if since_days is not None else self.counts
        return {i: src[i] for i in ids if i in src}

    async def last_applied_days(self, ids):
        return {i: self.idle[i] for i in ids if i in self.idle}

    async def key_base_counts(self, host, template, intent, days=7):
        return self.base

    async def set_state(self, pb_id, state, reason, *, apply_pct=None, promoted_diff=None):
        self.state_changes.append((pb_id, state, reason, apply_pct, promoted_diff))


def _cand(**kw):
    base = dict(host="x.example", template="/v/{int}/", intent="video", klass="overlay_wall",
                fails_7d=200, scored_7d=220, success_7d=5, video_7d=8,
                urls=["https://x.example/v/1/", "https://x.example/v/2/", "https://x.example/v/3/"])
    base.update(kw)
    return S.KeyCandidate(**base)


def _pilot(store, hub=None, llm=None, **cfg):
    return A.Autopilot(store=store, hub=hub or FakeHub(), llm=llm or FakeLLM([]), redis=FakeRedis(),
                       cfg=A.AutopilotConfig(replay_poll_s=0.0, **cfg), rng=random.Random(1),
                       sleep=_nosleep)


CLICK2 = {"action": "click", "ref": 2, "why": "age gate"}
DONE = {"action": "done", "why": "video plays"}


# ---- the single JSON parser --------------------------------------------------

def test_parse_json_object_tolerates_wrapping():
    assert parse_json_object('{"action": "done"}') == {"action": "done"}
    assert parse_json_object('<think>hmm {not json}</think>\n```json\n{"action":"click","ref":3}\n```')["ref"] == 3
    assert parse_json_object('Sure! {"a": {"b": "}"}} trailing')["a"]["b"] == "}"
    for bad in ("", "no json here", "[1, 2]", '{"unterminated": '):
        try:
            parse_json_object(bad)
        except LLMBadOutput:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_chat_url_shapes():
    assert chat_url("http://h:8000") == "http://h:8000/v1/chat/completions"
    assert chat_url("http://h:8000/v1/") == "http://h:8000/v1/chat/completions"
    assert chat_url("http://h/v1/chat/completions") == "http://h/v1/chat/completions"


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._data, self.text = code, data, json.dumps(data)

    def json(self):
        return self._data


class _Http:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.sent.append({"url": url, "body": json})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _completion(content, finish="stop", tokens=50, reasoning=None):
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return _Resp(200, {"choices": [{"message": msg, "finish_reason": finish}],
                       "usage": {"total_tokens": tokens}})


def test_gateway_reads_the_answer_not_the_thinking():
    from learner.llm import LLM
    http = _Http([_completion('{"action": "done"}', reasoning="Okay, let's see... {\"action\": \"click\"}")])
    llm = LLM(client=http, endpoint="http://e:1", model="m")
    assert asyncio.run(llm.chat_json("s", "u")) == {"action": "done"}
    assert (llm.calls, llm.tokens) == (1, 50)
    body = http.sent[0]["body"]
    assert http.sent[0]["url"] == "http://e:1/v1/chat/completions"
    assert body["max_tokens"] == 1500 and "chat_template_kwargs" not in body   # engine default untouched


def test_gateway_retries_once_without_thinking_when_cut_off():
    from learner.llm import LLM
    cut = _completion("", finish="length", tokens=1700, reasoning="thinking " * 500)
    http = _Http([cut, _completion('{"action": "wait"}', tokens=900)])
    llm = LLM(client=http, endpoint="http://e:1", model="m")
    assert asyncio.run(llm.chat_json("s", "u")) == {"action": "wait"}
    assert (llm.calls, llm.tokens, llm.cutoffs) == (2, 2600, 1)
    assert http.sent[1]["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    # thinking already off and still cut off: that is the model's answer being too long
    http = _Http([cut])
    llm = LLM(client=http, endpoint="http://e:1", model="m", thinking=False)
    try:
        asyncio.run(llm.chat_json("s", "u"))
    except LLMBadOutput as e:
        assert "cut off" in str(e) and llm.calls == 1
    else:
        raise AssertionError("cut-off reply accepted")
    assert http.sent[0]["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_gateway_treats_engine_trouble_as_infrastructure():
    from learner.llm import LLM
    for reply in (ConnectionError("refused"), _Resp(503, {}), _Resp(429, {}), _Resp(400, {"error": "x"})):
        llm = LLM(client=_Http([reply]), endpoint="http://e:1", model="m")
        try:
            asyncio.run(llm.chat_json("s", "u"))
        except LLMUnavailable:
            continue
        raise AssertionError(f"{reply!r} was not treated as infra")


def test_parse_decision_allows_only_the_closed_action_set():
    assert V.parse_decision({"action": "CLICK", "ref": "@7"}) == {"action": "click", "ref": 7, "why": ""}
    assert V.parse_decision({"action": "give_up", "barrier": "nonsense"})["barrier"] == "other"
    assert V.parse_decision({"action": "give_up", "barrier": "Gone"})["barrier"] == "gone"
    assert "gone" in V.URL_BARRIERS and "gone" not in V.HARD_BARRIERS
    for bad in ({"action": "type", "text": "x"}, {"action": "click"}, {"action": "click", "ref": 0}, {}, "x"):
        try:
            V.parse_decision(bad)
        except LLMBadOutput:
            continue
        raise AssertionError(f"accepted {bad!r}")


# ---- Compiler ----------------------------------------------------------------

def test_url_specific_selectors_are_dropped():
    assert C.is_stable_selector("button.age-yes")
    assert not C.is_stable_selector("#btn-8842117")
    assert not C.is_stable_selector("a.c9f3ab77d2e1")
    assert not C.is_stable_selector('[data-paprika-id="5"]')
    assert C.stable_selectors({"cands": ["button.btn-8842117", "button.age-yes"]}) == ["button.age-yes"]
    assert C.stable_text({"text": "Enter"}) == "Enter"
    assert C.stable_text({"text": "1,284,221 views"}) == ""


def test_compile_keeps_performed_clicks_only():
    trace = [
        {"action": "click", "ref": 9, "ok": False, "refused": "external"},
        {"action": "scroll"},
        {"action": "click", "ref": 2, "ok": True, "desc": FakePage().elements[2]},
        {"action": "click", "ref": 2, "ok": True, "desc": FakePage().elements[2]},   # repeated
        {"action": "done"},
    ]
    actions, why = C.compile_trace(trace)
    assert why == "ok"
    assert [a["kind"] for a in actions] == ["wait", "evaluate", "click", "wait"]
    assert actions[2]["selector"] == '[data-pb-t="s1"]'
    assert '"button.age-yes"' in actions[1]["expression"] and "8842117" not in actions[1]["expression"]
    assert '"I am 18 - Enter"' in actions[1]["expression"]
    assert C.summarize(actions) == [{"find": ["button.age-yes"], "text": "I am 18 - Enter"},
                                    {"click": '[data-pb-t="s1"]'}]
    # only what the worker's recipe player already understands
    assert {a["kind"] for a in actions} <= {"wait", "evaluate", "click"}


def test_compile_refuses_what_cannot_be_replayed():
    assert C.compile_trace([{"action": "scroll"}, {"action": "done"}]) == (None, "no_click_in_trace")
    unstable = {"tag": "div", "text": "", "cands": ["#x-99812377"]}
    assert C.compile_trace([{"action": "click", "ok": True, "desc": unstable}]) == (None, "unstable_target")
    many = [{"action": "click", "ok": True, "desc": {"cands": [f"button.b{chr(97 + i)}"], "text": ""}}
            for i in range(C.MAX_CLICKS + 1)]
    assert C.compile_trace(many) == (None, "too_many_clicks")


# ---- Solver ------------------------------------------------------------------

def test_solver_clicks_through_an_age_gate_and_reports_a_trace():
    hub, llm = FakeHub(), FakeLLM([CLICK2, DONE])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep))
    assert res.result == V.TRACE and res.clicks == 1 and hub.clicked == [2]
    assert hub.closed == ["ses_1"]                       # the lane is always released
    assert (res.llm_calls, res.tokens) == (2, 200)
    step = res.trace[0]
    assert step["desc"]["text"] == "I am 18 - Enter" and "overlay 0.95 -> 0.00" in step["effect"]
    assert step["effect"].startswith("the clicked element disappeared")
    # the model is told what its click did
    assert "clicked @2" in llm.prompts[1] and "videos with a source 0 -> 1" in llm.prompts[1]


def test_solver_refuses_typing_targets_and_offsite_links():
    hub = FakeHub()
    llm = FakeLLM([{"action": "click", "ref": 3}, {"action": "click", "ref": 4},
                   {"action": "click", "ref": 77}, CLICK2, DONE])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep,
                              cfg=V.SolverConfig(max_idle_steps=9)))
    assert hub.clicked == [2]
    refused = [s.get("refused") for s in res.trace if s.get("refused")]
    assert refused == ["editable", "external", "no_such_element"]
    assert "REFUSED, it is an input field" in llm.prompts[1]
    assert "REFUSED, it links to another site" in llm.prompts[2]


def test_account_flows_are_refused_in_code_whatever_the_model_says():
    # Seen live 2026-11-12: told never to log in, the model clicked
    # "ログイン" and then "Continue with Google".
    hub = FakeHub()
    llm = FakeLLM([{"action": "click", "ref": 5}, {"action": "click", "ref": 6}, CLICK2, DONE])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep))
    assert hub.clicked == [2]                            # neither login click reached the page
    assert [s.get("refused") for s in res.trace if s.get("refused")] == ["forbidden_label"] * 2
    assert "is not allowed" in llm.prompts[1]
    for label in ("Log in", "LOGIN", "Sign-in", "sign up free", "Register", "Subscribe now", "Buy tokens",
                  "Get Premium", "Continue with Apple", "会員登録はこちら", "新規登録", "購入する",
                  "パスワードを忘れた", "アプリをダウンロード", "登录", "Install"):
        assert V.FORBIDDEN_LABEL_RE.search(label), label
    for label in ("I am 18 - Enter", "はい", "入場する", "Accept all", "すべて許可", "Close", "閉じる",
                  "Play", "Skip ad", "×", "同意する", "Yes, I am over 18", "Free preview"):
        assert not V.FORBIDDEN_LABEL_RE.search(label), label


def test_leaving_the_site_ends_the_run_and_discards_the_trace():
    hub = FakeHub()
    llm = FakeLLM([CLICK2, {"action": "click", "ref": 7}, DONE])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep))
    assert (res.result, res.barrier) == (V.GAVE_UP, "other") and "accounts.other-site.test" in res.reason
    assert res.trace[-1] == {"action": "aborted", "why": "left_site"}
    assert llm.calls == 2 and hub.closed == ["ses_1"]
    # a subdomain of the same site is not "leaving"
    assert V.site_of("https://www.x.example/a") == V.site_of("https://cdn.x.example/b") == "x.example"
    assert V.site_of("https://a.b.example.co.jp/") == "example.co.jp"
    assert V.site_of("https://accounts.google.com/") != V.site_of("https://www.themoviedb.org/")


def test_a_login_form_appearing_ends_the_run():
    hub = FakeHub()
    res = asyncio.run(V.solve(hub, FakeLLM([{"action": "click", "ref": 8}, DONE]), "u", sleep=_nosleep))
    assert (res.result, res.barrier, res.reason) == (V.GAVE_UP, "login", "a click opened a login form")


def test_an_aborted_run_is_never_compiled_or_replayed():
    store = FakeStore(cands=[_cand()])
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([CLICK2, {"action": "click", "ref": 7}]))
    assert asyncio.run(ap.solver_tick()) == V.GAVE_UP
    assert hub.submitted == [] and store.playbooks == []
    assert store.key_updates[0]["solved"] is False


def test_solver_give_up_names_the_barrier():
    llm = FakeLLM([{"action": "give_up", "barrier": "login", "why": "needs an account"}])
    res = asyncio.run(V.solve(FakeHub(), llm, "https://x.example/v/1/", sleep=_nosleep))
    assert (res.result, res.barrier) == (V.GAVE_UP, "login")


def test_a_page_that_redirected_elsewhere_on_load_is_not_judged_as_the_key():
    page = FakePage()
    page.url = "https://ads.other-site.test/landing"    # an advertising redirect before any click
    res = asyncio.run(V.solve(FakeHub(page), FakeLLM([{"action": "give_up", "barrier": "no_video", "why": "An ad page."}]),
                              "https://x.example/v/1/", sleep=_nosleep))
    assert (res.result, res.barrier) == (V.GAVE_UP, "redirected") and "other-site.test" in res.reason
    assert "redirected" in V.URL_BARRIERS                # the autopilot moves on to another URL
    # A login wall on the OTHER site is not the key's login wall either.
    res = asyncio.run(V.solve(FakeHub(page), FakeLLM([{"action": "give_up", "barrier": "login"}]),
                              "https://x.example/v/1/", sleep=_nosleep))
    assert res.barrier == "redirected"


class AdPage(FakePage):
    """A player that shows a 30-second advertisement before the main video,
    and may start only after several clicks (the first opens an advertisement)."""

    def __init__(self, *, needs_clicks=0):
        super().__init__()
        self.gate = False
        self.waited = 0.0
        self.needs_clicks, self.clicks = needs_clicks, 0
        self.player = {"found": True, "external": False, "kind": "video", "pick": "media", "src": "",
                       "playing": False, "tag": "div", "text": "", "x": 100, "y": 50, "w": 800, "h": 450,
                       "vw": 1000, "vh": 1000}

    def player_clicked(self):
        self.clicks += 1

    def started(self):
        return self.clicks >= self.needs_clicks

    def probe(self):
        main = self.started() and self.waited >= 30
        vid = {"src": "https://cdn.x.example/main.mp4" if main else "https://ads.example/creative.mp4",
               "dur": 1800 if main else 30, "paused": not self.started()}
        return {"url": self.url, "title": "A video", "videos": [vid], "iframes": 0,
                "overlay": {"cover": 0.0, "text": ""}, "password_field": False}


def _sleep_on(page):
    async def sleep(seconds):
        if page.started():
            page.waited += seconds
    return sleep


WAIT40 = {"action": "wait", "seconds": 40, "why": "the advertisement"}
PLAYER = {"action": "click_player", "why": "start the video"}


def test_waiting_out_an_advertisement_is_a_trace_and_is_replayed_as_a_wait():
    page = AdPage()
    res = asyncio.run(V.solve(FakeHub(page), FakeLLM([WAIT40, DONE]), "https://x.example/v/1/", sleep=_sleep_on(page)))
    assert res.result == V.TRACE and res.clicks == 0 and res.replayable == 1
    assert res.trace[0]["seconds"] == 40 and "longest video 30s -> 1800s" in res.trace[0]["effect"]
    actions, why = C.compile_trace(res.trace)
    assert why == "ok" and [a["kind"] for a in actions] == ["wait", "wait"] and actions[1]["seconds"] == 40
    assert C.summarize(actions) == [{"wait_s": 40}]


def test_a_wait_that_changed_nothing_or_a_short_pause_is_not_replayed():
    assert C.compile_trace([{"action": "wait", "seconds": 40, "ok": False}])[0] is None
    assert C.compile_trace([{"action": "wait", "seconds": 2, "ok": True}])[0] is None
    assert V.parse_decision({"action": "wait"})["seconds"] == 2
    assert V.parse_decision({"action": "wait", "seconds": 900})["seconds"] == 60       # clamped
    assert V.parse_decision({"action": "wait", "seconds": "x"})["seconds"] == 2


def test_waiting_is_bounded_per_run():
    page = AdPage(needs_clicks=9)                         # never starts: waiting is useless
    cfg = V.SolverConfig(max_wait_total_s=70)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    res = asyncio.run(V.solve(FakeHub(page), FakeLLM([WAIT40] * 9), "https://x.example/v/1/", cfg=cfg, sleep=sleep))
    assert res.result == V.EXHAUSTED and sum(x for x in slept if x >= 5) <= 70


def test_the_player_may_be_clicked_again_when_the_first_click_only_opened_an_advert():
    page = AdPage(needs_clicks=2)
    hub = FakeHub(page)
    res = asyncio.run(V.solve(hub, FakeLLM([PLAYER, PLAYER, WAIT40, DONE]), "https://x.example/v/1/",
                              sleep=_sleep_on(page)))
    assert hub.clicked == ["player", "player"] and res.result == V.TRACE
    assert [s.get("ok") for s in res.trace[:3]] == [False, True, True]     # 1st click: nothing visible
    actions, _ = C.compile_trace(res.trace)
    # Both player clicks are replayed (the first may be the necessary one), then the wait.
    assert [a["kind"] for a in actions] == ["wait", "evaluate", "click", "wait", "evaluate", "click", "wait", "wait"]
    assert actions[2]["selector"] == '[data-pb-t="p1"]' and actions[5]["selector"] == '[data-pb-t="p2"]'
    # The player is clicked with a real mouse click, in its middle when nothing proposes a spot.
    assert hub.mouse == [(0, 0), (0, 0)]
    assert actions[2]["mouse"] is True and (actions[2]["dx"], actions[2]["dy"]) == (0, 0)
    assert C.summarize(actions) == [
        {"find": "the video player"}, {"mouse_click": '[data-pb-t="p1"]', "from_middle": [0, 0]},
        {"find": "the video player"}, {"mouse_click": '[data-pb-t="p2"]', "from_middle": [0, 0]},
        {"wait_s": 40}]


def test_player_clicks_that_led_nowhere_are_not_replayed_and_are_limited():
    page = AdPage(needs_clicks=99)
    hub = FakeHub(page)
    res = asyncio.run(V.solve(hub, FakeLLM([PLAYER] * 9), "https://x.example/v/1/", sleep=_nosleep))
    assert res.result == V.EXHAUSTED and len(hub.clicked) <= C.MAX_PLAYER_CLICKS
    assert C.compile_trace(res.trace)[0] is None


def test_a_player_covered_by_a_link_to_another_site_is_not_clicked():
    page = AdPage()
    page.player = dict(page.player, external=True, tag="a", text="Visit sponsor")
    hub = FakeHub(page)
    res = asyncio.run(V.solve(hub, FakeLLM([PLAYER, DONE]), "https://x.example/v/1/", sleep=_nosleep))
    assert hub.clicked == [] and res.trace[0]["refused"] == "covered"
    page.player = {"found": False}
    res = asyncio.run(V.solve(hub, FakeLLM([PLAYER, DONE]), "https://x.example/v/1/", sleep=_nosleep))
    assert hub.clicked == [] and res.trace[0]["refused"] == "no_player"


# ---- the player: a real mouse click, at a spot chosen on a screenshot -------

class FakeVision:
    """Answers with a point in fractions of the screenshot, an exception, or None."""
    enabled = True
    model = "fake-vision"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = 0

    async def next_click(self, png):
        self.asked += 1
        a = self.answers.pop(0) if self.answers else None
        if isinstance(a, Exception):
            raise a
        return a


class FramePage(AdPage):
    """A player inside an embedded frame of another site: the page itself has
    no <video>, so the only sign that a click started it is what the frame
    requests."""

    def __init__(self, *, needs_clicks=1):
        super().__init__(needs_clicks=needs_clicks)
        self.player = dict(self.player, kind="iframe", src="https://embed.cdn.test/?id=1", tag="iframe")
        self.net = [
            # An advertising banner elsewhere on the page plays videos all the time.
            {"url": "https://ads.test/banner.mp4", "mime": "video/mp4", "document_url": "https://ads.test/f",
             "timestamp": 10.0},
            {"url": "https://embed.cdn.test/poster.jpg", "mime": "image/jpeg",
             "document_url": "https://embed.cdn.test/?id=1", "timestamp": 11.0},
        ]

    def player_clicked(self):
        super().player_clicked()
        if self.started():
            t = 100.0 + self.clicks
            self.net += [
                {"url": "https://v.cdn.test/main.m3u8", "mime": "application/vnd.apple.mpegurl",
                 "document_url": "https://embed.cdn.test/?id=1", "timestamp": t},
                {"url": "https://v.cdn.test/seg1.ts", "mime": "video/mp2t",
                 "document_url": "https://embed.cdn.test/?id=1", "timestamp": t + 0.1},
            ]

    def probe(self):
        return {"url": self.url, "title": "A video", "videos": [], "iframes": 1,
                "overlay": {"cover": 0.0, "text": ""}, "password_field": False}


def test_the_spot_on_the_player_comes_from_the_screenshot_and_is_replayed():
    page = FramePage()
    hub = FakeHub(page)
    # The play button is below the middle of the frame: player box x 100..900, y 50..500 (middle 500, 275)
    # on a 1000x1000 screen, the proposed point at 500, 400.
    vision = FakeVision({"target": "play", "x": 0.5, "y": 0.4, "why": "the play button"})
    llm = FakeLLM([PLAYER, WAIT40, DONE])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep, vision=vision))
    assert res.result == V.TRACE and vision.asked == 1
    assert hub.mouse == [(0, 125)]                        # pixels from the middle of the player
    step = res.trace[0]
    assert step["ok"] and step["spot"] == {"dx": 0, "dy": 125, "by": "vision", "target": "play", "pick": "media"}
    # The page shows nothing; the frame's own requests do. The banner's video is not counted.
    assert step["effect"] == "the player requested media 0 -> 2 files"
    assert "its play button" in llm.prompts[1] and "MEDIA REQUESTS: 2 " in llm.prompts[1]
    # The player was looked up again for that spot: what lies THERE must not be a foreign link.
    assert len(hub.player_looks) == 2 and "hint = [500, 400]" in hub.player_looks[1]
    actions, why = C.compile_trace(res.trace)
    assert why == "ok" and actions[2] == {"kind": "click", "selector": '[data-pb-t="p1"]', "mouse": True,
                                           "dx": 0, "dy": 125}
    assert "dy = 125" in actions[1]["expression"] and 'want = "media"' in actions[1]["expression"]
    assert C.summarize(actions) == [{"find": "the video player"},
                                    {"mouse_click": '[data-pb-t="p1"]', "from_middle": [0, 125]}]


class PosterPage(AdPage):
    """A poster in a box; the first click replaces it with a frame of the same
    site, and the video is played by ANOTHER site's frame nested in that one.
    A second click on the player opens an advertisement and ends the run."""

    def __init__(self):
        super().__init__(needs_clicks=1)
        self.player = dict(self.player, kind="box", pick="box", tag="i")
        self.net = [{"url": "https://ads.test/banner.mp4", "mime": "video/mp4",
                     "document_url": "https://ads.test/f", "timestamp": 10.0}]

    def player_clicked(self):
        super().player_clicked()
        if self.clicks == 1:
            self.player = dict(self.player, kind="iframe", pick="media", tag="iframe",
                               src="https://x.example/iframe/abc")
            self.net += [{"url": "https://cdn.far.test/v/master.m3u8", "mime": "application/vnd.apple.mpegurl",
                          "document_url": "https://player.other.test/e/1", "timestamp": 100.0},
                         {"url": "https://cdn.far.test/v/720.m3u8", "mime": "",
                          "document_url": "https://player.other.test/e/1", "timestamp": 100.2}]
        else:
            self.url = "https://ads.test/landing"

    def probe(self):
        return {"url": self.url, "title": "A video", "videos": [], "iframes": 1 if self.clicks else 0,
                "overlay": {"cover": 0.0, "text": ""}, "password_field": False}


def test_a_stream_started_by_a_frame_inside_the_frame_counts_as_the_player_starting():
    page = PosterPage()
    hub = FakeHub(page)
    llm = FakeLLM([PLAYER, DONE])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep))
    # One click was enough, and the run can tell: the model is not left to click again.
    assert res.result == V.TRACE and hub.clicked == ["player"]
    assert res.trace[0]["ok"] and res.trace[0]["effect"] == "the player requested media 0 -> 2 files"
    assert "MEDIA REQUESTS: 0 " in llm.prompts[0] and "MEDIA REQUESTS: 2 " in llm.prompts[1]
    actions, why = C.compile_trace(res.trace)
    assert why == "ok" and [a["kind"] for a in actions] == ["wait", "evaluate", "click", "wait"]
    assert 'want = "box"' in actions[1]["expression"]     # replayed on the poster box, as it was found


def test_a_model_that_gives_up_after_the_stream_started_is_overruled():
    # 2026-11-14: the click started the stream, the model waited, saw "nothing
    # changed" in the page (the video is in another site's frame) and gave up.
    page = PosterPage()
    hub = FakeHub(page)
    wait = {"action": "wait", "seconds": 10, "why": "let it load"}
    quit_ = {"action": "give_up", "barrier": "other", "why": "the video did not start playing"}
    llm = FakeLLM([PLAYER, wait, quit_])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep))
    assert res.result == V.TRACE and hub.clicked == ["player"]
    assert res.trace[-1]["action"] == "done" and res.trace[-1]["by"] == "stream_started"
    assert res.trace[-1]["model"]["barrier"] == "other"   # what the model said is kept for the record
    assert "normal for a player inside a frame" in llm.prompts[2]
    actions, why = C.compile_trace(res.trace)
    assert why == "ok" and "click" in [a["kind"] for a in actions]


class SlowPosterPage(PosterPage):
    """The poster goes away at once; the nested frame asks for the stream only
    some seconds later."""

    def __init__(self, late_looks=3):
        super().__init__()
        self.late_looks = late_looks
        self.looks_after_click = 0
        self.late = []

    def player_clicked(self):
        n = len(self.net)
        super().player_clicked()
        if self.clicks == 1:
            self.late, self.net = self.net[n:], self.net[:n]

    def probe(self):
        if self.clicks:
            self.looks_after_click += 1
            if self.looks_after_click == self.late_looks:
                self.net += self.late
        p = super().probe()
        p["overlay"] = {"cover": 0.0 if self.clicks else 0.19, "text": ""}
        return p


def test_a_click_is_watched_until_the_stream_shows_not_only_until_the_page_changes():
    # 2026-11-14: the first look saw only the poster going away, the model
    # clicked the player again and that click opened an advertisement.
    page = SlowPosterPage()
    hub = FakeHub(page)
    llm = FakeLLM([PLAYER, DONE])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep))
    assert res.result == V.TRACE and hub.clicked == ["player"]
    assert res.trace[0]["effect"] == "overlay 0.19 -> 0.00; the player requested media 0 -> 2 files"
    # It is not watched for ever: a stream that never shows leaves the page change as the effect.
    page = SlowPosterPage(late_looks=99)
    res = asyncio.run(V.solve(FakeHub(page), FakeLLM([PLAYER, DONE]), "https://x.example/v/1/", sleep=_nosleep))
    assert res.trace[0]["effect"] == "overlay 0.19 -> 0.00" and page.looks_after_click == 1 + V.START_LOOKS


def test_a_give_up_stands_when_no_stream_started_or_the_page_itself_is_the_barrier():
    # No click made the player request anything: the model's give_up is the result.
    hub = FakeHub(PosterPage())
    llm = FakeLLM([{"action": "give_up", "barrier": "other", "why": "nothing to click"}])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep))
    assert res.result == V.GAVE_UP and res.barrier == "other"
    # A login wall is a fact about the page, whatever the player requested.
    hub = FakeHub(PosterPage())
    llm = FakeLLM([PLAYER, {"action": "give_up", "barrier": "login", "why": "members only"}])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep))
    assert res.result == V.GAVE_UP and res.barrier == "login"


def test_what_counts_as_the_player_asking_for_a_video():
    pm = V.PlayerMedia()
    entries = [
        {"url": "https://a.test/x.m3u8", "mime": "", "document_url": "https://nested.test/p", "timestamp": 1.0},
        {"url": "https://a.test/seg-1.ts?t=1", "mime": "video/mp2t", "document_url": "", "timestamp": 2.0},
        {"url": "https://a.test/init.mpd", "mime": "application/dash+xml", "document_url": "https://x.test/", "timestamp": 3.0},
        {"url": "https://ads.test/banner.mp4", "mime": "video/mp4", "document_url": "https://ads.test/f", "timestamp": 4.0},
        {"url": "https://embed.test/file.mp4", "mime": "video/mp4", "document_url": "https://embed.test/e", "timestamp": 5.0},
        {"url": "https://a.test/poster.jpg", "mime": "image/jpeg", "document_url": "https://nested.test/p", "timestamp": 6.0},
    ]

    class Hub:
        async def network(self, sid, since=0.0):
            return [e for e in entries if e["timestamp"] > since]

    assert asyncio.run(pm.read(Hub(), "s")) == 3          # the three stream requests, whoever made them
    assert asyncio.run(pm.read(Hub(), "s")) == 3          # nothing new: nothing counted twice
    # Once the player frame is known, the plain video file IT asked for counts too -- the banner's never does.
    assert pm.watch_frame("embed.test") and not pm.watch_frame("embed.test") and not pm.watch_frame("")
    assert asyncio.run(pm.read(Hub(), "s")) == 4

    class Down:
        async def network(self, sid, since=0.0):
            raise HubUnavailable("504")

    assert asyncio.run(pm.read(Down(), "s")) == 4          # an extra signal: its failure is not the run's


def test_a_spot_outside_the_player_or_no_answer_falls_back_to_the_middle():
    from learner.vision import VisionUnavailable
    for answer, note in (
        ({"target": "play", "x": 0.5, "y": 0.9, "why": "a small video below"}, "vision pointed outside the player"),
        (None, "vision proposed no click"),
        (VisionUnavailable("both engines down"), "vision unavailable: both engines down"),
    ):
        page = FramePage()
        hub = FakeHub(page)
        res = asyncio.run(V.solve(hub, FakeLLM([PLAYER, DONE]), "https://x.example/v/1/", sleep=_nosleep,
                                  vision=FakeVision(answer)))
        assert hub.mouse == [(0, 0)] and res.trace[0]["spot"]["by"] == "centre"
        assert res.trace[0]["spot"]["note"] == note
        # Only a proposed point is put to the page a second time.
        assert len(hub.player_looks) == (2 if isinstance(answer, dict) else 1)


def test_a_spot_covered_by_a_link_to_another_site_is_not_clicked():
    page = FramePage()
    hub = FakeHub(page)

    async def evaluate(sid, expr, _orig=hub.evaluate):
        out = await _orig(sid, expr)
        if "data-pb-t" in expr and "hint = [500, 400]" in expr:   # the look-up for the proposed spot
            return dict(out, external=True, tag="a", text="")
        return out

    hub.evaluate = evaluate
    vision = FakeVision({"target": "play", "x": 0.5, "y": 0.4, "why": ""})
    res = asyncio.run(V.solve(hub, FakeLLM([PLAYER, DONE]), "https://x.example/v/1/", sleep=_nosleep, vision=vision))
    assert hub.clicked == [] and res.trace[0]["refused"] == "covered"


def test_a_playing_video_is_not_clicked_whatever_the_models_say():
    page = AdPage()
    page.player = dict(page.player, playing=True)
    hub = FakeHub(page)
    vision = FakeVision({"target": "play", "x": 0.5, "y": 0.3, "why": "a play icon in the control bar"})
    res = asyncio.run(V.solve(hub, FakeLLM([PLAYER, DONE]), "https://x.example/v/1/", sleep=_nosleep, vision=vision))
    assert hub.clicked == [] and vision.asked == 0 and res.trace[0]["refused"] == "playing"


def test_a_click_in_a_frame_that_started_nothing_is_not_replayed():
    page = FramePage(needs_clicks=99)                     # the frame never requests any media
    hub = FakeHub(page)
    res = asyncio.run(V.solve(hub, FakeLLM([PLAYER, PLAYER, DONE]), "https://x.example/v/1/", sleep=_nosleep))
    assert hub.clicked == ["player", "player"] and [s["ok"] for s in res.trace[:2]] == [False, False]
    assert C.compile_trace(res.trace)[0] is None


def test_vision_replies_are_only_ever_a_point_or_nothing():
    from learner import vision as VI
    ok = VI.parse_click('```json\n{"target": "play", "point_2d": [380, 559], "why": "centre"}\n```')
    assert ok == {"target": "play", "x": 0.38, "y": 0.559, "why": "centre"}
    assert VI.parse_click('{"target": "close", "point_2d": [555, 360]}')["target"] == "close"
    for bad in ('{"target": "none", "point_2d": [1, 1]}', '{"target": "play"}', '{"target": "play", "point_2d": [1]}',
                '{"target": "play", "point_2d": [1200, 10]}', '{"target": "gate", "point_2d": [10, 10]}',
                '{"target": "play", "point_2d": ["a", "b"]}', "I cannot see a player.", ""):
        assert VI.parse_click(bad) is None, bad


def test_vision_uses_the_other_engine_when_one_is_down():
    from learner import vision as VI

    class Resp:
        def __init__(self, code, content=""):
            self.status_code = code
            self._content = content

        def json(self):
            return {"choices": [{"message": {"content": self._content}}], "usage": {"total_tokens": 2500}}

    class Http:
        def __init__(self):
            self.urls = []

        async def post(self, url, json=None, headers=None, timeout=None):
            self.urls.append(url)
            assert json["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
            assert "response_format" not in json          # JSON mode has crashed this engine before
            if "a.test" in url:
                return Resp(503)
            return Resp(200, '{"target": "play", "point_2d": [500, 500], "why": ""}')

    http = Http()
    v = VI.Vision(endpoints=["http://a.test:1", " http://b.test:2/v1 ", ""], model="m", client=http)
    got = asyncio.run(v.next_click(b"png"))
    assert got["x"] == 0.5 and http.urls == ["http://a.test:1/v1/chat/completions", "http://b.test:2/v1/chat/completions"]

    async def metered():
        m = V.start_meter()
        await v.next_click(b"png")                        # starts with the second engine this time
        return m

    m = asyncio.run(metered())
    assert m.calls == 1 and m.tokens == 2500 and len(http.urls) == 3

    dead = VI.Vision(endpoints=["http://a.test:1"], model="m", client=http)
    try:
        asyncio.run(dead.next_click(b"png"))
        raise AssertionError("expected VisionUnavailable")
    except VI.VisionUnavailable as e:
        assert "503" in str(e)
    assert not VI.Vision(endpoints=[""], model="m").enabled and not VI.Vision(endpoints=["x"], model="").enabled


def test_hand_written_steps_may_use_the_mouse_click():
    ok = [{"kind": "click", "selector": "#player", "mouse": True, "dx": 0, "dy": 160}]
    assert C.check_manual_actions(ok) == ""
    assert C.check_manual_actions([dict(ok[0], fx=0.5, fy=0.8)]) == ""
    assert "dy must be" in C.check_manual_actions([dict(ok[0], dy=9000)])
    assert "fx must be" in C.check_manual_actions([dict(ok[0], fx="left")])
    # A trace from before the spot was recorded still compiles: the middle.
    old = [{"action": "click_player", "ok": True, "performed": True}]
    assert C.compile_trace(old)[0][2] == {"kind": "click", "selector": '[data-pb-t="p1"]', "mouse": True,
                                          "dx": 0, "dy": 0}


def test_solver_without_a_click_is_not_a_trace():
    res = asyncio.run(V.solve(FakeHub(), FakeLLM([DONE]), "u", sleep=_nosleep))
    assert res.result == V.NO_ACTION
    waits = [{"action": "wait"}] * 50
    res = asyncio.run(V.solve(FakeHub(), FakeLLM(waits), "u", sleep=_nosleep,
                              cfg=V.SolverConfig(max_steps=4, max_idle_steps=99)))
    assert res.result == V.EXHAUSTED and res.llm_calls == 4


def test_a_stuck_model_is_cut_off_early():
    # waiting on a page where nothing changes: three idle steps, not twelve
    res = asyncio.run(V.solve(FakeHub(), FakeLLM([{"action": "wait"}] * 50), "u", sleep=_nosleep))
    assert res.result == V.EXHAUSTED and res.llm_calls == 3
    assert res.trace[-1] == {"action": "stopped", "why": "no_progress"}


def test_a_click_that_changes_nothing_is_not_repeated_or_replayed():
    hub = FakeHub()
    home = {"action": "click", "ref": 1}                 # the nav link: no visible effect
    llm = FakeLLM([home, home, CLICK2, DONE])
    res = asyncio.run(V.solve(hub, llm, "https://x.example/v/1/", sleep=_nosleep))
    assert hub.clicked == [1, 2]                         # the second "home" never reached the page
    assert "REFUSED, you already clicked it and nothing changed" in llm.prompts[2]
    assert res.result == V.TRACE and res.clicks == 1
    actions, why = C.compile_trace(res.trace)
    assert why == "ok" and "nav-home" not in json.dumps(actions)   # only the click that did something


def test_infrastructure_trouble_is_not_a_solver_failure():
    res = asyncio.run(V.solve(FakeHub(open_error=HubUnavailable("no lane")), FakeLLM([]), "u", sleep=_nosleep))
    assert res.result == V.INFRA
    hub = FakeHub()
    res = asyncio.run(V.solve(hub, FakeLLM([LLMUnavailable("thermal")]), "u", sleep=_nosleep))
    assert res.result == V.INFRA and hub.closed == ["ses_1"]
    hub = FakeHub(click_error=HubUnavailable("worker gone"))
    res = asyncio.run(V.solve(hub, FakeLLM([CLICK2]), "u", sleep=_nosleep))
    assert res.result == V.INFRA and hub.closed == ["ses_1"]


def test_repeated_garbage_from_the_model_ends_the_run():
    bad = [LLMBadOutput("x")] * 5
    res = asyncio.run(V.solve(FakeHub(), FakeLLM(bad), "u", sleep=_nosleep))
    assert res.result == V.BAD_OUTPUT and res.llm_calls == 3
    # one slip is tolerated
    res = asyncio.run(V.solve(FakeHub(), FakeLLM([LLMBadOutput("x"), CLICK2, DONE]), "u", sleep=_nosleep))
    assert res.result == V.TRACE


# ---- scheduler ---------------------------------------------------------------

def test_rank_prefers_traffic_and_skips_ineligible_keys():
    big, small = _cand(host="big.example", fails_7d=900), _cand(host="small.example", fails_7d=30)
    cands = [
        small, big,
        _cand(host="busy.example", fails_7d=5000),
        _cand(host="dormant.example", fails_7d=5000),
        _cand(host="justran.example", fails_7d=5000),
        _cand(host="tiny.example", fails_7d=2),
        _cand(host="oneurl.example", fails_7d=5000, urls=["https://oneurl.example/v/1/"] * 5),
        _cand(host="page.example", fails_7d=5000, intent="page"),
    ]
    states = {
        ("dormant.example", "/v/{int}/", "video"): S.KeyState(runs=2, fail_streak=2,
                                                              dormant_until=NOW + dt.timedelta(days=2)),
        ("justran.example", "/v/{int}/", "video"): S.KeyState(runs=1, last_run_at=NOW - dt.timedelta(hours=3)),
    }
    busy = {("busy.example", "/v/{int}/", "video")}
    wins = 0
    for seed in range(40):
        ranked = S.rank(cands, states, {}, busy, now=NOW, rng=random.Random(seed))
        assert {c.host for _v, c in ranked} == {"big.example", "small.example"}
        wins += ranked[0][1].host == "big.example"
    assert wins >= 36          # 30x the failures: almost always first, never excluded outright


def test_a_key_that_never_produced_a_video_is_not_worked_on():
    # A shop item page a client asks a video of: thousands of "wall" failures,
    # never one playable video. However much traffic, no click makes it a video page.
    shop = _cand(host="shop.example", template="/item/{id}/", fails_7d=5000, video_7d=0)
    once = _cand(host="tube.example", fails_7d=40, video_7d=1)      # one short clip counts
    assert S.eligible(shop, None, set(), NOW, S.SchedulerConfig()) == (False, "no_video_seen")
    assert S.eligible(once, None, set(), NOW, S.SchedulerConfig()) == (True, "")
    ranked = S.rank([shop, once], {}, {}, set(), now=NOW, rng=random.Random(1))
    assert [c.host for _v, c in ranked] == ["tube.example"]
    # 0 turns the check off (the behaviour before it existed).
    off = S.SchedulerConfig(min_video_7d=0)
    assert S.eligible(shop, None, set(), NOW, off) == (True, "")


def test_keys_that_keep_failing_lose_priority():
    fresh = _cand(host="fresh.example", fails_7d=100)
    worn = _cand(host="worn.example", fails_7d=100)
    states = {worn.key: S.KeyState(runs=12, solved=0)}
    wins = sum(S.rank([fresh, worn], states, {}, set(), now=NOW, rng=random.Random(s))[0][1].host
               == "fresh.example" for s in range(60))
    assert wins >= 48


def test_class_prior_follows_measured_solver_results():
    cfg = S.SchedulerConfig()
    assert S.class_prior({}, "overlay_wall", cfg) == cfg.default_prior
    assert S.class_prior({"overlay_wall": (3, 5)}, "overlay_wall", cfg) == cfg.default_prior   # too few runs
    a, b = S.class_prior({"overlay_wall": (30, 40)}, "overlay_wall", cfg)
    assert a > b
    a, b = S.class_prior({"video_not_captured": (1, 40)}, "video_not_captured", cfg)
    assert a < b


def test_dormancy_grows_and_hard_barriers_wait_a_month():
    assert [S.dormancy(n).total_seconds() / 86400 for n in (1, 2, 3, 4, 9)] == [0.25, 1, 3, 7, 7]
    assert S.dormancy(1, "login").days == 30 and S.dormancy(1, "captcha").days == 30
    assert S.dormancy(1, "no_video") == S.dormancy(1)   # one sighting: maybe just that URL
    assert S.dormancy(1, "not_video").days == 30        # seen on two URLs: a verdict on the key
    assert S.dormancy(1, "gone") == S.dormancy(1)


def test_budget_and_url_picking():
    cfg = S.SchedulerConfig(daily_runs=3, daily_tokens=1000)
    assert S.budget_left(2, 999, cfg) and not S.budget_left(3, 0, cfg) and not S.budget_left(0, 1000, cfg)
    assert S.pick_urls(["a", "a", "", None, "b", "c", "d"]) == ["a", "b", "c"]


# ---- replay judgement ----------------------------------------------------------

def _rep(first, *others):
    out = {"u0": {"outcome": first, "reason": "r"}}
    for i, o in enumerate(others, 1):
        out[f"u{i}"] = {"outcome": o, "reason": "r"}
    return out


def test_a_playbook_must_reproduce_on_other_urls():
    assert A.judge_replay(_rep("success", "success", "success"), "u0", 2)[0] == A.SOLVED
    assert A.judge_replay(_rep("success", "success", "fail"), "u0", 2)[0] == A.GENERALIZE_FAIL
    assert A.judge_replay(_rep("fail", "success", "success"), "u0", 2)[0] == A.REPLAY_FAIL
    assert A.judge_replay(_rep("partial", "success", "success"), "u0", 2)[0] == A.REPLAY_FAIL
    # an unsettled / infrastructure outcome is evidence of nothing
    assert A.judge_replay(_rep("success", "success", None), "u0", 2)[0] == V.INFRA
    assert A.judge_replay(_rep("infra", "success", "success"), "u0", 2)[0] == V.INFRA
    assert A.judge_replay(_rep(None, None, None), "u0", 2)[0] == V.INFRA


# ---- the loop: explore -----------------------------------------------------------

def test_explore_creates_a_candidate_only_after_the_replays_succeed():
    store = FakeStore(cands=[_cand()], outcomes={
        "job1": ("success", "video_ok"), "job2": ("success", "video_ok"), "job3": ("success", "video_ok")})
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([CLICK2, DONE]))
    assert asyncio.run(ap.solver_tick()) == A.SOLVED
    # three ordinary fetch jobs replayed the compiled steps -- no model involved
    assert [s["url"][-3:] for s in hub.submitted] == ["/1/", "/2/", "/3/"]
    assert all(s["learn"]["arm"] == "solver" and s["download_video"] for s in hub.submitted)
    assert [s["learn"]["role"] for s in hub.submitted] == ["explore", "gen", "gen"]
    (pb,) = store.playbooks
    assert pb.state == P.CANDIDATE and pb.actions == hub.submitted[0]["actions"]
    prov = pb.provenance
    assert prov["manifest"] == {"solver_prompt": V.PROMPT_VERSION, "model": "fake-model", "vision": "",
                                "compiler": C.COMPILER_VERSION, "verifier": prov["manifest"]["verifier"]}
    assert prov["generalization"] == ["https://x.example/v/2/", "https://x.example/v/3/"]
    (run,) = store.runs
    assert (run["result"], run["pb_id"], run["tokens"]) == (A.SOLVED, 1, 200)
    (ku,) = store.key_updates
    assert ku["solved"] is True and ku["dormant_until"] is None
    json.dumps(run["trace"])                             # the audit trail is storable


def test_a_one_url_trick_is_rejected_and_the_key_backs_off():
    store = FakeStore(cands=[_cand()], outcomes={
        "job1": ("success", "video_ok"), "job2": ("fail", "video_not_captured"),
        "job3": ("success", "video_ok")})
    ap = _pilot(store, FakeHub(), FakeLLM([CLICK2, DONE]))
    assert asyncio.run(ap.solver_tick()) == A.GENERALIZE_FAIL
    assert store.playbooks == []
    (ku,) = store.key_updates
    assert ku["solved"] is False and 0.9 < (ku["dormant_until"] - _utc()).days + 1 <= 1.1


def test_a_short_clip_on_replay_is_retried_once_with_a_wait_for_the_advertisement():
    # In a plain fetch the player shows an advertisement first; the Solver's session did not, so it never waited.
    short = ("partial", "video_short")
    store = FakeStore(cands=[_cand()], outcomes={
        "job1": short, "job2": short, "job3": short,
        "job4": ("success", "video_ok"), "job5": ("success", "video_ok"), "job6": ("success", "video_ok")})
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([CLICK2, DONE]))
    assert asyncio.run(ap.solver_tick()) == A.SOLVED
    assert len(hub.submitted) == 6
    first, second = hub.submitted[0]["actions"], hub.submitted[3]["actions"]
    assert second == first + [{"kind": "wait", "seconds": C.AD_WAIT_S}] and not C.has_long_wait(first)
    (pb,) = store.playbooks
    assert pb.actions == second                          # the steps that passed are the ones kept


def test_the_retry_with_a_wait_happens_once_and_only_for_a_short_clip():
    short = ("partial", "video_short")
    store = FakeStore(cands=[_cand()], outcomes={f"job{i}": short for i in range(1, 7)})
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([CLICK2, DONE]))
    assert asyncio.run(ap.solver_tick()) == A.REPLAY_FAIL
    assert len(hub.submitted) == 6 and store.playbooks == []
    assert "with a 40s wait" in store.runs[0]["reason"]
    # Nothing saved at all is not "an advertisement played first": no second attempt.
    store = FakeStore(cands=[_cand()], outcomes={f"job{i}": ("fail", "video_not_captured") for i in range(1, 7)})
    hub = FakeHub()
    assert asyncio.run(_pilot(store, hub, FakeLLM([CLICK2, DONE])).solver_tick()) == A.REPLAY_FAIL
    assert len(hub.submitted) == 3
    # Steps that already wait for an advertisement are not given another wait.
    assert C.has_long_wait(C.with_ad_wait([{"kind": "wait", "seconds": 1}]))
    assert not C.has_long_wait([{"kind": "wait", "seconds": 5}, {"kind": "click", "selector": "#a"}])


def test_a_login_wall_puts_the_key_to_sleep_for_a_month():
    login = {"action": "give_up", "barrier": "login"}
    store = FakeStore(cands=[_cand(success_7d=0)])
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([login, login]))
    assert asyncio.run(ap.solver_tick()) == V.GAVE_UP
    assert hub.submitted == []                           # nothing to replay
    assert len(store.runs) == 2                          # checked on a second URL first
    (ku,) = store.key_updates
    assert 28 <= (ku["dormant_until"] - _utc()).days <= 30


def test_one_login_sighting_or_a_key_that_yields_videos_is_not_shelved_for_a_month():
    login = {"action": "give_up", "barrier": "login"}
    # Named on one URL only: the second URL had an ordinary wall.
    store = FakeStore(cands=[_cand(success_7d=0)], outcomes={"job1": ("fail", "overlay_wall")})
    ap = _pilot(store, FakeHub(), FakeLLM([login, CLICK2, DONE]))
    asyncio.run(ap.solver_tick())
    assert (store.key_updates[0]["dormant_until"] - _utc()).days < 1
    # Named twice, but the ledger shows whole videos from this key.
    store = FakeStore(cands=[_cand(success_7d=12)])
    ap = _pilot(store, FakeHub(), FakeLLM([login, login]))
    asyncio.run(ap.solver_tick())
    (ku,) = store.key_updates
    assert (ku["dormant_until"] - _utc()).days < 1 and ku["result"] == "gave_up:login"


# ---- the loop: one URL is not the key ----------------------------------------------

FIVE = [f"https://x.example/v/{i}/" for i in range(1, 6)]
GONE = {"action": "give_up", "barrier": "gone", "why": "This video was deleted."}
NO_VIDEO = {"action": "give_up", "barrier": "no_video", "why": "A shop item page."}


def test_a_deleted_video_sends_the_solver_to_the_next_url():
    # The newest failing URL is a deleted video: that is about the URL. The
    # next one has the age gate the key is really failing on.
    store = FakeStore(cands=[_cand(urls=FIVE)], outcomes={
        "job1": ("success", "video_ok"), "job2": ("success", "video_ok"), "job3": ("success", "video_ok")})
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([GONE, CLICK2, DONE]))
    assert asyncio.run(ap.solver_tick()) == A.SOLVED
    # Every session is on record, and the give-up carries the barrier it named.
    assert [(r["url"][-3:], r["result"]) for r in store.runs] == [("/1/", V.GAVE_UP), ("/2/", A.SOLVED)]
    assert store.runs[0]["reason"] == "[gone] This video was deleted."
    # The steps are replayed on the URL they were found on and on two others -- never on the dead one.
    assert [s["url"][-3:] for s in hub.submitted] == ["/2/", "/3/", "/4/"]
    assert store.playbooks[0].provenance["explore_url"] == FIVE[1]
    (ku,) = store.key_updates                            # the key is charged once, not per URL
    assert ku["solved"] is True


def test_a_key_of_deleted_videos_backs_off_like_any_failure():
    store = FakeStore(cands=[_cand(urls=FIVE)])
    ap = _pilot(store, FakeHub(), FakeLLM([GONE] * 9))
    assert asyncio.run(ap.solver_tick()) == V.GAVE_UP
    assert len(store.runs) == 4                          # max_explore_urls, not the whole list
    (ku,) = store.key_updates
    assert (ku["dormant_until"] - _utc()).days <= 1      # the next failing URLs may be alive


def test_a_page_with_no_video_is_checked_on_a_second_url_before_the_key_is_shelved():
    store = FakeStore(cands=[_cand(urls=FIVE, success_7d=0)])
    ap = _pilot(store, FakeHub(), FakeLLM([NO_VIDEO, NO_VIDEO, CLICK2]))
    assert asyncio.run(ap.solver_tick()) == V.GAVE_UP
    assert [r["url"][-3:] for r in store.runs] == ["/1/", "/2/"]     # two sightings are enough
    (ku,) = store.key_updates
    assert 28 <= (ku["dormant_until"] - _utc()).days <= 30
    # A key the ledger shows whole videos for is never shelved on the model's word.
    store = FakeStore(cands=[_cand(urls=FIVE, success_7d=40)])
    ap = _pilot(store, FakeHub(), FakeLLM([NO_VIDEO, NO_VIDEO]))
    assert asyncio.run(ap.solver_tick()) == V.GAVE_UP
    (ku,) = store.key_updates
    assert (ku["dormant_until"] - _utc()).days < 1 and ku["result"] == "gave_up:no_video"
    # One odd page among video pages does not shelve the key.
    store = FakeStore(cands=[_cand(urls=FIVE)], outcomes={
        "job1": ("success", "video_ok"), "job2": ("success", "video_ok"), "job3": ("success", "video_ok")})
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([NO_VIDEO, CLICK2, DONE]))
    assert asyncio.run(ap.solver_tick()) == A.SOLVED
    assert [s["url"][-3:] for s in hub.submitted] == ["/2/", "/3/", "/4/"]


def test_nothing_to_click_on_one_url_is_tried_on_the_others():
    # The wall is not shown on every visit: "nothing to click" on one URL
    # says little. The model gets the other URLs before the key is charged.
    store = FakeStore(cands=[_cand(urls=FIVE)])
    ap = _pilot(store, FakeHub(), FakeLLM([DONE, DONE, DONE, DONE, DONE]))
    assert asyncio.run(ap.solver_tick()) == V.NO_ACTION
    assert [r["url"][-3:] for r in store.runs] == ["/1/", "/2/", "/3/", "/4/"]
    assert len(store.key_updates) == 1


def test_steps_are_not_kept_when_too_few_live_urls_remain_to_test_them():
    store = FakeStore(cands=[_cand()])                   # three URLs, and the first is dead
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([GONE, CLICK2, DONE]))
    assert asyncio.run(ap.solver_tick()) == A.REPLAY_FAIL
    assert hub.submitted == [] and store.playbooks == []
    assert store.runs[-1]["reason"] == "too few live URLs to test the steps on"


def test_infra_on_a_later_url_still_charges_nothing_to_the_key():
    store = FakeStore(cands=[_cand(urls=FIVE)])
    ap = _pilot(store, FakeHub(), FakeLLM([GONE, LLMUnavailable("engine down")]))
    assert asyncio.run(ap.solver_tick()) == V.INFRA
    assert [r["result"] for r in store.runs] == [V.GAVE_UP, V.INFRA] and store.key_updates == []


def test_the_model_is_told_what_the_fetches_of_the_key_ran_into():
    store = FakeStore(cands=[_cand(klass="video_short", urls=FIVE)])
    llm = FakeLLM([NO_VIDEO, NO_VIDEO])
    asyncio.run(_pilot(store, FakeHub(), llm).solver_tick())
    assert "advertisement" in llm.prompts[0] and "WHAT IS KNOWN" in llm.prompts[0]
    assert set(A.KLASS_NOTES) == set(S.KNOWN_REASONS)     # every target reason has its note


def test_what_the_solver_concluded_about_the_key_is_kept():
    store = FakeStore(cands=[_cand(urls=FIVE, success_7d=0)])
    ap = _pilot(store, FakeHub(), FakeLLM([NO_VIDEO, NO_VIDEO]))
    asyncio.run(ap.solver_tick())
    assert store.key_updates[0]["result"] == "gave_up:not_video"
    store = FakeStore(cands=[_cand()])
    ap = _pilot(store, FakeHub(), FakeLLM([{"action": "give_up", "barrier": "login"}] * 2))
    asyncio.run(ap.solver_tick())
    assert store.key_updates[0]["result"] == "gave_up:login"


# ---- the loop: a run a person asked for ------------------------------------------------

def _ask(ap, url, **kw):
    req = R.build_request(url, rid="req1", **kw)
    asyncio.run(R.submit(ap.redis, req))
    return req


def test_a_requested_url_is_run_first_whatever_the_scheduler_would_say():
    store = FakeStore(cands=[_cand(host="other.example")], outcomes={
        "job1": ("success", "video_ok"), "job2": ("success", "video_ok"), "job3": ("success", "video_ok")})
    store.usage = (10 ** 6, 0)                           # the day's budget is long gone
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([CLICK2, DONE]))
    _ask(ap, "https://x.example/v/9/", klass="video_short")
    assert asyncio.run(ap.solver_tick()) == A.SOLVED
    assert store.asked_url == ("https://x.example/v/9/", "video_short")
    # The URL the person gave is the one explored; the replay test on other URLs still applies.
    assert [s["url"][-3:] for s in hub.submitted] == ["/9/", "/1/", "/2/"]
    (run,) = store.runs
    assert run["url"] == "https://x.example/v/9/" and run["reason"].startswith("[manual] ")
    res = asyncio.run(R.wait_result(ap.redis, "req1", timeout_s=0))
    assert (res["result"], res["host"], res["sessions"]) == (A.SOLVED, "x.example", 1)
    assert res["urls"] == ["https://x.example/v/9/"]
    # The queue is empty again: the next tick is the scheduler's (and the budget stops it).
    assert asyncio.run(ap.solver_tick()) == "budget_exhausted"


def test_a_requested_url_of_a_key_with_one_url_is_explored_but_makes_no_playbook():
    store = FakeStore()
    store.manual_others = []
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([CLICK2, DONE]))
    _ask(ap, "https://x.example/v/9/")
    assert asyncio.run(ap.solver_tick()) == A.REPLAY_FAIL
    assert hub.clicked == [2] and hub.submitted == [] and store.playbooks == []
    assert store.runs[0]["reason"] == "[manual] too few live URLs to test the steps on"


def test_requests_that_cannot_be_run_are_answered_without_a_session():
    store = FakeStore()
    store.playbooks = [P.Playbook(id=1, host="x.example", template="/v/{int}/", intent="video",
                                  state=P.TRIAL, actions=[{"kind": "wait"}])]
    hub = FakeHub()
    ap = _pilot(store, hub)
    _ask(ap, "https://x.example/v/9/")
    assert asyncio.run(ap.solver_tick()) == "has_playbook"
    assert asyncio.run(R.wait_result(ap.redis, "req1", timeout_s=0))["result"] == "has_playbook"
    asyncio.run(ap.redis.rpush(A.REQUESTS_KEY, "not json"))
    asyncio.run(ap.redis.rpush(A.REQUESTS_KEY, json.dumps({"url": "file:///etc/passwd"})))
    assert asyncio.run(ap.solver_tick()) == "bad_request"
    assert asyncio.run(ap.solver_tick()) == "bad_request"
    assert hub.clicked == [] and store.runs == []


def test_a_request_reports_its_own_sessions_not_those_of_runs_going_on_beside_it():
    store = FakeStore()
    ap = _pilot(store, FakeHub(), FakeLLM([GONE, GONE, GONE, GONE]))
    ap.stats["runs"] = 0

    class Busy(FakeLLM):                                 # another run records sessions meanwhile
        async def chat_json(self, system, user, **kw):
            ap.stats["runs"] += 5
            return await super().chat_json(system, user, **kw)

    ap.llm = Busy([GONE, GONE])
    store.manual_others = ["https://x.example/v/1/"]
    _ask(ap, "https://x.example/v/9/")
    asyncio.run(ap.solver_tick())
    res = asyncio.run(R.wait_result(ap.redis, "req1", timeout_s=0))
    assert res["sessions"] == 2 and res["urls"] == ["https://x.example/v/9/", "https://x.example/v/1/"]


# ---- the loop: steps a person wrote ---------------------------------------------------

STEPS = [{"kind": "wait", "seconds": 2},
         {"kind": "evaluate", "expression": "location.href = document.querySelector('iframe').src"},
         {"kind": "click", "selector": "#close-ad"},
         {"kind": "wait", "seconds": 15}]


def test_steps_a_person_wrote_pass_the_same_replay_test_and_become_a_candidate():
    store = FakeStore(outcomes={
        "job1": ("success", "video_ok"), "job2": ("success", "video_ok"), "job3": ("success", "video_ok")})
    hub = FakeHub()
    llm = FakeLLM([])
    ap = _pilot(store, hub, llm)
    asyncio.run(R.submit(ap.redis, R.build_request("https://x.example/v/9/", actions=STEPS, rid="req1")))
    assert asyncio.run(ap.solver_tick()) == A.SOLVED
    assert llm.calls == 0 and hub.clicked == []          # no model, no exploring session
    assert [s["url"][-3:] for s in hub.submitted] == ["/9/", "/1/", "/2/"]
    assert all(s["actions"] == STEPS for s in hub.submitted)
    (pb,) = store.playbooks
    assert pb.actions == STEPS and pb.provenance["source"] == "manual" and pb.state == P.CANDIDATE
    (run,) = store.runs
    assert run["reason"] == "[manual steps] replayed_on_all_urls" and run["pb_id"] == 1 and run["tokens"] == 0
    res = asyncio.run(R.wait_result(ap.redis, "req1", timeout_s=0))
    assert res["result"] == A.SOLVED and res["urls"] == ["https://x.example/v/9/"]


def test_steps_that_do_not_work_everywhere_make_no_playbook():
    store = FakeStore(outcomes={
        "job1": ("success", "video_ok"), "job2": ("fail", "video_not_captured"), "job3": ("success", "video_ok")})
    ap = _pilot(store, FakeHub(), FakeLLM([]))
    asyncio.run(R.submit(ap.redis, R.build_request("https://x.example/v/9/", actions=STEPS)))
    assert asyncio.run(ap.solver_tick()) == A.GENERALIZE_FAIL
    assert store.playbooks == [] and len(store.key_updates) == 1


def test_only_the_three_kinds_the_recipe_player_runs_are_accepted():
    assert C.check_manual_actions(STEPS) == ""
    for bad in ([], "x", [{"kind": "navigate", "url": "https://x"}], [{"kind": "wait", "seconds": 0}],
                [{"kind": "wait", "seconds": 9999}], [{"kind": "click"}], [{"kind": "evaluate", "expression": " "}],
                [{"kind": "wait", "seconds": 1}] * 21):
        assert C.check_manual_actions(bad) != "", bad
        try:
            R.build_request("https://x.example/v/1/", actions=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    ap = _pilot(FakeStore(), FakeHub())
    asyncio.run(ap.redis.rpush(A.REQUESTS_KEY, json.dumps({"url": "https://x.example/v/1/", "actions": [{"kind": "type"}]})))
    assert asyncio.run(ap.solver_tick()) == "bad_request"


class LossyHub(FakeHub):
    """A hub that finishes every job but whose outcome for some never arrives."""

    async def job_status(self, job_id):
        return "completed"


def test_a_replay_job_whose_outcome_is_lost_is_submitted_again():
    # job3 (the third URL) finishes, but its outcome never reaches the ledger;
    # the second attempt for that URL (job4) is judged normally.
    store = FakeStore(cands=[_cand()], outcomes={
        "job1": ("success", "video_ok"), "job2": ("success", "video_ok"), "job4": ("success", "video_ok")})
    hub = LossyHub()
    ap = _pilot(store, hub, FakeLLM([CLICK2, DONE]), replay_lost_after_s=0.0)
    assert asyncio.run(ap.solver_tick()) == A.SOLVED
    assert [s["url"][-3:] for s in hub.submitted] == ["/1/", "/2/", "/3/", "/3/"]
    assert store.playbooks[0].provenance["replay"]["https://x.example/v/3/"]["job_id"] == "job4"


def test_a_lost_outcome_is_resubmitted_only_so_often_and_then_counts_as_infrastructure():
    store = FakeStore(cands=[_cand()], outcomes={"job1": ("success", "video_ok"), "job2": ("success", "video_ok")})
    hub = LossyHub()
    ap = _pilot(store, hub, FakeLLM([CLICK2, DONE]), replay_lost_after_s=0.0, replay_timeout_s=0.05)
    assert asyncio.run(ap.solver_tick()) == V.INFRA
    assert len(hub.submitted) <= 3 + ap.cfg.replay_resubmits and store.key_updates == []


def test_request_validation():
    assert R.build_request(" https://x.example/v/1/ ", now=5.0, rid="a") == {
        "id": "a", "url": "https://x.example/v/1/", "klass": "", "ts": 5.0}
    for bad in (("ftp://x/", ""), ("x.example/v/1", ""), ("https://x.example/", "nonsense")):
        try:
            R.build_request(*bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


# ---- the loop: several runs at once ---------------------------------------------------

class SlowHub(FakeHub):
    """Sessions that take a moment to open, so runs really overlap."""

    def __init__(self):
        super().__init__()
        self.opened = []

    async def open_session(self, url, **_kw):
        self.opened.append(url)
        await asyncio.sleep(0.01)
        return f"ses_{len(self.opened)}"


def test_runs_at_the_same_time_never_take_the_same_key():
    cands = [_cand(host=f"h{i}.example", urls=[f"https://h{i}.example/v/{n}/" for n in range(1, 6)])
             for i in range(3)]
    store = FakeStore(cands=cands)
    hub = SlowHub()
    ap = _pilot(store, hub, FakeLLM([GONE] * 40))

    async def go():
        return await asyncio.gather(*(ap.solver_tick() for _ in range(3)))

    assert asyncio.run(go()) == [V.GAVE_UP] * 3
    assert sorted(ku["host"] for ku in store.key_updates) == ["h0.example", "h1.example", "h2.example"]
    assert ap._in_flight == set()                        # released, also after a failure
    # A fourth run while all keys are taken has nothing to do.
    ap._in_flight = {c.key for c in cands}
    assert asyncio.run(ap.solver_tick()) == "nothing_eligible"


def test_each_run_is_billed_only_its_own_model_calls():
    # One run makes 1 call, the other 3; they overlap. "Counter now minus
    # counter at start" on the shared client would bill each for both.
    hub = SlowHub()

    async def one(url, replies):
        mine = FakeLLM(replies)
        real = mine.chat_json

        async def slow(system, user, **kw):
            await asyncio.sleep(0.005)
            return await real(system, user, **kw)

        mine.chat_json = slow
        return await V.solve(hub, mine, url, sleep=_nosleep)

    async def go():
        return await asyncio.gather(
            one("https://a.example/v/1/", [GONE]),
            one("https://b.example/v/1/", [{"action": "wait"}, {"action": "wait"}, GONE]))

    a, b = asyncio.run(go())
    assert (a.llm_calls, a.tokens) == (1, 100) and (b.llm_calls, b.tokens) == (3, 300)


def test_the_candidate_list_is_shared_and_refreshed_after_a_while():
    store = FakeStore(cands=[_cand(urls=FIVE)])
    asked = []
    real = store.candidates

    async def counting(reasons=None):
        asked.append(reasons)
        return await real(reasons=reasons)

    store.candidates = counting
    ap = _pilot(store, FakeHub(), FakeLLM([GONE] * 20))
    asyncio.run(ap.solver_tick())
    asyncio.run(ap.solver_tick())
    assert len(asked) == 1                               # the second run reused the list
    ap._cands_at -= ap.cfg.candidates_ttl_s + 1
    asyncio.run(ap.solver_tick())
    assert len(asked) == 2


def test_infra_results_are_logged_but_never_charged_to_the_key():
    store = FakeStore(cands=[_cand()])
    ap = _pilot(store, FakeHub(open_error=HubUnavailable("fleet at capacity")), FakeLLM([]))
    assert asyncio.run(ap.solver_tick()) == V.INFRA
    assert store.runs[0]["result"] == V.INFRA and store.key_updates == []
    # replay jobs that never settle are infrastructure too
    store = FakeStore(cands=[_cand()], outcomes={"job1": ("unknown", "download_in_progress")})
    ap = _pilot(store, FakeHub(), FakeLLM([CLICK2, DONE]), replay_timeout_s=0.0)
    assert asyncio.run(ap.solver_tick()) == V.INFRA
    assert store.key_updates == [] and store.playbooks == []


def test_budget_and_empty_queue_stop_the_solver():
    store = FakeStore(cands=[_cand()])
    store.usage = (1000, 0)
    hub = FakeHub()
    assert asyncio.run(_pilot(store, hub).solver_tick()) == "budget_exhausted"
    empty = FakeStore()
    assert asyncio.run(_pilot(empty).solver_tick()) == "no_candidates"
    assert empty.asked_reasons == ("overlay_wall", "video_not_captured", "video_short")
    store = FakeStore(cands=[_cand()])
    store.playbooks = [P.Playbook(id=1, host="x.example", template="/v/{int}/", intent="video",
                                  state=P.TRIAL, actions=[{"kind": "wait"}])]
    assert asyncio.run(_pilot(store, hub).solver_tick()) == "nothing_eligible"   # key already has a playbook
    assert hub.submitted == [] and hub.clicked == []


# ---- the loop: life cycle ----------------------------------------------------------

def _pb(state, **kw):
    base = dict(id=1, host="x.example", template="/v/{int}/", intent="video", state=state,
                actions=[{"kind": "click", "selector": "button.age-yes"}],
                state_changed_at=_utc() - dt.timedelta(hours=5),
                provenance={"klass": "overlay_wall"})
    base.update(kw)
    return P.Playbook(**base)


def test_candidate_starts_its_trial_and_is_published_for_the_hubs():
    store = FakeStore()
    store.playbooks = [_pb(P.CANDIDATE)]
    ap = _pilot(store)
    moved = asyncio.run(ap.lifecycle_tick())
    assert moved == [(1, P.TRIAL, "trial_started")]
    assert store.state_changes == [(1, P.TRIAL, "trial_started", 50, None)]   # control never succeeds: 50%
    doc = json.loads(ap.redis.kv[P.REDIS_KEY])
    (item,) = doc["items"]
    assert (item["id"], item["state"], item["apply_pct"]) == (1, "trial", 50)


def test_trial_is_promoted_from_measured_counts_without_anyone_asking():
    store = FakeStore()
    store.playbooks = [_pb(P.TRIAL, apply_pct=20)]
    store.counts = {1: {"trial": {"n_success": 22, "n_fail": 3}, "pb_control": {"n_success": 4, "n_fail": 96}}}
    ap = _pilot(store)
    assert asyncio.run(ap.lifecycle_tick()) == [(1, P.ACTIVE, "trial_beats_control")]
    (_id, state, _reason, _pct, diff) = store.state_changes[0]
    assert state == P.ACTIVE and abs(diff - (22 / 25 - 0.04)) < 1e-9
    (item,) = json.loads(ap.redis.kv[P.REDIS_KEY])["items"]
    assert (item["state"], item["apply_pct"], item["holdout_pct"]) == ("active", 100, 5)
    assert store.key_updates == []


def test_a_failed_trial_is_withdrawn_and_the_key_backs_off():
    store = FakeStore()
    store.playbooks = [_pb(P.TRIAL)]
    store.counts = {1: {"trial": {"n_success": 1, "n_fail": 39}, "pb_control": {"n_success": 50, "n_fail": 50}}}
    ap = _pilot(store)
    assert asyncio.run(ap.lifecycle_tick()) == [(1, P.RETIRED, "trial_worse_than_control")]
    assert json.loads(ap.redis.kv[P.REDIS_KEY])["items"] == []          # the hubs stop applying it
    (ku,) = store.key_updates
    assert ku["solved"] is False and ku["klass"] == "overlay_wall" and ku["dormant_until"] is not None


def test_a_decayed_active_playbook_retires_itself():
    store = FakeStore()
    store.playbooks = [_pb(P.ACTIVE, promoted_diff=0.7)]
    store.counts = {1: {"active": {"n_success": 900, "n_fail": 200}, "pb_holdout": {"n_success": 2, "n_fail": 60}}}
    store.recent = {1: {"active": {"n_success": 5, "n_fail": 75}, "pb_holdout": {"n_success": 1, "n_fail": 14}}}
    store.idle = {1: 0.0}
    ap = _pilot(store)
    assert asyncio.run(ap.lifecycle_tick()) == [(1, P.RETIRED, "active_decayed")]
    assert json.loads(ap.redis.kv[P.REDIS_KEY])["items"] == []


def test_nothing_due_still_republishes_so_the_set_never_goes_stale():
    store = FakeStore()
    store.playbooks = [_pb(P.TRIAL)]
    ap = _pilot(store)
    assert asyncio.run(ap.lifecycle_tick()) == []
    first = json.loads(ap.redis.kv[P.REDIS_KEY])
    assert asyncio.run(ap.lifecycle_tick()) == []
    second = json.loads(ap.redis.kv[P.REDIS_KEY])
    assert second["rev"] > first["rev"] and second["ts"] >= first["ts"] and len(second["items"]) == 1


class _HubHttp:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    async def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        self.sent.append({"method": method, "url": url, "body": json})
        return self.replies.pop(0)


def test_hub_gateway_waits_out_the_same_url_dedup(monkeypatch):
    """The hub answers 409 while the same URL is queued or running for
    someone else (its own dedup). That means "wait", not "the replay
    failed" -- seen live on the first replay test, 2026-11-12."""
    from learner import hubapi

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(hubapi.asyncio, "sleep", no_sleep)
    busy = _Resp(409, {"detail": "a fetch job for this URL is already queued"})
    http = _HubHttp([busy, busy, _Resp(200, {"job_id": "j1"})])
    hub = hubapi.Hub("http://h", client=http)
    assert asyncio.run(hub.submit_fetch("http://x/1", actions=None, learn={"arm": "solver"})) == "j1"
    assert len(http.sent) == 3
    # never frees up: infrastructure, not a verdict on the key
    hub = hubapi.Hub("http://h", client=_HubHttp([busy] * 3))
    try:
        asyncio.run(hub.submit_fetch("http://x/1", actions=None, learn={"arm": "solver"}, retries=3))
    except hubapi.HubUnavailable:
        pass
    else:
        raise AssertionError("a busy URL was not reported as infrastructure")
    # any other refusal is still a refusal
    hub = hubapi.Hub("http://h", client=_HubHttp([_Resp(422, {"detail": "bad options"})]))
    try:
        asyncio.run(hub.submit_fetch("http://x/1", actions=None, learn={"arm": "solver"}))
    except hubapi.HubError as e:
        assert e.status == 422
    else:
        raise AssertionError("a 422 was swallowed")
