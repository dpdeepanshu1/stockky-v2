"""E485: three gaps of the NaN / inf class in gateway main.py that earlier rounds fixed in a twin and missed here.

(1) `_gw_quote_px` tested `px > 0` only, so +inf ("Infinity" in the JSON body, or 1e999) was returned as the price and kept in `_GW_BULK_PX_CACHE`.
    An infinite field now falls through to the next key, as a NaN already did.
(2) `_movers_rows_from_market_data` read the volume with `int(float(...))` inside `except (TypeError, ValueError)`; `int(float("inf"))` raises
    OverflowError, which went to the chunk-level `except` and dropped every row after it in that chunk of 50.
(3) `_movers_empty_backoff_sec`: MOVERS_EMPTY_BACKOFF_SEC=inf raised OverflowError out of the movers fetch.

No fastapi needed (functions lifted from main.py by AST). Runs with pytest or `python3 tests/test_e485_...py`."""
from __future__ import annotations

import ast
import asyncio
import logging
import math
import os
import time
import types
from datetime import datetime, timezone

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TREE = ast.parse(open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read())
INF = float("inf")


def _lift(names, ns):
    body = [n for n in _TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in body} == set(names), set(names) - {n.name for n in body}
    exec(compile(ast.Module(body=body, type_ignores=[]), "main.py", "exec"), ns)
    return ns


def _base_ns():
    from typing import Any, Dict, List, Optional
    ns = dict(math=math, os=os, time=time, asyncio=asyncio, logger=logging.getLogger("e485"), List=List, Dict=Dict, Optional=Optional, Any=Any,
              datetime=datetime, _dt_timezone=timezone, MARKET_DATA_URL="http://md.local", _GW_BULK_PX_CACHE={})
    _lift({"_gw_env_num", "_gw_quote_px", "_gw_bulk_row_fresh", "_hot_float"}, ns)
    return ns


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def test_an_infinite_price_field_falls_through_to_the_next_key():
    px = _base_ns()["_gw_quote_px"]
    assert px({"price": INF, "close": 50.0}) == 50.0
    assert px({"price": "Infinity", "close": "51.5"}) == 51.5
    assert px({"price": 1e999, "ltp": 52}) == 52.0
    assert px({"price": float("nan"), "last": 53}) == 53.0


def test_only_infinite_or_bad_fields_give_none():
    px = _base_ns()["_gw_quote_px"]
    assert px({"price": INF, "close": -INF, "ltp": float("nan"), "last": "x"}) is None
    assert px({}) is None and px(None) is None and px([]) is None
    assert px({"price": 0, "close": -5}) is None


def test_normal_quotes_are_unchanged():
    px = _base_ns()["_gw_quote_px"]
    assert px({"price": 123.45}) == 123.45
    assert px({"price": None, "close": "99"}) == 99.0
    assert px({"price": 1, "close": 2}) == 1.0


def test_bulk_first_never_stores_an_infinite_price():
    ns = _base_ns()
    ns["_gw_quotes_closed"] = lambda: False
    _lift({"_fetch_prices_bulk_first"}, ns)

    class Resp:
        status_code = 200

        def json(self):
            return {"quotes": [
                {"symbol": "AAA", "price": INF, "close": 40.0, "fetched_at": _now_iso()},
                {"symbol": "BBB", "price": INF, "fetched_at": _now_iso()},
                {"symbol": "CCC", "price": 10.0, "fetched_at": _now_iso()},
            ]}

    class Client:
        async def post(self, url, json=None, timeout=None):
            return Resp()

    out = asyncio.run(ns["_fetch_prices_bulk_first"](["AAA", "BBB", "CCC"], Client()))
    assert out == {"AAA": 40.0, "CCC": 10.0}
    assert set(ns["_GW_BULK_PX_CACHE"]) == {"AAA", "CCC"}
    assert all(math.isfinite(v[1]) for v in ns["_GW_BULK_PX_CACHE"].values())


def _movers(quotes):
    ns = _base_ns()

    class Resp:
        status_code = 200

        def json(self):
            return {"quotes": quotes}

    ns["httpx"] = types.SimpleNamespace(post=lambda url, json=None, timeout=None: Resp())
    _lift({"_movers_rows_from_market_data"}, ns)
    return ns["_movers_rows_from_market_data"]([q["symbol"] for q in quotes if isinstance(q, dict)])


def _q(sym, **kw):
    q = {"symbol": sym, "price": 110.0, "previous_close": 100.0, "volume": 1000, "day_high": 112.0, "day_low": 99.0, "fetched_at": _now_iso()}
    q.update(kw)
    return q


def test_an_infinite_volume_does_not_drop_the_rest_of_the_chunk():
    rows = _movers([_q("AAA"), _q("BAD", volume=INF), _q("CCC"), _q("DDD")])
    by = {r["symbol"]: r for r in rows}
    assert set(by) == {"AAA", "BAD", "CCC", "DDD"}
    assert by["BAD"]["volume"] == 0 and by["CCC"]["volume"] == 1000


def test_an_infinite_price_field_gives_a_finite_row_from_the_next_key():
    rows = _movers([_q("AAA", price=INF, close=108.0)])
    assert len(rows) == 1 and rows[0]["price"] == 108.0 and rows[0]["change"] == 8.0 and rows[0]["change_pct"] == 8.0
    assert all(math.isfinite(v) for k, v in rows[0].items() if k != "symbol")


def test_a_symbol_whose_only_price_is_infinite_is_left_out():
    assert _movers([_q("AAA", price=INF)]) == []


def test_a_normal_quote_gives_the_same_row():
    rows = _movers([_q("AAA", volume="2500")])
    assert rows == [{"symbol": "AAA", "price": 110.0, "change": 10.0, "change_pct": 10.0, "volume": 2500, "high": 112.0, "low": 99.0}]


def test_the_empty_backoff_setting_survives_junk_values():
    ns = {"os": os}
    _lift({"_movers_empty_backoff_sec"}, ns)
    f = ns["_movers_empty_backoff_sec"]
    saved = os.environ.get("MOVERS_EMPTY_BACKOFF_SEC")
    try:
        for raw, want in (("inf", 30), ("-inf", 30), ("nan", 30), ("abc", 30), ("-3", 30), ("", 30), ("0", 0), ("5", 5), ("12.9", 12)):
            os.environ["MOVERS_EMPTY_BACKOFF_SEC"] = raw
            assert f() == want, (raw, f())
    finally:
        if saved is None:
            os.environ.pop("MOVERS_EMPTY_BACKOFF_SEC", None)
        else:
            os.environ["MOVERS_EMPTY_BACKOFF_SEC"] = saved


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
