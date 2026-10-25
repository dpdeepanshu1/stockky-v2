"""E148: a NaN / inf metric in an uploaded or manual feed row is "missing", not a value (it used to pass through normalize_feed_payload
and get stored / merged). Run from services/api-gateway: python3 -m pytest tests/test_e148_feed_nonfinite.py -v"""
import math
import os
import sys

_SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)

import data_feed as df  # noqa: E402


def test_nan_and_inf_become_none_and_are_stripped():
    out = df.normalize_feed_payload({"RSI": float("nan"), "pe": float("inf"), "ROCE": float("-inf"), "Price": "1,234.5"})
    assert out["rsi"] is None and out["pe_ratio"] is None and out["roce"] is None
    assert out["prev_close"] == 1234.5
    cleaned = df.strip_none_fields(out)
    assert "rsi" not in cleaned and "pe_ratio" not in cleaned and "roce" not in cleaned
    assert cleaned["prev_close"] == 1234.5


def test_finite_values_unchanged():
    out = df.normalize_feed_payload({"rsi": 0, "pe": 18, "macd": -1.5, "flag": True})
    assert out["rsi"] == 0.0 and out["pe_ratio"] == 18.0 and out["macd_hist"] == -1.5 and out["flag"] is True
    assert all(not isinstance(v, float) or math.isfinite(v) for v in out.values())
