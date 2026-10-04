"""tests/test_instant_scanner_price_only_cap.py — group 64.

Before: a symbol with NO fundamental/technical data (just a live price) was scored from made-up
defaults (fund ~84, tech ~80) and reached PREPARE TO BUY at 0% and BUY NOW at +1% with High
confidence — a classic false-breakout signal that would also trigger buy_sniper alerts.

Now: a bullish label (BUY NOW / PREPARE TO BUY) needs real feed evidence (fundamental_score,
technical_score, combined_score, metrics, rsi, pe_ratio or roce). Without it the card is capped at
HOLD / Low and says why. Bearish labels are kept — a falling price is real evidence on its own.

Run from services/api-gateway:
    python3 -m pytest tests/test_instant_scanner_price_only_cap.py -v
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(os.path.dirname(_HERE), "instant_scanner.py")
_BULLISH = ("BUY NOW", "PREPARE TO BUY")


@pytest.fixture
def ins(monkeypatch):
    for k in ("MAX_STOCK_PRICE", "VALUE_BUY_THRESHOLD"):
        monkeypatch.delenv(k, raising=False)
    spec = importlib.util.spec_from_file_location("instant_scanner_price_only_cap_under_test", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _card(ins, feed, price=100.0, change_pct=0.0):
    prev = price / (1.0 + change_pct / 100.0)
    return ins.compute_instant_scores("TESTCO", feed, {"price": price, "prev_close": prev})


@pytest.mark.parametrize("change_pct", [0.0, 0.3, 0.5, 1.0, 2.0, 5.0, 12.0, 19.0])
def test_empty_feed_never_gets_a_bullish_label(ins, change_pct):
    out = _card(ins, {}, change_pct=change_pct)
    assert out["decision"] not in _BULLISH
    assert (out["decision"], out["confidence"]) == ("HOLD", "Low")
    assert out["decision_capped"] is True
    assert out["provisional_defaults"] is True and out["from_data_feed"] is False


@pytest.mark.parametrize("feed", [
    {"price": 100.0}, {"close": 100.0}, {"prev_close": 99.0}, {"price": 100.0, "prev_close": 99.0},
    {"sector": "Auto"}, {"news_score": 7}, {"metrics": {}}, {"metrics": None},
])
def test_price_only_feed_rows_are_capped_too(ins, feed):
    # price / prev_close / close mark a row as a real feed (from_data_feed) but carry no evidence
    out = _card(ins, feed, change_pct=1.0)
    assert out["decision"] not in _BULLISH and out["decision_capped"] is True


def test_the_cap_reason_is_on_the_card(ins):
    out = _card(ins, {}, change_pct=1.0)
    assert "No fundamental/technical data" in out["decision_cap_reason"]
    assert out["decision_cap_reason"] in out["reasons"]["lite"]
    assert out["natural_language_summary"].startswith("TESTCO: instant — HOLD")


@pytest.mark.parametrize("change_pct,label", [(-3.0, "AVOID")])
def test_bearish_labels_are_kept_without_data(ins, change_pct, label):
    out = _card(ins, {}, change_pct=change_pct)
    assert out["decision"] == label and out["decision_capped"] is False


@pytest.mark.parametrize("feed", [
    {"technical_score": 90, "fundamental_score": 90, "prev_close": 100},
    {"fundamental_score": 80},
    {"technical_score": 80},
    {"combined_score": 80},
    {"rsi": 55},
    {"pe_ratio": 20},
    {"roce": 20},
    {"metrics": {"pe_ratio": 20}},
])
def test_any_real_signal_lifts_the_cap(ins, feed):
    out = _card(ins, feed, change_pct=1.0)
    assert out["decision_capped"] is False and out["decision_cap_reason"] is None
    assert "decision_cap" not in " ".join(out["reasons"]["lite"])


def test_a_strong_feed_backed_setup_is_still_buy_now(ins):
    out = ins.compute_instant_scores("TCS", {"technical_score": 90, "fundamental_score": 90, "prev_close": 100},
                                     {"price": 102})
    assert (out["decision"], out["confidence"]) == ("BUY NOW", "High")
    assert out["decision_capped"] is False
    assert out["reasons"]["lite"] == ["Instant scanner: DB data-feed + live quote (no downstream HTTP)"]


def test_feed_without_any_price_keeps_the_existing_low_confidence_card(ins):
    out = ins.compute_instant_scores("XYZ", {"rsi": 50}, {})
    assert out["confidence"] == "Low" and out["data_insufficient"] is True
    assert out["decision_capped"] is False
