"""E220: a manual BUY is refused while the market is closed and after today's one-shot EOD square-off has run (orders/manual_buy_window.py).

check() tests use a fixed clock and run anywhere. The integration tests drive attempt_manual_entry() on the test_entry fixtures (real models on
in-memory SQLite, scripted broker) and need real pytest + SQLAlchemy."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import config
import tz_utils
from orders import manual_buy_window
from tz_utils import IST

MON = datetime(2026, 10, 26, tzinfo=IST)      # a Monday, not an NSE holiday
SAT = datetime(2026, 10, 24, tzinfo=IST)
TODAY = "2026-10-26"


def at(day, h, m=0):
    return day.replace(hour=h, minute=m)


def gate(fired=None):
    return SimpleNamespace(eod_squareoff_fired_date=fired)


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    """conftest turns the guard off for older tests; every test here wants the real default."""
    monkeypatch.setattr(config, "MANUAL_BUY_WINDOW_GUARD", True, raising=False)


# ---- check() -------------------------------------------------------------------------------------------------------
def test_allowed_inside_the_session_before_the_sweep():
    assert manual_buy_window.check(gate(), TODAY, at(MON, 11, 0)) is None
    assert manual_buy_window.check(gate(fired="2026-10-23"), TODAY, at(MON, 14, 59)) is None   # an older sweep date does not count


def test_refused_when_market_closed():
    for when in (at(MON, 8, 0), at(MON, 15, 31), at(MON, 20, 0), at(SAT, 11, 0)):
        msg = manual_buy_window.check(gate(), TODAY, when)
        assert msg and "market is closed" in msg, when


def test_refused_after_todays_sweep_even_inside_market_hours():
    msg = manual_buy_window.check(gate(fired=TODAY), TODAY, at(MON, 15, 10))
    assert msg and "square-off has already run" in msg


def test_closed_message_wins_over_swept_message():
    assert "market is closed" in manual_buy_window.check(gate(fired=TODAY), TODAY, at(MON, 18, 0))


def test_session_edges():
    assert manual_buy_window.check(gate(), TODAY, at(MON, 9, 15)) is None
    assert manual_buy_window.check(gate(), TODAY, at(MON, 15, 30)) is None
    assert manual_buy_window.check(gate(), TODAY, at(MON, 9, 14)) is not None


def test_missing_gate_row_only_checks_the_clock():
    assert manual_buy_window.check(None, TODAY, at(MON, 11, 0)) is None
    assert manual_buy_window.check(None, TODAY, at(MON, 20, 0)) is not None


def test_switch_off_restores_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(config, "MANUAL_BUY_WINDOW_GUARD", False)
    assert manual_buy_window.check(gate(fired=TODAY), TODAY, at(MON, 20, 0)) is None


def test_never_raises_and_fails_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("calendar broke")
    monkeypatch.setattr(tz_utils, "is_market_open_ist", boom)
    assert manual_buy_window.check(gate(fired=TODAY), TODAY, at(MON, 11, 0)) is None


def test_switch_is_registered():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert "MANUAL_BUY_WINDOW_GUARD" in open(os.path.join(here, "control", "catalog.py")).read()
    assert "MANUAL_BUY_WINDOW_GUARD" in open(os.path.join(here, "config.py")).read()
    assert "MANUAL_BUY_WINDOW_GUARD" in open(os.path.join(here, "..", "calibration-runner-service", "autocalib.py")).read()


# ---- through attempt_manual_entry (needs the test_entry fixtures) ----------------------------------------------------
from orders import entry                                     # noqa: E402
from test_entry import available, env, gate_of, n_positions   # noqa: E402,F401


def _clock(monkeypatch, open_):
    monkeypatch.setattr(tz_utils, "is_market_open_ist", lambda now=None, honor_session_proof=True: open_)


def test_manual_buy_refused_when_market_closed_places_nothing(env, monkeypatch):
    db, b, _ = env
    monkeypatch.setattr(config, "MANUAL_BUY_WINDOW_GUARD", True)
    _clock(monkeypatch, False)
    before = available(db)
    with pytest.raises(entry.ManualEntryRejected, match="market is closed"):
        entry.attempt_manual_entry(db, "ABC", 500.0)
    assert b.of("place_super_order") == [] and b.of("place_order") == []
    assert n_positions(db) == 0
    assert available(db) == pytest.approx(before)       # nothing reserved


def test_manual_buy_refused_after_the_eod_sweep(env, monkeypatch):
    db, b, _ = env
    monkeypatch.setattr(config, "MANUAL_BUY_WINDOW_GUARD", True)
    _clock(monkeypatch, True)
    g = gate_of(db)
    g.eod_squareoff_fired_date = entry.ist_today_str()
    db.commit()
    with pytest.raises(entry.ManualEntryRejected, match="square-off has already run"):
        entry.attempt_manual_entry(db, "ABC", 500.0)
    assert b.of("place_super_order") == [] and b.of("place_order") == []


def test_manual_buy_goes_through_inside_the_window(env, monkeypatch):
    db, _, _ = env
    monkeypatch.setattr(config, "MANUAL_BUY_WINDOW_GUARD", True)
    _clock(monkeypatch, True)
    assert entry.attempt_manual_entry(db, "ABC", 500.0).status == "OPEN"
