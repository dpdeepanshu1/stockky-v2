"""E553 (round 533): the nightly AI idea source (agent.py). Offline, stdlib only. Plain asserts or pytest (python tests/test_agent.py)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import agent as G  # noqa: E402
import autocalib as A  # noqa: E402
import jobs as J  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
RT, PS = A.RT, A.PS
# Tuesday 2026-11-17 16:20 IST (market closed)
NOW = datetime(2026, 11, 17, 16, 20, tzinfo=IST).timestamp()
GOOD_KEY = "AIzaSyD-EXAMPLEKEYEXAMPLEKEYEXAMPLE12345"


def frow(key, effective="true", kind="bool"):
    return {"key": key, "title": key, "kind": kind, "effective": effective, "looser": "off", "choices": [], "lo": None, "hi": None}


class FakeControl:
    def __init__(self, flags, down=()):
        self.f, self.down = flags, set(down)
        self.sets = []

    def flags(self, svc):
        if svc in self.down:
            raise RuntimeError("down")
        return {k: dict(v) for k, v in self.f.get(svc, {}).items()}

    def set(self, *a, **k):          # the agent must never call this
        self.sets.append(a)
        raise AssertionError("the agent set a switch")


class FakeEvidence:
    def __init__(self, items=None):
        self.items = items or {}

    def get(self, job):
        return self.items.get(job)


def ev(job, finished, text="", files=None):
    return A.Ev(job, f"run-{job}", finished, text, lambda n, f=files or {}: f.get(n))


class FakeAuto:
    def __init__(self, control, evidence, pinned=(), enabled=True):
        self.control, self.evidence, self.step_pct = control, evidence, 0.25
        self._s = {"enabled": enabled, "dry_run": False, "pinned": list(pinned), "cooldown_h": 24, "step_pct": 0.25}

    def settings(self):
        return dict(self._s)


class FakeRun:
    def __init__(self, rid, status="queued", note=""):
        self.d = {"id": rid, "status": status, "note": note}


class FakeRunner:
    def __init__(self, blocked=False, fail=None):
        self.submitted, self.blocked, self.fail = [], blocked, fail

    def submit(self, kind, target, params=None, by="", trigger="manual", **kw):
        if self.fail:
            raise ValueError(self.fail)
        self.submitted.append((kind, target, params, by, trigger))
        return FakeRun(f"rid-{target}", "blocked" if self.blocked else "queued", "Blocked: market hours" if self.blocked else "")


class ScriptedPost:
    """Stands in for the HTTPS call: answers from a script of (status, text) per URL fragment."""
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, url, headers, body, timeout=45.0):
        self.calls.append((url, headers, body))
        return self.script.pop(0)


def gemini_ok(doc):
    return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(doc)}]}}]})


def groq_ok(doc):
    return 200, json.dumps({"choices": [{"message": {"content": json.dumps(doc)}}]})


def make_llm(script, **kw):
    clock = {"t": 0.0}
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    d = dict(gemini_key=GOOD_KEY, gemini_model="gemini-3.5-flash-lite", gemini_backup="gemini-3.1-flash-lite", groq_key="gsk_" + "x" * 40, groq_model="openai/gpt-oss-20b",
             min_gap_s=7, post=ScriptedPost(script), sleep=sleep, clock=lambda: clock["t"])
    d.update(kw)
    return G.LLM(**d), slept


def make_agent(llm, evidence=None, flags=None, runner=None, pinned=(), mode="report", notify=None, now=NOW, **kw):
    flags = flags if flags is not None else {RT: {"STRUCTURE_MAX_CHASE_ATR": frow("STRUCTURE_MAX_CHASE_ATR", "1.5", "float"), "OVERNIGHT_HOLD_ENABLED": frow("OVERNIGHT_HOLD_ENABLED"),
                                              "KRONOS_GATE_MODE": frow("KRONOS_GATE_MODE", "off", "enum")},
                                             PS: {"SETUP_S2_ENABLED": frow("SETUP_S2_ENABLED"), "ENTRY_VWAP_GATE_ENABLED": frow("ENTRY_VWAP_GATE_ENABLED")}}
    if evidence is None:
        old = NOW - 3 * 86400
        evidence = FakeEvidence({
            "calibration_pack": ev("calibration_pack", old, "A/B verdicts: seeds 7, 11, 13 all `pass`.\n- chase: SUGGEST 1.2\n"),
            "hold_bucket": ev("hold_bucket", old, "1 day vs same day: no clear difference\n2-5 days vs same day: worse\n6+ days vs same day: no clear difference\n"),
            "setup_backtest": ev("setup_backtest", old, files={"setup_backtest.json": {"S2_RS_BREAKOUT_LONG": {"verdict": "no_edge"}}}),
            "intraday_gates": ev("intraday_gates", old, files={"gate_backtest.json": {"vwap_gate": {"verdict": "pass"}}}),
            "kronos_backtest": ev("kronos_backtest", old, "VERDICT: NOT PROVEN\n"),
        })
    auto = FakeAuto(FakeControl(flags), evidence, pinned)
    d = tempfile.mkdtemp()
    ag = G.Agent(d, auto, runner or FakeRunner(), J.JOBS, llm, notify=notify, mode=mode, now_fn=lambda: now, **kw)
    return ag, d


# -------------------------------------------------------------------------------------------------- allow-list and scrub
def test_allow_list_is_the_calibrated_non_protected_switches():
    allowed = G.allowed_rules(A.RULES)
    assert allowed and set(allowed) == {r.key for r in A.RULES}
    assert not set(allowed) & set(A.PROTECTED)
    assert "BROKER_STOP_ENABLED" not in allowed and "SHORT_MODE" not in allowed


def test_scrub_removes_keys_tokens_and_control_characters():
    s = G.scrub(f"call with {GOOD_KEY} and Bearer abc.def.ghi\nnext\x00line api_key=SECRET123", 200)
    assert GOOD_KEY not in s and "abc.def.ghi" not in s and "SECRET123" not in s and "\n" not in s and "\x00" not in s
    assert len(G.scrub("x " * 400, 50)) <= 50


# -------------------------------------------------------------------------------------------------- the input
def test_input_is_aggregates_only_and_read_only():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm)
    inp = G.build_input(ag.auto, ag.rules, NOW)
    text = json.dumps(inp)
    assert inp["date"] == "2026-11-17"
    by = {r["switch"]: r for r in inp["switches"]}
    assert by["STRUCTURE_MAX_CHASE_ATR"]["status"] == "would_change" and by["STRUCTURE_MAX_CHASE_ATR"]["test_suggests"] == "1.2"
    assert by["OVERNIGHT_HOLD_ENABLED"]["test_suggests"] == "false"
    assert by["KRONOS_GATE_MODE"]["status"] in ("hold", "would_change")
    assert by["SETUP_S2_ENABLED"]["test_age_days"] == 3.0
    assert not ag.auto.control.sets
    for banned in ("token", "balance", "account", "password", "secret", "client_id", "pnl"):
        assert banned not in text.lower(), banned
    assert set(by) <= set(ag.rules)


def test_input_marks_missing_evidence_and_unreachable_service():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm, evidence=FakeEvidence({}))
    assert all(r["status"] == "no_evidence" for r in G.build_input(ag.auto, ag.rules, NOW)["switches"] if "current" in r)
    ag.auto.control.down.add(PS)
    st = {r["switch"]: r["status"] for r in G.build_input(ag.auto, ag.rules, NOW)["switches"]}
    assert st["SETUP_S2_ENABLED"] == "service_unreachable"


def test_input_leaves_out_pinned_switches():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm, pinned=["OVERNIGHT_HOLD_ENABLED"])
    assert "OVERNIGHT_HOLD_ENABLED" not in {r["switch"] for r in G.build_input(ag.auto, ag.rules, NOW)["switches"]}


# -------------------------------------------------------------------------------------------------- validate
def test_validate_keeps_only_allowed_switches_and_one_step_directions():
    allowed = G.allowed_rules(A.RULES)
    doc = {"weaknesses": ["chase limit looks loose", 5, ""],
           "ideas": [{"switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "DOWN", "why": "walk-forward says tighter"},
                     {"switch": "BROKER_STOP_ENABLED", "direction": "down", "why": "protected"},
                     {"switch": "NOT_A_SWITCH", "direction": "up", "why": "invented"},
                     {"switch": "OVERNIGHT_HOLD_ENABLED", "direction": "sideways", "why": "bad direction"},
                     {"switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "up", "why": "duplicate"},
                     {"switch": "SETUP_S2_ENABLED", "direction": "down", "why": "pinned"},
                     "garbage", {"switch": "OPENING_GATE_ENABLED", "direction": "up", "why": "ok"}]}
    v = G.validate(doc, allowed, pinned=["SETUP_S2_ENABLED"], max_ideas=5)
    assert [(i["switch"], i["direction"], i["steps"]) for i in v["ideas"]] == [("STRUCTURE_MAX_CHASE_ATR", "down", 1), ("OPENING_GATE_ENABLED", "up", 1)]
    reasons = {(d["switch"], d["reason"]) for d in v["dropped"]}
    assert ("BROKER_STOP_ENABLED", "not in the allow-list") in reasons and ("NOT_A_SWITCH", "not in the allow-list") in reasons
    assert ("SETUP_S2_ENABLED", "pinned by you") in reasons and ("STRUCTURE_MAX_CHASE_ATR", "duplicate") in reasons
    assert v["weaknesses"] == ["chase limit looks loose"]


def test_validate_caps_ideas_and_survives_garbage():
    allowed = G.allowed_rules(A.RULES)
    ideas = [{"switch": k, "direction": "up", "why": "x"} for k in list(allowed)[:6]]
    v = G.validate({"ideas": ideas}, allowed, max_ideas=2)
    assert len(v["ideas"]) == 2 and sum(1 for d in v["dropped"] if d["reason"] == "over the nightly cap") == 4
    for bad in (None, [], "text", {"ideas": "no"}, {"ideas": [None, 3]}):
        out = G.validate(bad, allowed)
        assert out["ideas"] == [] and out["dropped"]


def test_free_text_is_scrubbed_and_never_a_setting():
    allowed = G.allowed_rules(A.RULES)
    v = G.validate({"ideas": [{"switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "down", "why": f"use key {GOOD_KEY}\n" + "long " * 100}]}, allowed)
    assert GOOD_KEY not in v["ideas"][0]["why"] and len(v["ideas"][0]["why"]) <= 200 and "\n" not in v["ideas"][0]["why"]
    assert set(v["ideas"][0]) == {"switch", "direction", "steps", "why"}


def test_parse_json_strips_fences_and_prose():
    assert G.parse_json('```json\n{"ideas": []}\n```') == {"ideas": []}
    assert G.parse_json('Sure! {"ideas": [1]} hope it helps') == {"ideas": [1]}
    assert G.parse_json("no json here") is None and G.parse_json(None) is None and G.parse_json("[1,2]") is None


# -------------------------------------------------------------------------------------------------- the LLM client
def test_gemini_first_with_key_header_and_json_mode():
    llm, _ = make_llm([gemini_ok({"ideas": []})])
    text, src = llm.ask("sys", "user")
    assert src == "gemini" and json.loads(text) == {"ideas": []}
    url, headers, body = llm.post.calls[0]
    assert "gemini-3.5-flash-lite:generateContent" in url and headers["x-goog-api-key"] == GOOD_KEY
    assert body["generationConfig"]["responseMimeType"] == "application/json" and GOOD_KEY not in url


def test_429_falls_back_to_groq_with_browser_agent():
    llm, _ = make_llm([(429, '{"error":"RESOURCE_EXHAUSTED"}'), (429, '{"error":"RESOURCE_EXHAUSTED"}'), groq_ok({"ideas": []})])      # round 536: one back-off + retry first
    text, src = llm.ask("sys", "user")
    assert src == "groq" and llm.usage["gemini"] == 2 and llm.usage["groq"] == 1
    assert llm.post.calls[2][0].endswith("/chat/completions") and llm.post.calls[2][1]["Authorization"].startswith("Bearer gsk_")
    assert any("429" in e for e in llm.usage["errors"])


def test_retired_model_404_uses_the_backup_model_once():
    llm, _ = make_llm([(404, "no longer available to new users"), gemini_ok({"ideas": []})])
    text, src = llm.ask("s", "u")
    assert src == "gemini" and "gemini-3.1-flash-lite:generateContent" in llm.post.calls[1][0]


def test_groq_retries_without_json_mode_when_refused():
    llm, _ = make_llm([(500, "x"), (400, "response_format not supported"), groq_ok({"ideas": []})])
    text, src = llm.ask("s", "u")
    assert src == "groq" and "response_format" not in llm.post.calls[2][2]


def test_both_failing_returns_none_and_never_raises():
    llm, _ = make_llm([(0, "URLError"), (503, "busy")])
    assert llm.ask("s", "u") == (None, "")
    llm2, _ = make_llm([], gemini_key="", groq_key="")
    assert llm2.ask("s", "u") == (None, "") and llm2.post.calls == []


def test_calls_are_spaced_at_least_the_gap():
    llm, slept = make_llm([(429, "x"), (429, "y"), groq_ok({"ideas": []})])
    llm.ask("s", "u")
    assert slept and sum(slept) >= 6.9


# -------------------------------------------------------------------------------------------------- a whole run
def test_report_mode_saves_counts_and_messages_without_queueing():
    sent = []
    idea = {"weaknesses": ["chase limit loose"], "ideas": [{"switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "down", "why": "walk-forward suggests tighter"}]}
    llm, _ = make_llm([gemini_ok(idea)])
    runner = FakeRunner()
    ag, d = make_agent(llm, runner=runner, notify=lambda t, m, u: sent.append((t, m, u)))
    rep = ag.run(trigger="schedule")
    assert rep["ran"] and rep["source"] == "gemini" and [i["switch"] for i in rep["ideas"]] == ["STRUCTURE_MAX_CHASE_ATR"]
    assert runner.submitted == [] and ag.auto.control.sets == []
    day = json.load(open(os.path.join(d, "agent", "2026-11-17.json")))
    assert day["ideas"][0]["direction"] == "down"
    tr = json.load(open(os.path.join(d, "agent", "trials.json")))
    assert tr["ideas"] == 1 and tr["runs"] == 1 and tr["log"][0]["switch"] == "STRUCTURE_MAX_CHASE_ATR"
    assert len(sent) == 1 and "no setting was changed" in sent[0][1] and "STRUCTURE_MAX_CHASE_ATR down" in sent[0][1] and len(sent[0][1]) <= 1500
    body = json.dumps(llm.post.calls[0][2])
    assert GOOD_KEY not in body and "gsk_" not in body


def test_propose_queues_the_existing_light_test_only_once_per_test():
    ideas = {"ideas": [{"switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "down", "why": "a"}, {"switch": "STRUCTURE_MAX_STRETCH_ATR", "direction": "down", "why": "b"}]}
    llm, _ = make_llm([gemini_ok(ideas)])
    runner = FakeRunner()
    # both switches are decided by calibration_pack; make its last result old enough to be re-run
    ev_old = FakeEvidence({"calibration_pack": ev("calibration_pack", NOW - 3 * 86400, "A/B verdicts: all `pass`.\n- chase: SUGGEST 1.2\n")})
    flags = {RT: {"STRUCTURE_MAX_CHASE_ATR": frow("STRUCTURE_MAX_CHASE_ATR", "1.5", "float"), "STRUCTURE_MAX_STRETCH_ATR": frow("STRUCTURE_MAX_STRETCH_ATR", "2", "float")}, PS: {}}
    ag, _ = make_agent(llm, evidence=ev_old, flags=flags, runner=runner, mode="propose")
    rep = ag.run()
    assert runner.submitted == [("job", "calibration_pack", {}, "agent", "manual")]
    assert [q["test"] for q in rep["queued"]] == ["calibration_pack"] and rep["queued"][0]["switch"] == "STRUCTURE_MAX_CHASE_ATR"
    assert not ag.auto.control.sets


def test_propose_never_queues_a_long_or_download_test():
    llm, _ = make_llm([gemini_ok({"ideas": [{"switch": "KRONOS_GATE_MODE", "direction": "down", "why": "x"}]})])
    runner = FakeRunner()
    ag, _ = make_agent(llm, runner=runner, mode="propose")
    rep = ag.run()
    assert runner.submitted == [] and rep["skipped"] and "more than an hour" in rep["skipped"][0]["reason"]
    assert G.queueable(J.JOBS["kronos_backtest"])[0] is False
    assert G.queueable(J.JOBS["gate_check"])[0] and G.queueable(J.JOBS["setup_backtest"])[0] and G.queueable(J.JOBS["intraday_gates"])[0]


def test_propose_skips_a_fresh_result_and_respects_the_queue_cap_and_the_runner_guard():
    ideas = {"ideas": [{"switch": "SETUP_S2_ENABLED", "direction": "down", "why": "a"}, {"switch": "ENTRY_VWAP_GATE_ENABLED", "direction": "down", "why": "b"},
                       {"switch": "OVERNIGHT_HOLD_ENABLED", "direction": "down", "why": "c"}]}   # round 541: the fixture value is "true", so only "down" is a possible move (the idea check drops "up")
    fresh = FakeEvidence({"setup_backtest": ev("setup_backtest", NOW - 3600, files={"setup_backtest.json": {}}),
                          "intraday_gates": ev("intraday_gates", NOW - 5 * 86400, files={"gate_backtest.json": {}}),
                          "hold_bucket": ev("hold_bucket", NOW - 5 * 86400, "1 day vs same day: no clear difference\n")})
    llm, _ = make_llm([gemini_ok(ideas)])
    runner = FakeRunner()
    ag, _ = make_agent(llm, evidence=fresh, runner=runner, mode="propose", max_queue=1)
    rep = ag.run()
    reasons = " | ".join(s["reason"] for s in rep["skipped"])
    assert "only 1.0 h old" in reasons and "over the nightly queue cap" in reasons
    assert [q["test"] for q in rep["queued"]] == ["intraday_gates"]
    llm2, _ = make_llm([gemini_ok({"ideas": [{"switch": "ENTRY_VWAP_GATE_ENABLED", "direction": "down", "why": "x"}]})])
    ag2, _ = make_agent(llm2, evidence=fresh, runner=FakeRunner(blocked=True), mode="propose")
    rep2 = ag2.run()
    assert rep2["queued"] == [] and "Blocked" in rep2["skipped"][0]["reason"]
    llm3, _ = make_llm([gemini_ok({"ideas": [{"switch": "ENTRY_VWAP_GATE_ENABLED", "direction": "down", "why": "x"}]})])
    ag3, _ = make_agent(llm3, evidence=fresh, runner=FakeRunner(fail="X is already waiting in the queue"), mode="propose")
    assert "already waiting" in ag3.run()["skipped"][0]["reason"]


def test_both_llms_down_saves_the_failure_and_sends_one_warning_a_night():
    sent = []
    llm, _ = make_llm([(429, "x"), (429, "y"), (429, "z"), (429, "w")])
    ag, d = make_agent(llm, notify=lambda t, m, u: sent.append(m))
    rep = ag.run(trigger="schedule")
    assert rep["ran"] and rep["ideas"] == [] and rep["errors"] and "no LLM answered" in rep["note"]
    assert os.path.exists(os.path.join(d, "agent", "2026-11-17.json"))
    assert len(sent) == 1 and "429" in sent[0], "round 536: ONE health warning a night, no idea message"
    ag.llm.post.script += [(429, "x"), (429, "y"), (429, "z"), (429, "w")]
    ag.run(trigger="schedule")
    assert len(sent) == 1, "a second automatic run the same night stays silent"
    ag.llm.post.script += [(429, "x"), (429, "y"), (429, "z"), (429, "w")]
    ag.run(trigger="manual")
    assert len(sent) == 2 and "no LLM answered" in sent[1], "a manual run always answers"


def test_no_evidence_means_no_llm_call():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm, evidence=FakeEvidence({}))
    rep = ag.run()
    assert rep["ran"] and llm.post.calls == [] and "no auto-calibrated switch has a usable test result" in rep["note"]


def test_garbage_answer_is_dropped_not_applied():
    llm, _ = make_llm([(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "I think you should set BROKER_STOP_ENABLED to false"}]}}]}))])
    ag, _ = make_agent(llm, mode="propose", runner=FakeRunner())
    rep = ag.run()
    assert rep["ideas"] == [] and rep["dropped"] and rep["queued"] == [] and ag.auto.control.sets == []


# -------------------------------------------------------------------------------------------------- modes, timetable, status
def test_off_mode_runs_nothing_and_unknown_mode_is_report():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm, mode="off")
    assert ag.run() == {"ran": False, "reason": "AGENT_MODE is off"} and llm.post.calls == []
    assert make_agent(llm, mode="apply")[0].mode == "report"      # there is no apply mode


def test_due_is_weekday_evening_once_a_day():
    llm, _ = make_llm([])
    ag, d = make_agent(llm, run_after="16:10")
    at = lambda day, hh, mm: datetime(2026, 11, day, hh, mm, tzinfo=IST).timestamp()
    assert ag.due(at(17, 16, 20)) is True        # Tuesday after the cut-off
    assert ag.due(at(17, 15, 50)) is False       # market-hours guard still on
    assert ag.due(at(14, 16, 20)) is False       # Saturday
    assert ag.due(at(17, 23, 45)) is False       # too late
    os.makedirs(os.path.join(d, "state"), exist_ok=True)
    json.dump({"last_auto_date": "2026-11-17"}, open(os.path.join(d, "state", "agent.json"), "w"))
    assert ag.due(at(17, 16, 20)) is False       # already ran today
    assert make_agent(llm, mode="off")[0].due(at(17, 16, 20)) is False


def test_status_shows_flags_not_keys():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm)
    st = ag.status()
    assert st["keys"] == {"gemini": True, "groq": True} and GOOD_KEY not in json.dumps(st)
    assert st["models"]["gemini"] == "gemini-3.5-flash-lite" and "STRUCTURE_MAX_CHASE_ATR" in st["allow_list"] and "BROKER_STOP_ENABLED" not in st["allow_list"]


def test_a_second_run_at_the_same_time_is_refused():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm)
    assert ag.lock.acquire(blocking=False)
    try:
        assert ag.run()["reason"] == "the agent is already running"
    finally:
        ag.lock.release()


def test_module_has_no_way_to_set_a_switch_or_touch_a_broker():
    """Scans the CODE, comments and the module docstring left out (the docstring explains the rule, so it may use the words)."""
    import ast
    src = Path(HERE / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):          # strip every docstring
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(getattr(node.body[0], "value", None), ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body[0].value.value = ""
    code = "\n".join(l for l in ast.unparse(tree).splitlines() if not l.strip().startswith("#")).lower()
    for banned in (".set(", "control/flags", "place_order", "dhan", "broker"):
        assert banned not in code, banned


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
