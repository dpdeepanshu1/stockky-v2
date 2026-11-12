"""E474: `_resolve_quote_price` (the WebSocket quote push and the snapshot sent on subscribe) passed `price`, `close` and `change_pct` through
without a finite check. `ConnectionManager.broadcast` and the WS handler serialise with `json.dumps` (NaN allowed), so a NaN / inf value from
market-data (Python's JSON reader accepts `NaN`) or from yfinance's `fast_info` (a NaN is truthy, so `float(nan) or None` and `if price:` both
let it through) went out as the bare token `NaN`. That is not JSON: the browser's `JSON.parse` rejects the frame, so the symbol's quotes
were lost until the value cleared. The old `price or regularMarketPrice or close or last` chain also stopped at a NaN price and never reached a
valid `close` behind it. Now every number in the frame is finite and positive (`change_pct`: finite or None).

No fastapi needed (functions lifted from main.py by AST, run against fakes). Runs with pytest or `python3 tests/test_e474_ws_quote_finite.py`."""
from __future__ import annotations

import ast
import json
import logging
import math
import os
import types
from datetime import datetime, timezone, timedelta

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_IST = timezone(timedelta(hours=5, minutes=30))


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


def _build(md=None, fast_info=None, md_raises=None, direct_yf=True):
    """-> (resolve, calls). md / fast_info are what the fake market-data answer / yfinance fast_info return."""
    calls = {"yf": []}

    def get(url, timeout=None):
        if md_raises:
            raise md_raises
        return md if md is not None else _Resp(200, {})

    class Ticker:
        def __init__(self, t):
            calls["yf"].append(t)
            self.fast_info = fast_info if fast_info is not None else {}

    ns = {
        "math": math, "datetime": datetime, "IST": _IST, "logger": logging.getLogger("t"),
        "MARKET_DATA_URL": "http://md.local",
        "httpx": types.SimpleNamespace(get=get),
        "yf": types.SimpleNamespace(Ticker=Ticker),
        "resolve_ns_ticker": lambda s: s + ".NS",
        "_direct_yf_ok": lambda: direct_yf,
    }
    want = {"_ws_quote_num", "_resolve_quote_price"}
    for n in ast.parse(_SRC).body:
        if isinstance(n, ast.FunctionDef) and n.name in want:
            exec(compile(ast.Module(body=[n], type_ignores=[]), "main.py", "exec"), ns)
    assert want <= set(ns), want - set(ns)
    return ns["_resolve_quote_price"], ns["_ws_quote_num"], calls


def _strict(q):
    """The frame must survive a strict JSON round trip (what the browser's JSON.parse needs)."""
    return json.loads(json.dumps(q, allow_nan=False))


NAN, INF = float("nan"), float("inf")


def test_nan_price_falls_to_the_next_valid_key():
    r, _, calls = _build(md=_Resp(200, {"price": NAN, "close": 100.0}))
    q = r("TCS")
    assert q["price"] == 100.0 and q["source"] == "market-data" and calls["yf"] == []
    _strict(q)


def test_every_number_in_a_market_data_frame_is_finite():
    r, _, _ = _build(md=_Resp(200, {"price": 101.5, "close": NAN, "change_pct": NAN}))
    q = r("TCS")
    assert q["price"] == 101.5 and q["close"] == 101.5 and q["change_pct"] is None
    _strict(q)
    r, _, _ = _build(md=_Resp(200, {"price": 101.5, "close": INF, "change_pct": -INF}))
    q = r("TCS")
    assert q["close"] == 101.5 and q["change_pct"] is None
    _strict(q)


def test_nan_and_inf_only_prices_fall_through_to_yfinance():
    for bad in (NAN, INF, -INF, -5.0, "NaN", "inf"):
        r, _, calls = _build(md=_Resp(200, {"price": bad}), fast_info={"last_price": 3500.0})
        q = r("TCS")
        assert q["source"] == "yfinance_fast" and q["price"] == 3500.0, bad
        _strict(q)


def test_nan_yfinance_price_gives_none_not_a_nan_frame():
    for bad in (NAN, INF, -INF, -1.0):
        r, _, _ = _build(md_raises=RuntimeError("down"), fast_info={"last_price": bad})
        assert r("TCS") is None, bad


def test_yfinance_nan_last_price_still_reaches_a_valid_camel_case_key():
    r, _, _ = _build(md_raises=RuntimeError("down"), fast_info={"last_price": NAN, "lastPrice": 12.5})
    q = r("TCS")
    assert q["price"] == 12.5 and q["close"] == 12.5
    _strict(q)


def test_normal_frames_are_unchanged():
    r, _, calls = _build(md=_Resp(200, {"price": "101.5", "close": 100, "change_pct": 1.5,
                                        "as_of": "2026-10-01T10:00:00+05:30", "source": "nse"}))
    assert r("TCS") == {"symbol": "TCS", "price": 101.5, "close": 100, "change_pct": 1.5,
                        "as_of": "2026-10-01T10:00:00+05:30", "source": "nse"}
    r, _, _ = _build(md=_Resp(200, {"price": 0, "regularMarketPrice": 5}))
    assert r("TCS")["price"] == 5.0
    r, _, _ = _build(md=_Resp(200, {"price": 101.5}))
    q = r("TCS")
    assert q["close"] == 101.5 and q["change_pct"] is None and q["source"] == "market-data"
    r, _, _ = _build(md_raises=RuntimeError("down"), fast_info={"last_price": 3500.0})
    q = r("TCS")
    assert set(q) == {"symbol", "price", "close", "as_of", "source"} and q["price"] == 3500.0


def test_a_negative_change_pct_is_kept():
    r, _, _ = _build(md=_Resp(200, {"price": 90.0, "change_pct": -2.25}))
    assert r("TCS")["change_pct"] == -2.25


def test_helper_table():
    _, num, _ = _build()
    assert num("1.5") == 1.5 and num(3) == 3.0 and num(0) == 0.0 and num(-2) == -2.0
    assert num(0, positive=True) is None and num(-2, positive=True) is None and num(2, positive=True) == 2.0
    for bad in (None, True, False, "x", "", NAN, INF, -INF, "nan", [], {}):
        assert num(bad) is None, bad


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print("ok", _n)
