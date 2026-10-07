"""Group 232 - COHANCE phantom OPEN: a holding sold today still sits in Dhan's settled holdings
until T+1, while get_positions() already shows netQty < 0. holdings_sync_reconcile must net the
sell and force-close the stale position."""
import asyncio
from datetime import datetime, timedelta, timezone

import models
from execution import dhan_client
from portfolio import portfolio as pf
from tests.test_portfolio import _open_position  # noqa: F401  (same helper, same fixtures)
from tests.test_portfolio import db  # noqa: F401


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


def test_sold_today_still_in_holdings_is_closed(db, monkeypatch):
    pos = _open_position(db, symbol="COHANCE", qty=2, entry=450.0, opened_at=_old())
    acct = _cash(db)
    _feeds(monkeypatch,
           [{"tradingSymbol": "COHANCE", "totalQty": 2, "avgCostPrice": 450.0}],
           [{"tradingSymbol": "COHANCE", "netQty": -2, "productType": "CNC", "sellAvg": 455.5}])
    res = pf.holdings_sync_reconcile(db)
    assert res["closed"] == 1 and res["symbols"] == ["COHANCE"]
    db.refresh(pos)
    assert pos.status == "CLOSED" and pos.qty_open == 0
    db.refresh(acct)
    assert acct.cash_available == 5000.0 + 900.0
    ev = db.query(models.TradePositionEvent).filter_by(position_id=pos.id, event_type="GHOST_CLOSED").one()
    assert "net -2" in ev.detail and "455.50" in ev.detail


def test_second_pass_is_idempotent(db, monkeypatch):
    _open_position(db, symbol="COHANCE", qty=2, entry=450.0, opened_at=_old())
    _cash(db)
    _feeds(monkeypatch,
           [{"tradingSymbol": "COHANCE", "totalQty": 2}],
           [{"tradingSymbol": "COHANCE", "netQty": -2}])
    assert pf.holdings_sync_reconcile(db)["closed"] == 1
    assert pf.holdings_sync_reconcile(db)["closed"] == 0


def test_partial_sell_today_caps_qty(db, monkeypatch):
    pos = _open_position(db, symbol="PARTCO", qty=4, entry=100.0, opened_at=_old())
    acct = _cash(db)
    _feeds(monkeypatch,
           [{"tradingSymbol": "PARTCO", "totalQty": 4}],
           [{"tradingSymbol": "PARTCO", "netQty": -1}])
    res = pf.holdings_sync_reconcile(db)
    assert res["closed"] == 0 and "PARTCO" in res["synced"]
    db.refresh(pos)
    assert pos.qty_open == 3 and pos.status == "PARTIALLY_CLOSED"
    db.refresh(acct)
    assert acct.cash_available == 5000.0 + 100.0


def test_our_own_booked_sell_is_not_netted_twice(db, monkeypatch):
    """10 held, 2 sold through this app and already booked (qty_open 8): net -2 explains the gap."""
    pos = _open_position(db, symbol="OWNCO", qty=8, entry=100.0, opened_at=_old())
    _cash(db)
    _feeds(monkeypatch,
           [{"tradingSymbol": "OWNCO", "totalQty": 10}],
           [{"tradingSymbol": "OWNCO", "netQty": -2}])
    res = pf.holdings_sync_reconcile(db)
    assert res["closed"] == 0 and res["synced"] == []
    db.refresh(pos)
    assert pos.qty_open == 8 and pos.status == "OPEN"


def test_holdings_already_dropped_not_netted_again(db, monkeypatch):
    pos = _open_position(db, symbol="TWICECO", qty=2, entry=100.0, opened_at=_old())
    _cash(db)
    _feeds(monkeypatch,
           [{"tradingSymbol": "TWICECO", "totalQty": 1}],
           [{"tradingSymbol": "TWICECO", "netQty": -1}])
    res = pf.holdings_sync_reconcile(db)
    assert res["closed"] == 0 and "TWICECO" in res["synced"]
    db.refresh(pos)
    assert pos.qty_open == 1


def test_positive_net_leaves_position_alone(db, monkeypatch):
    pos = _open_position(db, symbol="BUYCO", qty=2, entry=100.0, opened_at=_old())
    _feeds(monkeypatch,
           [{"tradingSymbol": "BUYCO", "totalQty": 2}],
           [{"tradingSymbol": "BUYCO", "netQty": 3}])
    assert pf.holdings_sync_reconcile(db)["closed"] == 0
    db.refresh(pos)
    assert pos.status == "OPEN" and pos.qty_open == 2


def test_recent_position_not_closed_by_net(db, monkeypatch):
    pos = _open_position(db, symbol="NEWCO", qty=2, entry=100.0)
    _feeds(monkeypatch,
           [{"tradingSymbol": "NEWCO", "totalQty": 2}],
           [{"tradingSymbol": "NEWCO", "netQty": -2}])
    assert pf.holdings_sync_reconcile(db)["closed"] == 0
    db.refresh(pos)
    assert pos.status == "OPEN"


def test_pending_real_sell_is_skipped(db, monkeypatch):
    pos = _open_position(db, symbol="PENDCO", qty=2, entry=100.0, opened_at=_old())
    db.add(models.TradeOrder(mode="REAL", symbol="PENDCO", side="SELL", qty=2,
                             order_type="MARKET", status="PLACED"))
    db.commit()
    _feeds(monkeypatch,
           [{"tradingSymbol": "PENDCO", "totalQty": 2}],
           [{"tradingSymbol": "PENDCO", "netQty": -2}])
    assert pf.holdings_sync_reconcile(db)["closed"] == 0
    db.refresh(pos)
    assert pos.status == "OPEN"


def test_bad_net_value_is_ignored(db, monkeypatch):
    pos = _open_position(db, symbol="BADNET", qty=2, entry=100.0, opened_at=_old())
    _feeds(monkeypatch,
           [{"tradingSymbol": "BADNET", "totalQty": 2}],
           [{"tradingSymbol": "BADNET", "netQty": "n/a", "sellAvg": "x"}])
    assert pf.holdings_sync_reconcile(db)["closed"] == 0
    db.refresh(pos)
    assert pos.status == "OPEN"


def test_closed_position_not_reimported_after_net_close(db, monkeypatch):
    """After the net-close the row is CLOSED today, so the 24h re-import guard keeps the still-listed holding out."""
    _open_position(db, symbol="COHANCE", qty=2, entry=450.0, opened_at=_old())
    _cash(db)
    _feeds(monkeypatch,
           [{"tradingSymbol": "COHANCE", "totalQty": 2, "avgCostPrice": 450.0}],
           [{"tradingSymbol": "COHANCE", "netQty": -2}])
    pf.holdings_sync_reconcile(db)
    from execution import shared_symbol_lock
    monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda db_, sym, mode="REAL": True)

    async def _no_quotes(symbols):
        return {}
    monkeypatch.setattr("market_feed.feed.get_quotes", _no_quotes)
    assert asyncio.run(pf.import_broker_holdings(db)) == 0
    assert db.query(models.TradePosition).filter_by(symbol="COHANCE", status="OPEN").count() == 0
