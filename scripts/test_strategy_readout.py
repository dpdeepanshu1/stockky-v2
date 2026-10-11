"""group322: strategy_readout.entry_window_verdict."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_readout as S


def b(n, e):
    return {"trades": n, "expectancy": e}


def test_sets_window_when_early_trades_lose_and_later_ones_do_better():
    r = S.entry_window_verdict({"09:00": b(20, -5.0), "10:00": b(15, -3.0), "10:30": b(40, 2.0), "11:00": b(30, 1.0)})
    assert r["before"] == {"trades": 35, "expectancy": -4.14} and r["verdict"] == "SET OPENING_ENTRY_NOT_BEFORE_IST=10:30"


def test_keeps_when_early_trades_make_money():
    r = S.entry_window_verdict({"09:00": b(40, 1.0), "10:30": b(40, 2.0)})
    assert r["verdict"] == "KEEP THE CURRENT WINDOW"


def test_keeps_when_early_loses_but_not_worse_than_later():
    assert S.entry_window_verdict({"09:00": b(40, -1.0), "11:00": b(40, -2.0)})["verdict"] == "KEEP THE CURRENT WINDOW"


def test_not_enough_data():
    assert S.entry_window_verdict({"09:00": b(5, -9.0), "11:00": b(80, 3.0)})["verdict"] == "NOT ENOUGH DATA"
    assert S.entry_window_verdict({})["verdict"] == "NOT ENOUGH DATA"


def test_hourly_bucket_straddling_cutoff_is_left_out():
    r = S.entry_window_verdict({"09": b(40, -2.0), "10": b(99, 9.0), "11": b(40, 1.0)})
    assert r["left_out_straddling"] == ["10"] and r["before"]["trades"] == 40 and r["after"]["trades"] == 40


def test_bad_rows_are_ignored():
    r = S.entry_window_verdict({"x": b(5, 1), "09:00": {"trades": 0, "expectancy": 1}, "10:30": "nope", "09:30": {"trades": 3}})
    assert r["before"]["trades"] == 0 and r["after"]["trades"] == 0
