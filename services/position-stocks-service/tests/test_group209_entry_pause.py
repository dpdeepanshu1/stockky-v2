"""
group209 (item 15): margin rejections pause ALL new entries briefly; an unclassified BUY placement failure rests
that one symbol; a margin-rejected dead entry no longer counts against the symbol's own group-192 limits.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_group209_entry_pause.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import models
from orders import entry, entry_pause, reconcile

MARGIN_TEXT = "RMS:1:Insufficient Funds. Add Rs. 1200 to place this order."


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture()
def clock(monkeypatch):
    class C:
        t = 1_000_000.0
    monkeypatch.setattr(entry_pause, "_now", lambda: C.t)
    return C


class TestEntryPauseModule:
    def test_nothing_paused_by_default(self):
        assert entry_pause.all_paused() is None
        assert entry_pause.symbol_blocked("SBIN") is None

    def test_pause_all_blocks_then_expires(self, clock):
        entry_pause.pause_all("INSUFFICIENT_FUNDS", 5)
        msg = entry_pause.all_paused()
        assert msg and msg.startswith("ENTRY_MARGIN_PAUSE:") and "INSUFFICIENT_FUNDS" in msg
        clock.t += 4 * 60
        assert entry_pause.all_paused() is not None
        clock.t += 61
        assert entry_pause.all_paused() is None

    def test_pause_all_extends_but_never_shortens(self, clock):
        entry_pause.pause_all("a", 5)
        entry_pause.pause_all("b", 1)
        assert "(a)" in entry_pause.all_paused()
        entry_pause.pause_all("c", 10)
        assert "(c)" in entry_pause.all_paused()

    def test_zero_or_negative_minutes_disable(self):
        entry_pause.pause_all("x", 0)
        entry_pause.pause_all("x", -3)
        entry_pause.cooldown_symbol("SBIN", 0)
        assert entry_pause.all_paused() is None and entry_pause.symbol_blocked("SBIN") is None

    def test_symbol_cooldown_is_per_symbol_and_expires(self, clock):
        entry_pause.cooldown_symbol("SBIN", 5, "boom")
        msg = entry_pause.symbol_blocked("SBIN")
        assert msg and msg.startswith("ENTRY_ORDER_FAILED_COOLDOWN:SBIN") and "boom" in msg
        assert entry_pause.symbol_blocked("TCS") is None
        clock.t += 301
        assert entry_pause.symbol_blocked("SBIN") is None

    def test_blank_symbol_ignored_and_reset_clears(self):
        entry_pause.cooldown_symbol("", 5)
        entry_pause.pause_all("x", 5)
        entry_pause.cooldown_symbol("SBIN", 5)
        entry_pause.reset()
        assert entry_pause.all_paused() is None and entry_pause.symbol_blocked("SBIN") is None


def _dead(db, symbol, reason, minutes_ago=1.0):
    p = models.ScalpPosition(
        symbol=symbol, dhan_security_id="1", window_source="5m", status="ERROR", entry_price=100.0, quantity=1,
        target_price=102.0, stop_price=99.0, adaptive_target_pct=2.0, adaptive_stop_pct=1.0, capital_risked=100.0,
        dhan_super_order_id="SO", error_message=f"Entry leg REJECTED on Dhan (reconciled){reason}",
        closed_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    db.add(p)
    db.commit()
    return p


class TestSymbolGuardIgnoresMarginRejections:
    def test_margin_rejected_entries_do_not_block_the_symbol(self, db):
        _dead(db, "SBIN", ": " + MARGIN_TEXT, 1)
        _dead(db, "SBIN", ": " + MARGIN_TEXT, 2)
        _dead(db, "SBIN", ": " + MARGIN_TEXT, 3)
        assert entry._rejected_entry_reject(db, "SBIN") is None

    def test_other_rejections_still_block(self, db):
        _dead(db, "HEGAM", ": RMS not allowed to be traded in Intraday", 1)
        assert entry._rejected_entry_reject(db, "HEGAM").startswith("ENTRY_REJECT_COOLDOWN:")

    def test_margin_rows_do_not_count_toward_the_day_cap(self, db):
        _dead(db, "SBIN", ": " + MARGIN_TEXT, 100)
        _dead(db, "SBIN", ": some other RMS reject", 90)
        # only one non-margin dead entry -> day cap (2) not reached, and it is 90 min old (> 30 cooldown)
        assert entry._rejected_entry_reject(db, "SBIN") is None


class TestReconcileMarginRejectionPausesEntries:
    def _pos(self, db):
        p = models.ScalpPosition(
            symbol="SBIN", dhan_security_id="1", window_source="5m", status="OPEN", entry_price=100.0, quantity=1,
            target_price=102.0, stop_price=99.0, adaptive_target_pct=2.0, adaptive_stop_pct=1.0,
            capital_risked=100.0, dhan_super_order_id="SO1",
            opened_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db.add(p)
        db.commit()
        return p

    def test_margin_reject_pauses_all_and_does_not_restrict_symbol(self, db, monkeypatch):
        from execution import dhan_client
        from screening import intraday_eligibility
        monkeypatch.setattr(config, "ENTRY_MARGIN_PAUSE_MINUTES", 5)
        p = self._pos(db)
        reconcile._learn_from_entry_rejection(db, p, "REJECTED", MARGIN_TEXT)
        assert entry_pause.all_paused() is not None
        assert not intraday_eligibility.is_restricted(db, "SBIN")

    def test_cancelled_entry_never_pauses(self, db):
        p = self._pos(db)
        reconcile._learn_from_entry_rejection(db, p, "CANCELLED", MARGIN_TEXT)
        assert entry_pause.all_paused() is None

    def test_pause_knob_zero_disables(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_MARGIN_PAUSE_MINUTES", 0)
        p = self._pos(db)
        reconcile._learn_from_entry_rejection(db, p, "REJECTED", MARGIN_TEXT)
        assert entry_pause.all_paused() is None
