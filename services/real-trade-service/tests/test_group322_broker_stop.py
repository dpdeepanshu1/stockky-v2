"""group322: optional broker-side stop (execution/broker_stop.py). Offline: scripted Dhan client, in-memory SQLite."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from execution import broker_stop as B
from execution import dhan_client
from exit_engine import exit as X

try:  # reuse the reconcile rig (same folder)
    from test_rt_reconcile import env as _rec_env, mk_order, mk_pos, row, run, R  # noqa: F401
    HAVE_RIG = True
except Exception:  # pragma: no cover
    HAVE_RIG = False

OLD = datetime.now(timezone.utc) - timedelta(days=3)


@pytest.fixture()
def rig(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    B.reset_state()
    monkeypatch.setattr(config, "BROKER_STOP_ENABLED", True)
    monkeypatch.setattr(config, "BROKER_STOP_BUFFER_PCT", 0.5)
    monkeypatch.setattr(config, "BROKER_STOP_MIN_MOVE_PCT", 0.25)
    monkeypatch.setattr(config, "BROKER_STOP_SYNC_SECONDS", 5)
    monkeypatch.setattr(config, "BROKER_STOP_CANCEL_MAX_FAILS", 3)
    monkeypatch.setattr(config, "BROKER_STOP_ORDER_TYPE", "STOP_LOSS_MARKET")
    st = SimpleNamespace(placed=[], cancelled=[], cancel_error=None, book=[], place_error=None, n=0)

    def place_order(db_, **kw):
        if st.place_error:
            raise st.place_error
        st.n += 1
        st.placed.append(kw)
        return {"orderId": f"BS{st.n}"}

    def cancel_order(db_, *, is_armed, dhan_order_id):
        if st.cancel_error:
            raise st.cancel_error
        st.cancelled.append(dhan_order_id)
        return {}

    monkeypatch.setattr(dhan_client, "place_order", place_order)
    monkeypatch.setattr(dhan_client, "cancel_order", cancel_order)
    monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "123")
    monkeypatch.setattr(dhan_client, "get_order_list", lambda db_: list(st.book))
    yield db, st
    db.close()


def pos(db, symbol="ABC", qty=10, stop=98.0, opened=OLD, **kw):
    p = models.TradePosition(mode="REAL", symbol=symbol, status=kw.pop("status", "OPEN"), qty_open=qty, avg_entry_price=100.0,
                             opened_at=opened, current_stop=stop, **kw)
    db.add(p)
    db.commit()
    return p


def tick(price):
    return SimpleNamespace(price=price)


def sync(db, positions, ltp=105.0, now=1000.0):
    return asyncio.run(B.sync_stops(db, "REAL", positions, {p.symbol: tick(ltp) for p in positions}, now_mono=now))


# ---- eligibility / trigger -------------------------------------------------------------------

def test_skip_reasons(rig):
    db, _ = rig
    assert B.skip_reason(pos(db)) is None
    assert B.skip_reason(pos(db, "S1", opened=datetime.now(timezone.utc))) == "same_day_cdsl"
    assert B.skip_reason(pos(db, "S2", entry_product_type="INTRADAY")) == "intraday"
    assert B.skip_reason(pos(db, "S3", stop=None)) == "no_stop"
    assert B.skip_reason(pos(db, "S4", qty=0)) == "no_qty"
    assert B.skip_reason(pos(db, "S5", status="CLOSED")) == "status"
    assert B.skip_reason(pos(db, "S6", opened=datetime.now(timezone.utc), broker_imported=True)) is None


def test_trigger_is_buffered_below_stop_and_below_market():
    assert B.trigger_for(100.0, 110.0) == 99.5
    assert B.trigger_for(100.0, 99.0) is None          # at/above the market -> software emergency exit owns it
    assert B.trigger_for(100.0, None) is None
    assert B.trigger_for("x", 110.0) is None


# ---- sync ------------------------------------------------------------------------------------

def test_disabled_does_nothing(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_ENABLED", False)
    assert sync(db, [pos(db)])["placed"] == 0 and st.placed == []


def test_places_one_stop_for_held_position(rig):
    db, st = rig
    p = pos(db)
    assert sync(db, [p])["placed"] == 1
    kw = st.placed[0]
    assert kw["order_type"] == "STOP_LOSS_MARKET" and kw["transaction_type"] == "SELL" and kw["product_type"] == "CNC"
    assert kw["trigger_price"] == 97.51 and kw["price"] == 0.0 and kw["quantity"] == 10 and kw["tag"] == "BSTOP"
    o = B.open_stop(db, "ABC")
    assert o.exit_reason == "broker_stop" and o.dhan_order_id == "BS1" and o.limit_price == 97.51


def test_same_day_position_gets_no_stop(rig):
    db, st = rig
    p = pos(db, opened=datetime.now(timezone.utc))
    assert sync(db, [p])["skipped"] == 1 and st.placed == []


def test_throttled_between_passes(rig):
    db, st = rig
    p = pos(db)
    sync(db, [p], now=1000.0)
    assert sync(db, [p], now=1001.0)["checked"] == 0
    assert sync(db, [p], now=1010.0)["kept"] == 1 and len(st.placed) == 1


def test_ratchet_up_replaces_but_never_lowers(rig):
    db, st = rig
    p = pos(db)
    sync(db, [p], now=1000.0)
    p.current_stop = 100.0                      # trigger 99.5, > 97.5 by far more than 0.25%
    db.commit()
    assert sync(db, [p], now=1010.0)["replaced"] == 1
    assert st.cancelled == ["BS1"] and B.open_stop(db, "ABC").dhan_order_id == "BS2" and B.open_stop(db, "ABC").limit_price == 99.5
    p.current_stop = 97.0                       # lower -> keep
    db.commit()
    assert sync(db, [p], now=1020.0)["kept"] == 1 and len(st.placed) == 2


def test_tiny_move_keeps_and_qty_change_replaces(rig):
    db, st = rig
    p = pos(db)
    sync(db, [p], now=1000.0)
    p.current_stop = 98.1                       # +0.1% -> below the move threshold
    db.commit()
    assert sync(db, [p], now=1010.0)["kept"] == 1
    p.qty_open = 5                              # partial exit happened
    db.commit()
    assert sync(db, [p], now=1020.0)["replaced"] == 1 and st.placed[-1]["quantity"] == 5


def test_placement_failure_never_raises_and_warns_once(rig, caplog):
    db, st = rig
    st.place_error = RuntimeError("CDSL eDIS authorisation required: please complete TPIN")
    p = pos(db)
    with caplog.at_level("WARNING", logger="real-trade-broker-stop"):
        assert sync(db, [p], now=1000.0)["failed"] == 1
        assert sync(db, [p], now=1010.0)["failed"] == 1
    assert B.open_stop(db, "ABC") is None
    assert len([r for r in caplog.records if "broker stop for ABC" in r.getMessage()]) == 1


def test_limit_variant_sends_price_below_trigger(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_ORDER_TYPE", "STOP_LOSS")
    sync(db, [pos(db)])
    assert st.placed[0]["order_type"] == "STOP_LOSS" and 0 < st.placed[0]["price"] < st.placed[0]["trigger_price"]


# ---- cancel before a software sell -----------------------------------------------------------

def test_no_stop_means_go_ahead(rig):
    db, st = rig
    assert B.cancel_for_exit(db, pos(db)) is True and st.cancelled == []


def test_cancel_success_marks_cancelled(rig):
    db, st = rig
    p = pos(db)
    sync(db, [p])
    assert B.cancel_for_exit(db, p) is True
    assert st.cancelled == ["BS1"] and B.open_stop(db, "ABC") is None
    assert db.query(models.TradeOrder).filter_by(dhan_order_id="BS1").one().status == "CANCELLED"


def test_cancel_works_even_when_flag_turned_off(rig, monkeypatch):
    db, st = rig
    p = pos(db)
    sync(db, [p])
    monkeypatch.setattr(config, "BROKER_STOP_ENABLED", False)
    assert B.cancel_for_exit(db, p) is True and st.cancelled == ["BS1"]


def test_already_traded_stop_blocks_the_software_sell(rig):
    db, st = rig
    p = pos(db)
    sync(db, [p])
    st.cancel_error = RuntimeError("order already traded")
    st.book = [{"orderId": "BS1", "orderStatus": "TRADED"}]
    assert B.cancel_for_exit(db, p) is False
    assert B.open_stop(db, "ABC") is not None            # left for reconcile to book


def test_already_cancelled_at_broker_lets_the_sell_go(rig):
    db, st = rig
    p = pos(db)
    sync(db, [p])
    st.cancel_error = RuntimeError("cannot cancel")
    st.book = [{"orderId": "BS1", "orderStatus": "CANCELLED"}]
    assert B.cancel_for_exit(db, p) is True and B.open_stop(db, "ABC") is None


def test_unknown_cancel_failure_waits_then_sells_anyway(rig):
    db, st = rig
    p = pos(db)
    sync(db, [p])
    st.cancel_error = RuntimeError("gateway timeout")
    assert B.cancel_for_exit(db, p) is False
    assert B.cancel_for_exit(db, p) is False
    assert B.cancel_for_exit(db, p) is True              # 3rd failure -> proceed


# ---- exit engine coupling --------------------------------------------------------------------

def test_stop_row_does_not_count_as_pending_sell_but_a_normal_sell_does(rig):
    db, _ = rig
    p = pos(db)
    sync(db, [p])
    assert X._has_pending_real_sell(db, "ABC") is False
    db.add(models.TradeOrder(mode="REAL", symbol="ABC", side="SELL", order_type="MARKET", qty=10, status="PLACED",
                             dhan_order_id="N1", execution_source="AUTO", exit_reason=None))
    db.commit()
    assert X._has_pending_real_sell(db, "ABC") is True


# ---- place_order trigger_price ---------------------------------------------------------------

class _Sdk:
    def __init__(self):
        self.kw = None

    def place_order(self, **kw):
        self.kw = kw
        return {"status": "success", "data": {"orderId": "Z1"}}


@pytest.fixture()
def sdk(monkeypatch):
    s = _Sdk()
    monkeypatch.setattr(dhan_client, "_get_sdk_client", lambda db_: s)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda db_: [])
    return s


def _po(**kw):
    base = dict(is_armed=True, security_id="1", exchange_segment="NSE_EQ", transaction_type="SELL", quantity=1,
                order_type="STOP_LOSS_MARKET", price=0.0, product_type="CNC")
    base.update(kw)
    return dhan_client.place_order(None, **base)


def test_place_order_stop_sends_rounded_trigger(sdk):
    _po(trigger_price=97.53)
    assert sdk.kw["trigger_price"] == dhan_client.round_to_tick(97.53) and sdk.kw["price"] == 0.0


def test_place_order_market_sends_no_trigger_key(sdk):
    _po(order_type="MARKET")
    assert "trigger_price" not in sdk.kw


def test_place_order_rejects_bad_trigger_combinations(sdk):
    with pytest.raises(ValueError):
        _po()                                            # stop without trigger
    with pytest.raises(ValueError):
        _po(order_type="MARKET", trigger_price=90.0)     # trigger on a market order
    with pytest.raises(ValueError):
        _po(trigger_price=0)


# ---- reconcile coupling ----------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_RIG, reason="reconcile rig not importable")
def test_dead_broker_stop_is_not_an_exit_failure(_rec_env):
    db, rig_ = _rec_env
    p = mk_pos(db, symbol="ABC", status="OPEN", qty=10, avg=100.0)
    mk_order(db, side="SELL", symbol="ABC", qty=10, status="PLACED", dhan_id="BS9", exit_reason="broker_stop")
    rig_.book = [row(oid="BS9", status="CANCELLED", avg=0.0, filled=0)]
    run(R.reconcile_real_orders(db))
    o = db.query(models.TradeOrder).filter_by(dhan_order_id="BS9").one()
    db.refresh(p)
    assert o.status == "CANCELLED"
    assert p.status == "OPEN" and (p.consecutive_exit_failures or 0) == 0
    assert rig_.sent == []


@pytest.mark.skipif(not HAVE_RIG, reason="reconcile rig not importable")
def test_filled_broker_stop_closes_the_position(_rec_env):
    db, rig_ = _rec_env
    p = mk_pos(db, symbol="ABC", status="OPEN", qty=10, avg=100.0)
    mk_order(db, side="SELL", symbol="ABC", qty=10, status="PLACED", dhan_id="BS8", exit_reason="broker_stop")
    rig_.book = [row(oid="BS8", status="TRADED", avg=97.0, filled=10)]
    run(R.reconcile_real_orders(db))
    db.refresh(p)
    assert p.status == "CLOSED"
