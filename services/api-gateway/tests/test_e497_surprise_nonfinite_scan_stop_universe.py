"""E497: a read of `gateway_support/bound_routes_c.py` in full, plus the `surprise_scanner.score_stock` it serves through `/surprise/scan`.

1. `score_stock` took tick / static values as sent. NaN / inf is truthy and passes every `> 0` test, so an infinite price made a full "breakout" row
   (price / change_pct / target_1 inf) and a NaN open / high / low / vwap / volume / buy share put NaN into the row; text raised ValueError (and
   `scan()` does not guard the call, so one bad tick would end the whole scan). Non-finite or unreadable values now count as missing.
2. `GET /surprise/scan` returned the engine result as is: any NaN / inf in a row (or in the durable last result, which can still hold the bare tokens)
   made the response itself fail (500) for every caller. The result is sanitised (NaN / inf become null), as `/hot-stocks` and the WebSocket frames do.
3. `POST /data-feed/stop` called the durable store (`job`, `set_job`, `set_meta`: 4-5 DB round trips) straight from the event loop, at the moment a feed
   run is saturating the DB. They run in a worker thread now (same order, same values).
4. `GET /universe`, training-service fallback: a dict / text under `symbols` was iterated into keys / characters and a null name became "None".

No fastapi / httpx needed (handlers lifted from the source by AST, the real `surprise_scanner` is imported with a stand-in httpx when it is missing).
Runs with pytest or `python3 tests/test_e497_surprise_nonfinite_scan_stop_universe.py`.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import math
import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)
try:                                    # noqa: SIM105
    import httpx  # noqa: F401
except Exception:                       # the sandbox has no httpx; surprise_scanner only needs the name at import time
    _m = types.ModuleType("httpx")
    _m.AsyncClient = _m.Client = object
    for _n in ("HTTPError", "TimeoutException", "ConnectError", "ReadTimeout", "HTTPStatusError", "RequestError"):
        setattr(_m, _n, type(_n, (Exception,), {}))
    sys.modules["httpx"] = _m

import surprise_scanner as ss                          # noqa: E402
from json_safe import sanitize                         # noqa: E402

NAN, INF = float("nan"), float("inf")
IST = timezone(timedelta(hours=5, minutes=30))
_BRC = ast.parse(open(os.path.join(_SERVICE, "gateway_support", "bound_routes_c.py"), encoding="utf-8").read())


def _handler(name, ref):
    build = next(n for n in _BRC.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_c")
    fn = next(n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    fn.decorator_list = []
    ns = {"ref": ref}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "bound_routes_c.py", "exec"), ns)
    return ns[name]


def _strict(obj):
    return json.dumps(obj, allow_nan=False)


# ── 1. score_stock ───────────────────────────────────────────────────────────

_STATIC = {"prev_close": 100.0, "avg_15m_volume": 100000, "daily_atr": 2.0, "dist_52w_pct": 1.0, "sector": "X"}
_TICK = {"price": 104, "open": 100, "high": 105, "low": 100, "vol_15m": 400000, "buy_pct": 80}


def _score(tick=None, **static_over):
    eng = ss.SurpriseStockEngine()
    eng.static_cache = {"ABC": {**_STATIC, **static_over}}
    return eng.score_stock("ABC", dict(_TICK if tick is None else tick))


def _finite_row(row):
    return all((not isinstance(v, float)) or math.isfinite(v) for v in row.values())


def test_good_tick_row_is_unchanged():
    row = _score()
    assert row["tier"] == "breakout" and row["score"] == 100 and row["price"] == 104.0 and row["rvol"] == 4.0
    assert row["buy_pct"] == 80.0 and row["change_pct"] == 4.0 and row["target_1"] == 109.2


def test_an_infinite_price_no_longer_makes_a_breakout_row():
    assert _score({**_TICK, "price": INF}) is None          # old code: a row with price / change_pct / target_1 / cmp inf
    assert _score({**_TICK, "price": NAN}) is None


def test_non_finite_or_text_tick_fields_count_as_missing_and_the_row_stays_finite():
    for key in ("open", "high", "low", "vol_15m", "buy_pct", "vwap", "orb_high"):
        for bad in (NAN, INF, "n/a"):
            row = _score({**_TICK, key: bad})               # old code: NaN / inf in the row, or ValueError for the text
            assert row is not None and _finite_row(row), (key, bad)
            _strict(row)


def test_an_unreadable_bid_quantity_leaves_the_buy_share_neutral():
    tick = {k: v for k, v in _TICK.items() if k != "buy_pct"}
    for bad in (NAN, INF, "n/a"):
        row = _score({**tick, "total_bid_qty": bad, "total_ask_qty": 10})
        assert row is not None and row["buy_pct"] == 50.0 and _finite_row(row), bad


def test_non_finite_static_values_do_not_raise_or_reach_the_row():
    for key, bad in (("avg_15m_volume", NAN), ("avg_15m_volume", INF), ("daily_atr", NAN), ("dist_52w_pct", NAN), ("dist_52w_pct", INF)):
        row = _score(**{key: bad})                          # old code: ValueError (avg volume) / NaN in dist_52w_pct and avg_traded_value
        assert row is None or _finite_row(row), (key, bad)
    assert _score(prev_close=NAN) is None                   # no usable previous close and a live price: the change cannot be computed


def test_a_real_zero_is_still_a_zero():
    # dist_52w_pct 0 = trading AT the 52-week high; a supplied vwap of 0 is used as given (both used to be read with plain float()).
    assert _score(dist_52w_pct=0)["trigger_type"] == "Near 52W High"
    assert _score({**_TICK, "vwap": 0})["trailing_stop"] == 0.0


# ── 2. /surprise/scan response ───────────────────────────────────────────────

def _scan_ref(result, closed=None):
    async def closed_fn(engine, limit):
        return closed

    def start(engine, *, client, sym_list, force_reload, cached):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(result)
        return fut

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code, self.detail = status_code, detail

    return types.SimpleNamespace(
        _surprise_closed_market_result=closed_fn, _get_http_client=lambda: None, _start_surprise_scan=start, asyncio=asyncio,
        SURPRISE_SCAN_DEADLINE_S=5.0, HTTPException=HTTPException, logger=logging.getLogger("e497"), time=time, _json_sanitize=sanitize)


def _call_scan(ref, **kw):
    handler = _handler("api_surprise_scan", ref)
    args = dict(force_reload=False, symbols=None, cached=False, limit=None, refresh=False)
    args.update(kw)
    return asyncio.run(handler(**args))


def test_a_nan_in_a_scored_row_no_longer_fails_the_scan_response():
    result = {"count": 2, "stocks": [{"symbol": "A", "score": 70, "rvol": NAN, "price": 10.0}, {"symbol": "B", "score": 60, "price": INF}], "elapsed_sec": 1.5}
    out = _call_scan(_scan_ref(result), refresh=True)
    _strict(out)                                            # old code: ValueError (the response encoder refuses NaN / inf)
    assert out["stocks"][0]["rvol"] is None and out["stocks"][1]["price"] is None and out["stocks"][0]["price"] == 10.0


def test_limit_and_clean_results_are_unchanged():
    result = {"count": 3, "stocks": [{"symbol": s, "score": 60 + i} for i, s in enumerate("ABC")], "from_cache": False}
    assert _call_scan(_scan_ref(result), refresh=True) == result
    out = _call_scan(_scan_ref(result), refresh=True, limit=2)
    assert [r["symbol"] for r in out["stocks"]] == ["A", "B"] and out["count"] == 3
    assert _call_scan(_scan_ref(result), refresh=True, limit=0)["stocks"] == []


def test_the_closed_market_result_is_sanitised_too():
    closed = {"stocks": [{"symbol": "A", "score": NAN}], "closed": True}
    out = _call_scan(_scan_ref({"stocks": []}, closed=closed))
    assert out == {"stocks": [{"symbol": "A", "score": None}], "closed": True}
    _strict(out)


# ── 3. /data-feed/stop: durable calls off the event loop ─────────────────────

class _Store:
    def __init__(self, job):
        self.job_val = dict(job)
        self.threads = []
        self.calls = []

    def job(self):
        self.threads.append(threading.get_ident())
        self.calls.append("job")
        return dict(self.job_val)

    def set_job(self, **kw):
        self.threads.append(threading.get_ident())
        self.calls.append("set_job:" + str(kw.get("status")))
        self.job_val.update(kw)
        return dict(self.job_val)

    def set_meta(self, **kw):
        self.threads.append(threading.get_ident())
        self.calls.append("set_meta")


def _stop(job, **kw):
    store = _Store(job)
    stops = []
    ref = types.SimpleNamespace(
        _feed_store=lambda: store, request_data_feed_stop=lambda: stops.append(1), asyncio=asyncio, datetime=datetime, IST=IST,
        logger=logging.getLogger("e497"), _safe_int=lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0)
    handler = _handler("data_feed_stop", ref)

    async def go():
        loop_thread = threading.get_ident()
        return loop_thread, await handler(**kw)

    loop_thread, out = asyncio.run(go())
    return store, stops, loop_thread, out


def test_stop_runs_every_durable_call_in_a_worker_thread():
    job = {"status": "running", "processed": 3, "total": 10, "ok_count": 4, "error_count": 1,
           "checkpoint": {"cursor": 6, "done": ["A", "B", "C", "D", "E"], "universe": ["A", "B"]}}
    store, stops, loop_thread, out = _stop(job)
    assert store.threads and all(t != loop_thread for t in store.threads)           # old code: every call ran on the loop thread
    assert store.calls == ["job", "set_job:stopping", "job", "set_meta", "set_job:stopped"]          # same order as before
    assert stops == [1] and out["ok"] is True and out["stopped"] is True and out["processed"] == 6 and out["ok_count"] == 5


def test_stop_of_an_idle_job_is_still_a_no_op():
    store, stops, loop_thread, out = _stop({"status": "done"})
    assert out["ok"] is True and out["stopped"] is False and "Not running" in out["detail"]
    assert store.calls == ["job"] and stops == []
    assert all(t != loop_thread for t in store.threads)


# ── 4. /universe training-service fallback ───────────────────────────────────

def _universe(body, monkeypatch_modules=True):
    built = ["BUILT1"]

    class _Resp:
        status_code = 200

        def json(self):
            return body

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=None):
            return _Resp()

    fake_df = types.ModuleType("data_feed")
    fake_df.list_feed_symbols_from_neon_under_max_price = lambda cap: []
    saved = sys.modules.get("data_feed")
    sys.modules["data_feed"] = fake_df
    try:
        ref = types.SimpleNamespace(
            asyncio=asyncio, List=list, MAX_UNIVERSE_PRICE=5000, logger=logging.getLogger("e497"), DECISION_URL="http://dec.local/decision",
            _env_url=lambda name, default, rstrip=False: default, httpx=types.SimpleNamespace(AsyncClient=_Client),
            _build_scan_universe=lambda: built, _filter_symbols_under_max_price=lambda syms: list(syms))
        return asyncio.run(_handler("get_universe", ref)())
    finally:
        if saved is None:
            sys.modules.pop("data_feed", None)
        else:
            sys.modules["data_feed"] = saved


def test_a_dict_or_text_under_symbols_is_not_iterated_into_names():
    assert _universe({"symbols": "ABC"}) == ["BUILT1"]                      # old code: ["A", "B", "C"]
    assert _universe({"symbols": {"error": "x"}}) == ["BUILT1"]             # old code: ["error"]
    assert _universe({"universe": 5}) == ["BUILT1"]


def test_null_empty_and_nested_names_are_dropped():
    assert _universe(["A", None, "", "  ", ["x"], {"s": 1}, " B "]) == ["A", "B"]    # old code kept "None", "", "['x']" ...


def test_good_training_lists_are_unchanged():
    assert _universe(["A", 7]) == ["A", "7"]
    assert _universe({"symbols": ["SYM1"], "universe": ["UNI1"]}) == ["SYM1"]
    assert _universe({"universe": ["UNI1"]}) == ["UNI1"]


if __name__ == "__main__":
    failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except Exception as _e:         # noqa: BLE001
                failed += 1
                print("FAIL", _name, repr(_e))
    sys.exit(1 if failed else 0)
