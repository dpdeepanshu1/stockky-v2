"""E578 round 553: scripts/strategy_registry.py (strategy lifecycle from repeated evidence). Pure; synthetic evidence files."""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_registry as sr

KEY = "swing | BUY NOW"
D0 = date(2026, 11, 16)


def day(i):
    return (D0 + timedelta(days=i)).isoformat()


def rep(i, verdict, key=KEY, n=None, **extra):
    n = 40 + i if n is None else n                     # round 563: a new day normally brings new closed trades; pass n= to model a quiet day
    cell = {"n": n, "verdict": verdict, "mean_net_ret_pct": 0.2, "lower90_pct": 0.1, "dsr_prob": 0.9, "win_pct": 55.0}
    cell.update(extra)
    return {"generated_at": f"{day(i)}T11:00:00+00:00", "groups": {"strategy": {key: cell}}}


def run(store, i, verdict, **kw):
    return sr.update(store, rep(i, verdict, **kw), day(i))


def fresh():
    return {"version": 1, "strategies": {}, "updated": None}


def test_next_status_table():
    assert sr.next_status("testing", "POSITIVE", 3) == ("proven", "3 days in a row of POSITIVE")
    assert sr.next_status("testing", "POSITIVE", 2)[0] == "testing"
    assert sr.next_status("watch", "POSITIVE", 3)[0] == "proven"
    assert sr.next_status("proven", "NEGATIVE", 3)[0] == "demoted" and sr.next_status("testing", "NEGATIVE", 3)[0] == "demoted"
    assert sr.next_status("proven", "NOT_PROVEN", 3)[0] == "watch"
    assert sr.next_status("demoted", "NOT_PROVEN", 3)[0] == "testing"
    assert sr.next_status("testing", "NOT_PROVEN", 9)[0] == "testing"
    assert sr.next_status("retired", "POSITIVE", 9) == ("retired", None) and sr.next_status("retired", "NEGATIVE", 9)[1] is None


def test_three_separate_positive_days_make_a_strategy_proven_not_two():
    s = fresh()
    assert run(s, 0, "POSITIVE") == [] and s["strategies"][KEY]["status"] == "testing"
    assert run(s, 1, "POSITIVE") == [] and s["strategies"][KEY]["status"] == "testing"
    ch = run(s, 2, "POSITIVE")
    assert ch == [{"strategy": KEY, "from": "testing", "to": "proven", "why": "3 days in a row of POSITIVE"}]
    assert s["strategies"][KEY]["history"][-1]["to"] == "proven"


def test_a_rerun_on_the_same_day_refreshes_numbers_but_never_the_streak():
    s = fresh()
    for _ in range(6):
        run(s, 0, "POSITIVE", n=41)
    st = s["strategies"][KEY]
    assert st["status"] == "testing" and st["streak"]["days"] == 1 and st["last"]["n"] == 41


def test_a_different_verdict_restarts_the_streak_and_not_enough_data_changes_nothing():
    s = fresh()
    run(s, 0, "POSITIVE"); run(s, 1, "POSITIVE")
    run(s, 2, "NOT_ENOUGH_DATA")                       # no information: streak stays at 2
    assert s["strategies"][KEY]["streak"] == {"verdict": "POSITIVE", "days": 2, "last_day": day(1), "last_n": 41}
    run(s, 3, "NOT_PROVEN")                            # restarts
    assert s["strategies"][KEY]["streak"]["verdict"] == "NOT_PROVEN" and s["strategies"][KEY]["streak"]["days"] == 1 and s["strategies"][KEY]["status"] == "testing"


def test_proven_goes_to_watch_then_back_and_a_clear_loser_is_demoted():
    s = fresh()
    for i in range(3):
        run(s, i, "POSITIVE")
    assert s["strategies"][KEY]["status"] == "proven"
    for i in range(3, 6):
        run(s, i, "NOT_PROVEN")
    assert s["strategies"][KEY]["status"] == "watch"
    for i in range(6, 9):
        run(s, i, "NEGATIVE")
    assert s["strategies"][KEY]["status"] == "demoted"
    for i in range(9, 12):
        run(s, i, "NOT_PROVEN")
    assert s["strategies"][KEY]["status"] == "testing"
    assert [h["to"] for h in s["strategies"][KEY]["history"]] == ["proven", "watch", "demoted", "testing"]


def test_after_a_move_the_next_move_needs_a_fresh_run_of_days():
    s = fresh()
    for i in range(3):
        run(s, i, "NEGATIVE")
    assert s["strategies"][KEY]["status"] == "demoted"
    run(s, 3, "NEGATIVE"); run(s, 4, "NEGATIVE")        # stays demoted, no change recorded
    assert s["strategies"][KEY]["status"] == "demoted" and len(s["strategies"][KEY]["history"]) == 1


def test_retired_by_hand_is_never_moved_by_evidence(tmp_path):
    s = fresh()
    run(s, 0, "POSITIVE")
    assert sr.set_status(s, KEY, "retired", day(0)) == "testing"
    for i in range(1, 8):
        run(s, i, "POSITIVE")
    assert s["strategies"][KEY]["status"] == "retired"
    try:
        sr.set_status(s, KEY, "bogus", day(0))
        raise AssertionError("an unknown status must be refused")
    except ValueError:
        pass


def test_garbage_cells_and_missing_group_do_not_raise():
    s = fresh()
    sr.update(s, {"generated_at": f"{day(0)}T00:00:00+00:00", "groups": {"strategy": {"a": "x", "b": {}, "c": {"verdict": None}}}}, day(0))
    sr.update(s, {"groups": {}}, day(0))
    sr.update(s, {}, day(0))
    assert sr.evidence_day({"generated_at": "not a date"}) is None and sr.evidence_day({}) is None


def _write(p, doc):
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def test_main_keeps_state_between_runs_and_writes_json(tmp_path):
    store, out, ev = str(tmp_path / "state" / "reg.json"), str(tmp_path / "out" / "reg.json"), str(tmp_path / "ev.json")
    for i in range(3):
        _write(ev, rep(i, "POSITIVE"))
        assert sr.main(["--evidence", ev, "--store", store, "--json", out, "--today", day(i)]) == 0
    doc = json.load(open(store))
    assert doc["strategies"][KEY]["status"] == "proven"
    o = json.load(open(out))
    assert [c["to"] for c in o["changes"]] == ["proven"] and o["strategies"][KEY]["status"] == "proven"      # the third run made the move
    _write(ev, rep(3, "POSITIVE"))
    assert sr.main(["--evidence", ev, "--store", store, "--json", out, "--today", day(3)]) == 0
    assert json.load(open(out))["changes"] == [], "a later run with no move lists no change"


def test_main_refuses_stale_unreadable_and_old_format_evidence(tmp_path):
    store, ev = str(tmp_path / "reg.json"), str(tmp_path / "ev.json")
    _write(ev, rep(0, "POSITIVE"))
    assert sr.main(["--evidence", ev, "--store", store, "--today", day(10)]) == 1                   # 10 days old
    assert json.load(open(store))["strategies"] == {}, "a stale file advanced nothing"
    open(ev, "w").write("{broken")
    assert sr.main(["--evidence", ev, "--store", store, "--today", day(0)]) == 1
    _write(ev, {"generated_at": f"{day(0)}T00:00:00+00:00", "groups": {"decision_label": {}}})
    assert sr.main(["--evidence", ev, "--store", store, "--today", day(0)]) == 1
    assert sr.main(["--evidence", str(tmp_path / "missing.json"), "--store", store, "--today", day(0)]) == 1


def test_main_set_by_hand_and_bad_status_exit_codes(tmp_path):
    store = str(tmp_path / "reg.json")
    assert sr.main(["--store", store, "--set", f"{KEY}=retired", "--today", day(0)]) == 0
    assert json.load(open(store))["strategies"][KEY]["status"] == "retired"
    assert sr.main(["--store", store, "--set", f"{KEY}=nonsense"]) == 2


def test_damaged_store_starts_fresh_and_render_lists_a_demotion_note(tmp_path):
    p = str(tmp_path / "reg.json")
    open(p, "w").write("not json")
    assert sr.load_store(p)["strategies"] == {}
    s = fresh()
    ch = []
    for i in range(3):
        ch = run(s, i, "NEGATIVE")
    txt = sr.render(s, ch)
    assert "demoted" in txt and "decide yourself" in txt and "moves no switch" in txt


# ---------------------------------------------------------------------------------------------------- round 563: a day without new closed trades is not new evidence
def test_no_new_trades_helper_table():
    assert sr.no_new_trades(40, 40) is True and sr.no_new_trades(40, 39) is True
    assert sr.no_new_trades(40, 41) is False
    assert sr.no_new_trades(None, 40) is False and sr.no_new_trades(40, None) is False and sr.no_new_trades("x", "y") is False
    assert sr.no_new_trades(True, 40) is False and sr.no_new_trades(-1, 5) is False


def test_days_without_new_closed_trades_do_not_advance_the_streak():
    s = fresh()
    for i in range(3):
        assert run(s, i, "POSITIVE", n=40) == []              # the same 40 trades read on three days
    st = s["strategies"][KEY]
    assert st["status"] == "testing" and st["streak"]["days"] == 1 and st["last_seen"] == day(2)
    assert run(s, 3, "POSITIVE", n=41) == [] and st["streak"]["days"] == 2
    ch = run(s, 4, "POSITIVE", n=45)
    assert ch and ch[0]["to"] == "proven"


def test_a_quiet_day_in_the_middle_does_not_break_or_advance_the_streak():
    s = fresh()
    run(s, 0, "POSITIVE", n=40); run(s, 1, "POSITIVE", n=41)
    run(s, 2, "POSITIVE", n=41)                                 # quiet day: streak stays at 2
    st = s["strategies"][KEY]
    assert st["streak"] == {"verdict": "POSITIVE", "days": 2, "last_day": day(1), "last_n": 41}
    assert run(s, 3, "POSITIVE", n=44)[0]["to"] == "proven"


def test_fewer_trades_is_a_new_baseline_and_is_not_counted():
    s = fresh()
    run(s, 0, "POSITIVE", n=50)
    run(s, 1, "POSITIVE", n=30)                                 # another export filter: baseline 30, no count
    st = s["strategies"][KEY]
    assert st["streak"]["days"] == 1 and st["streak"]["last_n"] == 30
    run(s, 2, "POSITIVE", n=31)
    assert st["streak"]["days"] == 2


def test_a_cell_without_n_counts_as_before():
    s = fresh()
    for i in range(3):
        doc = rep(i, "POSITIVE")
        del doc["groups"]["strategy"][KEY]["n"]
        sr.update(s, doc, day(i))
    assert s["strategies"][KEY]["status"] == "proven"


# ----------------------------------------------------------------------------------------------- E590 round 565: count rows with a usable return
def test_trades_counted_prefers_n_ret_and_falls_back_to_n():
    assert sr.trades_counted({"n": 50, "n_ret": 44}) == 44
    assert sr.trades_counted({"n": 50}) == 50
    assert sr.trades_counted({"n": 50, "n_ret": None}) == 50
    assert sr.trades_counted({"n": 50, "n_ret": "x"}) == 50
    assert sr.trades_counted("junk") is None


def test_rows_without_a_readable_return_do_not_make_a_new_day():
    """n grows by one a day (a closed row with no entry price), n_ret stays at 40: the verdict has seen nothing new, so no day counts after the first."""
    s = fresh()
    for i in range(5):
        run(s, i, "POSITIVE", n=50 + i, n_ret=40)
    st = s["strategies"][KEY]
    assert st["status"] == "testing" and st["streak"]["days"] == 1 and st["streak"]["last_n"] == 40


def test_new_usable_trades_still_count_when_n_ret_is_present():
    s = fresh()
    for i in range(3):
        run(s, i, "POSITIVE", n=50 + i, n_ret=40 + i)
    assert s["strategies"][KEY]["status"] == "proven"


def test_drift_cell_n_is_already_the_usable_count():
    d = lambda i, n: {"generated_at": f"{day(i)}T12:00:00+00:00", "strategies": {KEY: {"state": "DECAY", "z": -3.0, "n": n}}}
    s = fresh()
    run(s, 0, "POSITIVE"); run(s, 1, "POSITIVE"); run(s, 2, "POSITIVE")
    assert s["strategies"][KEY]["status"] == "proven"
    for i in range(3, 6):
        sr.apply_drift(s, d(i, 60), day(i))                                           # same 60 trades each day
    assert s["strategies"][KEY]["status"] == "proven" and s["strategies"][KEY]["drift_streak"]["days"] == 1


# ----------------------------------------------------------------------------------------------- E591 round 566: newest closed time, not a count
def test_rolling_window_new_trades_count_even_when_the_count_falls():
    """The export keeps only the newest 60 days: each day 3 old trades fall out and 1 new one closes, so n goes DOWN while there is new evidence."""
    s = fresh()
    for i in range(3):
        run(s, i, "POSITIVE", n=50 - 2 * i, n_ret=50 - 2 * i, last_closed=f"{day(i)}T10:30:00")
    st = s["strategies"][KEY]
    assert st["status"] == "proven"
    assert st["streak"]["last_closed"] == f"{day(2)}T10:30:00"


def test_same_newest_closed_time_is_not_new_even_if_the_count_grows():
    s = fresh()
    for i in range(5):
        run(s, i, "POSITIVE", n=40 + i, n_ret=40 + i, last_closed=f"{day(0)}T10:30:00")     # window shifts, nothing new closed
    st = s["strategies"][KEY]
    assert st["status"] == "testing" and st["streak"]["days"] == 1


def test_an_older_newest_time_from_another_filter_never_lowers_the_baseline():
    s = fresh()
    run(s, 0, "POSITIVE", last_closed=f"{day(0)}T10:30:00")
    run(s, 1, "POSITIVE", n=10, n_ret=10, last_closed=f"{day(0)}T09:00:00")                 # another --since: older newest trade
    run(s, 2, "POSITIVE", n=90, n_ret=90, last_closed=f"{day(0)}T10:30:00")                 # back to the full set: still the same newest trade
    st = s["strategies"][KEY]["streak"]
    assert st["days"] == 1 and st["last_closed"] == f"{day(0)}T10:30:00"


def test_missing_last_closed_falls_back_to_the_count_rule():
    assert sr.new_since(None, None, 40, {"n": 41}) is True
    assert sr.new_since(None, None, 40, {"n": 40}) is False
    assert sr.new_since("2026-11-16T10:00:00", None, 40, {"n": 41}) is True
    assert sr.new_since("2026-11-16T10:00:00", "2026-11-16T10:00:00", 40, {"n": 90}) is False
    assert sr.new_since("2026-11-16T10:00:00", "2026-11-17T09:20:00", 40, {"n": 30}) is True


def test_drift_streak_uses_newest_closed_time_too():
    d = lambda i, n, lc: {"generated_at": f"{day(i)}T12:00:00+00:00", "strategies": {KEY: {"state": "DECAY", "z": -3.0, "n": n, "last_closed": lc}}}
    s = fresh()
    for i in range(3):
        run(s, i, "POSITIVE")
    assert s["strategies"][KEY]["status"] == "proven"
    for i in range(3, 6):
        sr.apply_drift(s, d(i, 60 - i, f"{day(i)}T11:00:00"), day(i))                       # count falls, newest trade is new each day
    assert s["strategies"][KEY]["status"] == "watch"


# ----------------------------------------------------------------------------------------------- E592 round 567: demoted -> testing on POSITIVE
def test_demoted_that_turns_positive_goes_back_to_testing_not_to_proven():
    assert sr.next_status("demoted", "POSITIVE", 3)[0] == "testing" and sr.next_status("demoted", "POSITIVE", 2)[0] == "demoted"
    s = fresh()
    for i in range(3):
        run(s, i, "NEGATIVE")
    assert s["strategies"][KEY]["status"] == "demoted"
    for i in range(3, 6):
        run(s, i, "POSITIVE")
    assert s["strategies"][KEY]["status"] == "testing"                                       # not proven yet
    for i in range(6, 9):
        run(s, i, "POSITIVE")
    assert s["strategies"][KEY]["status"] == "proven"                                        # three more new-trade days re-earn it
    assert [h["to"] for h in s["strategies"][KEY]["history"]] == ["demoted", "testing", "proven"]


def test_demoted_positive_days_without_new_trades_do_not_count():
    s = fresh()
    for i in range(3):
        run(s, i, "NEGATIVE")
    for i in range(3, 8):
        run(s, i, "POSITIVE", n=43)                                                          # the same trades every day
    assert s["strategies"][KEY]["status"] == "demoted"
