"""
tests/test_adaptive_thresholds.py
==================================
Coverage target: adaptive_thresholds.py  20% -> 100%  (108 statements)

adaptive_thresholds.py is the self-adjusting regime-gate module: it reads/
writes MarketRegimeHistory rows via a real SQLAlchemy Session and falls back
to config.py's static constants whenever the DB is unavailable or there's
not yet enough history. Tests use a REAL sqlite in-memory engine (same
fixture convention tests/test_portfolio.py already established) for the
success paths, and a MagicMock db double whose .query()/.commit()/.execute()
raise for the fallback/exception paths -- exercising the actual except
blocks rather than assuming they work.

Two module bits would otherwise make staleness-related tests depend on
whatever the real wall-clock date happens to be when the suite runs:
  - _REGIME_CONSTANTS's hardcoded "last reviewed" dates (2026-09-03)
  - STALE_THRESHOLD_DAYS (computed once at import time from an env var, so
    re-setting the env var after import has no effect)
Tests that care about staleness monkeypatch these two module attributes
directly instead, so they pass regardless of today's date.

Run from services/real-trade-service:
    python3 -m pytest tests/test_adaptive_thresholds.py -v \
        --cov=adaptive_thresholds --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
import adaptive_thresholds as at

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


def _insert_score(db, score, recorded_at):
    db.add(models.MarketRegimeHistory(score=score, recorded_at=recorded_at))
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# record_market_score() / _prune_old_scores()
# ══════════════════════════════════════════════════════════════════════════════

class TestRecordMarketScore:
    def test_inserts_row_and_prunes(self, db):
        at.record_market_score(db, 42)
        rows = db.query(models.MarketRegimeHistory).all()
        assert len(rows) == 1
        assert rows[0].score == 42.0

    def test_commit_failure_is_swallowed_and_rolled_back(self):
        bad_db = MagicMock()
        bad_db.commit.side_effect = RuntimeError("db down")
        at.record_market_score(bad_db, 10)  # must not raise
        assert bad_db.rollback.called

    def test_rollback_failure_after_commit_failure_also_swallowed(self):
        bad_db = MagicMock()
        bad_db.commit.side_effect = RuntimeError("db down")
        bad_db.rollback.side_effect = RuntimeError("rollback also down")
        at.record_market_score(bad_db, 10)  # must not raise


class TestPruneOldScores:
    def test_prunes_rows_beyond_history_window(self, db):
        too_old = datetime.now(timezone.utc) - timedelta(days=at.ADAPTIVE_HISTORY_DAYS + 10)
        recent = datetime.now(timezone.utc)
        _insert_score(db, 10, too_old)
        _insert_score(db, 20, recent)
        at._prune_old_scores(db)
        remaining = [r.score for r in db.query(models.MarketRegimeHistory).all()]
        assert remaining == [20.0]

    def test_row_within_window_is_kept(self, db):
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        _insert_score(db, 33, recent)
        at._prune_old_scores(db)
        remaining = [r.score for r in db.query(models.MarketRegimeHistory).all()]
        assert remaining == [33.0]

    def test_execute_failure_is_swallowed_and_rolled_back(self):
        bad_db = MagicMock()
        bad_db.execute.side_effect = RuntimeError("db down")
        at._prune_old_scores(bad_db)  # must not raise
        assert bad_db.rollback.called

    def test_rollback_failure_after_execute_failure_also_swallowed(self):
        bad_db = MagicMock()
        bad_db.execute.side_effect = RuntimeError("db down")
        bad_db.rollback.side_effect = RuntimeError("rollback also down")
        at._prune_old_scores(bad_db)  # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# adaptive_regime_threshold()
# ══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveRegimeThreshold:
    def test_no_history_returns_static(self, db):
        threshold, source = at.adaptive_regime_threshold(db)
        assert threshold == config.ENTRY_REGIME_MIN_SCORE
        assert source == "static"

    def test_too_few_distinct_days_returns_static(self, db):
        now = datetime.now(timezone.utc)
        for i in range(5):  # well under ADAPTIVE_MIN_HISTORY_DAYS (30)
            _insert_score(db, 50 + i, now - timedelta(days=i))
        threshold, source = at.adaptive_regime_threshold(db)
        assert threshold == config.ENTRY_REGIME_MIN_SCORE
        assert source == "static"

    def test_sufficient_history_computes_p20(self, db):
        now = datetime.now(timezone.utc)
        # 40 distinct days, scores 50..89 (one score per day, well within the
        # 90-day window) -> sorted == [50, 51, ..., 89], n=40.
        # idx = max(0, int(40*20/100) - 1) = 7 -> p20 = sorted[7] = 57
        for i in range(40):
            _insert_score(db, 50 + i, now - timedelta(days=i))
        threshold, source = at.adaptive_regime_threshold(db)
        assert threshold == 57
        assert source == "adaptive_40d_p20"

    def test_threshold_clamped_to_minimum_20(self, db):
        now = datetime.now(timezone.utc)
        # 30 distinct days, scores -10..19 -> sorted == [-10, ..., 19], n=30
        # idx = max(0, int(30*20/100) - 1) = 5 -> p20 = sorted[5] = -5,
        # which must be clamped up to the floor of 20.
        for i in range(30):
            _insert_score(db, -10 + i, now - timedelta(days=i))
        threshold, source = at.adaptive_regime_threshold(db)
        assert threshold == 20
        assert source == "adaptive_30d_p20"

    def test_query_exception_falls_back_to_static(self):
        bad_db = MagicMock()
        bad_db.query.side_effect = RuntimeError("db down")
        threshold, source = at.adaptive_regime_threshold(bad_db)
        assert threshold == config.ENTRY_REGIME_MIN_SCORE
        assert source == "static"


# ══════════════════════════════════════════════════════════════════════════════
# _days_since()
# ══════════════════════════════════════════════════════════════════════════════

class TestDaysSince:
    def test_valid_date_returns_correct_age(self):
        fifteen_days_ago = (datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%d")
        assert at._days_since(fifteen_days_ago) == 15

    def test_invalid_format_swallows_exception_returns_zero(self):
        assert at._days_since("not-a-date") == 0


# ══════════════════════════════════════════════════════════════════════════════
# check_threshold_staleness() / startup_staleness_warning()
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckThresholdStaleness:
    def test_nothing_stale_when_threshold_huge(self, monkeypatch):
        monkeypatch.setattr(at, "STALE_THRESHOLD_DAYS", 10**9)
        assert at.check_threshold_staleness() == []

    def test_everything_stale_when_threshold_zero(self, monkeypatch):
        monkeypatch.setattr(at, "STALE_THRESHOLD_DAYS", 0)
        stale = at.check_threshold_staleness()
        assert {item["constant"] for item in stale} == set(at._REGIME_CONSTANTS.keys())
        for item in stale:
            assert item["constant"] in item["warning"]
            assert "current_value" in item and "age_days" in item


class TestStartupStalenessWarning:
    def test_logs_and_returns_when_nothing_stale(self, monkeypatch, caplog):
        monkeypatch.setattr(at, "STALE_THRESHOLD_DAYS", 10**9)
        import logging
        with caplog.at_level(logging.INFO, logger="real-trade-adaptive"):
            at.startup_staleness_warning()
        assert "all regime constants reviewed" in caplog.text

    def test_notifies_when_stale_and_notifier_available(self, monkeypatch):
        monkeypatch.setattr(at, "STALE_THRESHOLD_DAYS", 0)
        fake_notifier = MagicMock()
        with patch.dict(sys.modules, {"notifier": fake_notifier}):
            at.startup_staleness_warning()
        fake_notifier.notify_sync.assert_called_once()
        msg = fake_notifier.notify_sync.call_args[0][0]
        assert "Stale trading thresholds detected" in msg

    def test_notifier_unavailable_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(at, "STALE_THRESHOLD_DAYS", 0)
        with patch.dict(sys.modules, {"notifier": None}):
            at.startup_staleness_warning()  # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# threshold_age_note()
# ══════════════════════════════════════════════════════════════════════════════

class TestThresholdAgeNote:
    def test_known_constant_returns_formatted_string(self, monkeypatch):
        monkeypatch.setattr(at, "_REGIME_CONSTANTS", {"FOO": (38, "2026-01-01")})
        note = at.threshold_age_note("FOO")
        expected_age = at._days_since("2026-01-01")
        assert note == f"(gate=38, set 2026-01-01, {expected_age}d ago)"

    def test_unknown_constant_returns_empty_string(self):
        assert at.threshold_age_note("NOT_A_REAL_CONSTANT") == ""


# ══════════════════════════════════════════════════════════════════════════════
# adaptive_status()
# ══════════════════════════════════════════════════════════════════════════════

class TestAdaptiveStatus:
    def test_status_with_no_history(self, db):
        status = at.adaptive_status(db)
        assert status["threshold_source"] == "static"
        assert status["history_readings_available"] == 0
        assert status["history_days_available"] == 0
        assert status["adaptive_active"] is False
        assert status["latest_market_score"] is None
        assert status["static_fallback"] == config.ENTRY_REGIME_MIN_SCORE
        assert status["min_history_needed"] == at.ADAPTIVE_MIN_HISTORY_DAYS
        assert "needs" in status["advice"]
        assert set(status["regime_constants"].keys()) == set(at._REGIME_CONSTANTS.keys())

    def test_status_with_sufficient_history(self, db):
        now = datetime.now(timezone.utc)
        for i in range(40):
            _insert_score(db, 50 + i, now - timedelta(days=i))
        status = at.adaptive_status(db)
        assert status["adaptive_active"] is True
        assert status["threshold_source"].startswith("adaptive_")
        assert status["history_readings_available"] == 40
        assert status["history_days_available"] == 40
        # most-recent inserted row (i=0, "now") carries score 50
        assert status["latest_market_score"] == 50.0
        assert "auto-adjusts" in status["advice"]

    def test_status_db_exception_falls_back_cleanly(self):
        bad_db = MagicMock()
        bad_db.query.side_effect = RuntimeError("db down")
        status = at.adaptive_status(bad_db)
        assert status["threshold_source"] == "static"
        assert status["history_readings_available"] == 0
        assert status["history_days_available"] == 0
        assert status["latest_market_score"] is None
