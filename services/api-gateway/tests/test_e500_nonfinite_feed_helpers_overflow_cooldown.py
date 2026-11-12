"""E500: a read of the sync feed / alert helpers in `main.py` (and `data_feed` price helpers) plus the instant scorer they feed.

1. `instant_scanner._f` returned `float(val)` as is, so a NaN / inf in a stored feed row made the symbol's instant score NaN. Non-finite -> default.
2. `_feed_missing_fields` counted a NaN / inf `sentiment_score` as populated; `_feed_scoring_sentiment` handed the NaN to the decision engine.
3. `_lite_evaluate_from_feed(live_price=inf)` put inf in the tick; the three px reads in the scan-stream chunk loop used `float(x or 0)`.
4. An int of 400+ digits (valid JSON) raises OverflowError from `float()`; `_coerce_price`, `_hot_float`, `_scan_notify_num` and
   `_apply_price_alert_hits` only caught (TypeError, ValueError) (the last one aborted the check of EVERY alert).
5. `_ops_alert_cooldown_sec`: OPS_ALERT_COOLDOWN_SEC=nan gave 0 (throttling off), inf gave a cooldown that never ends -> default 900.

No fastapi / httpx needed (functions lifted from the sources by AST; the real instant_scanner is imported).
Runs with pytest or `python3 tests/test_e500_nonfinite_feed_helpers_overflow_cooldown.py`.
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

import instant_scanner as isc  # noqa: E402

NAN, INF = float("nan"), float("inf")
BIG = 10 ** 400
_MAIN = ast.parse(open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read())
_DF = ast.parse(open(os.path.join(_SERVICE, "data_feed.py"), encoding="utf-8").read())
_J = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_j.py"), encoding="utf-8").read()


def _lift(name, extra=None, tree=None):
    fn = next(n for n in (tree or _MAIN).body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {"math": math, "Any": object, "Optional": object, "dict": dict, "os": os, "logger": __import__("logging").getLogger("t")}
    ns.update(extra or {})
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "src.py", "exec"), ns)
    return ns[name]


def _safe_float(val, default=0.0):
    return _lift("_safe_float")(val, default)


# ── 1. instant_scanner._f ────────────────────────────────────────────────────

def test_instant_f_non_finite_gives_default_and_good_values_unchanged():
    assert isc._f(NAN) == 0.0 and isc._f(INF) == 0.0 and isc._f(-INF, 5.0) == 5.0 and isc._f("nan") == 0.0 and isc._f("inf", 2.0) == 2.0
    assert isc._f(BIG, 4.0) == 4.0
    assert isc._f(0) == 0.0 and isc._f("12.5") == 12.5 and isc._f(None, 3.0) == 3.0 and isc._f("") == 0.0 and isc._f("abc", 7.0) == 7.0 and isc._f(-4.2) == -4.2


def test_instant_score_for_a_row_with_nan_fields_is_finite():
    feed = {"price": 100.0, "close": 100.0, "rsi": NAN, "pe_ratio": INF, "roce": NAN, "sentiment_score": NAN, "ema20": NAN, "macd_hist": NAN}
    out = isc.compute_instant_scores("ABC", feed, {})
    bad = [k for k, v in out.items() if isinstance(v, float) and not math.isfinite(v)]
    assert bad == []


# ── 2. feed sentiment ────────────────────────────────────────────────────────

def _missing():
    return _lift("_feed_missing_fields", {"_feed_resolved_price": lambda d: 100.0, "_safe_float": _safe_float})


def test_missing_fields_nan_sentiment_is_missing_real_zero_is_not():
    f = _missing()
    base = {"rsi": 50, "pe_ratio": 20, "roce": 15}
    assert f({**base, "sentiment_score": NAN}) == ["sentiment_score"]
    assert f({**base, "sentiment_score": INF}) == ["sentiment_score"]
    assert f({**base, "sentiment_score": None}) == ["sentiment_score"]
    assert f({**base, "sentiment_score": 0}) == [] and f({**base, "sentiment_score": 0.0}) == [] and f({**base, "sentiment_score": 61.5}) == []


def test_scoring_sentiment_nan_is_none_and_real_values_pass_through():
    f = _lift("_feed_scoring_sentiment")
    assert f({"sentiment_score": NAN, "news_score": INF}) == (None, None)
    assert f({"sentiment_score": 0, "news_score": 0.0}) == (0, 0.0)
    assert f({"sentiment_score": 61.5, "news_score": 40}) == (61.5, 40)
    assert f({"sentiment_score": 0.65, "sentiment_seed": True, "news_score": 0.65}) == (50.0, 50.0)
    assert f({"market_score": 33.0}) == (33.0, None)


# ── 3. lite live price ───────────────────────────────────────────────────────

def test_lite_evaluate_does_not_put_an_inf_or_text_price_in_the_tick():
    seen = {}
    mod = types.ModuleType("instant_scanner")
    mod.compute_instant_scores = lambda sym, feed, tick: seen.setdefault("tick", dict(tick)) is not None and {"symbol": sym}
    saved = sys.modules.get("instant_scanner")
    sys.modules["instant_scanner"] = mod
    try:
        f = _lift("_lite_evaluate_from_feed", {"_safe_float": _safe_float})
        for bad in (INF, NAN, "abc", None, 0, -3.0, BIG):
            seen.clear()
            f("ABC", {}, bad)
            assert seen["tick"] == {}, bad
        seen.clear()
        f("ABC", {}, 101.5)
        assert seen["tick"] == {"price": 101.5}
        seen.clear()
        f("ABC", {}, "99.5")
        assert seen["tick"] == {"price": 99.5}
    finally:
        if saved is None:
            sys.modules.pop("instant_scanner", None)
        else:
            sys.modules["instant_scanner"] = saved


def test_scan_stream_chunk_px_reads_use_safe_float():
    assert "float(chunk_prices.get(base) or 0)" not in _J
    assert _J.count("ref._safe_float(chunk_prices.get(base))") == 3


# ── 4. OverflowError from a huge int ─────────────────────────────────────────

def test_coerce_price_huge_int_is_no_price_and_good_values_unchanged():
    f = _lift("_coerce_price", tree=_DF)
    assert f(BIG) == 0.0 and f(-BIG) == 0.0 and f(str(BIG)) == 0.0
    assert f("1,234.5") == 1234.5 and f(99) == 99.0 and f(INF) == 0.0 and f(NAN) == 0.0 and f(None) == 0.0 and f("abc") == 0.0


def test_hot_float_and_scan_notify_num_huge_int_is_none():
    for name in ("_hot_float", "_scan_notify_num"):
        f = _lift(name)
        assert f(BIG) is None and f(NAN) is None and f(INF) is None and f("x") is None and f(None) is None, name
        assert f("12.5") == 12.5 and f(0) == 0.0 and f(-3) == -3.0, name


def test_price_alert_hits_one_huge_price_does_not_abort_the_other_alerts():
    f = _lift("_apply_price_alert_hits", {"_now_iso": lambda: "2026-11-12T10:00:00+05:30"}, tree=_DF)
    alerts = [
        {"id": "a", "symbol": "AAA", "target_price": 100, "direction": "above", "enabled": True},
        {"id": "b", "symbol": "BBB", "target_price": 100, "direction": "above", "enabled": True},
        {"id": "c", "symbol": "CCC", "target_price": BIG, "direction": "above", "enabled": True},
    ]
    triggered, updated = f(alerts, {"AAA": BIG, "BBB": 150.0, "CCC": 5.0}, 1_000_000.0)
    assert updated is True
    assert [t["symbol"] for t in triggered] == ["BBB"] and triggered[0]["current_price"] == 150.0


# ── 5. ops alert cooldown ────────────────────────────────────────────────────

def test_ops_alert_cooldown_non_finite_env_gives_default():
    f = _lift("_ops_alert_cooldown_sec")
    old = os.environ.get("OPS_ALERT_COOLDOWN_SEC")
    try:
        for raw, want in (("nan", 900.0), ("inf", 900.0), ("-inf", 900.0), ("abc", 900.0), ("", 900.0), ("0", 0.0), ("-5", 0.0), ("120", 120.0), ("30.5", 30.5)):
            os.environ["OPS_ALERT_COOLDOWN_SEC"] = raw
            assert f() == want, raw
    finally:
        if old is None:
            os.environ.pop("OPS_ALERT_COOLDOWN_SEC", None)
        else:
            os.environ["OPS_ALERT_COOLDOWN_SEC"] = old


if __name__ == "__main__":
    fails = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("PASS", _n)
            except Exception as _e:  # noqa: BLE001
                fails += 1
                print("FAIL", _n, type(_e).__name__, str(_e)[:160])
    sys.exit(1 if fails else 0)
