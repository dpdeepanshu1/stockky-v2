"""group434: CANDIDATE_52W_LOG_ONLY - the 52-week rule is evaluated and counted but does not reject. Default (off) = unchanged."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from candidate_engine import candidates as cd
from test_group379_52w_rule_and_etf_filter import _analyse, _overextended_by_tf, _reset_52w_state  # noqa: F401  (autouse fixture re-used)


def test_default_is_off_and_rule_still_rejects(monkeypatch):
    monkeypatch.setattr(cd, "RANGE_52W_LOG_ONLY", False)
    assert _analyse(_overextended_by_tf())["reject_kind"] == "range_52w"
    assert cd.range_52w_log_only_stats()["log_only"] is False


def test_log_only_does_not_reject_and_counts(monkeypatch, caplog):
    monkeypatch.setattr(cd, "RANGE_52W_LOG_ONLY", True)
    n0 = cd.range_52w_log_only_stats()["would_reject"]
    with caplog.at_level("INFO"):
        r = _analyse(_overextended_by_tf())
    assert r.get("reject_kind") != "range_52w"
    assert cd.range_52w_log_only_stats()["would_reject"] == n0 + 1
    assert any("52W_LOG_ONLY would reject TCS" in m for m in caplog.messages)


def test_log_only_does_not_count_when_rule_would_not_fire(monkeypatch):
    monkeypatch.setattr(cd, "RANGE_52W_LOG_ONLY", True)
    monkeypatch.setattr(cd, "_cycle_market_score", 80.0)        # strong market: regime gate says the rule does not apply
    n0 = cd.range_52w_log_only_stats()["would_reject"]
    _analyse(_overextended_by_tf())
    assert cd.range_52w_log_only_stats()["would_reject"] == n0
