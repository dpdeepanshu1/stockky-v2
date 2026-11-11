"""E423: `_normalize_decision_response` — an explicit None for symbol / decision / confidence / combined_score / holding_period /
valuation takes the default, like a missing key. The "None = not computed" fields keep their None."""
from __future__ import annotations

import pytest

from test_main_scoring import gw, price_resolver_ok  # noqa: F401


def test_explicit_none_takes_the_default_for_the_text_and_number_fields(price_resolver_ok):
    raw = {k: None for k in ("symbol", "decision", "confidence", "combined_score", "holding_period", "valuation")}
    out = gw._normalize_decision_response(raw, "INFY")
    assert out["symbol"] == "INFY" and out["decision"] == "DO NOT BUY" and out["confidence"] == "Low"
    assert out["combined_score"] == 0 and out["holding_period"] == "N/A" and out["valuation"] == "fair"


def test_real_values_and_a_real_zero_are_kept(price_resolver_ok):
    out = gw._normalize_decision_response(
        {"decision": "BUY NOW", "confidence": "High", "combined_score": 0, "holding_period": "5d", "valuation": "cheap"}, "A")
    assert (out["decision"], out["confidence"], out["combined_score"], out["holding_period"], out["valuation"]) == \
        ("BUY NOW", "High", 0, "5d", "cheap")


@pytest.mark.parametrize("key", ["technical_score", "fundamental_score", "event_risk", "news_score", "prediction_score",
                                 "close", "support", "resistance", "entry_range", "target", "stop_loss"])
def test_none_means_not_computed_for_the_other_fields(key, price_resolver_ok):
    out = gw._normalize_decision_response({key: None}, "A")
    assert out[key] is None


def test_the_input_is_not_mutated(price_resolver_ok):
    raw = {"decision": None}
    gw._normalize_decision_response(raw, "A")
    assert raw == {"decision": None}
