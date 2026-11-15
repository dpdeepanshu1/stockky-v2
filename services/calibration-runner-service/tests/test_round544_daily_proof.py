"""Round 544 (E569, S4 of the Self Improve plan): the daily proof. A run log, a day file that a manual run can no longer replace, and ONE Telegram warning when no agent
run was saved by 17:30 IST on a weekday. Offline, stdlib only. Plain asserts or pytest (python tests/test_round544_daily_proof.py)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import agent as G  # noqa: E402
import selfimprove as SI  # noqa: E402
from test_agent import NOW, gemini_ok, make_agent, make_llm  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(d, hh, mm):
    return datetime(2026, 11, d, hh, mm, tzinfo=IST).timestamp()       # 17 Nov 2026 is a Tuesday, 21 Nov a Saturday


def _new(n_answers=6, notify=None, **kw):
    llm, _ = make_llm([gemini_ok({"ideas": []}) for _ in range(n_answers)])
    sent = []
    ag, d = make_agent(llm, notify=notify if notify is not None else (lambda t, m, u: sent.append((t, m, u))), **kw)
    return ag, d, sent


def _clock(ag, ts):
    ag.now = lambda: ts


def _adir(d):
    return os.path.join(d, "agent")


# ---------------------------------------------------------------------------------------------------------- the day file and the run log
def test_a_manual_run_after_the_scheduled_run_does_not_replace_it():
    ag, d, _ = _new()
    _clock(ag, at(17, 16, 12))
    a = ag.run(trigger="schedule")
    _clock(ag, at(17, 18, 40))
    b = ag.run(trigger="manual", by="admin")
    assert a["file"] == "2026-11-17.json" and b["file"] == "2026-11-17-manual-184000.json"
    day = json.load(open(os.path.join(_adir(d), "2026-11-17.json")))
    assert day["trigger"] == "schedule" and day["at"] == at(17, 16, 12), "the scheduled report is still the day file"
    assert json.load(open(os.path.join(_adir(d), b["file"])))["by"] == "admin"
    h = ag.history()
    assert [x["date"] for x in h["days"]] == ["2026-11-17"] and h["days"][0]["trigger"] == "schedule" and h["days"][0]["runs_n"] == 2 and h["days"][0]["file"] == "2026-11-17.json"


def test_a_scheduled_run_after_a_manual_one_moves_the_manual_file_aside():
    ag, d, _ = _new()
    _clock(ag, at(17, 12, 0))
    ag.run(trigger="manual", by="admin")
    first = json.load(open(os.path.join(_adir(d), "2026-11-17.json")))
    assert first["trigger"] == "manual"
    _clock(ag, at(17, 16, 12))
    s = ag.run(trigger="schedule")
    assert json.load(open(os.path.join(_adir(d), "2026-11-17.json")))["trigger"] == "schedule" and s["file"] == "2026-11-17.json"
    moved = json.load(open(os.path.join(_adir(d), "2026-11-17-manual-120000.json")))
    assert moved["at"] == first["at"] and moved["by"] == "admin", "nothing was lost"


def test_two_manual_runs_before_the_schedule_still_replace_each_other_as_before():
    ag, d, _ = _new()
    _clock(ag, at(17, 12, 0))
    ag.run(trigger="manual")
    _clock(ag, at(17, 12, 30))
    r = ag.run(trigger="manual")
    assert r["file"] == "2026-11-17.json" and not [n for n in os.listdir(_adir(d)) if "-manual-" in n]


def test_only_the_newest_20_manual_files_are_kept():
    ag, d, _ = _new(n_answers=30)
    _clock(ag, at(17, 16, 12))
    ag.run(trigger="schedule")
    for i in range(24):
        _clock(ag, at(17, 17, 0) + i * 60)
        ag.run(trigger="manual")
    man = sorted(n for n in os.listdir(_adir(d)) if "-manual-" in n)
    assert len(man) == 20 and man[0] == "2026-11-17-manual-170400.json" and man[-1] == "2026-11-17-manual-172300.json"
    assert "2026-11-17.json" in os.listdir(_adir(d))


def test_the_run_log_has_one_line_per_run_newest_first_and_keeps_200():
    ag, d, _ = _new()
    _clock(ag, at(17, 16, 12))
    ag.run(trigger="schedule")
    _clock(ag, at(17, 18, 40))
    ag.run(trigger="manual", by="admin")
    log = ag.run_log()
    assert [x["trigger"] for x in log] == ["manual", "schedule"] and log[0]["by"] == "admin" and log[0]["day"] == "2026-11-17" and log[0]["ok"] is True and log[0]["file"].startswith("2026-11-17-manual-")
    assert {"mode", "source", "ideas", "dropped", "queued", "errors", "note", "at_ist"} <= set(log[0])
    for i in range(210):
        ag._runlog_add({"ok": True, "trigger": "manual"})
    assert len(open(ag.runlog_path).read().splitlines()) == G.RUNLOG_KEEP
    with open(ag.runlog_path, "a") as f:
        f.write("{damaged\n")
    assert len(ag.run_log(500)) == G.RUNLOG_KEEP, "a damaged line is skipped"
    json.dumps(ag.history())


def test_a_fresh_install_has_an_empty_log_and_no_file_problems():
    ag, d, _ = _new()
    assert ag.run_log() == [] and ag.history()["runs"] == []
    assert ag.proof(at(17, 10, 0))["state"] == "waiting"


# ---------------------------------------------------------------------------------------------------------- the proof
def test_proof_waits_then_reports_missing_after_1730():
    ag, d, _ = _new()
    p = ag.proof(at(17, 16, 0))
    assert p["state"] == "waiting" and p["warn_after_ist"] == "17:30" and not p["ran_today"] and "17:30" in p["detail"]
    assert ag.proof(at(17, 17, 29))["state"] == "waiting"
    m = ag.proof(at(17, 17, 30))
    assert m["state"] == "missed" and m["detail"].startswith("no agent run was saved today") and "no run started after 16:10" in m["detail"]


def test_proof_is_ok_after_any_saved_run_and_counts_runs():
    ag, d, _ = _new()
    _clock(ag, at(17, 16, 12))
    ag.run(trigger="schedule")
    p = ag.proof(at(17, 18, 0))
    assert p["state"] == "ok" and p["ran_today"] and p["runs_today"] == 1 and p["scheduled_today"] == 1 and "16:12" in p["detail"]
    assert ag.proof(at(18, 18, 0))["state"] == "missed", "yesterday's run is not today's proof"


def test_a_manual_run_counts_as_proof_that_the_agent_works_today():
    ag, d, _ = _new()
    _clock(ag, at(17, 17, 40))
    ag.run(trigger="manual")
    assert ag.proof(at(17, 17, 45))["state"] == "ok" and ag.proof(at(17, 17, 45))["scheduled_today"] == 0


def test_proof_off_and_weekend_never_warn():
    ag, d, sent = _new(mode="off")
    assert ag.proof(at(17, 19, 0))["state"] == "off" and ag.check_missed(at(17, 19, 0)) is None
    ag2, d2, sent2 = _new()
    p = ag2.proof(at(21, 19, 0))
    assert p["state"] == "weekend" and ag2.check_missed(at(21, 19, 0)) is None and not sent and not sent2
    ag2._put_state({"stop": {"reason": "x"}})
    assert ag2.proof(at(17, 19, 0))["state"] == "off", "a self-off agent is not expected to run, and already told you"


def test_proof_says_why_it_is_missing():
    ag, d, _ = _new()
    ag._put_state({"last_auto_date": "2026-11-17"})
    assert "started but saved no report" in ag.proof(at(17, 18, 0))["detail"]
    ag._runlog_add({"ok": False, "trigger": "schedule", "error": "crashed: KeyError('x')"})
    _clock(ag, at(17, 18, 0))
    ag._runlog_add({"ok": False, "trigger": "schedule", "error": "crashed: KeyError('x')"})
    p = ag.proof(at(17, 18, 5))
    assert p["state"] == "missed" and "crashed" in p["detail"] and not p["ran_today"], "a crash line is not a saved run"


def test_a_run_in_progress_is_waiting_not_missed():
    ag, d, sent = _new()
    assert ag.lock.acquire(blocking=False)
    try:
        assert ag.proof(at(17, 17, 40))["state"] == "waiting" and ag.check_missed(at(17, 17, 40)) is None and not sent
    finally:
        ag.lock.release()


def test_the_warn_time_follows_a_later_run_after_time_but_never_passes_2345():
    ag, d, _ = _new()
    assert ag.warn_min() == 17 * 60 + 30
    ag.configure({"run_after": "17:00"})
    assert ag.warn_min() == 18 * 60 + 20 and ag.proof(at(17, 18, 0))["state"] == "waiting" and ag.proof(at(17, 18, 20))["state"] == "missed"
    ag.configure({"run_after": "23:00"})
    assert ag.warn_min() == 23 * 60 + 45
    ag.configure({"run_after": "16:10"})
    assert ag.warn_min() == 17 * 60 + 30


# ---------------------------------------------------------------------------------------------------------- the one warning
def test_one_warning_a_day_with_the_reason_and_what_to_do():
    ag, d, sent = _new()
    assert ag.check_missed(at(17, 17, 29)) is None and not sent
    t = ag.check_missed(at(17, 17, 31))
    assert t and len(sent) == 1 and sent[0][0] == "Stockky AI agent" and sent[0][1] == t
    assert "2026-11-17" in t and "17:30" in t and "No run started after 16:10" in t and "Run now" in t
    assert ag.check_missed(at(17, 17, 46)) is None and ag.check_missed(at(17, 20, 0)) is None and len(sent) == 1, "once per day"
    assert ag.proof(at(17, 20, 0))["warned_today"] is True
    t2 = ag.check_missed(at(18, 17, 31))
    assert t2 and len(sent) == 2, "the next weekday warns again"


def test_no_warning_when_the_agent_ran_and_a_late_manual_run_ends_the_problem():
    ag, d, sent = _new()
    _clock(ag, at(17, 16, 12))
    ag.run(trigger="schedule")
    assert ag.check_missed(at(17, 17, 40)) is None and not sent
    ag2, d2, sent2 = _new()
    assert ag2.check_missed(at(17, 17, 40)) and len(sent2) == 1
    _clock(ag2, at(17, 18, 0))
    ag2.run(trigger="manual")
    assert ag2.proof(at(17, 18, 5))["state"] == "ok"


def test_a_failing_notifier_never_breaks_the_check_and_is_not_retried_every_tick():
    calls = []

    def boom(t, m, u):
        calls.append(1)
        raise RuntimeError("telegram down")
    ag, d, _ = _new(notify=boom)
    assert ag.check_missed(at(17, 17, 40)) and ag.check_missed(at(17, 17, 41)) is None and len(calls) == 1
    ag2, d2, _ = _new(notify=None)
    llm, _ = make_llm([])
    ag3, d3 = make_agent(llm, notify=None)
    assert ag3.check_missed(at(17, 17, 40))      # no notifier: the text is still returned, nothing is sent


def test_the_warning_state_survives_a_restart():
    ag, d, sent = _new()
    ag.check_missed(at(17, 17, 40))
    llm, _ = make_llm([])
    ag2 = G.Agent(d, ag.auto, ag.runner, ag.jobs, llm, notify=lambda *a: sent.append(a), now_fn=lambda: at(17, 17, 50))
    assert ag2.check_missed() is None and len(sent) == 1


# ---------------------------------------------------------------------------------------------------------- the loop tick
def test_the_tick_runs_the_agent_when_due_and_warns_only_when_it_did_not():
    ag, d, sent = _new()
    _clock(ag, at(17, 16, 15))
    ag._tick()
    assert ag.proof()["state"] == "ok" and ag.run_log()[0]["trigger"] == "schedule" and not sent
    ag._tick()
    assert len(ag.run_log()) == 1, "once per day"


def test_a_crashed_automatic_run_is_logged_and_the_warning_names_it():
    ag, d, sent = _new()

    def broken(mode, trigger, by):
        raise RuntimeError("disk full")
    ag._run = broken
    _clock(ag, at(17, 16, 15))
    ag._tick()
    log = ag.run_log()
    assert log[0]["ok"] is False and "disk full" in log[0]["error"] and not sent
    _clock(ag, at(17, 17, 31))
    ag._tick()
    assert len(sent) == 1 and "crashed" in sent[0][1] and "disk full" in sent[0][1]


def test_the_tick_never_raises():
    ag, d, _ = _new()
    ag.proof = lambda now=None: (_ for _ in ()).throw(RuntimeError("x"))
    ag._tick()


# ---------------------------------------------------------------------------------------------------------- the views
def test_status_and_the_loop_view_carry_the_proof():
    ag, d, _ = _new()
    assert ag.status()["proof"]["state"] in ("waiting", "missed", "ok")
    from test_round541_self_improve import _new as _n541, at as at541, FakeAuto2
    ag2, _d = _n541()
    view = SI.loop_view(ag2, FakeAuto2(ag2.auto), {"enabled": True, "entries": [], "last_fired": {}}, [], now_ts=at541(2026, 11, 17, 18, 0))
    assert view["agent"]["proof"]["state"] == "missed" and view["agent"]["proof"]["warn_after_ist"] == "17:30"
    json.dumps(view)


def test_the_proof_and_the_run_log_set_no_switch():
    src = Path(G.__file__).read_text(encoding="utf-8")
    for name in ("proof", "check_missed", "run_log", "_runlog_add", "_day_file"):
        i = src.index(f"def {name}(")
        body = src[i:src.index("\n    def ", i + 10)] if "\n    def " in src[i + 10:] else src[i:]
        assert ".set(" not in body and "submit(" not in body, name


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} tests passed")
