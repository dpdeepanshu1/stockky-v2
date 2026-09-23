"""
tests/test_adaptive_market_params.py
=====================================
Coverage target: adaptive_market_params.py  25% -> 100%  (146 statements)

adaptive_market_params.py generalizes adaptive_thresholds.py's single-metric
pattern (record a daily reading, compute a trailing-window percentile once
enough history exists, fall back to a static config.py constant otherwise)
to several parameters via a shared AdaptiveMetricSnapshot table. Same test
convention as test_adaptive_thresholds.py: a REAL sqlite in-memory engine for
success paths, and a MagicMock db double whose .query()/.commit()/.add()
raise for the fallback/exception paths -- exercising the actual except
blocks rather than assuming they work.

Run from services/real-trade-service:
    python3 -m pytest tests/test_adaptive_market_params.py -v \
        --cov=adaptive_market_params --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
import adaptive_market_params as amp

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


def _insert_metric(db, name, value, recorded_at):
    db.add(models.AdaptiveMetricSnapshot(metric_name=name, value=value, recorded_at=recorded_at))
    db.commit()


def _insert_score(db, score, recorded_at):
    db.add(models.MarketRegimeHistory(score=score, recorded_at=recorded_at))
    db.commit()


def _spread_readings(db, name, values, start_days_ago):
    """Insert one reading per distinct calendar day, walking backward from
    start_days_ago so every reading lands on its own day (distinct_days ==
    len(values))."""
    now = datetime.now(timezone.utc)
    for i, v in enumerate(values):
        _insert_metric(db, name, v, now - timedelta(days=start_days_ago - i))


# ── record_metric ───────────────────────────────────────────────────────

class TestRecordMetric:
    def test_inserts_row_and_prunes_old(self, db):
        old = datetime.now(timezone.utc) - timedelta(days=config.__dict__.get("ADAPTIVE_HISTORY_DAYS", 90) + 999)
        _insert_metric(db, "m1", 1.0, old)
        amp.record_metric(db, "m1", 5.0)
        rows = db.query(models.AdaptiveMetricSnapshot).filter(
            models.AdaptiveMetricSnapshot.metric_name == "m1"
        ).all()
        # the ancient row should have been pruned, only today's remains
        assert len(rows) == 1
        assert rows[0].value == 5.0

    def test_keeps_rows_within_window(self, db):
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        _insert_metric(db, "m2", 1.0, recent)
        amp.record_metric(db, "m2", 5.0)
        rows = db.query(models.AdaptiveMetricSnapshot).filter(
            models.AdaptiveMetricSnapshot.metric_name == "m2"
        ).all()
        assert len(rows) == 2

    def test_commit_failure_is_swallowed_and_rolls_back(self):
        mock_db = MagicMock()
        mock_db.commit.side_effect = Exception("commit boom")
        amp.record_metric(mock_db, "m3", 1.0)  # must not raise
        mock_db.rollback.assert_called_once()

    def test_commit_failure_rollback_also_raising_is_swallowed(self):
        mock_db = MagicMock()
        mock_db.commit.side_effect = Exception("commit boom")
        mock_db.rollback.side_effect = Exception("rollback boom too")
        amp.record_metric(mock_db, "m3b", 1.0)  # must not raise

    def test_prune_failure_is_swallowed_and_rolls_back(self):
        mock_db = MagicMock()
        # first db.commit() (after add) succeeds; db.query(...) for the
        # prune step raises.
        mock_db.commit.return_value = None
        mock_db.query.side_effect = Exception("prune boom")
        amp.record_metric(mock_db, "m4", 1.0)  # must not raise
        mock_db.rollback.assert_called_once()

    def test_prune_failure_rollback_also_raising_is_swallowed(self):
        mock_db = MagicMock()
        mock_db.commit.return_value = None
        mock_db.query.side_effect = Exception("prune boom")
        mock_db.rollback.side_effect = Exception("rollback boom too")
        amp.record_metric(mock_db, "m4b", 1.0)  # must not raise


# ── adaptive_percentile_value / _readings_and_distinct_days ─────────────

class TestAdaptivePercentileValue:
    def test_static_fallback_when_too_few_distinct_days(self, db):
        _spread_readings(db, "thin_metric", [1.0, 2.0, 3.0], start_days_ago=2)
        value, source = amp.adaptive_percentile_value(
            db, "thin_metric", static_default=99.0, percentile=80.0,
            guardrail_min=0.0, guardrail_max=1000.0,
        )
        assert value == 99.0
        assert source == "static"

    def test_adaptive_percentile_computed_once_enough_history(self, db):
        values = [float(i) for i in range(1, 41)]  # 1.0 .. 40.0
        _spread_readings(db, "p80_metric", values, start_days_ago=39)
        value, source = amp.adaptive_percentile_value(
            db, "p80_metric", static_default=0.0, percentile=80.0,
            guardrail_min=0.0, guardrail_max=1000.0,
            history_days=90, min_history_days=30,
        )
        # sorted 1..40, idx = max(0,min(39, int(40*80/100)-1)) = 31 -> value 32.0
        assert value == 32.0
        assert source == "adaptive_40r_40d_p80"

    def test_guardrail_max_clamps_result(self, db):
        values = [float(i) for i in range(1, 41)]
        _spread_readings(db, "clamp_hi", values, start_days_ago=39)
        value, source = amp.adaptive_percentile_value(
            db, "clamp_hi", static_default=0.0, percentile=80.0,
            guardrail_min=0.0, guardrail_max=10.0,
            history_days=90, min_history_days=30,
        )
        assert value == 10.0
        assert source.startswith("adaptive_")

    def test_guardrail_min_clamps_result(self, db):
        values = [float(i) for i in range(1, 41)]
        _spread_readings(db, "clamp_lo", values, start_days_ago=39)
        value, source = amp.adaptive_percentile_value(
            db, "clamp_lo", static_default=0.0, percentile=1.0,
            guardrail_min=50.0, guardrail_max=1000.0,
            history_days=90, min_history_days=30,
        )
        assert value == 50.0
        assert source.startswith("adaptive_")

    def test_exception_falls_back_to_static(self):
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("query boom")
        value, source = amp.adaptive_percentile_value(
            mock_db, "whatever", static_default=42.0, percentile=80.0,
            guardrail_min=0.0, guardrail_max=1000.0,
        )
        assert value == 42.0
        assert source == "static"


# ── adaptive_max_atr_pct ─────────────────────────────────────────────────

class TestAdaptiveMaxAtrPct:
    def test_static_fallback_with_no_data(self, db):
        value, source = amp.adaptive_max_atr_pct(db)
        assert value == config.CANDIDATE_MAX_ATR_PCT
        assert source == "static"

    def test_adaptive_value_within_guardrails(self, db):
        values = [5.0 + (i % 5) for i in range(35)]  # stays within [4,12]
        _spread_readings(db, "universe_atr_pct", values, start_days_ago=34)
        value, source = amp.adaptive_max_atr_pct(db)
        assert 4.0 <= value <= 12.0
        assert source.startswith("adaptive_")


# ── _current_market_score / _regime_tilt ─────────────────────────────────

class TestCurrentMarketScore:
    def test_no_rows_falls_back_to_static(self, db):
        score, source = amp._current_market_score(db)
        assert score == float(config.ENTRY_REGIME_MIN_SCORE)
        assert source == "static"

    def test_averages_last_three_readings(self, db):
        now = datetime.now(timezone.utc)
        _insert_score(db, 10.0, now - timedelta(minutes=30))
        _insert_score(db, 20.0, now - timedelta(minutes=20))
        _insert_score(db, 30.0, now - timedelta(minutes=10))
        _insert_score(db, 90.0, now - timedelta(minutes=1))  # only last 3 count is wrong; newest-first
        score, source = amp._current_market_score(db)
        # order_by desc limit 3 -> [90, 30, 20] avg = 46.666...
        assert score == pytest.approx((90.0 + 30.0 + 20.0) / 3.0)
        assert source == "latest_3r_avg"

    def test_exception_falls_back_to_static(self):
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("boom")
        score, source = amp._current_market_score(mock_db)
        assert score == float(config.ENTRY_REGIME_MIN_SCORE)
        assert source == "static"


class TestRegimeTilt:
    def test_weak_regime_positive_tilt(self, db):
        _insert_score(db, 20.0, datetime.now(timezone.utc))  # delta = 30 (>=0)
        tilt, source = amp._regime_tilt(db, weak_bonus=8.0, strong_penalty=5.0)
        assert tilt == pytest.approx(min(8.0, 30 * (8.0 / 30.0)))
        assert tilt > 0

    def test_strong_regime_negative_tilt(self, db):
        _insert_score(db, 90.0, datetime.now(timezone.utc))  # delta = -40 (<0)
        tilt, source = amp._regime_tilt(db, weak_bonus=8.0, strong_penalty=5.0)
        assert tilt < 0
        assert tilt == pytest.approx(max(-5.0, -40 * (5.0 / 30.0)))


# ── adaptive_quality_floor ────────────────────────────────────────────────

class TestAdaptiveQualityFloor:
    def test_static_score_gives_expected_tilt(self, db):
        # no market_score rows -> static ENTRY_REGIME_MIN_SCORE (25) ->
        # delta = 50-25 = 25 (weak regime) -> positive tilt
        value, source = amp.adaptive_quality_floor(db, config.VOLUME_SHOCK_FUND_ABS_FLOOR)
        assert value >= config.VOLUME_SHOCK_FUND_ABS_FLOOR
        assert source.startswith("regime_tilt(")

    def test_clamped_to_upper_bound_55(self, db):
        _insert_score(db, 0.0, datetime.now(timezone.utc))  # max weak tilt
        value, _ = amp.adaptive_quality_floor(db, 100.0)  # base pushes past 55
        assert value == 55.0

    def test_clamped_to_lower_bound_20(self, db):
        _insert_score(db, 100.0, datetime.now(timezone.utc))  # max strong tilt
        value, _ = amp.adaptive_quality_floor(db, 0.0)  # base pulls below 20
        assert value == 20.0


# ── adaptive_min_market_cap_cr ────────────────────────────────────────────

class TestAdaptiveMinMarketCapCr:
    def test_never_below_absolute_floor(self, db):
        _insert_score(db, 100.0, datetime.now(timezone.utc))  # strong -> big negative tilt
        value, source = amp.adaptive_min_market_cap_cr(db)
        assert value >= config.MIN_MARKET_CAP_CR_ABSOLUTE_FLOOR
        assert source.startswith("regime_tilt(")

    def test_weak_regime_raises_above_static(self, db):
        _insert_score(db, 0.0, datetime.now(timezone.utc))  # weak -> positive tilt
        value, _ = amp.adaptive_min_market_cap_cr(db)
        assert value > config.MIN_MARKET_CAP_CR_STATIC


# ── adaptive_rsi_bounds ───────────────────────────────────────────────────

class TestAdaptiveRsiBounds:
    def test_neutral_score_gives_classic_30_70(self, db):
        _insert_score(db, 50.0, datetime.now(timezone.utc))
        oversold, overbought, source = amp.adaptive_rsi_bounds(db)
        assert oversold == 30.0
        assert overbought == 70.0
        assert source.startswith("regime_tilt(")

    def test_bullish_score_shifts_bounds_up(self, db):
        _insert_score(db, 100.0, datetime.now(timezone.utc))  # delta=+50, shift clamps to +15
        oversold, overbought, _ = amp.adaptive_rsi_bounds(db)
        assert oversold == 40.0  # clamped guardrail max
        assert overbought == 85.0  # clamped guardrail max

    def test_bearish_score_shifts_bounds_down(self, db):
        _insert_score(db, 0.0, datetime.now(timezone.utc))  # delta=-50, shift clamps to -15
        oversold, overbought, _ = amp.adaptive_rsi_bounds(db)
        assert oversold == 15.0  # clamped guardrail min
        assert overbought == 60.0  # clamped guardrail min


# ── adaptive_extension_thresholds ─────────────────────────────────────────

class TestAdaptiveExtensionThresholds:
    def test_no_history_uses_ratio_one(self, db):
        ext_1m, ext_short, source = amp.adaptive_extension_thresholds(db)
        assert ext_1m == pytest.approx(0.18)
        assert ext_short == pytest.approx(0.05)
        assert source.startswith("atr_scaled(")

    def test_ratio_clamped_to_upper_1_6(self, db):
        # push adaptive ATR pct up to its own guardrail max (12) to force
        # ratio = 12/7 (~1.714) clamped down to 1.6
        values = [12.0] * 35
        _spread_readings(db, "universe_atr_pct", values, start_days_ago=34)
        ext_1m, ext_short, _ = amp.adaptive_extension_thresholds(db)
        expected_ratio = 1.6
        assert ext_1m == pytest.approx(round(min(0.30, 0.18 * expected_ratio), 3))
        assert ext_short == pytest.approx(round(min(0.09, 0.05 * expected_ratio), 3))

    def test_ratio_clamped_to_lower_0_6(self, db):
        # push adaptive ATR pct down to its own guardrail min (4) to force
        # ratio = 4/7 (~0.571) clamped up to 0.6
        values = [4.0] * 35
        _spread_readings(db, "universe_atr_pct", values, start_days_ago=34)
        ext_1m, ext_short, _ = amp.adaptive_extension_thresholds(db)
        expected_ratio = 0.6
        assert ext_1m == pytest.approx(round(max(0.10, 0.18 * expected_ratio), 3))
        assert ext_short == pytest.approx(round(max(0.03, 0.05 * expected_ratio), 3))

    def test_zero_static_atr_pct_guards_division(self, db, monkeypatch):
        monkeypatch.setattr(config, "CANDIDATE_MAX_ATR_PCT", 0.0)
        ext_1m, ext_short, _ = amp.adaptive_extension_thresholds(db)
        # ratio falls back to 1.0 when config.CANDIDATE_MAX_ATR_PCT is falsy
        assert ext_1m == pytest.approx(0.18)
        assert ext_short == pytest.approx(0.05)


# ── adaptive_signal_weights ───────────────────────────────────────────────

class TestAdaptiveSignalWeights:
    def test_no_rows_static_fallback(self, db):
        trend_w, meanrev_w, source = amp.adaptive_signal_weights(db)
        assert trend_w == 1.0
        assert meanrev_w == 1.0
        assert source == "static"

    def test_thin_history_static_fallback(self, db):
        _spread_readings(db, "universe_adx", [25.0, 26.0], start_days_ago=1)
        trend_w, meanrev_w, source = amp.adaptive_signal_weights(db)
        assert trend_w == 1.0
        assert meanrev_w == 1.0
        assert source == "static"

    def test_strongly_trending_saturates_tilt(self, db):
        values = [50.0] * 35  # avg 50 >= 45 saturation point
        _spread_readings(db, "universe_adx", values, start_days_ago=34)
        trend_w, meanrev_w, source = amp.adaptive_signal_weights(db)
        assert trend_w == pytest.approx(1.3)
        assert meanrev_w == pytest.approx(0.8)
        assert "avgadx" in source

    def test_partially_trending_scales_tilt(self, db):
        values = [30.0] * 35  # avg 30, between 25 and 45
        _spread_readings(db, "universe_adx", values, start_days_ago=34)
        trend_w, meanrev_w, _ = amp.adaptive_signal_weights(db)
        strength = min(1.0, (30.0 - 25.0) / 20.0)
        assert trend_w == pytest.approx(round(1.0 + 0.3 * strength, 3))
        assert meanrev_w == pytest.approx(round(1.0 - 0.2 * strength, 3))

    def test_strongly_range_bound_saturates_tilt(self, db):
        values = [2.0] * 35  # avg 2 <= 5 saturation point
        _spread_readings(db, "universe_adx", values, start_days_ago=34)
        trend_w, meanrev_w, _ = amp.adaptive_signal_weights(db)
        assert trend_w == pytest.approx(0.8)
        assert meanrev_w == pytest.approx(1.3)

    def test_partially_range_bound_scales_tilt(self, db):
        values = [15.0] * 35  # avg 15, between 5 and 20
        _spread_readings(db, "universe_adx", values, start_days_ago=34)
        trend_w, meanrev_w, _ = amp.adaptive_signal_weights(db)
        strength = min(1.0, (20.0 - 15.0) / 15.0)
        assert trend_w == pytest.approx(round(1.0 - 0.2 * strength, 3))
        assert meanrev_w == pytest.approx(round(1.0 + 0.3 * strength, 3))

    def test_neutral_between_20_and_25_no_tilt(self, db):
        values = [22.0] * 35
        _spread_readings(db, "universe_adx", values, start_days_ago=34)
        trend_w, meanrev_w, _ = amp.adaptive_signal_weights(db)
        assert trend_w == 1.0
        assert meanrev_w == 1.0

    def test_exception_falls_back_to_static(self):
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("boom")
        trend_w, meanrev_w, source = amp.adaptive_signal_weights(mock_db)
        assert trend_w == 1.0
        assert meanrev_w == 1.0
        assert source == "static"


# ── market_cap_tier ────────────────────────────────────────────────────────

class TestMarketCapTier:
    def test_none_is_unknown(self):
        assert amp.market_cap_tier(None) == "unknown"

    def test_large_at_and_above_threshold(self):
        assert amp.market_cap_tier(config.MARKET_CAP_LARGE_CR) == "large"
        assert amp.market_cap_tier(config.MARKET_CAP_LARGE_CR + 1) == "large"

    def test_mid_between_thresholds(self):
        assert amp.market_cap_tier(config.MARKET_CAP_MID_CR) == "mid"

    def test_small_between_thresholds(self):
        assert amp.market_cap_tier(config.MARKET_CAP_SMALL_CR) == "small"

    def test_micro_below_small_threshold(self):
        assert amp.market_cap_tier(config.MARKET_CAP_SMALL_CR - 1) == "micro"


# ── _latest_reading_age_hours ─────────────────────────────────────────────

class TestLatestReadingAgeHours:
    def test_no_reading_returns_none(self, db):
        assert amp._latest_reading_age_hours(db, "never_recorded") is None

    def test_naive_datetime_treated_as_utc(self, db):
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        db.add(models.AdaptiveMetricSnapshot(metric_name="naive_m", value=1.0, recorded_at=naive))
        db.commit()
        age = amp._latest_reading_age_hours(db, "naive_m")
        assert age is not None
        assert 1.5 <= age <= 2.5

    def test_aware_datetime(self, db):
        aware = datetime.now(timezone.utc) - timedelta(hours=5)
        _insert_metric(db, "aware_m", 1.0, aware)
        age = amp._latest_reading_age_hours(db, "aware_m")
        assert 4.5 <= age <= 5.5

    def test_exception_returns_none(self):
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("boom")
        assert amp._latest_reading_age_hours(mock_db, "whatever") is None


# ── adaptive_params_status ────────────────────────────────────────────────

class TestAdaptiveParamsStatus:
    def test_fresh_deploy_no_data(self, db):
        status = amp.adaptive_params_status(db)
        assert status["candidate_max_atr_pct"]["source"] == "static"
        assert status["data_freshness"]["last_universe_atr_pct_reading_hours_ago"] is None
        assert status["data_freshness"]["stale"] is False
        assert "advice" in status
        assert status["signal_weights"]["history"]["active"] is False

    def test_recent_reading_not_stale(self, db):
        _insert_metric(db, "universe_atr_pct", 6.0, datetime.now(timezone.utc) - timedelta(hours=1))
        status = amp.adaptive_params_status(db)
        assert status["data_freshness"]["stale"] is False
        assert status["data_freshness"]["last_universe_atr_pct_reading_hours_ago"] < 2.0

    def test_old_reading_is_stale(self, db):
        _insert_metric(db, "universe_atr_pct", 6.0, datetime.now(timezone.utc) - timedelta(hours=72))
        status = amp.adaptive_params_status(db)
        assert status["data_freshness"]["stale"] is True
        assert status["data_freshness"]["last_universe_atr_pct_reading_hours_ago"] > 48.0

    def test_full_history_reports_active_true(self, db):
        values = [6.0] * 35
        _spread_readings(db, "universe_atr_pct", values, start_days_ago=34)
        status = amp.adaptive_params_status(db)
        assert status["candidate_max_atr_pct"]["history"]["active"] is True
        assert status["candidate_max_atr_pct"]["history"]["distinct_days"] == 35
        assert status["candidate_max_atr_pct"]["source"].startswith("adaptive_")
