"""Round 459 (E480): instant_scanner._f turns NaN / inf into the default, like None or junk. No network."""
from __future__ import annotations

import importlib.util
import math
import os

_GATEWAY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location("instant_scanner_e480", os.path.join(_GATEWAY, "instant_scanner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_f_keeps_finite_numbers_and_numeric_strings():
    ins = _load()
    assert ins._f(5) == 5.0 and ins._f("12.5") == 12.5 and ins._f(-3.2) == -3.2 and ins._f(0) == 0.0


def test_f_returns_the_default_for_none_blank_and_junk():
    ins = _load()
    assert ins._f(None, 7.0) == 7.0 and ins._f("", 7.0) == 7.0 and ins._f("abc", 7.0) == 7.0 and ins._f([1], 7.0) == 7.0


def test_f_returns_the_default_for_nan_and_inf_in_every_spelling():
    ins = _load()
    for bad in (float("nan"), float("inf"), float("-inf"), "nan", "NaN", "inf", "-Infinity", 10 ** 400):
        assert ins._f(bad, 4.0) == 4.0, bad
    assert ins._f(float("nan")) == 0.0


def test_a_stored_nan_indicator_falls_back_to_the_static_default_not_nan():
    ins = _load()
    out = ins.resolve_stock_features("TCS", {"rsi": float("nan"), "pe_ratio": float("inf"), "roce": "nan", "macd_hist": float("nan"), "ema20": float("inf"), "price": 100.0})
    for k, v in out.items():
        if isinstance(v, float):
            assert math.isfinite(v), k
    assert out["rsi"] == ins.DEFAULT_STATIC_INDICATORS["rsi"]


def test_an_inf_price_in_the_fallback_walk_is_not_taken_as_a_price():
    ins = _load()
    import sys
    import types
    broken = types.ModuleType("price_resolver")          # no extract_safe_price: forces the fallback walk
    old = sys.modules.get("price_resolver")
    sys.modules["price_resolver"] = broken
    try:
        assert ins._extract_price("TCS", {"price": float("inf"), "close": 250.0}, {"ltp": float("nan")}) == 250.0
    finally:
        if old is not None:
            sys.modules["price_resolver"] = old
        else:
            sys.modules.pop("price_resolver", None)
