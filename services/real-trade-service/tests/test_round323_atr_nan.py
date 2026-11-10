"""E323: the ATR helpers skip a candle whose high / low / previous close is NaN, inf or inverted. A NaN high used to make the whole ATR NaN, and the candidate engine's
`if atr_val and atr_val > 0` then skipped its too-volatile veto (NaN is truthy, `NaN > 0` is False)."""
from __future__ import annotations

import math

from candidate_engine import candidates as cand
from market_feed import feed

NAN = float("nan")


def _candles(n=20):
    return [{"high": 101.0, "low": 99.0, "close": 100.0} for _ in range(n)]


def _both():
    return (cand._compute_atr_from_candles, feed._compute_atr_from_candles)


def test_clean_candles_unchanged():
    for fn in _both():
        assert fn(_candles()) == 2.0


def test_nan_high_is_skipped_not_poisoning():
    for fn in _both():
        c = _candles()
        c[18]["high"] = NAN
        r = fn(c)
        assert r is not None and math.isfinite(r) and r == 2.0


def test_nan_close_and_inf_and_inverted_are_skipped():
    for fn in _both():
        for key, val in (("close", NAN), ("high", float("inf")), ("low", NAN)):
            c = _candles()
            c[10][key] = val
            r = fn(c)
            assert r is not None and math.isfinite(r), (key, val)
        c = _candles()
        c[10]["high"], c[10]["low"] = 98.0, 102.0
        assert math.isfinite(fn(c))


def test_too_few_good_candles_is_none_not_nan():
    for fn in _both():
        c = _candles(15)
        for i in range(2, 8):
            c[i]["high"] = NAN
        assert fn(c) is None
