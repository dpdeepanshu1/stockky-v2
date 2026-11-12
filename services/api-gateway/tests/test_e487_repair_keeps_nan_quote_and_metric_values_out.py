"""E487: Repair (`_patch_single_stock_feed_impl`) stored upstream values as sent for four quote fields, `fundamental_score` and the `metrics` dict.

Python's JSON reader accepts `NaN` / `Infinity`, so a market-data or fundamental reply can carry them. The price step copied `previous_close`, `day_high`,
`day_low` and `day_change_pct` with only a None filter, the fundamental step stored `fundamental_score` as sent and merged `metrics` (minus None) over the
stored metrics, so a NaN replaced a good stored value (repair promises never to wipe one) and `instant_scanner._f` passes NaN through (E475 fixed the same
for the technical extras). Now `_repair_apply_quote_extras` (finite numbers only, numeric text parsed, type kept) and `_repair_clean_metrics` (no None,
no NaN / inf float) do it, and `fundamental_score` is read with `_scan_num`.

No fastapi needed (functions lifted from main.py by AST). Runs with pytest or `python3 tests/test_e487_...py`."""
from __future__ import annotations

import ast
import math
import os

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_TREE = ast.parse(_SRC)
NAN, INF = float("nan"), float("inf")


def _fns():
    names = {"_hot_float", "_scan_num", "_repair_apply_quote_extras", "_repair_clean_metrics"}
    body = [n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in body} == names, names - {n.name for n in body}
    ns = dict(math=math)
    exec(compile(ast.Module(body=body, type_ignores=[]), "main.py", "exec"), ns)
    return ns


def test_nan_inf_and_text_quote_extras_leave_the_stored_value_alone():
    apply = _fns()["_repair_apply_quote_extras"]
    row = {"previous_close": 100.0, "day_high": 105.0, "day_low": 99.0, "day_change_pct": 1.5}
    apply(row, {"previous_close": NAN, "day_high": INF, "day_low": "n/a", "day_change_pct": -INF})
    assert row == {"previous_close": 100.0, "day_high": 105.0, "day_low": 99.0, "day_change_pct": 1.5}
    empty = {}
    apply(empty, {"previous_close": NAN, "day_high": 1e999})
    assert empty == {}


def test_finite_quote_extras_are_stored_with_their_type_and_text_is_parsed():
    apply = _fns()["_repair_apply_quote_extras"]
    row = {"day_high": 105.0}
    apply(row, {"previous_close": 100, "day_high": "107.5", "day_low": 0.0, "day_change_pct": -2.25, "volume": 5})
    assert row == {"day_high": 107.5, "previous_close": 100, "day_low": 0.0, "day_change_pct": -2.25}
    assert isinstance(row["previous_close"], int)


def test_quote_extras_never_raise_on_a_bad_quote():
    apply = _fns()["_repair_apply_quote_extras"]
    row = {"a": 1}
    apply(row, None)
    apply(row, {"previous_close": [1], "day_high": {"x": 1}})
    assert row == {"a": 1}


def test_metrics_merge_cannot_put_nan_over_a_real_value():
    clean = _fns()["_repair_clean_metrics"]
    stored = {"roe": 14.2, "debt_equity": 0.4}
    merged = {**stored, **clean({"roe": NAN, "debt_equity": 0.5, "pb": INF, "ps": None, "eps": 12, "note": "ok"})}
    assert merged == {"roe": 14.2, "debt_equity": 0.5, "eps": 12, "note": "ok"}
    assert clean(None) == {} and clean([1]) == {}


def test_repair_uses_the_helpers_and_reads_fundamental_score_safely():
    fn = next(n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_patch_single_stock_feed_impl")
    src = ast.get_source_segment(_SRC, fn)
    assert "_repair_apply_quote_extras(current, cleaned)" in src
    assert "_repair_clean_metrics(metrics)" in src
    assert '_scan_num(f.get("fundamental_score"))' in src
    assert "current[ok] = cleaned[ok]" not in src


if __name__ == "__main__":
    import traceback
    failed = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("ok  ", _n)
            except Exception:
                failed += 1
                print("FAIL", _n)
                traceback.print_exc()
    raise SystemExit(1 if failed else 0)
