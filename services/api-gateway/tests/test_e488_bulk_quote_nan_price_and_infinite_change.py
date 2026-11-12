"""E488: two NaN / inf gaps behind the momentum-movers fallback seed scan.

(1) `data_feed.bulk_yahoo_download_prices` read the price as `float(q.get("price") or q.get("cmp") or 0)`. A NaN price (Python's JSON reader accepts
`NaN`) is truthy, and `nan <= 0` / `nan > cap` are both False, so the row passed both checks with price NaN instead of falling back to a valid `cmp`
or being skipped. Now the first FINITE positive of price / cmp is used, and a junk `price` next to it is replaced by that value in the record.
(2) `_compute_momentum_movers` step 4 added a symbol when `abs(day_change_pct) >= 5`; an infinite change (previous close 0) passed. It needs a finite one.

Runs with pytest or `python3 tests/test_e488_...py` (uses the real `data_feed` module with a fake httpx).
"""
from __future__ import annotations

import ast
import math
import os
import sys
import types

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)
NAN, INF = float("nan"), float("inf")


def _run(quotes, symbols):
    import data_feed as df

    class Resp:
        status_code = 200
        content = b"x"
        text = ""

        def json(self):
            return {"quotes": quotes}

    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = types.SimpleNamespace(post=lambda url, json=None, timeout=None: Resp())
    try:
        return df.bulk_yahoo_download_prices(symbols)
    finally:
        if saved is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = saved


def test_a_nan_price_falls_back_to_a_valid_cmp_and_the_record_carries_it():
    out = _run([{"symbol": "AAA", "price": NAN, "cmp": 42.5}], ["AAA"])
    assert out["AAA"]["source"] == "yahoo_bulk" and out["AAA"]["price"] == 42.5


def test_a_nan_or_infinite_price_without_a_usable_cmp_is_a_placeholder():
    out = _run([{"symbol": "AAA", "price": NAN}, {"symbol": "BBB", "price": INF, "cmp": NAN}, {"symbol": "CCC", "price": 10}], ["AAA", "BBB", "CCC"])
    assert out["AAA"]["source"] == "yahoo_missing" and out["BBB"]["source"] == "yahoo_missing"
    assert out["CCC"]["source"] == "yahoo_bulk" and out["CCC"]["price"] == 10


def test_a_zero_price_with_a_valid_cmp_uses_the_cmp():
    out = _run([{"symbol": "AAA", "price": 0, "cmp": "20"}], ["AAA"])
    assert out["AAA"]["source"] == "yahoo_bulk" and out["AAA"]["price"] == 20.0


def test_normal_records_are_unchanged():
    out = _run([{"symbol": "AAA", "price": 100.0, "fetched_at": "T0"}, {"symbol": "BBB", "cmp": "50"}], ["AAA", "BBB"])
    assert out["AAA"]["price"] == 100.0 and out["AAA"]["price_refreshed_at"] == "T0"
    assert "price" not in out["BBB"]            # only cmp was sent; nothing is invented


def test_momentum_step4_needs_a_finite_change():
    src = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_compute_momentum_movers")
    seg = ast.get_source_segment(src, fn)
    assert "math.isfinite(day_chg) and abs(day_chg) >= 5.0" in seg


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
