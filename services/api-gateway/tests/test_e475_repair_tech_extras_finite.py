"""E475: the Repair worker's technical step stored `ema20`, `technical_score` and `macd_hist` as the technical service sent them. A NaN went
into the stored row and `instant_scanner._f` passes it through, so the symbol's instant score came out NaN; `t.get("macd_hist", t.get("macd"))`
stored None for `macd_hist: null` beside a valid `macd`. Now `_apply_repair_tech_extras` takes finite numbers only.
No fastapi needed (functions lifted from main.py by AST). Runs with pytest or `python3 tests/test_e475_repair_tech_extras_finite.py`."""
from __future__ import annotations

import ast
import math
import os

_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
NAN, INF = float("nan"), float("inf")


def _fn():
    ns = {"math": math}
    for n in ast.parse(_SRC).body:
        if isinstance(n, ast.FunctionDef) and n.name in {"_hot_float", "_scan_num", "_apply_repair_tech_extras"}:
            exec(compile(ast.Module(body=[n], type_ignores=[]), "main.py", "exec"), ns)
    return ns["_apply_repair_tech_extras"]


def test_normal_reply_is_copied():
    row = {}
    _fn()(row, {"ema20": 101.5, "technical_score": 64, "macd_hist": -0.4})
    assert row == {"ema20": 101.5, "technical_score": 64, "macd_hist": -0.4}


def test_nan_and_inf_are_not_stored_and_leave_existing_values():
    row = {"ema20": 90.0, "macd_hist": 0.2}
    _fn()(row, {"ema20": NAN, "technical_score": INF, "macd_hist": NAN, "macd": INF})
    assert row == {"ema20": 90.0, "macd_hist": 0.2}


def test_null_macd_hist_falls_to_macd():
    row = {}
    _fn()(row, {"macd_hist": None, "macd": 0.7})
    assert row["macd_hist"] == 0.7
    row = {}
    _fn()(row, {"macd_hist": NAN, "macd": 0.3})
    assert row["macd_hist"] == 0.3


def test_technical_score_only_fills_an_empty_slot():
    row = {"technical_score": 40}
    _fn()(row, {"technical_score": 80})
    assert row["technical_score"] == 40
    row = {"technical_score": None}
    _fn()(row, {"technical_score": "80"})
    assert row["technical_score"] == 80


def test_junk_inputs_do_not_raise():
    f = _fn()
    f(None, {}); f({}, None); f({}, {"ema20": "x", "macd": [], "technical_score": True})
    row = {}
    f(row, {"ema20": "x", "macd": [], "technical_score": True})
    assert row == {}


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f(); print("ok", _n)
