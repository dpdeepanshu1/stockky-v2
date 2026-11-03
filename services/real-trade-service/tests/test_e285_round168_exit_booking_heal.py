"""E285 round 168 (CHOICEIN, 3 Nov): the 15:00 square-off sold the share and the fill was booked as an event, but the position row stayed OPEN, and the
holdings sync could not close it because a closed same-day CNC row (netQty 0, buyQty 1) read as "still held".
Pure helper tests first (stdlib), then the holdings_sync_reconcile cases on the test database."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import models
from execution import dhan_client
from portfolio import portfolio as pf
from tests.test_portfolio import _open_position  # noqa: F401
from tests.test_portfolio import db  # noqa: F401

_spec = importlib.util.spec_from_file_location("exit_booking_heal", Path(__file__).resolve().parent.parent / "portfolio" / "exit_booking_heal.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)


# ------------------------------------------------------------------ pure helpers
def test_closed_same_day_row_holds_nothing():
    assert H.broker_row_qty({"tradingSymbol": "CHOICEIN", "netQty": 0, "buyQty": 1, "sellQty": 1}) == 0
    assert H.broker_row_qty({"netQty": "0", "buyQty": 1}) == 0


def test_open_long_and_short_rows():
    assert H.broker_row_qty({"netQty": 3, "buyQty": 3}) == 3
    assert H.broker_row_qty({"netQty": -2, "sellQty": 2}) == 0     # a short is not a holding


def test_unreadable_or_missing_net_falls_back_to_the_buy_side():
    assert H.broker_row_qty({"buyQty": 5}) == 5
    assert H.broker_row_qty({"netQty": "n/a", "positiveQty": 4}) == 4
    assert H.broker_row_qty({}) == 0


def test_switch_off_restores_the_old_reading():
    assert H.broker_row_qty({"netQty": 0, "buyQty": 1}, net_authoritative=False) == 1


def test_booked_exit_totals_reads_the_event_details():
    evs = [("PARTIAL_EXIT", "eod_squareoff: 1 @ 746.05 (pnl -10.40, broker-confirmed)"),
           ("CLOSED", "stop_hit: 2 @ 2054.4 (pnl -10.60, broker-confirmed)"),
           ("STOP_TRAILED", "x: 9 @ 1.0 (pnl +1.00, broker-confirmed)"),      # wrong event type
           ("PARTIAL_EXIT", "no numbers here")]
    assert H.booked_exit_totals(evs) == (3, -21.0)
    assert H.booked_exit_totals([]) == (0, 0.0)


# ------------------------------------------------------------------ holdings_sync_reconcile
def _old():
    return datetime.now(timezone.utc) - timedelta(hours=2)


def _cash(db, v=5000.0):
    acct = db.query(models.TradeAccount).filter_by(mode="REAL").first()
    acct.cash_available = v
    db.commit()
    return acct


def _feeds(monkeypatch, holdings, positions):
    monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: holdings)
    monkeypatch.setattr(dhan_client, "get_positions", lambda db_: positions)


CLOSED_ROW = {"tradingSymbol": "CHOICEIN", "productType": "CNC", "buyQty": 1, "sellQty": 1, "netQty": 0, "sellAvg": 746.05}


def test_choicein_replay_open_row_with_a_booked_exit_is_closed_without_a_second_refund(db, monkeypatch):
    pos = _open_position(db, symbol="CHOICEIN", qty=1, entry=756.45, opened_at=_old())
    acct = _cash(db)
    db.add(models.TradePositionEvent(position_id=pos.id, event_type="PARTIAL_EXIT",
                                     detail="eod_squareoff: 1 @ 746.05 (pnl -10.40, broker-confirmed)"))
    db.commit()
    _feeds(monkeypatch, [], [CLOSED_ROW])
    res = pf.holdings_sync_reconcile(db)
    assert res["closed"] == 1 and res["symbols"] == ["CHOICEIN"]
    db.refresh(pos)
    assert pos.status == "CLOSED" and pos.qty_open == 0 and pos.realized_pnl == -10.40
    db.refresh(acct)
    assert acct.cash_available == 5000.0                      # the exit's cash was already credited with its event
    ev = db.query(models.TradePositionEvent).filter_by(position_id=pos.id, event_type="GHOST_CLOSED").one()
    assert "no cash refunded" in ev.detail


def test_flat_row_with_no_booked_exit_is_a_ghost_and_is_refunded_as_before(db, monkeypatch):
    pos = _open_position(db, symbol="CHOICEIN", qty=1, entry=756.45, opened_at=_old())
    acct = _cash(db)
    _feeds(monkeypatch, [], [CLOSED_ROW])
    assert pf.holdings_sync_reconcile(db)["closed"] == 1
    db.refresh(pos)
    assert pos.status == "CLOSED" and pos.realized_pnl == 0.0
    db.refresh(acct)
    assert acct.cash_available == 5000.0 + 756.45


def test_a_still_held_row_is_left_alone(db, monkeypatch):
    pos = _open_position(db, symbol="CHOICEIN", qty=1, entry=756.45, opened_at=_old())
    _cash(db)
    _feeds(monkeypatch, [], [{"tradingSymbol": "CHOICEIN", "productType": "CNC", "buyQty": 1, "netQty": 1}])
    assert pf.holdings_sync_reconcile(db)["closed"] == 0
    db.refresh(pos)
    assert pos.status == "OPEN" and pos.qty_open == 1


def test_switch_off_keeps_the_old_reading(db, monkeypatch):
    pos = _open_position(db, symbol="CHOICEIN", qty=1, entry=756.45, opened_at=_old())
    _cash(db)
    monkeypatch.setattr(config, "HOLDINGS_SYNC_NET_QTY", False)
    _feeds(monkeypatch, [], [CLOSED_ROW])
    assert pf.holdings_sync_reconcile(db)["closed"] == 0
    db.refresh(pos)
    assert pos.status == "OPEN"
