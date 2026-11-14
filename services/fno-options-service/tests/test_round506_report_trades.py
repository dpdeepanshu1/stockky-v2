"""Round 506 (E526): the replay's text report lists the day warnings and one line per trade (entry time, premiums, best / worst move, stop distance, hold time, exit reason, net)."""
from __future__ import annotations

from datetime import datetime

from backtest import report
from fno.provider import IST

T0 = datetime(2026, 11, 12, 10, 5, tzinfo=IST).timestamp()


def _trade(**k):
    t = {"entry_ts": T0, "exit_ts": T0 + 20 * 60, "underlying": "NIFTY", "opt_type": "CE", "strike": 25200.0, "strategy": "CORE", "signal_score": 61.4, "lots": 2, "entry_premium": 100.0,
         "exit_premium": 80.0, "max_premium": 112.0, "min_premium": 78.0, "sl_price": 80.0, "exit_reason": "SL", "net_pnl": -2650.5}
    t.update(k)
    return t


def _res(**k):
    r = {"kind": "REPLAY", "metrics": {"overall": {}}, "verdict": {"status": "NOT_PROVEN", "reasons": []}, "trades": [_trade()], "warnings": []}
    r.update(k)
    return r


def test_a_replay_report_lists_each_trade_with_its_best_worst_stop_and_hold():
    txt = report.render_text(_res())
    line = [x for x in txt.splitlines() if "25200" in x][0]
    assert "12 Nov 10:05" in line and "NIFTY" in line and "CE" in line and "CORE" in line and "+12%" in line and "-22%" in line and "-20%" in line and "20m" in line and "SL" in line and "-2650.50" in line
    assert txt.index("TRADES") < txt.index("VERDICT")


def test_day_warnings_are_printed_and_the_trade_list_is_capped_and_a_bad_row_does_not_stop_it():
    rows = [_trade(entry_ts=T0 + i) for i in range(70)] + [{"entry_ts": "x", "exit_ts": None, "entry_premium": 0, "max_premium": None}]
    txt = report.render_text(_res(trades=rows, warnings=["2026-11-12: recording starts 09:41, not at the open"]))
    assert "DAY WARNINGS" in txt and "recording starts 09:41" in txt and "11 more trades in the .json" in txt
    assert report.render_text(_res(trades=[{"entry_ts": "x", "entry_premium": 0, "max_premium": 1}])).count("TRADES") == 1      # zero entry premium, text time: dashes, no exception


def test_other_kinds_and_empty_results_get_no_trade_table():
    assert "TRADES" not in report.render_text(_res(kind="SIM"))
    assert "TRADES" not in report.render_text(_res(trades=[])) and "DAY WARNINGS" not in report.render_text(_res())
