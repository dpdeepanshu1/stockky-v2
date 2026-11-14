"""E530 (round 511): scripts/fno_rolling_index_candles.py builds the 5-minute index file from the rolling files' spot (no network)."""
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fno_rolling_index_candles as R  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def _t(day, hh, mm):
    return datetime(2026, 11, day, hh, mm, tzinfo=IST).timestamp()


def _write(folder, typ, rows, complete=True):
    name = f"NIFTY_{typ}_ATM_WEEK1_5m.json"
    (folder / name).write_text(json.dumps({"type": typ, "strike": "ATM", "interval": 5, "complete": complete, "rows": rows}))


def test_bars_chain_open_to_the_previous_close_and_drop_the_after_close_rows(tmp_path):
    _write(tmp_path, "CALL", [{"t": _t(12, 9, 15), "spot": 100.0}, {"t": _t(12, 9, 20), "spot": 103.0}, {"t": _t(12, 9, 25), "spot": 101.0}, {"t": _t(12, 15, 30), "spot": 90.0}, {"t": _t(13, 9, 15), "spot": 105.0}])
    bars, notes = R.build(str(tmp_path))
    assert [b["date"] for b in bars] == ["2026-11-12 09:15", "2026-11-12 09:20", "2026-11-12 09:25", "2026-11-13 09:15"] and notes == ["no PUT ATM file for NIFTY (WEEK1, 5m) in " + str(tmp_path)]
    assert (bars[0]["open"], bars[0]["close"]) == (100.0, 100.0)                          # the first candle of a day opens at its own spot
    assert (bars[1]["open"], bars[1]["high"], bars[1]["low"], bars[1]["close"]) == (100.0, 103.0, 100.0, 103.0)
    assert (bars[2]["open"], bars[2]["high"], bars[2]["low"], bars[2]["close"]) == (103.0, 103.0, 101.0, 101.0)
    assert bars[3]["open"] == 105.0                                                         # a new day does not chain to yesterday's close


def test_the_put_file_fills_a_candle_the_call_file_lacks_and_an_incomplete_file_is_noted(tmp_path):
    _write(tmp_path, "CALL", [{"t": _t(12, 9, 15), "spot": 100.0}, {"t": _t(12, 9, 25), "spot": 102.0}], complete=False)
    _write(tmp_path, "PUT", [{"t": _t(12, 9, 20), "spot": 101.0}, {"t": _t(12, 9, 25), "spot": 999.0}, {"t": _t(12, 9, 30), "spot": None}])
    bars, notes = R.build(str(tmp_path))
    assert [b["close"] for b in bars] == [100.0, 101.0, 102.0] and any("not complete" in n for n in notes)       # the CALL spot wins where both have the candle


def test_main_writes_a_csv_the_backtest_loader_reads_and_exits_1_with_nothing_to_write(tmp_path):
    _write(tmp_path, "CALL", [{"t": _t(12, 9, 15), "spot": 100.0}, {"t": _t(12, 9, 20), "spot": 103.0}])
    out = tmp_path / "x" / "nifty_5m.csv"
    assert R.main(["--rolling-dir", str(tmp_path), "--out", str(out)]) == 0
    rows = list(csv.DictReader(open(out)))
    assert rows[1]["date"] == "2026-11-12 09:20" and float(rows[1]["close"]) == 103.0 and list(rows[0]) == ["date", "open", "high", "low", "close", "volume"]
    assert R.main(["--rolling-dir", str(tmp_path / "empty"), "--out", str(tmp_path / "y.csv")]) == 1 and not (tmp_path / "y.csv").exists()
