"""E492: `/scan/watchlist` and the universe outcome recorder.

1. `_record_symbol_outcomes` (called at the END of both the full scan and /scan/watchlist, after every symbol was scored) was unguarded: one stored
   row that was not a dict, or whose counters were None / text (E451 only guarded the read side, `_is_symbol_pruned`), or one failing kv write,
   raised out of it and lost the whole scan. A row that cannot be read now restarts from zero; a failure on one symbol never stops the others.
2. `scan_watchlist._scan_one_symbol` merged news / event reasons into `normalized["reasons"]` with `.get("reasons", {})`: a null or non-dict
   `reasons` from the decision service (E413 fixed this in `_analyze_one_symbol_ultra` only) raised TypeError and the symbol went to `errors`.
   A text / list `entry_range` raised AttributeError the same way. Both now keep the symbol.
3. Its three env settings were bare int() / float(): a typo (`8x`, `18s`, `nan`) was an HTTP 500 on every call. They now use the default.

No fastapi / httpx needed (functions lifted from main.py / bound_routes_j.py by AST, run against fakes). Runs with pytest or `python3 tests/test_e492_...py`."""
from __future__ import annotations

import ast
import logging
import math
import os
import threading
import types
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta, timezone

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_MAIN = ast.parse(_MAIN_SRC)
_BRJ = ast.parse(open(os.path.join(_SERVICE, "gateway_support", "bound_routes_j.py"), encoding="utf-8").read())
IST = timezone(timedelta(hours=5, minutes=30))


def _main_fn(name, ns):
    fn = [n for n in _MAIN.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(fn) == 1, name
    exec(compile(ast.Module(body=fn, type_ignores=[]), "main.py", "exec"), ns)
    return ns[name]


# ── 1. _record_symbol_outcomes ───────────────────────────────────────────────

def _recorder(store, set_raises_for=()):
    sets = []

    def rget(key):
        return store.get(key)

    def rset(key, value, ttl=None):
        if key.endswith(tuple(set_raises_for)):
            raise RuntimeError("kv down")
        store[key] = value
        sets.append(key)

    ns = {"SYMBOL_PERF_KEY_PREFIX": "p:", "SYMBOL_PERF_TTL": 1, "_redis_get": rget, "_redis_set": rset, "datetime": datetime, "IST": IST,
          "logger": logging.getLogger("e492")}
    return _main_fn("_record_symbol_outcomes", ns), sets


def test_unreadable_stored_rows_restart_from_zero_instead_of_raising():
    for bad in ("junk", ["x"], 7, {"total_scans": None, "weak_streak": "3"}, {"total_scans": True, "weak_streak": 2.5}):
        store = {"p:AAA": bad}
        rec, _ = _recorder(store)
        rec([{"symbol": "AAA", "decision": "HOLD"}])
        assert store["p:AAA"]["total_scans"] == 1 and store["p:AAA"]["weak_streak"] == 1, bad


def test_one_failing_symbol_does_not_stop_the_others():
    store = {}
    rec, sets = _recorder(store, set_raises_for=("BBB",))
    rec([{"symbol": "AAA", "decision": "HOLD"}, {"symbol": "BBB", "decision": "HOLD"}, "not a row", {"symbol": "CCC", "decision": "BUY NOW"}])
    assert sets == ["p:AAA", "p:CCC"]


def test_good_rows_behave_as_before():
    store = {"p:AAA": {"weak_streak": 4, "total_scans": 5, "last_actionable_at": "old"}, "p:BBB": {"weak_streak": 7, "total_scans": 9, "last_actionable_at": None}}
    rec, _ = _recorder(store)
    rec([{"symbol": "AAA", "decision": "HOLD"}, {"symbol": "BBB", "decision": "BUY NOW"}, {"symbol": "ERR", "decision": "ERROR"}, {"symbol": "N", "decision": None}])
    assert store["p:AAA"] == {"weak_streak": 5, "total_scans": 6, "last_actionable_at": "old"}
    assert store["p:BBB"]["weak_streak"] == 0 and store["p:BBB"]["total_scans"] == 10 and store["p:BBB"]["last_actionable_at"]
    assert "p:ERR" not in store and "p:N" not in store


# ── 2 / 3. scan_watchlist ────────────────────────────────────────────────────

class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


class _HTTPError(Exception):
    pass


def _scan(payloads, news=None, env=None):
    """Lift the real scan_watchlist out of bound_routes_j and run it against fakes. Returns the route's result dict."""
    build = next(n for n in _BRJ.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_j")
    fn = next(n for n in build.body if isinstance(n, ast.FunctionDef) and n.name == "scan_watchlist")
    fn.decorator_list = []
    main_ns = {"Optional": object, "Dict": dict, "Any": object, "math": math, "datetime": datetime, "timedelta": timedelta, "IST": IST}
    keep = [n for n in _MAIN.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_NORMALIZE_NONE_KEEPS_DEFAULT" for t in n.targets)]
    exec(compile(ast.Module(body=keep, type_ignores=[]), "main.py", "exec"), main_ns)
    main_ns["_board_rank_key"] = lambda r: main_ns["_hot_float"](r.get("combined_score")) or 0
    for name in ("_normalize_decision_response", "_reasons_dict", "_estimate_holding_period", "_hot_float", "_scan_num", "_score_sort_key",
                 "_scan_horizon_picks"):
        _main_fn(name, main_ns)

    class Client:
        def __init__(self, **kw):
            pass

        def get(self, url, timeout=None):
            sym = url.rsplit("/", 1)[-1]
            if "/decide/" in url:
                return _Resp(payloads[sym])
            return _Resp({}, 404)

        def close(self):
            pass

    envd = dict(env or {})
    ref = types.SimpleNamespace(
        os=types.SimpleNamespace(getenv=lambda k, d=None: envd.get(k, d)), math=math, MAX_PARALLEL_WORKERS=8, threading=threading,
        ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed, wait=wait, logger=logging.getLogger("e492"),
        httpx=types.SimpleNamespace(Client=Client, Limits=lambda **kw: kw, HTTPError=_HTTPError),
        DECISION_URL="http://d", PREDICTION_URL="http://p",
        _load_watchlist=lambda: list(payloads),
        _normalize_decision_response=main_ns["_normalize_decision_response"], _reasons_dict=main_ns["_reasons_dict"],
        _estimate_holding_period=main_ns["_estimate_holding_period"],
        _fetch_price_from_quote=lambda s: 100.0, _merge_fundamentals=lambda n, s: None,
        _fetch_news=lambda s: news, _fetch_events=lambda s: None, _generate_summary=lambda n: "s",
        _score_sort_key=main_ns["_score_sort_key"], _scan_horizon_picks=main_ns["_scan_horizon_picks"], _select_top_picks=lambda rows, limit=5: rows[:limit],
        _board_rank_key=main_ns["_board_rank_key"], _record_symbol_outcomes=lambda rows: None,
        _send_scan_notification=lambda *a: None, _cleanup_scan_resources=lambda *a: None,
    )
    ns = {"ref": ref, "SimpleNamespace": types.SimpleNamespace}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "bound_routes_j.py", "exec"), ns)
    out = ns["scan_watchlist"]()
    for t in threading.enumerate():
        if t.name == "watchlist-scan-notify":
            t.join(2)
    return out


def _row(symbol="AAA", **extra):
    d = {"symbol": symbol, "decision": "HOLD", "combined_score": 50, "close": 100.0, "event_risk": True, "prediction_score": 50}
    d.update(extra)
    return d


def test_baseline_scan_returns_the_symbol():
    out = _scan({"AAA": _row(news_score=50)})
    assert out["scanned"] == 1 and out["errors"] == []


def test_null_or_non_dict_reasons_no_longer_drop_the_symbol_when_news_is_merged():
    news = {"news_score": 61, "reasons": ["good news"]}
    for bad in (None, "text", ["x"]):
        out = _scan({"AAA": _row(reasons=bad)}, news=news)
        assert out["errors"] == [] and out["scanned"] == 1, bad
        r = out["all_results"][0]
        assert r["news_score"] == 61 and r["reasons"]["news"] == ["good news"], bad


def test_a_real_reasons_dict_keeps_its_other_keys():
    out = _scan({"AAA": _row(reasons={"technical": ["t"]})}, news={"news_score": 61, "reasons": ["n"]})
    assert out["all_results"][0]["reasons"] == {"technical": ["t"], "news": ["n"]}


def test_a_text_or_list_entry_range_no_longer_drops_the_symbol():
    for bad in ("100-110", ["100", "110"], 5):
        out = _scan({"AAA": _row(news_score=50, entry_range=bad, target=120.0)})
        assert out["errors"] == [] and out["scanned"] == 1, bad


def test_a_dict_entry_range_still_feeds_the_holding_period():
    out = _scan({"AAA": _row(news_score=50, entry_range={"low": 100.0, "high": 105.0}, target=120.0, decision="BUY NOW")})
    assert out["all_results"][0]["holding_period_estimate"]["min_days"] >= 3


def test_bad_env_values_use_the_defaults_instead_of_a_500():
    env = {"WATCHLIST_SCAN_CONCURRENCY": "8x", "WATCHLIST_SCAN_HTTP_CONCURRENCY": "two", "WATCHLIST_SCAN_TIMEOUT_SECONDS": "18s"}
    out = _scan({"AAA": _row(news_score=50)}, env=env)
    assert out["scanned"] == 1
    for bad in ("nan", "inf", "-5", "0"):
        out = _scan({"AAA": _row(news_score=50)}, env={"WATCHLIST_SCAN_TIMEOUT_SECONDS": bad})
        assert out["scanned"] == 1, bad


if __name__ == "__main__":   # pragma: no cover
    import sys
    failed = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("ok  ", _n)
            except BaseException as _e:   # noqa: BLE001
                failed += 1
                print("FAIL", _n, type(_e).__name__, _e)
    sys.exit(1 if failed else 0)
