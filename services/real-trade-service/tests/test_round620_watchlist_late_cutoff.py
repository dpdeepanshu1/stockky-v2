"""tests/test_round620_watchlist_late_cutoff.py - round 620 (E645): the watchlist trigger holds once ENTRY_LAST_TIME_IST has passed.

Run from services/real-trade-service:  python3 -m pytest tests/test_round620_watchlist_late_cutoff.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from entry_engine import entry
from market_feed.feed import Tick
from tz_utils import IST

WED = (2026, 11, 18)
SAT = (2026, 11, 21)


def at(day, h, m):
    return datetime(*day, h, m, 0, tzinfo=IST)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _clear_logged():
    entry._LATE_CUTOFF_LOGGED.clear()
    yield
    entry._LATE_CUTOFF_LOGGED.clear()


def _row(db, symbol="TESTCO"):
    row = models.WatchlistEntry(
        mode="DEMO", symbol=symbol, catalyst_type="bulk_block", catalyst_price=100.0,
        catalyst_ts=datetime.now(timezone.utc), horizon_class="mid", decay_half_life_days=3.0,
        entry_band_pct=0.05, source_tier=1, conviction_score=70.0, status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1), created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def test_hold_before_and_after_cutoff(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "14:00")
    assert entry._wl_late_cutoff_hold("REAL", at(WED, 13, 59)) is None
    r = entry._wl_late_cutoff_hold("REAL", at(WED, 14, 3))
    assert r and r.startswith("LATE_ENTRY_CUTOFF")


def test_blank_cutoff_and_weekend_never_hold(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "")
    assert entry._wl_late_cutoff_hold("REAL", at(WED, 15, 0)) is None
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "14:00")
    assert entry._wl_late_cutoff_hold("REAL", at(SAT, 15, 0)) is None


def test_logs_once_per_mode_per_day(monkeypatch, caplog):
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "14:00")
    with caplog.at_level("INFO"):
        for m in (3, 6, 9):
            entry._wl_late_cutoff_hold("REAL", at(WED, 14, m))
        entry._wl_late_cutoff_hold("DEMO", at(WED, 14, 3))
    held = [r for r in caplog.records if "HELD" in r.getMessage()]
    assert len(held) == 2   # one per (site, mode)


def test_guard_error_means_not_late(monkeypatch):
    def boom(now=None):
        raise RuntimeError("x")
    monkeypatch.setattr(entry.calendar_gate, "late_entry_block_reason", boom)
    assert entry._wl_late_cutoff_hold("REAL") is None


def test_trigger_queues_nothing_after_cutoff_and_keeps_rows_active(db, monkeypatch):
    row = _row(db)
    monkeypatch.setattr(entry, "_wl_late_cutoff_hold", lambda mode, now=None: "LATE_ENTRY_CUTOFF:test")
    called = []

    async def _quotes(symbols, **_kw):
        called.append(symbols)
        return {"TESTCO": Tick(symbol="TESTCO", price=100.0, as_of=datetime.now(timezone.utc), atr=1.5, source="test")}
    monkeypatch.setattr(entry, "get_quotes", _quotes)
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["queued"] == 0 and tally["late_cutoff"] == 1
    assert called == []                                   # no price lookup at all
    db.refresh(row)
    assert row.status == "active"
    assert db.query(models.TradeCandidate).count() == 0


def test_trigger_unchanged_when_not_late(db, monkeypatch):
    _row(db)
    monkeypatch.setattr(entry, "_wl_late_cutoff_hold", lambda mode, now=None: None)

    async def _quotes(symbols, **_kw):
        return {"TESTCO": Tick(symbol="TESTCO", price=100.0, as_of=datetime.now(timezone.utc), atr=1.5, source="test")}
    monkeypatch.setattr(entry, "get_quotes", _quotes)
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert "late_cutoff" not in tally and tally["watchlist_checked"] >= 1


# ---- round 621 (E646): the calendar cut-offs hold the trigger too ------------------------------------------------------------------------
FRI = (2026, 11, 20)      # an ordinary Friday
TUE = (2026, 11, 17)      # a Tuesday = the weekly expiry weekday (CALENDAR_EXPIRY_WEEKDAY default 1)


def test_friday_cutoff_holds_trigger(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_RULES_ENABLED", True)
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "")
    monkeypatch.setattr(config, "CALENDAR_FRIDAY_ENTRY_CUTOFF_IST", "13:00")
    assert entry._wl_late_cutoff_hold("REAL", at(FRI, 12, 55)) is None
    r = entry._wl_late_cutoff_hold("REAL", at(FRI, 13, 5))
    assert r and r.startswith("CALENDAR_FRIDAY_CUTOFF")


def test_expiry_cutoff_holds_trigger_when_general_cutoff_is_off(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_RULES_ENABLED", True)
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "")
    monkeypatch.setattr(config, "CALENDAR_EXPIRY_ENTRY_CUTOFF_IST", "14:30")
    monkeypatch.setattr(config, "CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST", "")
    assert entry._wl_late_cutoff_hold("REAL", at(TUE, 14, 25)) is None
    r = entry._wl_late_cutoff_hold("REAL", at(TUE, 14, 35))
    assert r and "CUTOFF" in r


def test_calendar_rules_off_never_hold(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_RULES_ENABLED", False)
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "")
    monkeypatch.setattr(config, "CALENDAR_FRIDAY_ENTRY_CUTOFF_IST", "13:00")
    assert entry._wl_late_cutoff_hold("REAL", at(FRI, 15, 0)) is None


def test_ordinary_day_no_calendar_hold(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_RULES_ENABLED", True)
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "")
    monkeypatch.setattr(config, "CALENDAR_FRIDAY_ENTRY_CUTOFF_IST", "13:00")
    assert entry._wl_late_cutoff_hold("REAL", at((2026, 11, 18), 15, 0)) is None       # a Wednesday: not Friday / expiry


# ---- round 624 (E649): evaluate_mode returns before pricing / sizing once the cut-off has passed -------------------------------------------
def _cand(db, symbol="TESTCO"):
    c = models.TradeCandidate(mode="DEMO", symbol=symbol, decision_label="STRONG_BUY", conviction_score=80.0,
                              received_at=datetime.now(timezone.utc), consumed=False)
    db.add(c)
    db.commit()
    return c


def test_evaluate_mode_returns_before_pricing_after_cutoff(db, monkeypatch):
    c = _cand(db)
    monkeypatch.setattr(entry, "_wl_late_cutoff_hold", lambda mode, now=None, who="x", note="": "LATE_ENTRY_CUTOFF:test")
    quoted = []

    async def _quotes(symbols, **_kw):
        quoted.append(symbols)
        return {}
    monkeypatch.setattr(entry, "get_quotes", _quotes)
    res = run(entry.evaluate_mode(db, "DEMO", True))
    assert res["late_cutoff"] == 1 and res["waited"] == 1 and res["evaluated"] == 0 and res["entered"] == 0
    assert quoted == []                                     # nothing priced
    db.refresh(c)
    assert c.consumed is False                              # still queued
    assert db.query(models.TradeDecision).count() == 0      # no decision rows


def test_evaluate_mode_site_logs_once(db, monkeypatch, caplog):
    monkeypatch.setattr(config, "ENTRY_LAST_TIME_IST", "14:00")
    monkeypatch.setattr(entry.calendar_gate, "late_entry_block_reason", lambda now=None: "LATE_ENTRY_CUTOFF: test")
    _cand(db)
    with caplog.at_level("INFO"):
        run(entry.evaluate_mode(db, "DEMO", True))
        run(entry.evaluate_mode(db, "DEMO", True))
    held = [r.getMessage() for r in caplog.records if "entry_engine[DEMO]: HELD" in r.getMessage()]
    assert len(held) == 1 and "1 candidate(s) left queued" in held[0]
