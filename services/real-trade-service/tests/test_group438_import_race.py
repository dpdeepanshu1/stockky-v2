"""group 438 (2026-10-18): the bot's own just-filled CNC BUY was imported as a broker holding.

reconcile_real_orders ran import_broker_holdings BEFORE it booked pending order fills, so a BUY that had filled at Dhan showed up in the
broker's positions with no TradePosition yet and was imported (broker_imported=True, ATR stop, no decision label); record_real_fill then
ADDED the fill on top. Fix: the importer leaves a symbol alone while our own BUY is PLACED/PARTIAL (config.IMPORT_SKIP_PENDING_BUY_MIN),
and trade_records no longer calls such a row an import."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import trade_records as tr


def utc(h, m=0, s=0, day=16):
    return datetime(2026, 10, day, h, m, s, tzinfo=timezone.utc)


def pos(id=1, symbol="ADVAIT", *, imported=True, opened=None, closed=None):
    return NS(id=id, mode="REAL", symbol=symbol, status="CLOSED", avg_entry_price=100.0, realized_pnl=-5.0,
              opened_at=opened or utc(6, 6, 17), closed_at=closed or utc(9, 31), broker_imported=imported, watchlist_entry_id=None,
              source_tab=None, entry_decision_label=None, entry_conviction_score=None, is_regime_override=False,
              net_realized_pnl=None, realized_cost_estimate=None)


def buy(id=977, symbol="ADVAIT", *, at=None, source="AUTO", dhan="D1"):
    return NS(id=id, side="BUY", symbol=symbol, mode="REAL", status="FILLED", exit_reason=None, decision_id=None, watchlist_entry_id=None,
              execution_source=source, dhan_order_id=dhan, created_at=at or utc(6, 6, 16), updated_at=at or utc(6, 6, 16))


def sell(id=990, symbol="ADVAIT", at=None):
    return NS(id=id, side="SELL", symbol=symbol, mode="REAL", status="FILLED", exit_reason="eod_squareoff", decision_id=None,
              watchlist_entry_id=None, execution_source="EXIT", dhan_order_id="S1", created_at=at or utc(9, 31), updated_at=at or utc(9, 31))


def build(positions, orders, include_imported=False):
    return tr.build_records(positions, orders, (), (), (), (), (), include_imported=include_imported)


def test_bot_buy_one_second_before_the_import_counts_as_system_even_by_default():
    recs = build([pos()], [buy(), sell()])
    assert [r["position_id"] for r in recs] == [1]
    assert recs[0]["origin"] == "system" and recs[0]["import_race"] is True


def test_a_real_broker_import_with_no_bot_buy_stays_imported_and_hidden_by_default():
    assert build([pos()], [sell()]) == []
    r = build([pos()], [sell()], include_imported=True)[0]
    assert r["origin"] == "imported" and r["import_race"] is False


def test_manual_or_dhan_less_orders_do_not_count_as_the_bots_entry():
    assert build([pos()], [buy(source="MANUAL"), sell()]) == []
    assert build([pos()], [buy(dhan=None), sell()]) == []


def test_a_bot_buy_long_after_the_import_is_not_its_entry():
    late = buy(at=utc(8, 30))                       # 2 h 24 min after opened_at: a later add-on, not the entry
    assert build([pos()], [late, sell()]) == []


def test_ordinary_system_position_is_unchanged():
    r = build([pos(imported=False)], [buy(), sell()])[0]
    assert r["origin"] == "system" and r["import_race"] is False


def test_imported_row_no_longer_steals_nothing_from_an_older_system_position():
    older = pos(1, imported=False, opened=utc(4, 0, day=12), closed=utc(9, 31, day=12))
    o = [buy(10, at=utc(4, 0, day=12)), sell(11, at=utc(9, 31, day=12)), buy(977), sell(990)]
    recs = {r["position_id"]: r for r in build([older, pos(2)], o)}
    assert set(recs) == {1, 2} and recs[1]["origin"] == "system" and recs[2]["import_race"] is True


# ---- importer guard (needs sqlalchemy, like the other import tests) ----
try:
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import config
    import models
    import portfolio.portfolio as pf
    from execution import dhan_client, shared_symbol_lock
    _HAVE_DB = True
except Exception:   # pragma: no cover - sandbox without sqlalchemy
    _HAVE_DB = False

if _HAVE_DB:
    _engine = create_engine("sqlite:///:memory:")

    @pytest.fixture()
    def db(monkeypatch):
        models.Base.metadata.drop_all(_engine)
        models.Base.metadata.create_all(_engine)
        s = sessionmaker(bind=_engine)()
        s.add(models.TradeAccount(mode="REAL", starting_capital=100000.0, current_equity=100000.0, cash_available=100000.0))
        s.commit()
        monkeypatch.setattr(dhan_client, "get_holdings", lambda d: [])
        monkeypatch.setattr(dhan_client, "get_positions", lambda d: [{"tradingSymbol": "ADVAIT", "productType": "CNC", "positionType": "LONG",
                                                                      "netQty": 4, "buyQty": 4, "costPrice": 100.0, "buyAvg": 100.0}])
        monkeypatch.setattr(shared_symbol_lock, "try_claim", lambda d, sym, mode="REAL": True)

        async def _no_quotes(symbols):
            return {}

        monkeypatch.setattr("market_feed.feed.get_quotes", _no_quotes)
        yield s
        s.close()

    def _order(db, status, age_min=1):
        o = models.TradeOrder(mode="REAL", symbol="ADVAIT", side="BUY", qty=4, status=status, dhan_order_id="D1", execution_source="AUTO",
                              created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=age_min))
        db.add(o)
        db.commit()

    def test_helper_sees_only_young_pending_buys(db):
        assert pf._has_pending_bot_buy(db, "ADVAIT", 45) is False
        _order(db, "PLACED")
        assert pf._has_pending_bot_buy(db, "ADVAIT", 45) is True
        assert pf._has_pending_bot_buy(db, "ADVAIT", 0) is False          # guard off
        assert pf._has_pending_bot_buy(db, "OTHER", 45) is False

    def test_old_or_finished_buy_does_not_block(db):
        _order(db, "PLACED", age_min=120)
        assert pf._has_pending_bot_buy(db, "ADVAIT", 45) is False
        db.query(models.TradeOrder).delete()
        _order(db, "FILLED")
        assert pf._has_pending_bot_buy(db, "ADVAIT", 45) is False

    def test_pending_bot_buy_is_not_imported(db, monkeypatch):
        monkeypatch.setattr(config, "IMPORT_SKIP_PENDING_BUY_MIN", 45.0)
        _order(db, "PLACED")
        assert asyncio.run(pf.import_broker_holdings(db)) == 0
        assert db.query(models.TradePosition).count() == 0

    def test_without_a_pending_buy_the_cnc_position_is_still_imported(db, monkeypatch):
        monkeypatch.setattr(config, "IMPORT_SKIP_PENDING_BUY_MIN", 45.0)
        assert asyncio.run(pf.import_broker_holdings(db)) == 1

    def test_guard_off_restores_the_old_behaviour(db, monkeypatch):
        monkeypatch.setattr(config, "IMPORT_SKIP_PENDING_BUY_MIN", 0.0)
        _order(db, "PLACED")
        assert asyncio.run(pf.import_broker_holdings(db)) == 1
