"""E410: the per-symbol scan worker reads a Data Feed row's price live-first (last_price / current_price before the stale prev_close),
and rejects a stored 0 / NaN / inf. Covers the lite fast-path, the POST /decide/evaluate payload and the durable-feed overlay."""
from __future__ import annotations

from test_main_analyze_symbol import DResp, analyze, env, gw, log  # noqa: F401  (env / log are fixtures)

INF, NAN = float("inf"), float("nan")


def test_helper_prefers_live_over_prev_close():
    assert gw._feed_row_price({"prev_close": 100, "last_price": 103.5}) == 103.5
    assert gw._feed_row_price({"prev_close": 100, "current_price": 102}) == 102.0
    assert gw._feed_row_price({"close": 90, "last_price": 95}) == 95.0


def test_helper_rejects_zero_nan_inf_text_and_reads_commas():
    assert gw._feed_row_price({"last_price": 0, "close": NAN, "price": INF, "ltp": "bad", "prev_close": 7}) == 7.0
    assert gw._feed_row_price({"close": "1,234.5"}) == 1234.5
    assert gw._feed_row_price({"close": 0}) is None
    assert gw._feed_row_price({}) is None and gw._feed_row_price(None) is None and gw._feed_row_price([1]) is None


def test_helper_reads_a_nested_metrics_price():
    assert gw._feed_row_price({"metrics": {"ltp": 55}}) == 55.0


def test_lite_takes_the_live_price_not_the_stale_prev_close(env):
    out = analyze("TCS", lite=True, feed_row={"prev_close": 100, "last_price": 103.5, "combined_score": 60})
    assert out["close"] == 103.5 and out["support"] == round(103.5 * 0.95, 2)
    assert env.price_calls == []


def test_lite_zero_or_nan_feed_price_goes_to_the_quote(env):
    env.price = 88.0
    out = analyze("TCS", lite=True, feed_row={"close": 0, "price": NAN, "fundamental_score": 60})
    assert out["close"] == 88.0 and env.price_calls == ["TCS"]


def test_evaluate_payload_carries_the_live_price(env):
    analyze("TCS", feed_row={"rsi": 55, "prev_close": 100, "last_price": 103.5})
    assert env.post_calls and env.post_calls[0][3]["close"] == 103.5


def test_evaluate_payload_drops_close_when_the_row_has_no_usable_price(env):
    analyze("TCS", feed_row={"rsi": 55, "close": 0})
    assert "close" not in env.post_calls[0][3]


def test_overlay_fills_a_missing_close_from_the_live_price(env):
    env.post_result = DResp({"decision": "HOLD", "combined_score": 55, "close": None})
    out = analyze("TCS", feed_row={"rsi": 55, "prev_close": 100, "current_price": 101.25})
    assert out["close"] == 101.25
