"""E473: `_get_nifty50_data` built each yfinance movers row from `hist.iloc[-1]["Close"]` with no finite check. yfinance's newest 1-minute bar
often has a NaN close; that NaN became the row's price / change / change_pct. `_rank_market_rows` skips a NaN change_pct for gainers / losers
but ranks most-active on volume, so the NaN row was returned, FastAPI could not JSON-encode it (HTTP 500 on /market/most-active), and the list is
cached for the whole day. Now `_movers_row_from_history` uses the last FINITE close and finite high / low / volume.
No fastapi needed (function lifted from main.py by AST; real pandas). Runs with pytest or `python3 tests/test_e473_movers_row_nan_close.py`."""
from __future__ import annotations

import ast
import json
import math
import os
import types

import pandas as pd

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()


def _ns():
    want = {"_hot_float", "_safe_pct", "_movers_row_from_history"}
    ns = {"math": math}
    for n in ast.parse(_SRC).body:
        if isinstance(n, ast.FunctionDef) and n.name in want:
            exec(compile(ast.Module(body=[n], type_ignores=[]), "main.py", "exec"), ns)
    missing = want - set(ns)
    assert not missing, missing
    return ns["_movers_row_from_history"]


def _ticker(prev=None):
    return types.SimpleNamespace(fast_info={"previous_close": prev} if prev is not None else {})


def _frame(closes, vol=None, high=None, low=None, open_=None):
    d = {"Close": closes, "Volume": vol or [1000.0] * len(closes), "High": high or [c + 1 if c == c else float("nan") for c in closes],
         "Low": low or [c - 1 if c == c else float("nan") for c in closes]}
    if open_ is not None:
        d["Open"] = open_
    return pd.DataFrame(d)


def test_a_nan_last_close_uses_the_last_finite_close_and_the_row_is_json_safe():
    f = _ns()
    row = f("AAA", _frame([100.0, 102.0, float("nan")]), _ticker(prev=100.0))
    assert row["price"] == 102.0 and row["change"] == 2.0 and row["change_pct"] == 2.0
    json.dumps(row, allow_nan=False)


def test_old_code_would_have_produced_a_nan_row_here():
    hist = _frame([100.0, 102.0, float("nan")])
    latest = hist.iloc[-1]
    assert math.isnan(latest["Close"])                       # what the removed inline code rounded into price / change / change_pct


def test_no_finite_close_gives_no_row():
    f = _ns()
    assert f("AAA", _frame([float("nan"), float("nan")]), _ticker(prev=100.0)) is None
    assert f("AAA", pd.DataFrame({"Open": [1.0]}), _ticker(prev=100.0)) is None


def test_previous_close_sources_and_refusals():
    f = _ns()
    assert f("A", _frame([110.0, 110.0]), _ticker(prev=100.0))["change_pct"] == 10.0                  # fast_info first
    assert f("A", _frame([110.0, 110.0], open_=[105.0, 106.0]), _ticker())["change_pct"] == round(5 / 105 * 100, 2)   # then the first bar's open
    assert f("A", _frame([110.0, 111.0]), _ticker())["change"] == 1.0                                 # then the first close
    assert f("A", _frame([110.0, 111.0]), _ticker(prev=float("nan")))["change"] == 1.0                # a NaN fast_info value is not a previous close
    assert f("A", _frame([0.0, 0.0]), _ticker()) is None


def test_high_low_volume_are_finite_and_missing_columns_are_tolerated():
    f = _ns()
    row = f("A", _frame([100.0, 101.0], vol=[5.0, 7.0], high=[103.0, 104.0], low=[99.0, 98.0]), _ticker(prev=100.0))
    assert row["volume"] == 12 and row["high"] == 104.0 and row["low"] == 98.0
    row = f("A", pd.DataFrame({"Close": [100.0, 101.0]}), _ticker(prev=100.0))
    assert row["volume"] == 0 and row["high"] == 101.0 and row["low"] == 101.0
    row = f("A", _frame([100.0, 101.0], high=[float("nan")] * 2, low=[float("nan")] * 2), _ticker(prev=100.0))
    assert row["high"] == 101.0 and row["low"] == 101.0
    json.dumps(row, allow_nan=False)


def test_a_normal_frame_gives_the_same_row_the_old_inline_code_gave():
    f = _ns()
    hist = _frame([100.0, 101.0, 103.5], vol=[10, 20, 30], high=[101.0, 102.0, 104.0], low=[99.0, 100.0, 102.0])
    row = f("AAA", hist, _ticker(prev=100.0))
    assert row == {"symbol": "AAA", "price": 103.5, "change": 3.5, "change_pct": 3.5, "volume": 60, "high": 104.0, "low": 99.0}


def test_fetch_one_uses_the_helper():
    assert "return _movers_row_from_history(sym, hist, ticker)   # E473" in _SRC
    assert 'latest["Close"]' not in _SRC


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"{len(tests)} passed")
