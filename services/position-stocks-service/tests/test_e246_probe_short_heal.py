"""E246: an adopted SELL-probe short whose cover already filled at Dhan is closed by reconcile (ONGC, 28 Oct 2026).

The live picture: Dhan's positions showed ONGC closed (sold 223.83, bought 223.85) while Stockky kept an OPEN / EXIT_LEGS_REJECTED short row with 'Cover -'.
Pinned here: the row closes at the cover's real price with a short's P&L sign; nothing is closed while Dhan still shows a short, an unreadable book, or no row at all;
live (non-probe) shorts are never touched; and a cover BUY is never sent for a row that is already covered."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from execution import dhan_client
from orders import eod_squareoff, reconcile

SID = "2475"


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    monkeypatch.setattr(reconcile.notifier, "notify_fire_and_forget", lambda *a, **k: None)
    yield s
    s.close()


def _row(db, *, source="PROBE", status="EXIT_LEGS_REJECTED", exit_oid=None, sid=SID):
    p = models.ScalpPosition(symbol="ONGC", dhan_security_id=sid, window_source=source, status=status, side="SHORT", entry_price=223.83, signal_price=223.83, quantity=1,
                             target_price=217.13, stop_price=227.21, adaptive_target_pct=3.0, adaptive_stop_pct=1.5, dhan_super_order_id="23326102817975",
                             dhan_entry_order_id="23326102817975", dhan_exit_order_id=exit_oid, capital_risked=223.83, opened_at=datetime.now(timezone.utc))
    db.add(p)
    db.commit()
    return p


def _dhan(monkeypatch, positions, orders=()):
    monkeypatch.setattr(dhan_client, "get_positions", lambda db: positions if positions is not None else (_ for _ in ()).throw(RuntimeError("502")))
    monkeypatch.setattr(dhan_client, "get_order_list", lambda db: list(orders))


FLAT = [{"securityId": SID, "productType": "INTRADAY", "netQty": 0, "sellAvg": 223.83, "buyAvg": 223.85}]


def test_the_ongc_row_is_closed_at_the_cover_price_with_a_short_pnl(db, monkeypatch):
    p = _row(db)
    _dhan(monkeypatch, FLAT)
    assert reconcile.heal_covered_probe_shorts(db, force=True) == 1
    db.refresh(p)
    assert p.status == "MANUAL_EXIT" and p.exit_price == 223.85 and p.closed_at is not None
    assert round(p.realized_pnl, 2) == -0.02 and p.realized_pnl_pct < 0           # sold 223.83, bought back 223.85: a 2 paise loss on a short
    assert p.error_message is None


def test_the_covers_own_order_fill_is_preferred_over_the_position_average(db, monkeypatch):
    p = _row(db, exit_oid="321261028615307")
    book = [{"orderId": "321261028615307", "orderStatus": "TRADED", "transactionType": "BUY", "securityId": SID, "averageTradedPrice": 223.80}]
    _dhan(monkeypatch, FLAT, book)
    assert reconcile.heal_covered_probe_shorts(db, force=True) == 1
    db.refresh(p)
    assert p.exit_price == 223.80 and p.dhan_exit_order_id == "321261028615307"


def test_still_short_at_dhan_is_left_alone(db, monkeypatch):
    p = _row(db)
    _dhan(monkeypatch, [{"securityId": SID, "netQty": -1, "sellAvg": 223.83, "buyAvg": 0.0}])
    assert reconcile.heal_covered_probe_shorts(db, force=True) == 0
    db.refresh(p)
    assert p.status == "EXIT_LEGS_REJECTED" and p.exit_price is None


def test_a_flat_row_with_an_open_delivery_row_is_not_flat(db, monkeypatch):
    p = _row(db)
    _dhan(monkeypatch, [{"securityId": SID, "netQty": 0, "buyAvg": 223.85}, {"securityId": SID, "productType": "CNC", "netQty": 2, "buyAvg": 220.0}])
    assert reconcile.heal_covered_probe_shorts(db, force=True) == 0
    db.refresh(p)
    assert p.status == "EXIT_LEGS_REJECTED"


@pytest.mark.parametrize("positions", [None, [], [{"securityId": "other", "netQty": 0, "buyAvg": 1.0}]])
def test_unreadable_or_missing_positions_prove_nothing(db, monkeypatch, positions):
    p = _row(db)
    _dhan(monkeypatch, positions)
    assert reconcile.heal_covered_probe_shorts(db, force=True) == 0
    db.refresh(p)
    assert p.status == "EXIT_LEGS_REJECTED"


def test_flat_but_no_readable_cover_price_is_left_for_the_next_pass(db, monkeypatch):
    p = _row(db)
    _dhan(monkeypatch, [{"securityId": SID, "netQty": 0, "sellAvg": 223.83, "buyAvg": 0.0}])
    assert reconcile.heal_covered_probe_shorts(db, force=True) == 0
    db.refresh(p)
    assert p.status == "EXIT_LEGS_REJECTED"


def test_a_live_short_is_never_touched(db, monkeypatch):
    p = _row(db, source="1m", status="OPEN")
    _dhan(monkeypatch, FLAT)
    assert reconcile.heal_covered_probe_shorts(db, force=True) == 0
    db.refresh(p)
    assert p.status == "OPEN"


def test_no_probe_rows_means_no_dhan_calls(db, monkeypatch):
    calls = []
    monkeypatch.setattr(dhan_client, "get_positions", lambda d: calls.append(1) or [])
    assert reconcile.heal_covered_probe_shorts(db, force=True) == 0 and calls == []


def test_the_exit_path_sends_no_cover_buy_for_an_already_covered_probe_short(db, monkeypatch):
    p = _row(db)
    _dhan(monkeypatch, FLAT)
    sent = []
    monkeypatch.setattr(dhan_client, "place_order", lambda *a, **k: sent.append(k) or {"orderId": "X"})
    with pytest.raises(eod_squareoff.PositionAlreadyFlat):
        eod_squareoff._fire_flat_sell(db, p)
    assert sent == [], "a BUY here would have opened a real LONG"
    db.refresh(p)
    assert p.status == "MANUAL_EXIT" and p.exit_price == 223.85


def test_run_exit_reconciliation_runs_the_heal_pass(db, monkeypatch):
    p = _row(db)
    _dhan(monkeypatch, FLAT)
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda db, **k: [])
    reconcile._last_probe_heal_ts = float("-inf")
    reconcile.run_exit_reconciliation(db)
    db.refresh(p)
    assert p.status == "MANUAL_EXIT"
