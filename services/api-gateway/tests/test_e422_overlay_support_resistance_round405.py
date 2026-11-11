"""E422: when the feed overlay supplies `close` (the decision had none), support / resistance are filled too:
the feed row's own levels first, +-5% of the price as the last rung; an engine level is never replaced."""
from __future__ import annotations

import pytest

from test_main_analyze_symbol import DResp, analyze, env, gw  # noqa: F401


def test_overlay_price_gets_plus_minus_five_percent_levels(env):
    env.get_result = DResp({"decision": "HOLD"})
    out = analyze("TCS", feed_row={"last": "100", "rsi": 50})
    assert out["close"] == 100.0 and out["support"] == 95.0 and out["resistance"] == 105.0


def test_overlay_prefers_the_feed_rows_own_levels(env):
    env.get_result = DResp({"decision": "HOLD"})
    out = analyze("TCS", feed_row={"last": 100, "support": "90", "resistance": 120, "rsi": 50})
    assert (out["support"], out["resistance"]) == (90.0, 120.0)


def test_overlay_ignores_bad_feed_levels_and_keeps_engine_levels(env):
    env.get_result = DResp({"decision": "HOLD", "resistance": 111.0})
    out = analyze("TCS", feed_row={"last": 100, "support": "bad", "resistance": 5, "rsi": 50})
    assert out["support"] == 95.0 and out["resistance"] == 111.0      # bad -> +-5%; engine value kept


def test_engine_close_means_the_overlay_adds_no_levels(env):
    env.get_result = DResp({"decision": "HOLD", "close": 10.0})
    out = analyze("TCS", feed_row={"last": 100, "support": 90, "rsi": 50})
    assert out["close"] == 10.0 and out.get("support") is None


@pytest.mark.parametrize("close", [None, "x", 0, -5, float("nan"), float("inf")])
def test_helper_skips_an_unusable_close(close):
    n = {}
    gw._overlay_support_resistance(n, close, {})
    assert n == {}


def test_helper_rounds_and_tolerates_a_non_dict_feed_row():
    n = {}
    gw._overlay_support_resistance(n, 33.33, None)
    assert n == {"support": round(33.33 * 0.95, 2), "resistance": round(33.33 * 1.05, 2)}
