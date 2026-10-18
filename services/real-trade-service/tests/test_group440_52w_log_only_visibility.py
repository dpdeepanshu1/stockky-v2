"""group440: the 52-week rule runs log-only by default (compose) and, in that mode, leaves a trail:
a counter, a short list of the symbols it would have blocked, and a note on the candidate that passed.
Code default (CANDIDATE_52W_LOG_ONLY unset) is still 0 = rule rejects; compose supplies 1."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from candidate_engine import candidates as cd
from test_group379_52w_rule_and_etf_filter import _analyse, _overextended_by_tf, _reset_52w_state  # noqa: F401  (autouse fixture re-used)


@pytest.fixture(autouse=True)
def _clean_recent():
    cd._R52_LOG_ONLY_RECENT.clear()
    yield
    cd._R52_LOG_ONLY_RECENT.clear()


def test_log_only_candidate_passes_with_a_note_and_a_recent_entry(monkeypatch):
    monkeypatch.setattr(cd, "RANGE_52W_LOG_ONLY", True)
    monkeypatch.setattr(cd, "_cycle_market_score", 32.0)
    r = _analyse(_overextended_by_tf())
    assert r["reject_reason"] is None and r.get("reject_kind") is None
    assert "52w log-only" in r["market_note"] and "market score 32" in r["market_note"]
    st = cd.range_52w_log_only_stats()
    assert st["log_only"] is True and st["rule_enabled"] is True
    assert st["recent"][-1]["symbol"] == "TCS" and st["recent"][-1]["market_score"] == 32.0 and st["recent"][-1]["pos_pct"] > 88


def test_unknown_market_score_is_recorded_as_none_and_noted_as_na(monkeypatch):
    monkeypatch.setattr(cd, "RANGE_52W_LOG_ONLY", True)
    r = _analyse(_overextended_by_tf())
    assert "market score n/a" in r["market_note"]
    assert cd.range_52w_log_only_stats()["recent"][-1]["market_score"] is None


def test_rule_mode_has_no_note_and_no_recent_entry(monkeypatch):
    monkeypatch.setattr(cd, "RANGE_52W_LOG_ONLY", False)
    r = _analyse(_overextended_by_tf())
    assert r["reject_kind"] == "range_52w"
    assert cd.range_52w_log_only_stats()["recent"] == []


def test_strong_market_leaves_no_trail(monkeypatch):
    monkeypatch.setattr(cd, "RANGE_52W_LOG_ONLY", True)
    monkeypatch.setattr(cd, "_cycle_market_score", 80.0)       # regime gate: rule does not apply
    r = _analyse(_overextended_by_tf())
    assert "52w log-only" not in (r.get("market_note") or "")
    assert cd.range_52w_log_only_stats()["recent"] == []


def test_recent_list_is_capped_and_recording_never_raises(monkeypatch):
    for i in range(cd._R52_RECENT_MAX + 7):
        cd._r52_record_would_reject(f"S{i}", 95.0, 40.0)
    st = cd.range_52w_log_only_stats()
    assert len(st["recent"]) == cd._R52_RECENT_MAX and st["recent"][-1]["symbol"] == f"S{cd._R52_RECENT_MAX + 6}"
    cd._r52_record_would_reject("X", "not-a-number", None)       # bad input is swallowed, candidate path must not break


def test_hard_off_still_wins_over_log_only(monkeypatch):
    monkeypatch.setattr(cd, "RANGE_52W_ENABLED", False)
    monkeypatch.setattr(cd, "RANGE_52W_LOG_ONLY", True)
    n0 = cd.range_52w_log_only_stats()["would_reject"]
    r = _analyse(_overextended_by_tf())
    assert r["reject_reason"] is None and cd.range_52w_log_only_stats()["would_reject"] == n0


def test_compose_default_is_log_only_and_code_default_is_unchanged():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    with open(os.path.join(root, "docker-compose.yml"), encoding="utf-8") as fh:
        assert "CANDIDATE_52W_LOG_ONLY: ${CANDIDATE_52W_LOG_ONLY:-1}" in fh.read()
    with open(os.path.join(root, "services", "real-trade-service", "candidate_engine", "candidates.py"), encoding="utf-8") as fh:
        assert '_env_flag_on("CANDIDATE_52W_LOG_ONLY", "0")' in fh.read()
