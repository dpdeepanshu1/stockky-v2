"""2026-10-23: rules that switch by market regime are LOG ONLY: a hypothetical verdict per candidate, no enforce mode, never a WAIT."""
import json

import pytest

import config
from entry_engine import regime_rules as rr


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for k, v in (("REGIME_RULES_MODE", "log"), ("REGIME_RULES_STRONG_SCORE", 60.0), ("REGIME_RULES_TABLE_JSON", ""), ("ENTRY_MIN_REWARD_RISK", 2.0),
                 ("CANDIDATE_MIN_CONVICTION", 55.0)):
        monkeypatch.setattr(config, k, v, raising=False)
    rr.reset_stats()


def test_regime_buckets():
    assert rr.regime_of(30, 38) == "weak" and rr.regime_of(38, 38) == "neutral" and rr.regime_of(59.9, 38) == "neutral" and rr.regime_of(60, 38) == "strong"
    assert rr.regime_of(None, 38) is None and rr.regime_of("x", 38) is None and rr.regime_of(float("nan"), 38) is None
    assert rr.regime_of(50, None) == "neutral"


def test_weak_market_is_stricter_than_the_live_rules():
    v = rr.evaluate(30, 38, 60, 2.2)                       # passes today's 55 / 2.0, fails the weak-market 65 / 2.5
    assert v["regime"] == "weak" and v["would_reject"] and len(v["reasons"]) == 2 and v["risk_scale"] == 0.5
    assert not rr.evaluate(30, 38, 70, 3.0)["would_reject"]


def test_neutral_and_strong_use_the_live_floors():
    for score in (45, 75):
        assert not rr.evaluate(score, 38, 60, 2.2)["would_reject"]
        v = rr.evaluate(score, 38, 50, 1.5)
        assert v["would_reject"] and v["min_rr"] == 2.0 and v["min_conviction"] == 55.0


def test_missing_inputs_are_no_opinion_never_a_reject():
    assert rr.evaluate(None, 38, 60, 2.2)["opinion"] == "no_opinion"
    v = rr.evaluate(30, 38, None, None)
    assert v["opinion"] == "ok" and not v["would_reject"]
    assert not rr.evaluate("junk", object(), "x", [])["would_reject"]


def test_table_override_and_junk_override():
    config.REGIME_RULES_TABLE_JSON = json.dumps({"weak": {"min_rr": 3, "min_conviction": 70}, "bogus": {"min_rr": 9}, "strong": {"min_rr": "x"}})
    t = rr.table()
    assert t["weak"]["min_rr"] == 3.0 and t["weak"]["min_conviction"] == 70.0 and t["strong"]["min_rr"] is None and "bogus" not in t
    config.REGIME_RULES_TABLE_JSON = "{not json"
    assert rr.table()["weak"]["min_rr"] == 2.5


def test_there_is_no_enforce_mode_and_record_counts():
    config.REGIME_RULES_MODE = "enforce"
    assert rr.mode_for("REAL") == "log"
    config.REGIME_RULES_MODE = "off"
    assert rr.mode_for("DEMO") == "off"
    config.REGIME_RULES_MODE = "log"
    rr.record("infy", "REAL", rr.evaluate(30, 38, 60, 2.2), 1500.0)
    rr.record("tcs", "REAL", rr.evaluate(70, 38, 80, 3.0), 3500.0)
    s = rr.stats()
    assert s["evaluated"] == 2 and s["would_reject"] == 1 and s["by_regime"]["weak"] == {"n": 1, "would_reject": 1} and s["recent"][0]["symbol"] == "TCS"
