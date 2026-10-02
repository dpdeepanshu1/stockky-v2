"""tests/test_excursion.py — max/min price seen while OPEN (2026-10-02)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from feed import ws_client
from orders import excursion

T0 = datetime(2026, 10, 2, 4, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _pos(db, symbol="X", status="OPEN", entry=100.0, opened=T0):
    p = models.ScalpPosition(
        symbol=symbol, dhan_security_id=symbol, window_source="5m", status=status,
        entry_price=entry, quantity=1, target_price=104.0, stop_price=98.0,
        adaptive_target_pct=4.0, adaptive_stop_pct=2.0, capital_risked=entry,
        opened_at=opened.replace(tzinfo=None),   # DB hands datetimes back naive-UTC
    )
    db.add(p)
    db.commit()
    return p


def _ticks(monkeypatch, ticks):
    monkeypatch.setattr(ws_client, "get_tick_buffer", lambda sym: ticks)


def test_records_high_and_low_since_open(db, monkeypatch):
    p = _pos(db)
    t = T0.timestamp()
    _ticks(monkeypatch, [(t + 1, 101.0), (t + 2, 103.5), (t + 3, 99.2), (t + 4, 100.5)])
    assert excursion.run_excursion_tracking(db) == 1
    assert (p.max_price_seen, p.min_price_seen) == (103.5, 99.2)


def test_ignores_ticks_from_before_the_position_opened(db, monkeypatch):
    p = _pos(db)
    t = T0.timestamp()
    _ticks(monkeypatch, [(t - 600, 120.0), (t - 500, 80.0), (t + 5, 100.4)])
    excursion.run_excursion_tracking(db)
    assert (p.max_price_seen, p.min_price_seen) == (100.4, 100.0)   # entry price bounds the low


def test_extremes_only_widen_never_shrink(db, monkeypatch):
    p = _pos(db)
    t = T0.timestamp()
    _ticks(monkeypatch, [(t + 1, 104.0), (t + 2, 99.0)])
    excursion.run_excursion_tracking(db)
    _ticks(monkeypatch, [(t + 50, 101.0)])            # old spike has rolled out of the buffer
    assert excursion.run_excursion_tracking(db) == 0
    assert (p.max_price_seen, p.min_price_seen) == (104.0, 99.0)
    _ticks(monkeypatch, [(t + 60, 105.0)])
    assert excursion.run_excursion_tracking(db) == 1
    assert p.max_price_seen == 105.0 and p.min_price_seen == 99.0


def test_closed_positions_are_not_touched(db, monkeypatch):
    p = _pos(db, status="STOP_HIT")
    _ticks(monkeypatch, [(T0.timestamp() + 1, 110.0)])
    assert excursion.run_excursion_tracking(db) == 0
    assert p.max_price_seen is None


def test_no_ticks_or_feed_error_is_fail_open(db, monkeypatch):
    p = _pos(db)
    _ticks(monkeypatch, [])
    assert excursion.run_excursion_tracking(db) == 0

    def boom(sym):
        raise RuntimeError("ws down")
    monkeypatch.setattr(ws_client, "get_tick_buffer", boom)
    assert excursion.run_excursion_tracking(db) == 0
    assert p.max_price_seen is None


def test_bad_ticks_skipped(db, monkeypatch):
    p = _pos(db)
    t = T0.timestamp()
    _ticks(monkeypatch, [(t + 1, 0.0), (t + 2, -5.0), (t + 3, None), (t + 4, 101.0)])
    excursion.run_excursion_tracking(db)
    assert (p.max_price_seen, p.min_price_seen) == (101.0, 100.0)


def test_excursion_pcts(db):
    p = _pos(db)
    assert excursion.excursion_pcts(p) == {"max_gain_pct": None, "max_drawdown_pct": None}
    p.max_price_seen, p.min_price_seen = 101.234, 98.5
    assert excursion.excursion_pcts(p) == {"max_gain_pct": 1.23, "max_drawdown_pct": -1.5}
