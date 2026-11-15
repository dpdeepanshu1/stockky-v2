"""Round 536 (agent Rounds 4, 5 and 6): model health, 429 back-off, privacy guard, output limits and JSON repair, daily call cap, scoreboard, self-off, resume,
off-list warning, run-now market guard. Offline, stdlib only. Plain asserts or pytest (python tests/test_agent_hardening.py)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import agent as G  # noqa: E402
import autocalib as A  # noqa: E402
from test_agent import (FakeEvidence, FakeRunner, GOOD_KEY, NOW, PS, RT, ev, frow, gemini_ok, groq_ok, make_agent, make_llm)  # noqa: E402

DOC = {"weaknesses": ["chase limit looks loose"], "ideas": [{"switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "down", "why": "test suggests tighter"}]}


def _log(d, entries):
    os.makedirs(os.path.join(d, "agent"), exist_ok=True)
    tr = json.load(open(os.path.join(d, "agent", "trials.json"))) if os.path.exists(os.path.join(d, "agent", "trials.json")) else {"log": [], "ideas": 0, "dropped": 0, "runs": 0}
    tr["log"] += entries
    json.dump(tr, open(os.path.join(d, "agent", "trials.json"), "w"))


def _entry(i, verdict, at=NOW - 10 * 86400, sw="STRUCTURE_MAX_CHASE_ATR", direction="down", value="1.5"):
    return {"date": "2026-11-0x", "at": at + i, "switch": sw, "direction": direction, "mode": "report", "queued_run": None, "value": value, "kind": "float", "choices": [],
            "verdict": verdict, "verdict_at": at + 1}


# ------------------------------------------------------------------------------------------------ Round 4: model health and 429
def test_retired_model_falls_back_to_backup_then_groq_and_says_so():
    llm, _ = make_llm([(404, "model is no longer available to new users"), (404, "not found"), groq_ok({"ideas": []})])
    llm.begin(10)
    text, src = llm.ask("s", "u")
    assert src == "groq" and any("looks retired" in h and "gemini-3.1-flash-lite" in h for h in llm.health) and any("retired too" in h for h in llm.health)
    assert "gemini-3.1-flash-lite:generateContent" in llm.post.calls[1][0] and len(llm.post.calls) == 3
    llm2, _ = make_llm([(404, "x"), gemini_ok({"ideas": []})])
    llm2.begin(10)
    assert llm2.ask("s", "u")[1] == "gemini" and any("looks retired" in h for h in llm2.health)


def test_a_400_saying_no_longer_available_counts_as_retired():
    llm, _ = make_llm([(400, "This model is no longer available"), gemini_ok({"ideas": []})])
    llm.begin(10)
    assert llm.ask("s", "u")[1] == "gemini" and "gemini-3.1-flash-lite" in llm.post.calls[1][0]


def test_429_backs_off_once_then_succeeds_without_a_loop():
    llm, slept = make_llm([(429, '{"error":{"message":"Resource has been exhausted (per minute)"}}'), gemini_ok({"ideas": []})])
    llm.begin(10)
    assert llm.ask("s", "u")[1] == "gemini" and sum(slept) >= G.RETRY_429_S - 0.1 and len(llm.post.calls) == 2
    assert any("rate limit" in h for h in llm.health) and llm.gemini_blocked is False


def test_429_twice_goes_to_groq_and_never_loops():
    llm, _ = make_llm([(429, "a"), (429, "b"), groq_ok({"ideas": []})])
    llm.begin(10)
    assert llm.ask("s", "u")[1] == "groq" and len(llm.post.calls) == 3 and sum(1 for c in llm.post.calls if "googleapis" in c[0]) == 2
    llm2, _ = make_llm([(429, "a"), (429, "b"), (429, "c")])
    llm2.begin(10)
    assert llm2.ask("s", "u") == (None, "") and len(llm2.post.calls) == 3


def test_daily_quota_429_skips_gemini_for_the_rest_of_the_day():
    llm, _ = make_llm([(429, '{"error":{"message":"Quota exceeded for metric ... PerDay"}}'), groq_ok(DOC)])
    ag, d = make_agent(llm)
    rep = ag.run(trigger="schedule")
    assert rep["source"] == "groq" and len(llm.post.calls) == 2 and any("daily quota" in h for h in rep["health"])
    assert json.load(open(os.path.join(d, "agent", "usage.json")))["gemini_blocked"] is True
    llm.post.script += [groq_ok(DOC)]
    n = len(llm.post.calls)
    ag.run(trigger="manual")
    assert all("googleapis" not in c[0] for c in llm.post.calls[n:]), "Gemini is not asked again that day"


def test_health_reaches_the_one_telegram_message():
    sent = []
    llm, _ = make_llm([(404, "gone"), (404, "gone"), groq_ok(DOC)])
    ag, _ = make_agent(llm, notify=lambda t, m, u: sent.append(m))
    rep = ag.run(trigger="schedule")
    assert rep["ideas"] and len(sent) == 1 and "retired" in sent[0] and "STRUCTURE_MAX_CHASE_ATR down" in sent[0] and len(sent[0]) <= 1500
    llm.post.script += [(404, "gone"), (404, "gone"), groq_ok(DOC)]
    ag.run(trigger="schedule")
    assert len(sent) == 1, "one message a night for an automatic run"


# ------------------------------------------------------------------------------------------------ Round 4: privacy guard
def test_the_real_prompt_is_clean_and_a_planted_secret_blocks_the_whole_run():
    secret = "S3cr3tValue99"
    llm, _ = make_llm([])                        # any LLM call would raise IndexError: none may happen
    ag, d = make_agent(llm, secrets=lambda: [secret, GOOD_KEY])
    ag.auto.control.f[RT]["STRUCTURE_MAX_CHASE_ATR"]["effective"] = secret      # a value that flows into the prompt and that scrub() cannot know by shape
    rep = ag.run()
    assert rep["ran"] and rep["ideas"] == [] and "privacy guard" in rep["note"] and llm.post.calls == []
    assert any("privacy guard" in h for h in rep["health"])
    llm2, _ = make_llm([gemini_ok(DOC)])
    ag2, _ = make_agent(llm2, secrets=lambda: [secret, GOOD_KEY])
    rep2 = ag2.run()
    body = json.dumps(llm2.post.calls[0][2])
    assert rep2["ideas"] and secret not in body and GOOD_KEY not in body
    assert GOOD_KEY in json.dumps(llm2.post.calls[0][1]), "the key travels only in the header"


def test_privacy_problems_catches_keys_secrets_and_raw_row_fields():
    assert G.privacy_problems("plain aggregate text", ["abcdef123"]) == []
    assert G.privacy_problems("has abcdef123 inside", ["abcdef123"])
    assert G.privacy_problems("key AIzaSyD-EXAMPLEKEYEXAMPLEKEYEXAMPLE12345")
    assert G.privacy_problems("gsk_" + "a" * 30)
    assert G.privacy_problems('{"balance": 12000}') and G.privacy_problems("client_id 55") and G.privacy_problems("order_id 9") and G.privacy_problems("pnl 400")
    assert G.privacy_problems("x" * (G.MAX_PROMPT_CHARS + 1))
    assert G.privacy_problems("short", ["abc"]) == [], "a secret under 6 characters is ignored (would match everywhere)"


def test_whitelist_drops_any_field_not_listed():
    inp = {"date": "d", "switches": [{"switch": "A", "current": "1", "balance": 5, "raw_trades": [1], "verdict": "ok"}], "auto_calibration": {"enabled": True, "token": "x"}, "extra": 1}
    out = G.whitelist(inp)
    assert out == {"date": "d", "switches": [{"switch": "A", "current": "1", "verdict": "ok"}], "auto_calibration": {"enabled": True}}
    assert len(G.whitelist({"switches": [{"switch": str(i)} for i in range(200)]})["switches"]) == G.MAX_SWITCH_ROWS


def test_real_rule_names_and_titles_pass_the_guard():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm)
    prompt = G.SYSTEM_PROMPT.format(n=5) + json.dumps(G.whitelist(G.build_input(ag.auto, ag.rules, NOW)))
    assert G.privacy_problems(prompt) == []
    titles = " ".join(r.title for r in A.RULES) + " ".join(r.key for r in A.RULES)
    assert G.privacy_problems(titles) == []


# ------------------------------------------------------------------------------------------------ Round 4: limits and repair
def test_oversized_answer_is_rejected_for_both_llms():
    big = "x" * (G.MAX_ANSWER_CHARS + 5)
    llm, _ = make_llm([gemini_ok({"ideas": [], "pad": big}), gemini_ok({"ideas": [], "pad": big}), groq_ok({"ideas": [], "pad": big})])
    llm.begin(10)
    assert llm.ask("s", "u") == (None, "") and any("rejected" in e or "too long" in e for e in llm.usage["errors"])
    assert G.parse_json("{" + '"a":"' + big + '"}') is None


def test_parse_json_repairs_fences_trailing_commas_and_truncation():
    assert G.parse_json('```json\n{"ideas": [{"switch": "A", "direction": "up",}],}\n```') == {"ideas": [{"switch": "A", "direction": "up"}]}
    cut = '{"weaknesses": ["w"], "ideas": [{"switch": "A", "direction": "up", "why": "ok"}, {"switch": "B", "direction": "do'
    d = G.parse_json(cut)
    assert d and d["ideas"][0]["switch"] == "A" and all(i.get("switch") for i in d["ideas"])
    assert G.parse_json('{"ideas": [{"switch": "A"}, {"switch": "B"},') == {"ideas": [{"switch": "A"}, {"switch": "B"}]}
    for junk in ("", None, "{", "}{", "```", "[1]", "nothing", '{"a": "unterminated'):
        G.parse_json(junk)      # never raises


def test_a_truncated_answer_still_yields_the_complete_ideas_only():
    cut = '{"ideas": [{"switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "down", "why": "a"}, {"switch": "OVERNIGHT_HOLD_ENABLED", "direction": "u'
    llm, _ = make_llm([(200, json.dumps({"candidates": [{"content": {"parts": [{"text": cut}]}}]}))])
    ag, _ = make_agent(llm)
    rep = ag.run()
    assert [i["switch"] for i in rep["ideas"]] == ["STRUCTURE_MAX_CHASE_ATR"]


# ------------------------------------------------------------------------------------------------ Round 4: daily call counter and cap
def test_usage_file_counts_calls_and_the_cap_stops_the_next_run():
    llm, _ = make_llm([gemini_ok(DOC)])
    ag, d = make_agent(llm, max_calls_day=1)
    ag.run()
    u = json.load(open(os.path.join(d, "agent", "usage.json")))
    assert u["date"] == "2026-11-17" and u["gemini"] == 1 and u["groq"] == 0 and u["runs"] == 1
    assert ag.status()["usage_today"] == {"date": "2026-11-17", "gemini": 1, "groq": 0, "runs": 1, "used": 1, "cap": 1, "left": 0, "gemini_blocked": False}
    n = len(llm.post.calls)
    rep = ag.run()
    assert len(llm.post.calls) == n and "daily LLM call cap" in rep["note"] and any("cap" in h for h in rep["health"])


def test_cap_inside_a_run_stops_the_retry():
    llm, _ = make_llm([(429, "a"), gemini_ok(DOC)])
    ag, _ = make_agent(llm, max_calls_day=1)
    rep = ag.run()
    assert len(llm.post.calls) == 1 and rep["ideas"] == [] and any("cap" in h for h in rep["health"])


def test_usage_resets_on_a_new_day():
    llm, _ = make_llm([gemini_ok(DOC)])
    ag, d = make_agent(llm, max_calls_day=1)
    ag.run()
    ag.now = lambda: NOW + 86400
    assert ag.status()["usage_today"]["used"] == 0


# ------------------------------------------------------------------------------------------------ Round 5: scoreboard
def _fresh_evidence(ts):
    return FakeEvidence({"calibration_pack": ev("calibration_pack", ts, "A/B verdicts: seeds 7, 11, 13 all `pass`.\n- chase: SUGGEST 1.2\n"),
                         "hold_bucket": ev("hold_bucket", ts, "1 day vs same day: no clear difference\n2-5 days vs same day: worse\n6+ days vs same day: no clear difference\n")})


def test_idea_records_the_value_when_it_was_made():
    llm, _ = make_llm([gemini_ok(DOC)])
    ag, d = make_agent(llm)
    ag.run()
    e = json.load(open(os.path.join(d, "agent", "trials.json")))["log"][0]
    assert e["value"] == "1.5" and e["kind"] == "float" and e["at"] == NOW and e["verdict"] is None


def test_verdicts_survived_contradicted_not_confirmed_unclear_no_result_pending():
    llm, _ = make_llm([])
    ag, d = make_agent(llm, evidence=_fresh_evidence(NOW + 3600))
    ag.now = lambda: NOW + 86400
    t0 = NOW
    _log(d, [
        {**_entry(0, None, at=t0), "switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "down"},                                   # test says 1.2: down -> survived
        {**_entry(1, None, at=t0), "switch": "OVERNIGHT_HOLD_ENABLED", "direction": "up", "value": "true", "kind": "bool"},      # test says false: contradicted
        {**_entry(2, None, at=t0), "switch": "STRUCTURE_MAX_CHASE_ATR", "direction": "up", "value": "1.2"},                      # test says 1.2 = current: not confirmed
        {**_entry(3, None, at=t0), "switch": "GONE_SWITCH"},                                                                     # no rule any more: unclear
        {**_entry(4, None, at=t0), "switch": "SETUP_S2_ENABLED", "value": "true", "kind": "bool", "direction": "down"},          # test not run again, older than 7 days below
        {**_entry(5, None, at=NOW + 80000), "switch": "SETUP_S2_ENABLED", "value": "true", "kind": "bool", "direction": "down"}, # test not run, only 1 day old: pending
    ])
    ag.now = lambda: NOW + 9 * 86400
    ag._resolve(NOW + 9 * 86400)
    v = [e["verdict"] for e in json.load(open(os.path.join(d, "agent", "trials.json")))["log"]]
    assert v[:5] == ["survived", "contradicted", "not_confirmed", "unclear", "no_result"], v
    ag.now = lambda: NOW + 9 * 86400
    sb = ag.scoreboard()
    assert sb["survived"] == 1 and sb["contradicted"] == 1 and sb["not_confirmed"] == 1 and sb["unclear"] == 1 and sb["tested"] == 3 and sb["survival_pct"] == 33.3
    assert sb["rule"] == G.STOP_RULE and sb["since_resume"] is False


def test_a_result_older_than_the_idea_does_not_judge_it():
    llm, _ = make_llm([])
    ag, d = make_agent(llm, evidence=_fresh_evidence(NOW - 86400))
    _log(d, [_entry(0, None, at=NOW)])
    assert ag._resolve(NOW + 86400) == 0
    assert ag.scoreboard()["pending"] == 1


def test_direction_of_handles_numbers_bools_enums_and_junk():
    assert G.direction_of("1.5", "1.2") == "down" and G.direction_of("1.5", "2") == "up" and G.direction_of("1.5", "1.5") is None
    assert G.direction_of("true", "false") == "down" and G.direction_of("false", "true") == "up"
    assert G.direction_of("off", "shadow", "enum", ["off", "shadow", "on"]) == "up" and G.direction_of("on", "off", "enum", ["off", "shadow", "on"]) == "down"
    assert G.direction_of("abc", "def") is None and G.direction_of(None, "1") is None


# ------------------------------------------------------------------------------------------------ Round 5: stop rule and resume
def _seed(ag, d, ideas, survived, not_conf):
    es = [_entry(i, "survived") for i in range(survived)] + [_entry(survived + i, "not_confirmed") for i in range(not_conf)]
    es += [_entry(survived + not_conf + i, "no_result") for i in range(ideas - survived - not_conf)]
    _log(d, es)


def test_self_off_after_50_ideas_with_20_tested_and_under_5_percent():
    sent = []
    llm, _ = make_llm([])
    ag, d = make_agent(llm, notify=lambda t, m, u: sent.append(m))
    _seed(ag, d, 60, 1, 24)
    assert ag.scoreboard()["survival_pct"] == 4.0
    reason = ag.check_stop()
    assert reason and ag.eff_mode() == "off" and ag.status()["stopped"]["reason"] == reason and ag.status()["mode_effective"] == "off"
    again, _ = make_llm([])
    ag2 = G.Agent(d, ag.auto, FakeRunner(), G.J.JOBS if hasattr(G, "J") else {}, again, mode="propose", now_fn=lambda: NOW)
    assert ag2.eff_mode() == "off", "survives a restart"
    r = ag2.run()
    assert r["ran"] is False and "Resume" in r["reason"] and again.post.calls == []
    assert ag2.due(NOW + 3600 * 5) is False


def test_the_stop_rule_needs_all_three_conditions():
    llm, _ = make_llm([])
    for ideas, surv, nc in ((49, 0, 25), (60, 0, 19), (60, 3, 22), (60, 2, 20)):       # too few ideas / too few tested / 12% survived / exactly 9.1%
        ag, d = make_agent(llm)
        _seed(ag, d, ideas, surv, nc)
        assert ag.check_stop() is None, (ideas, surv, nc)
    ag, d = make_agent(llm)
    _seed(ag, d, 50, 1, 19)       # 50 ideas, 20 tested, 5.0%: not under 5
    assert ag.check_stop() is None
    ag, d = make_agent(llm)
    _seed(ag, d, 50, 0, 20)
    assert ag.check_stop()


def test_a_run_that_trips_the_stop_rule_says_so_once_and_resume_restarts_the_count():
    sent = []
    llm, _ = make_llm([gemini_ok(DOC)])
    ag, d = make_agent(llm, notify=lambda t, m, u: sent.append(m))
    _seed(ag, d, 60, 0, 25)
    rep = ag.run(trigger="schedule")
    assert rep.get("stopped") and len(sent) == 1 and "switched itself OFF" in sent[0] and "Resume" in sent[0]
    assert ag.run()["ran"] is False
    ag.now = lambda: NOW + 60
    out = ag.resume()
    assert out["resumed"] is True and ag.eff_mode() == ag.mode and ag.check_stop() is None
    sb = ag.scoreboard()
    assert sb["ideas"] == 0 and sb["since_resume"] is True, "old ideas no longer count after Resume"
    llm.post.script += [gemini_ok(DOC)]
    ag.now = lambda: NOW + 120
    assert ag.run(trigger="manual")["ran"] is True
    assert ag.scoreboard()["ideas"] == 1


def test_resume_when_not_stopped_is_harmless_and_sets_no_switch():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm)
    assert ag.resume()["resumed"] is False and ag.auto.control.sets == []


# ------------------------------------------------------------------------------------------------ Round 5: allow-list warning
def test_three_answers_outside_the_allow_list_in_a_week_warn_once():
    sent = []
    bad = {"ideas": [{"switch": "BROKER_STOP_ENABLED", "direction": "down", "why": "x"}, {"switch": "NOPE_ONE", "direction": "up", "why": "y"}]}
    llm, _ = make_llm([gemini_ok(bad), gemini_ok(bad), gemini_ok(bad)])
    ag, d = make_agent(llm, notify=lambda t, m, u: sent.append(m))
    r1 = ag.run(trigger="manual")
    assert r1["dropped"] and not any("allow-list" in h for h in r1["health"]), "2 hits so far"
    r2 = ag.run(trigger="manual")
    assert any("outside the allow-list" in h for h in r2["health"]), "4 hits in 7 days: warn"
    r3 = ag.run(trigger="manual")
    assert not any("outside the allow-list" in h for h in r3["health"]), "warned already this week"
    assert any("outside the allow-list" in m for m in sent)
    assert ag.auto.control.sets == []


def test_old_off_list_hits_expire_after_seven_days():
    llm, _ = make_llm([gemini_ok({"ideas": [{"switch": "NOPE", "direction": "up", "why": "y"}]})])
    ag, d = make_agent(llm)
    os.makedirs(os.path.join(d, "state"), exist_ok=True)
    json.dump({"offlist": [NOW - 20 * 86400, NOW - 19 * 86400]}, open(os.path.join(d, "state", "agent.json"), "w"))
    assert not any("allow-list" in h for h in ag.run()["health"])


# ------------------------------------------------------------------------------------------------ Round 6: run now guard and status
def test_run_now_is_refused_in_market_hours_but_the_schedule_is_not():
    llm, _ = make_llm([gemini_ok(DOC)])
    ag, _ = make_agent(llm, market_open=lambda: True)
    r = ag.run(trigger="manual")
    assert r["ran"] is False and r["market_open"] is True and "09:00-15:45" in r["reason"] and llm.post.calls == []
    assert ag.status()["market_open"] is True and ag.status()["run_now_allowed"] is False
    assert ag.run(trigger="schedule")["ran"] is True


def test_status_has_everything_the_card_reads_and_no_key():
    llm, _ = make_llm([gemini_ok(DOC)])
    ag, _ = make_agent(llm, mode="propose")
    ag.run()
    st = ag.status()
    for k in ("mode", "mode_effective", "stopped", "run_after_ist", "max_ideas", "max_queue", "market_open", "run_now_allowed", "keys", "models", "last", "scoreboard", "usage_today", "last_report", "allow_list"):
        assert k in st, k
    assert st["run_now_allowed"] is True and st["mode_effective"] == "propose" and st["last_report"]["ideas"][0]["switch"] == "STRUCTURE_MAX_CHASE_ATR"
    assert set(st["scoreboard"]) >= {"ideas", "survived", "not_confirmed", "contradicted", "unclear", "no_result", "tested", "pending", "survival_pct", "dropped", "since_resume", "rule"}
    blob = json.dumps(st)
    assert GOOD_KEY not in blob and "gsk_" not in blob
    off, _ = make_agent(make_llm([])[0], mode="off")
    assert off.status()["run_now_allowed"] is False


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
