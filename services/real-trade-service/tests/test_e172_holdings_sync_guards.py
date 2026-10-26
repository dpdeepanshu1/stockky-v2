"""E172 - holdings_sync_reconcile guards: empty broker answer needs a second read, "-EQ" symbols and float quantities match."""
from datetime import datetime, timedelta, timezone

import config
import models
from execution import dhan_client
from portfolio import portfolio as pf
from resilience.local_cache import load_snapshot, save_snapshot
from tests.test_portfolio import _open_position  # noqa: F401
from tests.test_portfolio import db  # noqa: F401


def _old():
    return datetime.now(timezone.utc) - timedelta(hours=2)


def _feeds(monkeypatch, holdings, positions):
    monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: holdings)
    monkeypatch.setattr(dhan_client, "get_positions", lambda db_: positions)


def test_empty_answer_needs_a_second_read_before_closing(db, monkeypatch):
    monkeypatch.setattr(config, "HOLDINGS_SYNC_EMPTY_CONFIRM_SECONDS", 120)
    pos = _open_position(db, symbol="GHOSTCO", opened_at=_old())
    _feeds(monkeypatch, [], [])
    assert pf.holdings_sync_reconcile(db)["closed"] == 0           # first empty read: only marked
    db.refresh(pos)
    assert pos.status in ("OPEN", "PARTIALLY_CLOSED") and (load_snapshot(db, "holdings_sync_empty_seen") or {}).get("at")
    assert pf.holdings_sync_reconcile(db)["closed"] == 0           # second read too soon
    save_snapshot(db, "holdings_sync_empty_seen", {"at": (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()})
    out = pf.holdings_sync_reconcile(db)                            # empty again, long enough after the first
    db.refresh(pos)
    assert out["closed"] == 1 and pos.status == "CLOSED"
    assert not (load_snapshot(db, "holdings_sync_empty_seen") or {}).get("at")


def test_a_non_empty_read_clears_the_empty_mark(db, monkeypatch):
    monkeypatch.setattr(config, "HOLDINGS_SYNC_EMPTY_CONFIRM_SECONDS", 120)
    pos = _open_position(db, symbol="GHOSTCO", opened_at=_old())
    _feeds(monkeypatch, [], [])
    pf.holdings_sync_reconcile(db)
    _feeds(monkeypatch, [{"tradingSymbol": "OTHER", "totalQty": 3}], [])
    assert pf.holdings_sync_reconcile(db)["closed"] == 1            # a non-empty answer is trusted at once: GHOSTCO is absent
    assert not (load_snapshot(db, "holdings_sync_empty_seen") or {}).get("at")


def test_broker_symbol_with_eq_suffix_still_matches(db, monkeypatch):
    pos = _open_position(db, symbol="ABC", qty=10, opened_at=_old())
    _feeds(monkeypatch, [{"tradingSymbol": "ABC-EQ", "totalQty": 10}], [])
    assert pf.holdings_sync_reconcile(db)["closed"] == 0
    db.refresh(pos)
    assert pos.status == "OPEN" and pos.qty_open == 10


def test_float_quantity_strings_are_not_read_as_zero(db, monkeypatch):
    pos = _open_position(db, symbol="ABC", qty=10, opened_at=_old())
    _feeds(monkeypatch, [{"tradingSymbol": "ABC", "totalQty": "10.0"}], [])
    assert pf.holdings_sync_reconcile(db)["closed"] == 0
    db.refresh(pos)
    assert pos.status == "OPEN"
