"""Round 545 (E570, S5 of the Self Improve plan): the weekly review and the CSV export of the idea history. Read only. Offline, stdlib only.
Plain asserts or pytest (python tests/test_round545_review_export.py)."""
from __future__ import annotations

import csv
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import agent as G  # noqa: E402
from test_agent import gemini_ok, make_agent, make_llm  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(d, hh=18, mm=0):
    return datetime(2026, 11, d, hh, mm, tzinfo=IST).timestamp()          # 16 Nov 2026 is a Monday, 21 Nov a Saturday


def _new(n=8, **kw):
    llm, _ = make_llm([gemini_ok({"ideas": []}) for _ in range(n)])
    return make_agent(llm, **kw)


def _day_file(d, date, ideas=(), **extra):
    os.makedirs(os.path.join(d, "agent"), exist_ok=True)
    doc = {"date": date, "at": at(int(date[-2:]), 16, 12), "mode": "report", "trigger": "schedule", "source": "gemini", "ideas": list(ideas), "dropped": [], "queued": [], "breakdown": {"summary": "s"}}
    doc.update(extra)
    json.dump(doc, open(os.path.join(d, "agent", f"{date}.json"), "w"))


def _idea(sw, why="a reason", direction="down", support="rule_holds"):
    return {"switch": sw, "direction": direction, "why": why, "support": support}


# ------------------------------------------------------------------------------------------------------------------------------ CSV
def test_csv_has_a_header_and_one_row_per_idea_oldest_day_first():
    ag, d = _new()
    _day_file(d, "2026-11-16", [_idea("SETUP_S2_ENABLED"), _idea("KRONOS_GATE_MODE", direction="up")])
    _day_file(d, "2026-11-17", [], note="no ideas tonight")
    rows = list(csv.reader(io.StringIO(ag.history_csv())))
    assert tuple(rows[0]) == G.CSV_COLUMNS
    body = rows[1:]
    assert [r[0] for r in body] == ["2026-11-16", "2026-11-16", "2026-11-17"], "oldest day first, one row per idea, one row for a run with no ideas"
    c = {k: i for i, k in enumerate(G.CSV_COLUMNS)}
    assert body[0][c["switch"]] == "SETUP_S2_ENABLED" and body[0][c["direction"]] == "down" and body[0][c["outcome"]] == "pending" and body[0][c["at_ist"]] == "2026-11-16 16:12"
    assert body[2][c["switch"]] == "" and body[2][c["note"]] == "no ideas tonight"
    assert all(len(r) == len(G.CSV_COLUMNS) for r in rows)


def test_csv_neutralises_formulas_and_keeps_commas_quotes_and_newlines_in_one_cell():
    ag, d = _new()
    _day_file(d, "2026-11-16", [_idea("SETUP_S2_ENABLED", why='=HYPERLINK("http://x","click")'), _idea("KRONOS_GATE_MODE", why='tight, "quoted"\nsecond line'), _idea("STRUCTURE_MIN_RR", why="@SUM(A1)"),
                                _idea("STRUCTURE_EXIT_MODE", why="+1+1"), _idea("OPENING_GATE_ENABLED", why="-2")])
    rows = list(csv.reader(io.StringIO(ag.history_csv())))
    why = [r[G.CSV_COLUMNS.index("why")] for r in rows[1:]]
    got = sorted(why)
    assert "'=HYPERLINK(\"http://x\",\"click\")" in got and "'@SUM(A1)" in got and "'+1+1" in got and "'-2" in got
    assert 'tight, "quoted" second line' in got and len(rows) == 6
    assert not any(w[:1] in "=+-@" for w in why if w), "no cell starts like a formula"


def test_csv_skips_a_damaged_day_and_an_empty_install_is_just_the_header():
    ag, d = _new()
    assert ag.history_csv().strip() == ",".join(G.CSV_COLUMNS)
    os.makedirs(os.path.join(d, "agent"), exist_ok=True)
    open(os.path.join(d, "agent", "2026-11-16.json"), "w").write("{broken")
    assert ag.history_csv().strip() == ",".join(G.CSV_COLUMNS)
    assert ag.csv_name().startswith("agent_ideas_") and ag.csv_name().endswith(".csv")


def test_the_outcome_column_follows_the_scoreboard():
    ag, d = _new()
    _day_file(d, "2026-11-16", [_idea("SETUP_S2_ENABLED")])
    json.dump({"log": [{"date": "2026-11-16", "switch": "SETUP_S2_ENABLED", "verdict": "survived"}]}, open(ag.trials_path, "w"))
    rows = list(csv.reader(io.StringIO(ag.history_csv())))
    assert rows[1][G.CSV_COLUMNS.index("outcome")] == "survived"


# ------------------------------------------------------------------------------------------------------------------------------ weekly
def test_weekly_names_each_day_ran_missed_weekend_or_pending():
    ag, d = _new()
    for day in (16, 17, 19):
        _day_file(d, f"2026-11-{day}", [_idea("SETUP_S2_ENABLED")] if day == 16 else [])
    w = ag.weekly(at(20, 18, 0))                                   # Friday evening: Sat 14 .. Fri 20
    st = {x["date"][-2:]: x["state"] for x in w["days"]}
    assert [x["date"] for x in w["days"]][0] == "2026-11-14" and w["to"] == "2026-11-20" and len(w["days"]) == 7
    assert st == {"14": "weekend", "15": "weekend", "16": "ran", "17": "ran", "18": "missed", "19": "ran", "20": "missed"}
    assert w["weekdays"] == 5 and w["weekdays_ran"] == 3 and w["missed"] == ["Wed 11-18", "Fri 11-20"]
    assert "3 of 5" in w["summary"] and "missed: Wed 11-18, Fri 11-20" in w["summary"] and "1 idea(s): 1 pending" in w["summary"]
    early = ag.weekly(at(20, 16, 0))
    assert {x["date"][-2:]: x["state"] for x in early["days"]}["20"] == "pending" and early["missed"] == ["Wed 11-18"]


def test_a_run_on_a_weekend_counts_and_the_run_log_counts_for_days_without_a_file():
    ag, d = _new()
    _day_file(d, "2026-11-14", [])
    ag.now = lambda: at(18, 17, 0)
    ag._runlog_add({"ok": True, "trigger": "manual"})
    w = ag.weekly(at(20, 18, 0))
    st = {x["date"][-2:]: x["state"] for x in w["days"]}
    assert st["14"] == "ran" and st["18"] == "ran" and st["15"] == "weekend"
    ag.now = lambda: at(19, 17, 0)
    ag._runlog_add({"ok": False, "trigger": "schedule", "error": "crashed"})
    assert {x["date"][-2:]: x["state"] for x in ag.weekly(at(20, 18, 0))["days"]}["19"] == "missed", "a crash line is not a run"


def test_weekly_counts_outcomes_and_names_the_most_repeated_switch():
    ag, d = _new()
    _day_file(d, "2026-11-16", [_idea("SETUP_S2_ENABLED"), _idea("KRONOS_GATE_MODE")])
    _day_file(d, "2026-11-17", [_idea("SETUP_S2_ENABLED")])
    json.dump({"log": [{"date": "2026-11-16", "switch": "SETUP_S2_ENABLED", "verdict": "survived"}, {"date": "2026-11-16", "switch": "KRONOS_GATE_MODE", "verdict": "contradicted"}]}, open(ag.trials_path, "w"))
    w = ag.weekly(at(18, 18, 0))
    assert w["ideas"] == 3 and w["outcomes"] == {"survived": 1, "contradicted": 1, "pending": 1}
    assert w["top_switches"][0] == {"switch": "SETUP_S2_ENABLED", "n": 2} and "Named most: SETUP_S2_ENABLED (2x)" in w["summary"]
    assert "1 survived, 1 contradicted, 1 pending" in w["summary"]
    json.dumps(w)


def test_weekly_with_the_agent_off_says_off_not_missed_and_a_fresh_install_is_honest():
    ag, d = _new(mode="off")
    w = ag.weekly(at(20, 18, 0))
    assert {x["state"] for x in w["days"]} == {"weekend", "off"} and w["missed"] == [] and "No ideas were saved" in w["summary"]
    ag2, _ = _new()
    w2 = ag2.weekly(at(20, 18, 0))
    assert w2["weekdays_ran"] == 0 and w2["ideas"] == 0 and len(w2["missed"]) == 5 and w2["scoreboard"]["ideas"] == 0


def test_the_new_methods_and_routes_only_read():
    src = Path(G.__file__).read_text(encoding="utf-8")
    for name in ("history_csv", "weekly", "_csv_cell", "csv_name"):
        i = src.index(f"def {name}(")
        body = src[i:src.index("\n    def ", i + 10)] if "\n    def " in src[i + 10:] else src[i:]
        assert ".set(" not in body and "submit(" not in body and "_put_state" not in body and "_atomic_write" not in body, name
    main = (HERE / "main.py").read_text(encoding="utf-8")
    for route in ('@app.get("/agent/history.csv")', '@app.get("/agent/weekly")'):
        i = main.index(route)
        body = main[i:main.index("\n\n\n", i)]
        assert "Depends(require_admin)" in body and ".set(" not in body and "submit(" not in body, route


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} tests passed")
