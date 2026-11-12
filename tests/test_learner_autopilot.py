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
from learner import compiler as C
from learner import playbook as P
from learner import scheduler as S
from learner import solver as V
from learner.hubapi import HubError, HubUnavailable
from learner.llm import LLMBadOutput, LLMUnavailable, chat_url, parse_json_object

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
        assert "password_field" in expr
        return self.page.probe()

    async def click(self, sid, selector):
        if self.click_error:
            raise self.click_error
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
        self.prompts.append(user)
        r = self.replies.pop(0) if self.replies else {"action": "done"}
        if isinstance(r, Exception):
            raise r
        return r


class FakeRedis:
    def __init__(self):
        self.kv = {}

    async def set(self, k, v):
        self.kv[k] = v


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
                fails_7d=200, scored_7d=220, success_7d=5,
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
    assert [S.dormancy(n).days for n in (1, 2, 3, 4, 9)] == [1, 3, 7, 30, 30]
    assert S.dormancy(1, "login").days == 30 and S.dormancy(1, "captcha").days == 30
    assert S.dormancy(1, "no_video").days == 1


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
    assert prov["manifest"] == {"solver_prompt": V.PROMPT_VERSION, "model": "fake-model",
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


def test_a_login_wall_puts_the_key_to_sleep_for_a_month():
    store = FakeStore(cands=[_cand()])
    hub = FakeHub()
    ap = _pilot(store, hub, FakeLLM([{"action": "give_up", "barrier": "login"}]))
    assert asyncio.run(ap.solver_tick()) == V.GAVE_UP
    assert hub.submitted == []                           # nothing to replay
    (ku,) = store.key_updates
    assert 28 <= (ku["dormant_until"] - _utc()).days <= 30


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
    store.usage = (40, 0)
    hub = FakeHub()
    assert asyncio.run(_pilot(store, hub).solver_tick()) == "budget_exhausted"
    empty = FakeStore()
    assert asyncio.run(_pilot(empty).solver_tick()) == "no_candidates"
    assert empty.asked_reasons == ("overlay_wall",)       # the first version works one class only
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
