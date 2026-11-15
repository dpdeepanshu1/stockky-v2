"""Round 541 (E566, S1 of the Self Improve plan): saved agent settings, the breakdown and banner in every run, the idea check, history, next run and the loop view.
Offline, stdlib only. Plain asserts or pytest (python tests/test_round541_self_improve.py)."""
from __future__ import annotations

import ast
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import agent as G  # noqa: E402
import autocalib as A  # noqa: E402
import jobs as J  # noqa: E402
import selfimprove as SI  # noqa: E402
from test_agent import (FakeEvidence, FakeRunner, NOW, PS, RT, ev, frow, gemini_ok, make_agent, make_llm)  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST).timestamp()


def _new(llm_doc=None, **kw):
    llm, _ = make_llm([gemini_ok(llm_doc if llm_doc is not None else {"ideas": []})])
    return make_agent(llm, **kw)


# the state of 15 Nov 2026 as the VM showed it: exit mode held by a failing G2, S2's verdict unclear, overnight hold already off
def _nov15():
    old = NOW - 7 * 86400
    flags = {RT: {"STRUCTURE_EXIT_MODE": frow("STRUCTURE_EXIT_MODE", "tighten", "enum"), "OVERNIGHT_HOLD_ENABLED": frow("OVERNIGHT_HOLD_ENABLED", "false")},
             PS: {"SETUP_S2_ENABLED": frow("SETUP_S2_ENABLED", "true"), "SETUP_S3_ENABLED": frow("SETUP_S3_ENABLED", "true")}}
    flags[RT]["STRUCTURE_EXIT_MODE"]["choices"] = ["off", "tighten", "exit"]
    evidence = FakeEvidence({"gate_check": ev("gate_check", old, "G2: FAIL\nG3: PASS\n"),
                             "hold_bucket": ev("hold_bucket", old, "1 day vs same day: no clear difference\n2-5 days vs same day: no clear difference\n"),
                             "setup_backtest": ev("setup_backtest", old, files={"setup_backtest.json": {"S2_RS_BREAKOUT_LONG": {"verdict": "unclear"}, "S3_EXHAUSTION_LONG": {"verdict": "unclear"}}})})
    pinned = [r.key for r in A.RULES if r.key not in ("STRUCTURE_EXIT_MODE", "OVERNIGHT_HOLD_ENABLED", "SETUP_S2_ENABLED", "SETUP_S3_ENABLED")]
    return flags, evidence, pinned


# ----------------------------------------------------------------------------------------------------------------- settings
def test_settings_are_bounded_and_reject_bad_values():
    ag, _ = _new()
    for bad in ({"mode": "apply"}, {"run_after": "15:44"}, {"run_after": "23:01"}, {"run_after": "ten"}, {"max_ideas": 0}, {"max_ideas": 11}, {"max_queue": -1}, {"max_queue": 7},
                {"max_ideas": 2.5}, {"max_ideas": True}, {"max_queue": "x"}, {"nonsense": 1}):
        try:
            ag.configure(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    assert ag.current_settings() == ag.env_defaults and not ag.saved


def test_a_saved_value_wins_survives_a_restart_and_reset_goes_back_to_env():
    ag, d = _new(max_ideas=5, max_queue=3, run_after="16:10")
    v = ag.configure({"max_ideas": 3, "run_after": "17:30", "max_queue": 2}, by="admin")
    assert v["values"] == {"mode": "report", "run_after": "17:30", "max_ideas": 3, "max_queue": 2} and v["source"]["max_ideas"] == "saved" and v["source"]["mode"] == "env"
    assert ag.max_ideas == 3 and ag.after_min == 17 * 60 + 30 and ag.max_queue == 2
    llm, _ = make_llm([])
    ag2 = G.Agent(d, ag.auto, FakeRunner(), J.JOBS, llm, mode="report", max_ideas=9, max_queue=6, run_after="16:10", now_fn=lambda: NOW)    # a new process: .env says 9 / 6 / 16:10
    assert ag2.max_ideas == 3 and ag2.max_queue == 2 and ag2.current_settings()["run_after"] == "17:30" and ag2.env_defaults["max_ideas"] == 9
    r = ag2.configure({}, reset=True, by="admin")
    assert r["values"] == {"mode": "report", "run_after": "16:10", "max_ideas": 9, "max_queue": 6} and r["saved"] == {} and "settings" not in ag2._state()
    ag3 = G.Agent(d, ag.auto, FakeRunner(), J.JOBS, llm, mode="report", max_ideas=9, max_queue=6, run_after="16:10", now_fn=lambda: NOW)
    assert ag3.max_ideas == 9


def test_a_damaged_saved_value_is_ignored_not_fatal():
    ag, d = _new()
    ag._put_state({"settings": {"max_ideas": 99, "run_after": "banana", "mode": "propose", "max_queue": 1}})
    llm, _ = make_llm([])
    ag2 = G.Agent(d, ag.auto, FakeRunner(), J.JOBS, llm, mode="report", max_ideas=5, max_queue=3, now_fn=lambda: NOW)
    assert ag2.max_ideas == 5 and ag2.current_settings()["run_after"] == "16:10" and ag2.mode == "propose" and ag2.max_queue == 1


def test_propose_needs_the_second_click_and_every_change_is_one_audit_line():
    ag, d = _new()
    try:
        ag.configure({"mode": "propose"}, by="admin")
        raise AssertionError("propose without confirm")
    except ValueError as e:
        assert "confirm" in str(e)
    assert ag.mode == "report" and not os.path.exists(ag.audit_path)
    ag.configure({"mode": "propose"}, by="admin", confirm=True)
    assert ag.mode == "propose"
    ag.configure({"mode": "propose"}, by="admin")            # no change: no new line, and no confirm needed to stay in propose
    ag.configure({"mode": "report", "max_ideas": 4}, by="admin")      # leaving propose needs no confirm
    lines = [json.loads(x) for x in open(ag.audit_path).read().splitlines()]
    assert len(lines) == 2 and lines[0]["changes"] == {"mode": {"from": "report", "to": "propose"}} and lines[0]["by"] == "admin"
    assert lines[1]["changes"]["max_ideas"] == {"from": 5, "to": 4} and ag.settings()["audit"][0]["changes"]["max_ideas"]["to"] == 4        # newest first
    assert not ag.auto.control.sets


def test_a_saved_run_after_time_moves_the_automatic_run():
    ag, _ = _new()
    tue_1640 = at(2026, 11, 17, 16, 40)
    assert ag.due(tue_1640)
    ag.configure({"run_after": "18:00"})
    assert not ag.due(tue_1640) and ag.due(at(2026, 11, 17, 18, 1))


def test_the_audit_file_keeps_only_the_last_200_lines():
    ag, _ = _new()
    for i in range(230):
        ag.configure({"max_ideas": 1 + i % 2 + (i % 5)})
    assert len(open(ag.audit_path).read().splitlines()) <= G.AUDIT_KEEP


# ----------------------------------------------------------------------------------------------------------------- breakdown and banner
def test_banner_says_off_dry_run_or_live_and_lists_the_unpinned_switches():
    allowed = ["A", "B", "C"]
    assert G.banner({"enabled": False}, allowed)["state"] == "off"
    b = G.banner({"enabled": True, "dry_run": True, "pinned": ["A"]}, allowed)
    assert b["state"] == "dry_run" and b["unpinned"] == ["B", "C"] and "DRY-RUN" in b["text"] and "nothing is applied" in b["text"]
    b = G.banner({"enabled": True, "dry_run": False, "pinned": ["A", "B", "C"]}, allowed)
    assert b["state"] == "live" and b["unpinned"] == [] and b["text"].startswith("LIVE")


def test_every_run_has_a_breakdown_near_the_top_with_the_banner():
    flags, evidence, pinned = _nov15()
    ag, _ = _new(flags=flags, evidence=evidence, pinned=pinned)
    ag.auto._s["dry_run"] = True
    rep = ag.run()
    keys = list(rep)
    assert keys.index("breakdown") < keys.index("source") < keys.index("ideas") and keys.index("auto_calibration") < keys.index("breakdown") and keys.index("breakdown") <= 8
    bd = rep["breakdown"]
    assert (bd["allowed"], bd["pinned"], bd["seen"], bd["hold"], bd["would_change"], bd["no_evidence"]) == (len(A.RULES), len(A.RULES) - 4, 4, 4, 0, 0)
    assert bd["asked"] is True and bd["ideas"] == 0 and "No ideas: the model named no switch" in bd["summary"] and f"{len(A.RULES) - 4} pinned" in bd["summary"]
    assert rep["auto_calibration"].startswith("DRY-RUN")
    assert json.load(open(os.path.join(ag.dir, "agent", rep["date"] + ".json")))["breakdown"]["summary"] == bd["summary"]
    assert ag.status()["last_report"]["breakdown"]["hold"] == 4


def test_why_no_ideas_is_plain_when_everything_is_pinned_or_has_no_result():
    all_pinned = [r.key for r in A.RULES]
    ag, _ = _new(pinned=all_pinned)
    rep = ag.run()
    assert rep["ideas"] == [] and rep["breakdown"]["seen"] == 0 and rep["breakdown"]["asked"] is False and "every switch the agent may name is pinned" in rep["breakdown"]["summary"]
    ag2, _ = _new(evidence=FakeEvidence({}), pinned=[r.key for r in A.RULES if r.key not in ("OVERNIGHT_HOLD_ENABLED", "SETUP_S2_ENABLED")])
    rep2 = ag2.run()
    b2 = rep2["breakdown"]
    assert b2["no_evidence"] == 2 and b2["asked"] is False and "none of the shown switches has a usable test result" in b2["summary"]


# ----------------------------------------------------------------------------------------------------------------- idea check
def test_15_nov_ideas_are_kept_but_labelled_as_having_no_support_from_their_rule():
    flags, evidence, pinned = _nov15()
    doc = {"weaknesses": ["Structure exit mode is held on a failing test G2."], "ideas": [
        {"switch": "STRUCTURE_EXIT_MODE", "direction": "down", "why": "The current exit mode stays tighten while G2 is FAIL."},
        {"switch": "SETUP_S2_ENABLED", "direction": "down", "why": "Setup S2 long verdict is unclear and the test is nearly a week old."}]}
    sent = []
    llm, _ = make_llm([gemini_ok(doc)])
    ag, _ = make_agent(llm, flags=flags, evidence=evidence, pinned=pinned, notify=lambda t, m, u: sent.append(m))
    rep = ag.run()
    assert [i["switch"] for i in rep["ideas"]] == ["STRUCTURE_EXIT_MODE", "SETUP_S2_ENABLED"] and all(i["support"] == "rule_holds" for i in rep["ideas"])
    assert "G2 is FAIL" in rep["ideas"][0]["rule_says"] and "unclear" in rep["ideas"][1]["rule_says"]
    assert sent and sent[0].count("(its rule says: no change)") == 2 and "auto-calibration" in sent[0].lower()
    log = json.load(open(ag.trials_path))["log"]
    assert [e["support"] for e in log] == ["rule_holds", "rule_holds"]


def test_an_idea_that_cannot_happen_or_has_no_result_or_opposes_its_rule_is_dropped_with_a_reason():
    flags, evidence, pinned = _nov15()
    doc = {"ideas": [{"switch": "OVERNIGHT_HOLD_ENABLED", "direction": "down", "why": "x"},      # already off
                     {"switch": "OVERNIGHT_HOLD_ENABLED", "direction": "up", "why": "dup"},
                     {"switch": "SETUP_S3_ENABLED", "direction": "up", "why": "y"}]}             # true -> up: already on
    ag, _ = _new(doc, flags=flags, evidence=evidence, pinned=pinned)
    rep = ag.run()
    why = {(d["switch"], d["reason"].split(":")[-1].strip()) for d in rep["dropped"]}
    assert rep["ideas"] == [] and len(rep["dropped"]) == 3 and ("OVERNIGHT_HOLD_ENABLED", "duplicate") in why      # the validator drops the duplicate first, the idea check the rest
    assert ("OVERNIGHT_HOLD_ENABLED", "already off (lowest value)") in why and ("SETUP_S3_ENABLED", "already on (highest value)") in why
    assert rep["breakdown"]["dropped"] == 3 and "No ideas kept: 3 answer(s) were dropped" in rep["breakdown"]["summary"]
    # no result for that switch
    ag2, _ = _new({"ideas": [{"switch": "SETUP_S2_ENABLED", "direction": "down", "why": "z"}]}, flags=flags, evidence=FakeEvidence({"hold_bucket": evidence.get("hold_bucket")}),
                  pinned=[r.key for r in A.RULES if r.key not in ("OVERNIGHT_HOLD_ENABLED", "SETUP_S2_ENABLED")])
    rep2 = ag2.run()
    assert rep2["ideas"] == [] and "no usable test result" in rep2["dropped"][0]["reason"] and "no_evidence" in rep2["dropped"][0]["reason"]


def test_an_idea_against_what_the_rule_would_do_is_dropped_and_one_with_it_is_labelled():
    # the walk-forward test suggests chase 1.2 (< 1.5): "down" agrees, "up" opposes
    ag, _ = _new({"ideas": [{"switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "up", "why": "wrong way"}]})
    rep = ag.run()
    assert rep["ideas"] == [] and "opposite to the switch's own rule" in rep["dropped"][0]["reason"] and "down" in rep["dropped"][0]["reason"]
    ag2, _ = _new({"ideas": [{"switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "down", "why": "tighter"}]})
    i = ag2.run()["ideas"][0]
    assert i["support"] == "rule_agrees" and "walk-forward" in i["rule_says"]


def test_a_dropped_idea_queues_no_test_in_propose_mode():
    flags, evidence, pinned = _nov15()
    runner = FakeRunner()
    llm, _ = make_llm([gemini_ok({"ideas": [{"switch": "OVERNIGHT_HOLD_ENABLED", "direction": "down", "why": "x"}]})])
    ag, _ = make_agent(llm, flags=flags, evidence=evidence, pinned=pinned, runner=runner, mode="propose")
    rep = ag.run()
    assert rep["ideas"] == [] and runner.submitted == [] and rep["queued"] == []


def test_the_at_end_helper_handles_bools_enums_and_numbers():
    assert G._at_end("false", "bool", [], "down") and G._at_end("true", "bool", [], "up") and G._at_end("true", "bool", [], "down") is None
    assert G._at_end("off", "enum", ["off", "log", "enforce"], "down") and G._at_end("enforce", "enum", ["off", "log", "enforce"], "up") and G._at_end("log", "enum", ["off", "log", "enforce"], "up") is None
    assert G._at_end("1.5", "float", [], "down") is None and G._at_end(None, "", [], "down") is None


# ----------------------------------------------------------------------------------------------------------------- history and next run
def test_history_is_newest_first_with_outcomes_and_survives_damage():
    flags, evidence, pinned = _nov15()
    doc = {"ideas": [{"switch": "SETUP_S2_ENABLED", "direction": "down", "why": "a"}]}
    ag, d = _new(doc, flags=flags, evidence=evidence, pinned=pinned)
    rep = ag.run()
    adir = os.path.join(d, "agent")
    with open(os.path.join(adir, "2026-11-10.json"), "w") as f:
        json.dump({"date": "2026-11-10", "mode": "report", "trigger": "schedule", "ideas": [], "dropped": [{"switch": "X", "reason": "r"}], "errors": ["e"], "breakdown": {"summary": "s"}}, f)
    open(os.path.join(adir, "2026-11-09.json"), "w").write("{not json")
    open(os.path.join(adir, "notes.json"), "w").write("{}")
    open(os.path.join(adir, "trials.json.bak"), "w").write("{}")
    h = ag.history()
    assert [x["date"] for x in h["days"]] == [rep["date"], "2026-11-10", "2026-11-09"] and h["days"][2].get("unreadable") is True
    top = h["days"][0]
    assert top["ideas"][0]["switch"] == "SETUP_S2_ENABLED" and top["ideas"][0]["outcome"] == "pending" and top["ideas"][0]["support"] == "rule_holds" and top["breakdown"]["hold"] == 4
    assert h["days"][1]["dropped"][0]["switch"] == "X" and h["days"][1]["errors_n"] == 1 and "scheduled run" in h["note"]
    tr = json.load(open(ag.trials_path))
    tr["log"][0]["verdict"] = "not_confirmed"
    json.dump(tr, open(ag.trials_path, "w"))
    assert ag.history(1)["count"] == 1 and ag.history(1)["days"][0]["ideas"][0]["outcome"] == "not_confirmed"
    json.dumps(h)


def test_history_of_a_fresh_install_is_empty():
    ag, _ = _new()
    h = ag.history()
    assert h["days"] == [] and h["count"] == 0 and h["runs"] == [] and "scheduled run" in h["note"]


def test_next_run_today_now_monday_or_off():
    ag, _ = _new()
    assert ag.next_run(at(2026, 11, 17, 15, 0)) == {"state": "scheduled", "at_ist": "2026-11-17 16:10", "reason": ""}      # Tuesday, before the time
    assert ag.next_run(at(2026, 11, 17, 16, 20))["state"] == "due_now"
    ag._put_state({"last_auto_date": "2026-11-17"})
    assert ag.next_run(at(2026, 11, 17, 16, 20))["at_ist"] == "2026-11-18 16:10"
    ag._put_state({})
    assert ag.next_run(at(2026, 11, 20, 23, 45))["at_ist"] == "2026-11-23 16:10"          # Friday after the window: Monday
    assert ag.next_run(at(2026, 11, 21, 10, 0))["at_ist"] == "2026-11-23 16:10"           # Saturday
    ag.configure({"mode": "off"})
    assert ag.next_run(at(2026, 11, 17, 15, 0))["state"] == "off" and ag.next_run()["at_ist"] is None


# ----------------------------------------------------------------------------------------------------------------- the loop view
class FakeAuto2:
    def __init__(self, inner, last=None, enabled=True, dry=True):
        self.inner, self.control, self.evidence, self.step_pct = inner, inner.control, inner.evidence, 0.25
        self.s = {"enabled": enabled, "dry_run": dry, "pinned": inner._s["pinned"], "cooldown_h": 24, "step_pct": 0.25}
        self.last = last

    def settings(self):
        return dict(self.s)

    def status(self, with_coverage=True):
        return {"settings": self.settings(), "last": self.last, "pending": {}, "history": [{"key": "K", "from": "a", "to": "b"}]}


def _sched(fired=(), enabled=True):
    entries = [dict(e) for e in J.SCHEDULE_DEFAULT]
    return {"enabled": enabled, "entries": entries, "last_fired": {k: "2026-11-17" for k in fired}, "last_skipped": None}


def _run(target, status, hh=16, mm=5, trigger="schedule", rid=None):
    return {"id": rid or f"r-{target}", "kind": "pack", "target": target, "status": status, "trigger": trigger, "queued_at": f"2026-11-17T{hh:02d}:{mm:02d}:03+05:30", "finished_at": None}


def test_slot_view_and_next_fire():
    now = datetime(2026, 11, 17, 16, 20, tzinfo=IST)
    e = next(x for x in J.SCHEDULE_DEFAULT if x["id"] == "s_daily_close")
    assert SI.slot_view(e, {}, now)["today"] == "not_fired" and SI.slot_view(e, {"s_daily_close": "2026-11-17"}, now)["today"] == "fired"
    assert SI.slot_view(e, {}, datetime(2026, 11, 17, 15, 0, tzinfo=IST))["today"] == "waiting" and SI.slot_view(e, {}, datetime(2026, 11, 21, 15, 0, tzinfo=IST))["today"] == "not_today"
    assert SI.slot_view(e, {}, now)["next_ist"] == "2026-11-18 16:05" and SI.slot_view(e, {}, now)["days"] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert SI.next_fire({**e, "enabled": False}, now) is None and SI.next_fire({**e, "time": "bad"}, now) is None
    monthly = {"id": "m", "pack": "monthly_evidence", "days": [5], "time": "13:00", "enabled": True, "first_week_only": True}
    assert SI.next_fire(monthly, now).strftime("%Y-%m-%d") == "2026-12-05"        # the first Saturday of December (Saturday 21 / 28 Nov are not in the first week)


def test_the_chain_tells_each_step_honestly():
    flags, evidence, pinned = _nov15()
    doc = {"ideas": [{"switch": "SETUP_S2_ENABLED", "direction": "down", "why": "a"}]}
    ag, _ = _new(doc, flags=flags, evidence=evidence, pinned=pinned)
    auto = FakeAuto2(ag.auto)
    ts = at(2026, 11, 17, 16, 20)
    # before the agent ran: daily_close done, the agent is due now, nothing queued, auto-calibration has nothing today
    v = SI.loop_view(ag, auto, _sched(["s_daily_close"]), [_run("daily_close", "success")], ts)
    st = {c["id"]: c for c in v["chain"]}
    assert st["daily_close"]["state"] == "done" and st["agent"]["state"] == "waiting" and "due now" in st["agent"]["detail"] and st["queued_tests"]["state"] == "not_due" and st["autocalib"]["state"] == "waiting"
    assert v["banner"]["state"] == "dry_run" and v["weekday"] == "Tue" and v["agent"]["next_run"]["state"] == "due_now" and v["agent"]["settings"]["values"]["mode"] == "report"
    # slot fired but no run in the list, and a missed slot
    assert {c["id"]: c for c in SI.loop_view(ag, auto, _sched(["s_daily_close"]), [], ts)["chain"]}["daily_close"]["state"] == "unknown"
    miss = {c["id"]: c for c in SI.loop_view(ag, auto, _sched([]), [], ts)["chain"]}["daily_close"]
    assert miss["state"] == "skipped" and "did not fire" in miss["detail"]
    assert {c["id"]: c for c in SI.loop_view(ag, auto, _sched([], enabled=False), [], ts)["chain"]}["daily_close"]["detail"] == "the timetable is switched off"
    # a failed evening pack is shown as failed
    assert {c["id"]: c for c in SI.loop_view(ag, auto, _sched(["s_daily_close"]), [_run("daily_close", "failed")], ts)["chain"]}["daily_close"]["state"] == "failed"
    # after the agent ran
    ag.now = lambda: ts
    ag.run(trigger="schedule")
    auto.last = {"at": ts, "trigger": "run daily_close", "dry_run": True, "changed": 0, "errors": 0, "waiting": 0}
    v2 = SI.loop_view(ag, auto, _sched(["s_daily_close"]), [_run("daily_close", "success")], ts)
    st2 = {c["id"]: c for c in v2["chain"]}
    assert st2["agent"]["state"] == "done" and "1 idea" in st2["agent"]["detail"] and st2["queued_tests"]["detail"] == "report mode: the agent queues nothing"
    assert st2["autocalib"]["state"] == "done" and "dry-run" in st2["autocalib"]["detail"]
    json.dumps(v2)
    assert not ag.auto.control.sets


def test_the_chain_for_a_weekend_a_failed_agent_and_queued_tests():
    flags, evidence, pinned = _nov15()
    ag, _ = _new(flags=flags, evidence=evidence, pinned=pinned)
    auto = FakeAuto2(ag.auto, enabled=False)
    sat = at(2026, 11, 21, 17, 0)
    st = {c["id"]: c for c in SI.loop_view(ag, auto, _sched(), [], sat)["chain"]}
    assert st["daily_close"]["state"] == "not_due" and st["agent"]["state"] == "not_due" and st["autocalib"]["state"] == "skipped"
    # an agent that ran but whose LLM did not answer
    ts = at(2026, 11, 17, 16, 30)
    llm, _ = make_llm([(500, "x"), (500, "x")])
    ag2, _ = make_agent(llm, flags=flags, evidence=evidence, pinned=pinned, now=ts)
    r = ag2.run(trigger="schedule")
    assert "no LLM answered" in r["note"]
    assert {c["id"]: c for c in SI.loop_view(ag2, FakeAuto2(ag2.auto), _sched(["s_daily_close"]), [_run("daily_close", "success")], ts)["chain"]}["agent"]["state"] == "failed"
    # propose mode: queued tests follow the runner's own statuses
    runner = FakeRunner()
    llm3, _ = make_llm([gemini_ok({"ideas": [{"switch": "SETUP_S2_ENABLED", "direction": "down", "why": "a"}]})])
    ev3 = FakeEvidence({**evidence.items, "setup_backtest": ev("setup_backtest", ts - 3 * 86400, files={"setup_backtest.json": {"S2_RS_BREAKOUT_LONG": {"verdict": "unclear"}}})})
    ag3, _ = make_agent(llm3, flags=flags, evidence=ev3, pinned=pinned, runner=runner, mode="propose", now=ts)
    ag3.run(trigger="schedule")
    q = ag3._state()["last_report"]["queued"]
    assert q and q[0]["test"] == "setup_backtest"
    for status, want in (("running", "waiting"), ("success", "done"), ("failed", "failed")):
        runs = [{"id": q[0]["run"], "target": "setup_backtest", "status": status, "queued_at": "2026-11-17T16:30:00+05:30"}]
        assert {c["id"]: c for c in SI.loop_view(ag3, FakeAuto2(ag3.auto), _sched(["s_daily_close"]), runs, ts)["chain"]}["queued_tests"]["state"] == want, status


# ----------------------------------------------------------------------------------------------------------------- wiring
def _route_functions():
    tree = ast.parse((HERE / "main.py").read_text(encoding="utf-8"))
    out = {}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef):
            for dec in n.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in ("get", "post", "put") and dec.args and isinstance(dec.args[0], ast.Constant):
                    out[(dec.func.attr.upper(), dec.args[0].value)] = ast.unparse(n)
    return out


def test_the_new_routes_exist_and_are_admin_only():
    routes = _route_functions()
    for key in (("GET", "/agent/settings"), ("POST", "/agent/settings"), ("GET", "/agent/history"), ("GET", "/agent/loop")):
        assert key in routes, key
        assert "Depends(require_admin)" in routes[key], key
    assert "HTTPException(400" in routes[("POST", "/agent/settings")] and "confirm=req.confirm" in routes[("POST", "/agent/settings")]
    for k, src in routes.items():
        if k[1].startswith("/agent/"):
            assert ".set(" not in src and "control" not in src.lower().replace("auto-calibration", ""), k      # no route of the agent sets a switch


def test_the_runner_image_copies_the_new_module():
    text = (HERE / "Dockerfile").read_text(encoding="utf-8")
    assert "services/calibration-runner-service/selfimprove.py" in text


def test_selfimprove_is_read_only():
    src = (HERE / "selfimprove.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("set", "submit", "configure", "run", "resume", "evaluate", "put"):
            raise AssertionError("selfimprove.py calls " + node.attr)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
