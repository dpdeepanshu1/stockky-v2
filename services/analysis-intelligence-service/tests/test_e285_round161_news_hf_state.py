"""E285 round 161 (plan item 20): the news score no longer presents the neutral fallback as a measured sentiment when Hugging Face is not scoring."""
from __future__ import annotations

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import main as nm  # noqa: E402


def _h(n):
    return [{"title": f"t{i}", "publisher": "Pub", "published": "2026-11-03"} for i in range(n)]


def _setup(monkeypatch, key, paused=False, score=0.0):
    monkeypatch.setattr(nm, "build_news_response", None)
    monkeypatch.setattr(nm, "HF_API_KEY", key)
    monkeypatch.setattr(nm, "_HF_BACKOFF_UNTIL", time.monotonic() + 600 if paused else 0.0)
    monkeypatch.setattr(nm, "_fetch_headlines", lambda s, max_items=15: _h(3))
    monkeypatch.setattr(nm, "_score_headline", lambda t: score)


def test_state_names_the_three_cases(monkeypatch):
    monkeypatch.setattr(nm, "HF_API_KEY", None)
    assert nm._hf_state() == "no_key"
    monkeypatch.setattr(nm, "HF_API_KEY", "K")
    monkeypatch.setattr(nm, "_HF_BACKOFF_UNTIL", time.monotonic() + 60)
    assert nm._hf_state() == "paused"
    monkeypatch.setattr(nm, "_HF_BACKOFF_UNTIL", 0.0)
    assert nm._hf_state() == "on"


def test_no_key_neutral_score_is_labelled_not_scored(monkeypatch):
    _setup(monkeypatch, None)
    out = nm.analyze("INFY")
    assert out["news_score"] == 50
    assert out["reasons"][-1] == "3 relevant headlines; sentiment not scored (HF_API_KEY not set), score is the neutral 50"
    assert out["data_quality"]["hf_sentiment"] is False and out["data_quality"]["hf_state"] == "no_key"


def test_paused_hf_is_not_reported_as_active(monkeypatch):
    _setup(monkeypatch, "K", paused=True)
    out = nm.analyze("INFY")
    assert "Hugging Face paused after errors" in out["reasons"][-1]
    assert out["data_quality"]["hf_sentiment"] is False and out["data_quality"]["hf_state"] == "paused"


def test_running_hf_keeps_the_old_wording(monkeypatch):
    _setup(monkeypatch, "K")
    out = nm.analyze("INFY")
    assert out["reasons"][-1] == "3 relevant headlines, average sentiment neutral"
    assert out["data_quality"]["hf_sentiment"] is True and out["data_quality"]["hf_state"] == "on"


def test_real_scores_are_never_called_not_scored(monkeypatch):
    _setup(monkeypatch, None, score=-0.8)                 # scores came from somewhere: they are readings
    out = nm.analyze("INFY")
    assert out["reasons"][-1].endswith("average sentiment negative")


def test_missing_key_warning_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(nm, "HF_API_KEY", None)
    monkeypatch.setattr(nm, "_HF_KEY_WARNED", False)
    with caplog.at_level(logging.WARNING):
        for i in range(5):
            assert nm._score_headline(f"h{i}") == 0.0
    assert sum("HF_API_KEY not set" in r.getMessage() for r in caplog.records) == 1
