"""
tests/test_exit_position_isolation_and_profile.py

Session82c (100%-coverage-plan Phase 1 #1, real bug found by reading the code
per the plan's own "read the block first" instruction for the per-position
dispatch loop):

  1. evaluate_mode's per-position body (held_days computation through the
     final HOLD fallback -- everything after the pending-real-sell guard) had
     NO exception isolation. One position raising ANY exception (bad
     watchlist_entry_id, malformed tick, a DB hiccup) aborted the entire
     cycle -- every other open position in that mode got zero stop/target/
     emergency-exit evaluation for that cycle, silently. Same incident class
     as session65's portfolio.import_broker_holdings fix. Fixed by wrapping
     the body in try/except, rolling back, logging a HOLD decision, and
     continuing to the next position.

  2. `_load_profile` and `_trail_atr_mult` (the functions that decision feeds
     into -- watchlist-sourced horizon profiles and the age-based ATR
     trailing-stop multiplier) had zero direct test coverage anywhere in the
     suite despite being called on every single evaluate_mode pass.

Run from services/real-trade-service:
    python -m pytest tests/test_exit_position_isolation_and_profile.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import exit_engine.exit as ex
from market_feed.feed import Tick


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(models.TradeAccount(
        mode="DEMO", starting_capital=100000.0, current_equity=100000.0,
        cash_available=100000.0,
    ))
    s.commit()
    yield s
    s.close()


def make_position(db, *, symbol="TESTCO", qty=10, entry=100.0, stop=95.0, target=110.0,
                   opened_days_ago=0, mode="DEMO", watchlist_entry_id=None,
                   source_tab=None):
    pos = models.TradePosition(
        mode=mode, symbol=symbol, status="OPEN", qty_open=qty,
        avg_entry_price=entry, current_stop=stop, current_target=target,
        initial_stop_distance=(entry - stop) if stop else None,
        opened_at=datetime.now(timezone.utc) - timedelta(days=opened_days_ago),
        watchlist_entry_id=watchlist_entry_id, source_tab=source_tab,
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def tick(price, atr=None, symbol="TESTCO"):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=atr,
                source="test")


def quotes(monkeypatch, mapping: dict):
    async def _q(symbols):
        return dict(mapping)
    monkeypatch.setattr(ex, "get_quotes", _q)


# ── 1. Per-position exception isolation ─────────────────────────────────────

class TestPerPositionIsolation:
    def test_one_position_raising_does_not_stop_the_others(self, db, monkeypatch):
        """3 positions in the same cycle; the middle one's _load_profile call
        raises. The other two must still be fully evaluated (one stop-hit
        full-exit, one plain hold) -- not silently skipped along with it."""
        make_position(db, symbol="FIRSTCO", qty=10, entry=100.0, stop=95.0, target=110.0)
        boom_pos = make_position(db, symbol="BOOMCO", qty=5, entry=50.0, stop=45.0, target=60.0)
        make_position(db, symbol="LASTCO", qty=7, entry=200.0, stop=190.0, target=220.0)

        quotes(monkeypatch, {
            "FIRSTCO": tick(94.0, symbol="FIRSTCO"),   # would stop-hit
            "BOOMCO":  tick(50.0, symbol="BOOMCO"),    # would otherwise just hold
            "LASTCO":  tick(205.0, symbol="LASTCO"),   # would otherwise just hold
        })

        real_load_profile = ex._load_profile

        def _boom_for_boomco(db_, position):
            if position.symbol == "BOOMCO":
                raise RuntimeError("simulated DB hiccup loading exit profile")
            return real_load_profile(db_, position)
        monkeypatch.setattr(ex, "_load_profile", _boom_for_boomco)

        tally = run(ex.evaluate_mode(db, "DEMO"))

        assert tally["evaluated"] == 3
        assert tally["full_exits"] == 1, "FIRSTCO's stop-hit must still fire despite BOOMCO's crash"
        # BOOMCO's own crash is recorded as a held/skipped position, LASTCO
        # holds normally -> 2 total holds this cycle.
        assert tally["held"] == 2

        db.refresh(boom_pos)
        assert boom_pos.status == "OPEN", "the crashing position must be left untouched, not partially mutated"

    def test_crashing_position_gets_a_hold_decision_logged(self, db, monkeypatch):
        pos = make_position(db, symbol="BOOMCO", qty=5, entry=50.0, stop=45.0, target=60.0)
        quotes(monkeypatch, {"BOOMCO": tick(50.0, symbol="BOOMCO")})
        monkeypatch.setattr(
            ex, "_load_profile",
            lambda db_, position: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        run(ex.evaluate_mode(db, "DEMO"))
        decisions = (
            db.query(models.TradeExitDecision)
            .filter(models.TradeExitDecision.position_id == pos.id)
            .all()
        )
        assert len(decisions) == 1
        assert decisions[0].action == "HOLD"
        assert "Evaluation error" in decisions[0].reasoning

    def test_isolation_rolls_back_partial_writes_from_the_failed_position(self, db, monkeypatch):
        """If the crash happens AFTER some db.add()/mutation already occurred
        for that position this cycle, the rollback must not corrupt the
        session for the next position's own commit."""
        make_position(db, symbol="BOOMCO", qty=5, entry=50.0, stop=45.0, target=60.0)
        make_position(db, symbol="OKCO", qty=10, entry=100.0, stop=95.0, target=110.0)
        quotes(monkeypatch, {
            "BOOMCO": tick(50.0, symbol="BOOMCO"),
            "OKCO":   tick(94.0, symbol="OKCO"),
        })

        real_write = ex._write_exit_decision

        def _selective_boom(db_, position, action, reasoning, ltp):
            if position.symbol == "BOOMCO":
                raise RuntimeError("simulated write failure mid-position")
            return real_write(db_, position, action, reasoning, ltp)
        monkeypatch.setattr(ex, "_write_exit_decision", _selective_boom)

        tally = run(ex.evaluate_mode(db, "DEMO"))
        assert tally["full_exits"] == 1, "OKCO's stop-hit must still commit cleanly after BOOMCO's failed write"


# ── 2. _load_profile ─────────────────────────────────────────────────────────

class TestLoadProfile:
    def test_manual_position_gets_global_defaults(self, db):
        pos = make_position(db, watchlist_entry_id=None, source_tab=None)
        prof = ex._load_profile(db, pos)
        assert prof["horizon_class"] is None
        assert prof["trail_atr_schedule"] == ex.TRAIL_ATR_SCHEDULE
        assert prof["breakeven_atr_trigger"] == ex.BREAKEVEN_ATR_TRIGGER
        assert prof["max_hold_days"] == ex.MAX_HOLD_DAYS
        assert prof["early_warn_days"] == ex.EARLY_WARN_DAYS
        assert prof["partial_exit_fraction"] == ex.PARTIAL_EXIT_FRACTION

    def test_volume_shock_position_gets_short_horizon_profile(self, db):
        """source_tab='volume_shock' with no watchlist_entry_id must route to
        the 'short' horizon profile instead of the global defaults (session12
        audit finding: volume_shock candidates never get a watchlist_entry_id,
        so without this branch they'd wrongly get the 10-day global default)."""
        pos = make_position(db, watchlist_entry_id=None, source_tab="volume_shock")
        prof = ex._load_profile(db, pos)
        assert prof["horizon_class"] == "short"
        assert prof != {
            "trail_atr_schedule": ex.TRAIL_ATR_SCHEDULE,
            "breakeven_atr_trigger": ex.BREAKEVEN_ATR_TRIGGER,
            "max_hold_days": ex.MAX_HOLD_DAYS,
            "early_warn_days": ex.EARLY_WARN_DAYS,
            "partial_exit_fraction": ex.PARTIAL_EXIT_FRACTION,
            "horizon_class": None,
        }

    def test_watchlist_sourced_position_uses_entry_horizon_class(self, db):
        if not hasattr(models, "WatchlistEntry"):
            pytest.skip("models.WatchlistEntry not available in this build")
        entry = models.WatchlistEntry(
            mode="DEMO", symbol="TESTCO", catalyst_type="results",
            catalyst_price=100.0, horizon_class="long",
            decay_half_life_days=5.0, entry_band_pct=3.0, source_tier=1,
            status="active", expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        pos = make_position(db, watchlist_entry_id=entry.id)
        prof = ex._load_profile(db, pos)
        assert prof["horizon_class"] == "long"

    def test_watchlist_entry_missing_falls_back_gracefully(self, db):
        """A watchlist_entry_id pointing at a row that no longer exists (e.g.
        pruned) must not raise -- horizon_class resolves to None and
        exit_profile_for(None) still returns a usable profile."""
        pos = make_position(db, watchlist_entry_id=999999)
        prof = ex._load_profile(db, pos)
        assert prof["horizon_class"] is None
        assert "trail_atr_schedule" in prof


# ── 3. _trail_atr_mult ───────────────────────────────────────────────────────

class TestTrailAtrMult:
    def test_uses_module_default_schedule_by_age_bucket(self):
        day0, day2, day3, day5, day6, day50 = (
            ex._trail_atr_mult(d) for d in (0, 2, 3, 5, 6, 50)
        )
        assert day0 == day2 == 1.8
        assert day3 == day5 == 1.3
        assert day6 == day50 == 0.9

    def test_accepts_a_custom_schedule_override(self):
        custom = [(1, 2.5), (4, 1.0)]
        assert ex._trail_atr_mult(0, schedule=custom) == 2.5
        assert ex._trail_atr_mult(1, schedule=custom) == 2.5
        assert ex._trail_atr_mult(2, schedule=custom) == 1.0
        assert ex._trail_atr_mult(4, schedule=custom) == 1.0

    def test_falls_back_to_last_entry_when_held_days_exceeds_every_bucket(self):
        """held_days beyond every (max_days, mult) pair in the schedule ->
        falls back to the schedule's last multiplier rather than raising."""
        custom = [(1, 2.0), (3, 1.0)]
        assert ex._trail_atr_mult(999, schedule=custom) == 1.0
