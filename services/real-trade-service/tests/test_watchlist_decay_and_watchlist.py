"""
tests/test_watchlist_decay_and_watchlist.py  — session89, step 1
=================================================================
Coverage targets:
  watchlist_engine/decay.py      67% → 100%   (missing: 47, 56-59)
  watchlist_engine/watchlist.py   0% → 100%   (all 48 statements)

Run from services/real-trade-service:
    python3 -m pytest tests/test_watchlist_decay_and_watchlist.py -v \
        --cov=watchlist_engine.decay --cov=watchlist_engine.watchlist \
        --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
from watchlist_engine import decay, watchlist


# ── helpers ──────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def _utc(**kw):
    return datetime.now(timezone.utc) + timedelta(**kw)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()


def _entry(db, *, symbol="INFY", mode="DEMO", status="active",
           catalyst_type="volume_shock", catalyst_ts=None,
           expires_at=None, catalyst_price=100.0):
    now = datetime.now(timezone.utc)
    row = models.WatchlistEntry(
        mode=mode,
        symbol=symbol,
        catalyst_type=catalyst_type,
        catalyst_price=catalyst_price,
        catalyst_ts=catalyst_ts or now,
        expires_at=expires_at or (now + timedelta(days=6)),
        horizon_class="short",
        decay_half_life_days=2.0,
        entry_band_pct=0.06,
        source_tier=3,
        status=status,
    )
    db.add(row)
    db.commit()
    return row


# ═══════════════════════════════════════════════════════════════════════════
# Part A — watchlist_engine/decay.py
# ═══════════════════════════════════════════════════════════════════════════

class TestProfileFor:
    """Lines 45-47 — profile_for()"""

    def test_known_types_return_correct_profile(self):
        for ctype, expected_horizon in [
            ("ipo", "short"),
            ("bulk_block", "short"),
            ("insider", "short"),
            ("results", "mid"),
            ("board", "mid"),
            ("volume_shock", "short"),
        ]:
            p = decay.profile_for(ctype)
            assert p["horizon_class"] == expected_horizon, f"{ctype} wrong horizon"
            assert "decay_half_life_days" in p
            assert "entry_band_pct" in p

    def test_unknown_type_returns_default_profile(self):
        # line 47: CATALYST_PROFILES.get(catalyst_type, DEFAULT_CATALYST_PROFILE)
        p = decay.profile_for("something_new")
        assert p == decay.DEFAULT_CATALYST_PROFILE
        assert p["horizon_class"] == "short"
        assert p["decay_half_life_days"] == 3
        assert p["entry_band_pct"] == 0.04

    def test_empty_string_returns_default(self):
        p = decay.profile_for("")
        assert p == decay.DEFAULT_CATALYST_PROFILE

    def test_none_string_literal_returns_default(self):
        p = decay.profile_for("None")
        assert p == decay.DEFAULT_CATALYST_PROFILE


class TestExpiryFrom:
    """Lines 50-59 — expiry_from()"""

    def test_known_type_expiry_is_3x_half_life(self):
        # line 56: profile = profile_for(catalyst_type)
        ts = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
        exp = decay.expiry_from(ts, "ipo")  # half_life=2 → 6 days
        assert exp == ts + timedelta(days=6)

    def test_volume_shock_expiry(self):
        ts = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
        exp = decay.expiry_from(ts, "volume_shock")  # half_life=2 → 6 days
        assert exp == ts + timedelta(days=6)

    def test_results_expiry_is_36_days(self):
        ts = datetime(2026, 9, 1, tzinfo=timezone.utc)
        exp = decay.expiry_from(ts, "results")  # half_life=12 → 36 days
        assert exp == ts + timedelta(days=36)

    def test_naive_ts_gets_utc_added(self):
        # lines 57-58: if catalyst_ts.tzinfo is None: catalyst_ts = ...replace(tzinfo=...)
        naive_ts = datetime(2026, 9, 1, 12, 0, 0)  # no tzinfo
        exp = decay.expiry_from(naive_ts, "ipo")
        # Should not raise, and result must be tz-aware
        assert exp.tzinfo is not None
        assert exp == datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(days=6)

    def test_aware_ts_unchanged(self):
        # line 59: return catalyst_ts + timedelta(...)
        aware_ts = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        exp = decay.expiry_from(aware_ts, "bulk_block")  # half_life=4 → 12 days
        assert exp == aware_ts + timedelta(days=12)

    def test_unknown_type_uses_default_half_life_3(self):
        ts = datetime(2026, 9, 1, tzinfo=timezone.utc)
        exp = decay.expiry_from(ts, "mystery_catalyst")
        assert exp == ts + timedelta(days=9)  # 3×3


class TestExitProfileFor:
    """exit_profile_for() — already ≥33% but hit it fully"""

    def test_short_returns_correct_profile(self):
        p = decay.exit_profile_for("short")
        assert p["max_hold_days"] == 4
        assert p["partial_exit_fraction"] == 0.65

    def test_mid_returns_correct_profile(self):
        p = decay.exit_profile_for("mid")
        assert p["max_hold_days"] == 15
        assert p["partial_exit_fraction"] == 0.55

    def test_none_returns_default(self):
        p = decay.exit_profile_for(None)
        assert p == decay.DEFAULT_EXIT_PROFILE

    def test_unknown_string_returns_default(self):
        p = decay.exit_profile_for("long")
        assert p == decay.DEFAULT_EXIT_PROFILE


# ═══════════════════════════════════════════════════════════════════════════
# Part B — watchlist_engine/watchlist.py
# ═══════════════════════════════════════════════════════════════════════════

class TestRefreshWatchlist:
    """refresh_watchlist() — lines 28-113"""

    def test_source_fetch_exception_returns_zero(self, db):
        # lines 33-35: except Exception → logger.error, return 0
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(side_effect=RuntimeError("network down"))):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 0

    def test_empty_candidates_returns_zero_no_commit(self, db):
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=[])):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 0
        assert db.query(models.WatchlistEntry).count() == 0

    def test_candidate_missing_symbol_is_skipped(self, db):
        candidates = [
            {"symbol": "", "catalyst_type": "ipo", "catalyst_ts": _utc()},
            {"catalyst_type": "board"},  # no symbol key at all
        ]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 0

    def test_new_candidate_inserts_row(self, db):
        ts = _utc(days=-1)
        candidates = [{
            "symbol": "RELIANCE",
            "catalyst_type": "ipo",
            "catalyst_ts": ts,
            "catalyst_price": 2500.0,
            "catalyst_price_source": "live",
            "source_tier": 1,
            "conviction_score": 0.85,
        }]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 1
        row = db.query(models.WatchlistEntry).filter_by(symbol="RELIANCE").one()
        assert row.status == "active"
        assert row.catalyst_price == 2500.0
        assert row.source_tier == 1
        assert row.conviction_score == pytest.approx(0.85)
        assert row.horizon_class == "short"    # ipo → short

    def test_duplicate_active_same_type_is_skipped(self, db):
        # pre-insert an active row
        _entry(db, symbol="TCS", catalyst_type="bulk_block", status="active")
        ts = _utc(days=-2)
        candidates = [{"symbol": "TCS", "catalyst_type": "bulk_block", "catalyst_ts": ts}]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 0
        assert db.query(models.WatchlistEntry).filter_by(symbol="TCS").count() == 1

    def test_duplicate_same_catalyst_ts_any_status_is_skipped(self, db):
        # Same catalyst_ts, but status=expired — should still deduplicate
        ts = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
        row = models.WatchlistEntry(
            mode="DEMO", symbol="WIPRO", catalyst_type="ipo",
            catalyst_price=300.0, catalyst_ts=ts,
            expires_at=ts + timedelta(days=1),
            horizon_class="short", decay_half_life_days=2, entry_band_pct=0.04,
            source_tier=1, status="expired",
        )
        db.add(row)
        db.commit()

        candidates = [{"symbol": "WIPRO", "catalyst_type": "ipo", "catalyst_ts": ts}]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 0

    def test_different_catalyst_type_same_symbol_is_inserted(self, db):
        _entry(db, symbol="HDFC", catalyst_type="ipo", status="active")
        ts = _utc(days=-1)
        candidates = [{"symbol": "HDFC", "catalyst_type": "board", "catalyst_ts": ts}]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 1
        assert db.query(models.WatchlistEntry).filter_by(symbol="HDFC").count() == 2

    def test_catalyst_ts_none_uses_now(self, db):
        candidates = [{"symbol": "AXIS", "catalyst_type": "results", "catalyst_ts": None}]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 1
        row = db.query(models.WatchlistEntry).filter_by(symbol="AXIS").one()
        assert row.catalyst_ts is not None

    def test_naive_catalyst_ts_gets_utc(self, db):
        naive_ts = datetime(2026, 9, 1, 9, 15, 0)  # no tzinfo
        candidates = [{"symbol": "ICICI", "catalyst_type": "board", "catalyst_ts": naive_ts}]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 1

    def test_catalyst_price_none_defaults_to_zero(self, db):
        ts = _utc()
        candidates = [{"symbol": "SBIN", "catalyst_type": "volume_shock",
                        "catalyst_ts": ts, "catalyst_price": None}]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 1
        row = db.query(models.WatchlistEntry).filter_by(symbol="SBIN").one()
        assert row.catalyst_price == 0.0

    def test_symbol_uppercased(self, db):
        ts = _utc()
        candidates = [{"symbol": "reliance", "catalyst_type": "board", "catalyst_ts": ts}]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 1
        row = db.query(models.WatchlistEntry).filter_by(symbol="RELIANCE").one()
        assert row.symbol == "RELIANCE"

    def test_no_catalyst_type_defaults_to_volume_shock(self, db):
        ts = _utc()
        candidates = [{"symbol": "BAJAJ", "catalyst_ts": ts}]  # no catalyst_type
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 1
        row = db.query(models.WatchlistEntry).filter_by(symbol="BAJAJ").one()
        assert row.catalyst_type == "volume_shock"

    def test_multiple_candidates_all_inserted(self, db):
        ts = _utc()
        candidates = [
            {"symbol": "A", "catalyst_type": "ipo", "catalyst_ts": ts, "catalyst_price": 100.0},
            {"symbol": "B", "catalyst_type": "board", "catalyst_ts": ts},
            {"symbol": "C", "catalyst_type": "results", "catalyst_ts": ts},
        ]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 3

    def test_mode_isolation_demo_vs_real(self, db):
        # An active REAL row should not block a DEMO insert for same symbol
        real_row = models.WatchlistEntry(
            mode="REAL", symbol="MARUTI", catalyst_type="ipo",
            catalyst_price=8000.0, catalyst_ts=_utc(),
            expires_at=_utc(days=6),
            horizon_class="short", decay_half_life_days=2, entry_band_pct=0.04,
            source_tier=1, status="active",
        )
        db.add(real_row)
        db.commit()
        ts = _utc()
        candidates = [{"symbol": "MARUTI", "catalyst_type": "ipo", "catalyst_ts": ts}]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 1

    def test_conviction_score_none_allowed(self, db):
        ts = _utc()
        candidates = [{"symbol": "ZOMATO", "catalyst_type": "volume_shock",
                        "catalyst_ts": ts, "conviction_score": None}]
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 1
        row = db.query(models.WatchlistEntry).filter_by(symbol="ZOMATO").one()
        assert row.conviction_score is None

    def test_source_tier_defaults_to_3(self, db):
        ts = _utc()
        candidates = [{"symbol": "PAYTM", "catalyst_type": "volume_shock",
                        "catalyst_ts": ts}]  # no source_tier
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=candidates)):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 1
        row = db.query(models.WatchlistEntry).filter_by(symbol="PAYTM").one()
        assert row.source_tier == 3

    def test_zero_added_no_commit_attempted(self, db):
        # Verify no commit when nothing added (empty list)
        commit_spy = MagicMock(wraps=db.commit)
        db.commit = commit_spy
        with patch("watchlist_engine.watchlist.fetch_watchlist_candidates",
                   new=AsyncMock(return_value=[])):
            result = run(watchlist.refresh_watchlist(db, "DEMO"))
        assert result == 0
        commit_spy.assert_not_called()


class TestExpireStaleEntries:
    """expire_stale_entries() — lines 117-144"""

    def test_stale_active_row_gets_expired(self, db):
        # expires_at in the past
        old_ts = _utc(days=-10)
        row = _entry(db, symbol="COAL", expires_at=old_ts, status="active")
        count = watchlist.expire_stale_entries(db, "DEMO")
        assert count == 1
        db.refresh(row)
        assert row.status == "expired"

    def test_fresh_active_row_not_expired(self, db):
        future = _utc(days=5)
        row = _entry(db, symbol="NTPC", expires_at=future, status="active")
        count = watchlist.expire_stale_entries(db, "DEMO")
        assert count == 0
        db.refresh(row)
        assert row.status == "active"

    def test_already_expired_row_not_touched(self, db):
        old_ts = _utc(days=-10)
        _entry(db, symbol="NHPC", expires_at=old_ts, status="expired")
        count = watchlist.expire_stale_entries(db, "DEMO")
        assert count == 0

    def test_mode_isolation(self, db):
        # REAL stale row should NOT be expired by DEMO call
        old_ts = _utc(days=-10)
        real_row = models.WatchlistEntry(
            mode="REAL", symbol="ADANI", catalyst_type="ipo",
            catalyst_price=500.0, catalyst_ts=_utc(days=-12),
            expires_at=old_ts,
            horizon_class="short", decay_half_life_days=2, entry_band_pct=0.04,
            source_tier=1, status="active",
        )
        db.add(real_row)
        db.commit()
        count = watchlist.expire_stale_entries(db, "DEMO")
        assert count == 0
        db.refresh(real_row)
        assert real_row.status == "active"

    def test_multiple_stale_rows_all_expired(self, db):
        old_ts = _utc(days=-10)
        for sym in ["A", "B", "C"]:
            _entry(db, symbol=sym, expires_at=old_ts, status="active")
        count = watchlist.expire_stale_entries(db, "DEMO")
        assert count == 3
        for sym in ["A", "B", "C"]:
            row = db.query(models.WatchlistEntry).filter_by(symbol=sym).one()
            assert row.status == "expired"

    def test_mixed_stale_and_fresh(self, db):
        _entry(db, symbol="STALE", expires_at=_utc(days=-1), status="active")
        _entry(db, symbol="FRESH", expires_at=_utc(days=5), status="active")
        count = watchlist.expire_stale_entries(db, "DEMO")
        assert count == 1
        stale = db.query(models.WatchlistEntry).filter_by(symbol="STALE").one()
        fresh = db.query(models.WatchlistEntry).filter_by(symbol="FRESH").one()
        assert stale.status == "expired"
        assert fresh.status == "active"

    def test_no_stale_no_commit(self, db):
        commit_spy = MagicMock(wraps=db.commit)
        db.commit = commit_spy
        count = watchlist.expire_stale_entries(db, "DEMO")
        assert count == 0
        commit_spy.assert_not_called()

    def test_returns_zero_when_table_empty(self, db):
        count = watchlist.expire_stale_entries(db, "DEMO")
        assert count == 0

    def test_entered_status_not_expired(self, db):
        # Only "active" rows should be targeted, not "entered"
        old_ts = _utc(days=-10)
        _entry(db, symbol="ENTERED", expires_at=old_ts, status="entered")
        count = watchlist.expire_stale_entries(db, "DEMO")
        assert count == 0
