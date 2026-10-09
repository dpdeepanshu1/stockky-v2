"""
group192 (HEGAM retry loop): a Super Order is ACCEPTED by Dhan's API and rejected a moment later by RMS
("not allowed to be traded in Intraday"). The rejection only surfaced in orders/reconcile.py as a dead
ENTRY_LEG, nothing recorded why, and attempt_entry() bought the same symbol again on the next cycle:
13 rejected HEGAM orders in four minutes.

Pinned here:
  * reconcile reads the rejection reason (super row first, then the plain order book),
  * an intraday-restricted / circuit-limit rejection is recorded in the restricted-symbol list,
  * a CANCELLED entry or an unrelated rejection is NOT recorded as restricted,
  * attempt_entry's guard skips the symbol for a cooldown, and for the day after N dead entries,
  * the guard ignores other symbols, old days, and other ERROR kinds, and fails open,
  * GET /trades/history reports rejected entries separately from trades.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_group192_entry_reject_learning.py -q
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
from execution import dhan_client
from orders import entry, reconcile
from screening import intraday_eligibility

RMS_TEXT = "RMS:34326100615275:Order rejected as this stock is not allowed to be traded in Intraday."


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_REJECT_COOLDOWN_MINUTES", 30)
    monkeypatch.setattr(config, "ENTRY_REJECT_MAX_PER_SYMBOL_DAY", 2)


def _pos(db, symbol="HEGAM", status="OPEN", error_message=None, closed_at=None, super_id="SO1"):
    p = models.ScalpPosition(
        symbol=symbol, dhan_security_id="1", window_source="5m", status=status, entry_price=242.45, quantity=13,
        target_price=247.75, stop_price=238.77, adaptive_target_pct=2.2, adaptive_stop_pct=1.5,
        capital_risked=3152.0, dhan_super_order_id=super_id, error_message=error_message, closed_at=closed_at,
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    db.add(p)
    db.commit()
    return p


def _super_row(status="REJECTED", **kw):
    row = {"orderId": "SO1", "orderStatus": status, "legName": "ENTRY_LEG", "legDetails": []}
    row.update(kw)
    return row


@pytest.fixture()
def broker(monkeypatch):
    class B:
        super_orders: list = []
        plain_orders: list = []
        plain_error = None
        plain_calls = 0

    b = B()

    def _plain(_db):
        b.plain_calls += 1
        if b.plain_error:
            raise b.plain_error
        return list(b.plain_orders)

    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda _db: list(b.super_orders))
    monkeypatch.setattr(dhan_client, "get_order_list", _plain)
    monkeypatch.setattr(dhan_client, "get_trade_history", lambda *a, **k: [])
    return b


# ── reconcile: reason capture + restriction learning ─────────────────────────
class TestReconcileLearnsFromRejection:
    def test_reason_on_the_super_row_is_stored_and_symbol_is_restricted(self, db, broker):
        p = _pos(db)
        broker.super_orders = [_super_row(omsErrorDescription=RMS_TEXT)]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "ERROR"
        assert p.error_message.startswith("Entry leg REJECTED on Dhan (reconciled): ")
        assert "not allowed to be traded in Intraday" in p.error_message
        assert "HEGAM" in intraday_eligibility.get_restricted_symbols(db)
        # group284: the whole reconcile pass also reads the plain order book in _filled_entry_behind_dead_parent
        # (a different guard), so count calls only around the reason lookup itself: the super row already
        # carries the reason, so that lookup must not touch the order book.
        broker.plain_calls = 0
        assert reconcile._entry_reject_reason(db, p, _super_row(omsErrorDescription=RMS_TEXT)) != ""
        assert broker.plain_calls == 0

    def test_reason_found_in_the_plain_order_book_when_the_super_row_has_none(self, db, broker):
        p = _pos(db)
        broker.super_orders = [_super_row()]
        broker.plain_orders = [{"orderId": "OTHER", "omsErrorDescription": "nope"},
                               {"orderId": "SO1", "orderStatus": "REJECTED", "omsErrorDescription": RMS_TEXT}]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert "not allowed to be traded in Intraday" in p.error_message
        assert intraday_eligibility.is_restricted(db, "HEGAM")

    def test_order_book_failure_still_closes_the_position_without_a_reason(self, db, broker):
        p = _pos(db)
        broker.super_orders = [_super_row()]
        broker.plain_error = RuntimeError("dhan down")
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "ERROR" and p.error_message == "Entry leg REJECTED on Dhan (reconciled)"
        assert not intraday_eligibility.is_restricted(db, "HEGAM")

    def test_no_order_id_anywhere_means_no_lookup(self, db, broker):
        p = _pos(db, super_id=None)
        row = _super_row()
        row.pop("orderId")
        assert reconcile._entry_reject_reason(db, p, row) == ""
        assert broker.plain_calls == 0

    def test_circuit_limit_rejection_is_recorded_too(self, db, broker):
        _pos(db)
        broker.super_orders = [_super_row(omsErrorDescription="RMS:1:Rate Not Within Ckt Limit 395.25 To 592.85")]
        reconcile.run_exit_reconciliation(db)
        assert intraday_eligibility.is_restricted(db, "HEGAM")

    def test_unrelated_rejection_is_not_recorded_as_restricted(self, db, broker):
        p = _pos(db)
        broker.super_orders = [_super_row(omsErrorDescription="RMS:1:Margin shortfall")]
        reconcile.run_exit_reconciliation(db)
        assert p.status == "ERROR" and "Margin shortfall" in p.error_message
        assert not intraday_eligibility.is_restricted(db, "HEGAM")

    def test_a_cancelled_entry_is_never_recorded_as_restricted(self, db, broker):
        p = _pos(db)
        broker.super_orders = [_super_row("CANCELLED", omsErrorDescription=RMS_TEXT)]
        reconcile.run_exit_reconciliation(db)
        assert p.status == "ERROR" and p.error_message.startswith("Entry leg CANCELLED")
        assert not intraday_eligibility.is_restricted(db, "HEGAM")

    def test_recording_failure_never_blocks_closing_the_position(self, db, broker, monkeypatch):
        p = _pos(db)

        def boom(*a, **k):
            raise RuntimeError("db hiccup")

        monkeypatch.setattr(intraday_eligibility, "record_restriction", boom)
        broker.super_orders = [_super_row(omsErrorDescription=RMS_TEXT)]
        assert reconcile.run_exit_reconciliation(db) == 1 and p.status == "ERROR"

    def test_reason_keys_are_combined_without_duplicates(self):
        row = {"omsErrorCode": "RMS-12", "omsErrorDescription": "bad", "remarks": "bad", "reason": None}
        assert reconcile._reason_from_row(row) == "bad | RMS-12"
        assert reconcile._reason_from_row(None) == ""
        assert reconcile._reason_from_row({"orderId": "x"}) == ""


# ── entry guard ──────────────────────────────────────────────────────────────
def _dead(db, symbol="HEGAM", minutes_ago=1.0, reason=": RMS not allowed"):
    return _pos(db, symbol=symbol, status="ERROR", error_message=f"Entry leg REJECTED on Dhan (reconciled){reason}",
                closed_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


class TestRejectedEntryGuard:
    def test_no_history_allows_the_entry(self, db):
        assert entry._rejected_entry_reject(db, "HEGAM") is None

    def test_recent_dead_entry_blocks_for_the_cooldown(self, db):
        _dead(db, minutes_ago=2)
        msg = entry._rejected_entry_reject(db, "HEGAM")
        assert msg and msg.startswith("ENTRY_REJECT_COOLDOWN:") and "waiting 30m" in msg

    def test_cooldown_expires(self, db):
        _dead(db, minutes_ago=45)
        assert entry._rejected_entry_reject(db, "HEGAM") is None

    def test_two_dead_entries_today_block_for_the_rest_of_the_day(self, db):
        # group284: minutes_ago=100/90 fell on YESTERDAY's IST date when the suite ran between 00:00 and 01:40 IST,
        # so the guard (which counts IST-today rows only) saw no dead entries. Keep both inside the first minutes.
        _dead(db, minutes_ago=2)
        _dead(db, minutes_ago=1)
        msg = entry._rejected_entry_reject(db, "HEGAM")
        assert msg and msg.startswith("ENTRY_REJECTED_TODAY:") and "limit 2" in msg

    def test_other_symbols_are_unaffected(self, db):
        _dead(db, symbol="HEGAM", minutes_ago=1)
        assert entry._rejected_entry_reject(db, "SBIN") is None

    def test_yesterdays_dead_entries_do_not_count(self, db):
        _dead(db, minutes_ago=60 * 30)
        _dead(db, minutes_ago=60 * 31)
        assert entry._rejected_entry_reject(db, "HEGAM") is None

    def test_other_error_kinds_do_not_count(self, db):
        _pos(db, status="ERROR", error_message="EOD_SQUAREOFF_SELL_DEAD: order X came back REJECTED",
             closed_at=datetime.now(timezone.utc))
        assert entry._rejected_entry_reject(db, "HEGAM") is None

    def test_both_knobs_zero_disables_the_guard(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_REJECT_COOLDOWN_MINUTES", 0)
        monkeypatch.setattr(config, "ENTRY_REJECT_MAX_PER_SYMBOL_DAY", 0)
        _dead(db, minutes_ago=1)
        assert entry._rejected_entry_reject(db, "HEGAM") is None

    def test_day_limit_alone_does_not_apply_cooldown_when_cooldown_is_zero(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_REJECT_COOLDOWN_MINUTES", 0)
        _dead(db, minutes_ago=1)
        assert entry._rejected_entry_reject(db, "HEGAM") is None

    def test_day_limit_disabled_leaves_only_the_cooldown(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_REJECT_MAX_PER_SYMBOL_DAY", 0)
        monkeypatch.setattr(config, "ENTRY_REJECT_COOLDOWN_MINUTES", 1)   # group284: see the note above (IST midnight)
        _dead(db, minutes_ago=6)
        _dead(db, minutes_ago=5)
        assert entry._rejected_entry_reject(db, "HEGAM") is None

    def test_db_error_fails_open(self):
        class Broken:
            def query(self, *a, **k):
                raise RuntimeError("boom")

        assert entry._rejected_entry_reject(Broken(), "HEGAM") is None

    def test_rows_without_closed_at_are_ignored(self, db):
        _pos(db, status="ERROR", error_message="Entry leg REJECTED on Dhan (reconciled)", closed_at=None)
        assert entry._rejected_entry_reject(db, "HEGAM") is None


# ── /trades/history ──────────────────────────────────────────────────────────
def test_trades_history_counts_rejected_entries_apart_from_trades(db):
    import main as m

    _dead(db, minutes_ago=1)
    _dead(db, minutes_ago=2)
    win = _pos(db, symbol="SBIN", status="TARGET_HIT", closed_at=datetime.now(timezone.utc))
    win.realized_pnl, win.exit_price = 25.0, 250.0
    db.commit()
    out = m.trades_history(db=db, limit=200, status_filter=None, range=None)
    assert out["summary"]["rejected_entries"] == 2
    assert out["summary"]["total_trades"] == 1 and out["summary"]["wins"] == 1
