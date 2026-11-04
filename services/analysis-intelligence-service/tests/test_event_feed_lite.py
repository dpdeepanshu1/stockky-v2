"""E285 round 173: event/event_feed.py (the feed that drives real entries and vetoes) next to event_lite.py: shadow / legacy / assist / lite."""
import importlib.util
from pathlib import Path

import pytest

EVENT_DIR = Path(__file__).resolve().parent.parent / "event"


def _ef():
    spec = importlib.util.spec_from_file_location("r173_event_feed", EVENT_DIR / "event_feed.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


USFDA = "Sun Pharma gets USFDA nod for generic cancer drug"      # keyword rules: regulatory, -1 (wrong); event_lite: +1
SKIPS = "Coal India skips interim dividend this year"            # keyword rules: +1 ('dividend', wrong); event_lite: -1
TELUGU = "Tata Power ke share mein teji, naya order mila"        # keyword rules: 0; event_lite: +1 (order)
CLEAR = "Infosys cuts FY27 revenue guidance, shares slide"       # both -1


@pytest.fixture
def ef(monkeypatch):
    monkeypatch.delenv("EVENT_FEED_CLASSIFIER", raising=False)
    m = _ef()
    m.reset_feed_lite_stats()
    return m


def test_default_is_assist_live(ef):
    assert ef.feed_classifier_mode() == "assist"
    assert ef.classify(TELUGU)["polarity"] == 1 and ef.classify(USFDA)["polarity"] == 0 and ef.classify(CLEAR)["polarity"] == -1
    assert ef.feed_lite_stats()["mode"] == "assist" and ef.feed_lite_stats()["applied"] == 2


def test_shadow_mode_changes_nothing(ef, monkeypatch):
    monkeypatch.setenv("EVENT_FEED_CLASSIFIER", "shadow")
    assert ef.feed_classifier_mode() == "shadow"
    for h in (USFDA, SKIPS, TELUGU, CLEAR, "Acme shares open flat", ""):
        assert ef.classify(h) == ef._classify_legacy(h)
    st = ef.feed_lite_stats()
    assert st["mode"] == "shadow" and st["seen"] == 5 and st["agree"] >= 2 and st["fills"] == 1 and st["contested"] == 2 and st["applied"] == 0


def test_legacy_mode_never_calls_lite(ef, monkeypatch):
    monkeypatch.setenv("EVENT_FEED_CLASSIFIER", "legacy")
    assert ef.classify(USFDA) == ef._classify_legacy(USFDA)
    assert ef.feed_lite_stats()["seen"] == 0


def test_assist_fills_unknown_and_neutralises_a_contested_sign_but_never_flips(ef, monkeypatch):
    monkeypatch.setenv("EVENT_FEED_CLASSIFIER", "assist")
    r = ef.classify(TELUGU)
    assert r["polarity"] == 1 and r["type"] == "order" and r["hits"][0].startswith("lite:")
    assert ef.classify(USFDA)["polarity"] == 0                    # contested: no veto from a wrong -1, and no entry either
    assert ef.classify(SKIPS)["polarity"] == 0
    assert ef.classify(CLEAR)["polarity"] == -1                   # agreement stands
    assert ef.classify("Acme shares open flat")["polarity"] == 0
    assert ef.feed_lite_stats()["applied"] == 3


def test_assist_thresholds_are_env_driven(ef, monkeypatch):
    monkeypatch.setenv("EVENT_FEED_CLASSIFIER", "assist")
    monkeypatch.setenv("EVENT_FEED_LITE_FILL_CONF", "0.99")
    monkeypatch.setenv("EVENT_FEED_LITE_VETO_CONF", "0.99")
    assert ef.classify(TELUGU)["polarity"] == 0 and ef.classify(USFDA)["polarity"] == -1
    monkeypatch.setenv("EVENT_FEED_LITE_FILL_CONF", "junk")
    monkeypatch.setenv("EVENT_FEED_LITE_VETO_CONF", "7")
    assert ef._conf_env("EVENT_FEED_LITE_FILL_CONF", 0.75) == 0.75 and ef._conf_env("EVENT_FEED_LITE_VETO_CONF", 0.8) == 0.8


def test_lite_mode_decides(ef, monkeypatch):
    monkeypatch.setenv("EVENT_FEED_CLASSIFIER", "lite")
    assert ef.classify(USFDA)["polarity"] == 1 and ef.classify(SKIPS)["polarity"] == -1 and ef.classify(SKIPS)["type"] == "deal"
    assert ef.classify("Acme shares open flat")["polarity"] == 0
    assert ef.feed_lite_stats()["applied"] >= 3


def test_lite_mode_below_half_confidence_is_zero(ef, monkeypatch):
    monkeypatch.setenv("EVENT_FEED_CLASSIFIER", "lite")
    monkeypatch.setattr(ef._event_lite, "classify", lambda h: {"type": "other", "pol": 1, "confidence": 0.4, "reason": "x"})
    assert ef.classify("anything at all")["polarity"] == 0


def test_failures_fall_back_to_the_keyword_answer(ef, monkeypatch):
    monkeypatch.setenv("EVENT_FEED_CLASSIFIER", "assist")

    def boom(h):
        raise RuntimeError("x")
    monkeypatch.setattr(ef._event_lite, "classify", boom)
    assert ef.classify(USFDA) == ef._classify_legacy(USFDA)
    assert ef.feed_lite_stats()["errors"] == 1
    monkeypatch.setattr(ef, "_event_lite", None)
    assert ef.classify(USFDA) == ef._classify_legacy(USFDA)
    assert ef.feed_lite_stats()["available"] is False


def test_make_event_uses_the_dispatching_classify(ef, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setenv("EVENT_FEED_CLASSIFIER", "assist")
    e = ef.make_event("TATAPOWER", TELUGU, datetime.now(timezone.utc), "t")
    assert e["polarity"] == 1 and e["strength"] > 0


def test_lite_stats_route_includes_feed(monkeypatch):
    import sys
    sys.path.insert(0, str(EVENT_DIR))
    try:
        for n in ("main", "event_lite", "event_feed"):
            sys.modules.pop(n, None)
        spec = importlib.util.spec_from_file_location("r173_event_main", EVENT_DIR / "main.py")
        em = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(em)
    finally:
        sys.path.remove(str(EVENT_DIR))
    assert "mode" in em.lite_stats()["feed"]


def test_positive_regulatory_or_legal_is_order_sized_not_0_9(ef, monkeypatch):
    assert ef._feed_type("regulatory", 1) == "order" and ef._feed_type("legal", 1) == "order"
    assert ef._feed_type("regulatory", -1) == "regulatory" and ef._feed_type("legal", -1) == "regulatory"
    assert ef._feed_type("unknown_type", 1) == "other"
    monkeypatch.setenv("EVENT_FEED_CLASSIFIER", "lite")
    r = ef.classify("RBI lifts restrictions on Acme Finance")
    assert r["polarity"] == 1 and r["type"] == "order"
    r = ef.classify("SEBI imposes Rs 25 lakh penalty on Reliance Power")
    assert r["polarity"] == -1 and r["type"] == "regulatory"
    assert ef.strength(ef.classify("Delhi High Court quashes tax demand against Hero MotoCorp")["type"], 1, 0) == 0.65
