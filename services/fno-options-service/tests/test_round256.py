"""Round 256: a fetched rolling series is checked before a backtest trusts it (backtest/rolling_check.py, scripts/fno_rolling_check.py), and the rolling sim says which days it could not test.
No network. The sanity checks are exercised with files shaped like Dhan's reply (77 bars a weekday, 09:15 .. 15:35)."""
from __future__ import annotations

import importlib.util
import json
from datetime import date as D, datetime, timedelta
from pathlib import Path

from backtest import rolling_check, rolling_sim

ROOT = Path(__file__).resolve().parents[3]
IST = rolling_check.IST


def _doc(days, spot=24500.0, strike_fn=lambda s: round(s / 50) * 50, label="ATM", complete=True, frm=None, to=None, shift=0, bars=77, **kw):
    rows = []
    for d in days:
        t0 = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST).timestamp() + shift
        for i in range(bars):
            s = spot + (d.day % 5) * 40 + i
            rows.append({"t": t0 + 300 * i, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1.0, "oi": 5.0, "iv": 14.0, "spot": s, "strike": float(strike_fn(s))})
    doc = {"underlying": "NIFTY", "type": "CALL", "strike": label, "interval": 5, "expiry_flag": "WEEK", "expiry_code": 1, "rows": rows, "failed_windows": [], "empty_windows": [], "complete": complete,
           "from": (frm or days[0]).isoformat(), "to": (to or days[-1] + timedelta(days=1)).isoformat()}
    doc.update(kw)
    return doc


WEEK = [D(2026, 11, 2), D(2026, 11, 3), D(2026, 11, 4), D(2026, 11, 5), D(2026, 11, 6)]


def test_a_good_file_has_no_problems_and_reports_the_77_bar_day():
    rep = rolling_check.check_doc(_doc(WEEK))
    assert rep["problems"] == [] and rep["days"] == 5 and rep["bars_per_day_median"] == 77 and rep["first_bar"] == "09:15" and rep["last_bar"] == "15:35" and rep["after_close_bars"] == 10
    assert rep["spot_gap_steps_max"] <= 0.5 and rep["distinct_strikes"] > 1
    assert any("candles" in l for l in rolling_check.format_report(rep))


def test_timestamps_shifted_off_the_bar_grid_are_a_problem():
    rep = rolling_check.check_doc(_doc(WEEK, shift=19800))              # +5:30 added to the epoch: bars start 14:45 .. and are off the grid
    assert any("grid" in p or "before 09:15" in p for p in rep["problems"]) or rep["first_bar"] != "09:15"
    rep = rolling_check.check_doc(_doc(WEEK, shift=60))
    assert any("grid" in p for p in rep["problems"])


def test_weekend_duplicates_and_out_of_range_candles_are_problems():
    rep = rolling_check.check_doc(_doc([D(2026, 11, 6), D(2026, 11, 7)], frm=D(2026, 11, 6), to=D(2026, 11, 7)))
    assert any("weekend" in p for p in rep["problems"]) and any("outside the requested range" in p for p in rep["problems"])
    d = _doc(WEEK)
    d["rows"].append(dict(d["rows"][0]))
    assert any("duplicate" in p for p in rolling_check.check_doc(d)["problems"])


def test_an_atm_label_far_from_spot_or_stuck_is_a_problem():
    rep = rolling_check.check_doc(_doc(WEEK, strike_fn=lambda s: round(s / 50) * 50 + 150))
    assert any("steps from spot" in p for p in rep["problems"])
    rep = rolling_check.check_doc(_doc(WEEK, strike_fn=lambda s: 24500))
    assert any("never changes" in p or "steps from spot" in p for p in rep["problems"])
    assert rolling_check.check_doc(_doc(WEEK, strike_fn=lambda s: round(s / 50) * 50 + 100, label="ATM+2"))["problems"] == []      # only the ATM label must track the spot


def test_incomplete_empty_and_missing_days_are_reported():
    assert any("not complete" in p for p in rolling_check.check_doc(_doc(WEEK, complete=False, failed_windows=[["a", "b", "x"]]))["problems"])
    assert "no candles" in rolling_check.check_doc({"rows": [], "complete": True})["problems"]
    rep = rolling_check.check_doc(_doc([D(2026, 11, 2), D(2026, 11, 4)], frm=D(2026, 11, 2), to=D(2026, 11, 5)))
    assert rep["problems"] == [] and any("2026-11-03" in n for n in rep["notes"])
    assert not any("2026-11-03" in n for n in rolling_check.check_doc(_doc([D(2026, 11, 2), D(2026, 11, 4)], frm=D(2026, 11, 2), to=D(2026, 11, 5)), ["2026-11-03"])["notes"])
    thin = _doc(WEEK)
    thin["rows"] = [r for r in thin["rows"] if not (datetime.fromtimestamp(r["t"], IST).day == 4 and datetime.fromtimestamp(r["t"], IST).hour >= 11)]
    assert any("under 80%" in n for n in rolling_check.check_doc(thin)["notes"])


def test_the_script_reads_a_folder_and_sets_the_exit_code(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("fno_rolling_check", ROOT / "scripts" / "fno_rolling_check.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.main(["--dir", str(tmp_path / "none")]) == 2
    (tmp_path / "NIFTY_CALL_ATM_WEEK1_5m.json").write_text(json.dumps(_doc(WEEK)))
    assert m.main(["--dir", str(tmp_path)]) == 0
    (tmp_path / "NIFTY_PUT_ATM_WEEK1_5m.json").write_text(json.dumps(_doc(WEEK, shift=60, type="PUT")))
    assert m.main(["--dir", str(tmp_path)]) == 1
    assert "PROBLEM" in capsys.readouterr().out


def test_the_sim_lists_days_it_could_not_test(monkeypatch):
    from test_backtests import build_world
    from test_round246 import book_from, make_files
    from fno.params import Params
    daily, intr = build_world()
    days = sorted({rolling_sim._d(b["t"]) for b in intr})
    full = rolling_sim.run("NIFTY", daily, intr, book_from(make_files(intr)), p=Params(min_score=30, max_trades_per_day=2))
    assert full["uncovered_days"] == [] and full["days"] >= 2
    half = [b for b in intr if rolling_sim._d(b["t"]) != days[-1]]           # the last day was never fetched
    part = rolling_sim.run("NIFTY", daily, intr, book_from(make_files(half)), p=Params(min_score=30, max_trades_per_day=2))
    assert days[-1].isoformat() in part["uncovered_days"] and part["days"] == full["days"] - 1
    assert any("NOT tested" in n and days[-1].isoformat() in n for n in part["notes"])
