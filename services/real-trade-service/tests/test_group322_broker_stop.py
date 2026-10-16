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
import notifier
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
    st = SimpleNamespace(placed=[], cancelled=[], cancel_error=None, book=[], place_error=None, n=0, alerts=[])
    monkeypatch.setattr(notifier, "notify_fire_and_forget", lambda text: st.alerts.append(text))

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
    assert kw["trigger_price"] == 97.5 and kw["price"] == 0.0 and kw["quantity"] == 10 and kw["tag"] == "BSTOP"
    o = B.open_stop(db, "ABC")
    assert o.exit_reason == "broker_stop" and o.dhan_order_id == "BS1" and o.limit_price == 97.5


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
    assert sdk.kw["trigger_price"] == dhan_client.round_stop_to_tick(97.53) == 97.55 and sdk.kw["price"] == 0.0


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


# ---- group384: Dhan "Trigger Price should be greater than Price" fallback ----------------------

TRIG_ERR = "Dhan API error: Trigger Price should be greater than Price"


def test_limit_price_below_is_strictly_under_trigger_even_for_cheap_stocks():
    for trig in (0.5, 1.0, 5.0, 19.95, 97.5, 450.0, 2100.0, 15000.0):
        px = B.limit_price_below(trig)
        assert 0 < px < trig, (trig, px)
    assert B.limit_price_below(100.0, 0.5) == 99.5


def test_trigger_price_error_detector():
    assert B._is_trigger_price_error(TRIG_ERR)
    assert B._is_trigger_price_error("trigger price should be GREATER THAN price")
    assert not B._is_trigger_price_error("CDSL eDIS authorisation required")
    assert not B._is_trigger_price_error("")


def test_trigger_price_rejection_retries_once_as_stop_limit(rig, monkeypatch):
    db, st = rig
    calls = []

    def flaky(db_, **kw):
        calls.append(kw)
        if kw["order_type"] == "STOP_LOSS_MARKET":
            raise RuntimeError(TRIG_ERR)
        st.n += 1
        return {"orderId": f"BS{st.n}"}

    monkeypatch.setattr(dhan_client, "place_order", flaky)
    p = pos(db)
    assert sync(db, [p])["placed"] == 1
    assert [c["order_type"] for c in calls] == ["STOP_LOSS_MARKET", "STOP_LOSS"]
    assert 0 < calls[1]["price"] < calls[1]["trigger_price"]
    o = B.open_stop(db, "ABC")
    assert o.order_type == "STOP_LOSS" and o.dhan_order_id == "BS1"


def test_other_errors_are_not_retried(rig):
    db, st = rig
    st.place_error = RuntimeError("some other broker error")
    assert sync(db, [pos(db)])["failed"] == 1
    assert B.open_stop(db, "ABC") is None


def test_trigger_price_rejection_that_also_fails_as_limit_warns_and_fails(rig, caplog):
    db, st = rig
    st.place_error = RuntimeError(TRIG_ERR)
    with caplog.at_level("WARNING", logger="real-trade-broker-stop"):
        assert sync(db, [pos(db)])["failed"] == 1
    assert B.open_stop(db, "ABC") is None
    assert any("could not be placed" in r.getMessage() for r in caplog.records)


def test_limit_price_below_one_tick_fallback_when_percent_gap_rounds_onto_trigger():
    # tiny trigger: a 0.5 % gap rounds back to the trigger, so the one-tick step is used instead
    px = B.limit_price_below(1.0, 0.5)
    assert 0 < px < 1.0


def test_failed_stop_sends_one_alert_per_cooldown(rig):
    db, st = rig
    st.place_error = RuntimeError(TRIG_ERR)
    p = pos(db)
    sync(db, [p], now=1000.0)
    sync(db, [p], now=1010.0)
    assert len(st.alerts) == 1 and "ABC" in st.alerts[0] and "software stop" in st.alerts[0]


def test_alert_failure_never_breaks_placement_path(rig, monkeypatch):
    db, st = rig
    st.place_error = RuntimeError("boom")

    def bad(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(notifier, "notify_fire_and_forget", bad)
    assert sync(db, [pos(db)])["failed"] == 1


# ---- group411: eDIS backoff + SL-M learning (the ABDL retry storm) ----------------------------

EDIS_ERR = "Dhan API error: Insufficient Holding Quantity"


def _abdl_like_dhan(monkeypatch, calls, edis_active=None):
    """Fake Dhan exactly as in the live log: STOP_LOSS_MARKET -> trigger-price error, STOP_LOSS -> eDIS refusal
    (until `edis_active['on']` is switched off, i.e. the user completed 'Verify Holdings')."""
    edis_active = edis_active if edis_active is not None else {"on": True}
    n = {"i": 0}

    def fake(db_, **kw):
        calls.append(kw["order_type"])
        if kw["order_type"] == "STOP_LOSS_MARKET":
            raise RuntimeError(TRIG_ERR)
        if edis_active["on"]:
            raise RuntimeError(EDIS_ERR)
        n["i"] += 1
        return {"orderId": f"BS{n['i']}"}

    monkeypatch.setattr(dhan_client, "place_order", fake)
    return edis_active


def test_edis_refusal_backs_off_instead_of_resending_every_pass(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_EDIS_BACKOFF_S", 900)
    calls = []
    _abdl_like_dhan(monkeypatch, calls)
    p = pos(db)
    t = sync(db, [p], now=1000.0)
    assert t["failed"] == 1 and t["backoff"] == 0
    assert calls == ["STOP_LOSS_MARKET", "STOP_LOSS"]          # the one real attempt
    for now in (1010.0, 1100.0, 1899.0):                          # still inside the 900 s window
        t = sync(db, [p], now=now)
        assert t["failed"] == 1 and t["backoff"] == 1 and t["placed"] == 0
    assert calls == ["STOP_LOSS_MARKET", "STOP_LOSS"]          # Dhan was NOT called again
    assert B.edis_backoff_left("ABC", now=1100.0) > 0


def test_edis_backoff_expires_and_self_heals_after_authorisation(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_EDIS_BACKOFF_S", 900)
    calls = []
    edis = _abdl_like_dhan(monkeypatch, calls)
    p = pos(db)
    sync(db, [p], now=1000.0)
    assert sync(db, [p], now=1500.0)["backoff"] == 1
    edis["on"] = False                                           # user completes Verify Holdings in the Dhan app
    t = sync(db, [p], now=1901.0)                                # window over -> one more try
    assert t["placed"] == 1 and t["failed"] == 0
    assert B.open_stop(db, "ABC").order_type == "STOP_LOSS"
    assert B.edis_backoff_left("ABC", now=1902.0) == 0
    assert sync(db, [p], now=1960.0)["kept"] == 1                # resting stop exists now


def test_edis_backoff_zero_keeps_the_old_every_pass_behaviour(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_EDIS_BACKOFF_S", 0)
    calls = []
    _abdl_like_dhan(monkeypatch, calls)
    p = pos(db)
    sync(db, [p], now=1000.0)
    t = sync(db, [p], now=1010.0)
    assert t["backoff"] == 0 and t["failed"] == 1
    assert len(calls) >= 3                                       # Dhan is called again on the second pass


def test_edis_backoff_is_per_symbol(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_EDIS_BACKOFF_S", 900)
    seen = []

    def fake(db_, **kw):
        seen.append((kw["security_id"], kw["order_type"]))
        if kw["security_id"] == "111":
            raise RuntimeError(EDIS_ERR)
        return {"orderId": "OK1"}

    monkeypatch.setattr(dhan_client, "place_order", fake)
    monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "111" if sym == "AAA" else "222")
    a, b = pos(db, symbol="AAA"), pos(db, symbol="BBB")
    t = sync(db, [a, b], now=1000.0)
    assert t["placed"] == 1 and t["failed"] == 1                 # BBB got its stop, AAA was refused
    t = sync(db, [a, b], now=1010.0)
    assert t["backoff"] == 1 and t["kept"] == 1                  # only AAA is backed off; BBB keeps its stop


def test_edis_backoff_forgotten_when_position_is_gone(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_EDIS_BACKOFF_S", 900)
    _abdl_like_dhan(monkeypatch, [])
    p = pos(db)
    sync(db, [p], now=1000.0)
    assert B.edis_backoff_left("ABC", now=1001.0) > 0
    sync(db, [], now=1010.0)                                     # position closed
    assert B.edis_backoff_left("ABC", now=1011.0) == 0


def test_non_edis_failure_is_not_backed_off(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_EDIS_BACKOFF_S", 900)
    st.place_error = RuntimeError("some other broker error")
    p = pos(db)
    sync(db, [p], now=1000.0)
    assert sync(db, [p], now=1010.0)["backoff"] == 0             # transient errors are still retried every pass


def test_stop_limit_learned_after_first_trigger_price_rejection(rig, monkeypatch):
    db, st = rig
    calls = []

    def fake(db_, **kw):
        calls.append(kw["order_type"])
        if kw["order_type"] == "STOP_LOSS_MARKET":
            raise RuntimeError(TRIG_ERR)
        return {"orderId": f"S{len(calls)}"}

    monkeypatch.setattr(dhan_client, "place_order", fake)
    a, b = pos(db, symbol="AAA"), pos(db, symbol="BBB")
    assert sync(db, [a], now=1000.0)["placed"] == 1
    assert calls == ["STOP_LOSS_MARKET", "STOP_LOSS"]
    assert sync(db, [a, b], now=1010.0)["placed"] == 1           # BBB: straight to STOP_LOSS, no doomed SL-M first
    assert calls[2:] == ["STOP_LOSS"]


def test_slm_learning_is_set_when_stop_limit_fails_for_edis_not_for_trigger(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_EDIS_BACKOFF_S", 0)
    calls = []
    _abdl_like_dhan(monkeypatch, calls)
    p = pos(db)
    sync(db, [p], now=1000.0)
    sync(db, [p], now=1010.0)
    assert calls == ["STOP_LOSS_MARKET", "STOP_LOSS", "STOP_LOSS"]  # second pass skips the SL-M that is known to fail


def test_slm_not_learned_when_stop_limit_also_hits_the_trigger_error(rig, monkeypatch):
    db, st = rig
    calls = []

    def fake(db_, **kw):
        calls.append(kw["order_type"])
        raise RuntimeError(TRIG_ERR)

    monkeypatch.setattr(dhan_client, "place_order", fake)
    p = pos(db)
    sync(db, [p], now=1000.0)
    sync(db, [p], now=1010.0)
    assert calls == ["STOP_LOSS_MARKET", "STOP_LOSS", "STOP_LOSS_MARKET", "STOP_LOSS"]  # nothing was learned


def test_configured_stop_limit_is_not_resent_unchanged_on_trigger_error(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_ORDER_TYPE", "STOP_LOSS")
    calls = []

    def fake(db_, **kw):
        calls.append(kw["order_type"])
        raise RuntimeError(TRIG_ERR)

    monkeypatch.setattr(dhan_client, "place_order", fake)
    assert sync(db, [pos(db)])["failed"] == 1
    assert calls == ["STOP_LOSS"]                                # was sent twice before group411


def test_edis_alert_tells_the_user_what_to_do(rig, monkeypatch):
    db, st = rig
    _abdl_like_dhan(monkeypatch, [])
    sync(db, [pos(db)], now=1000.0)
    assert len(st.alerts) == 1
    assert "Verify Holdings" in st.alerts[0] and "software stop" in st.alerts[0] and "ABC" in st.alerts[0]


# ---- group 412: stop prices must be valid whatever the real tick is (Railtel EXCH:16283, 86 rejected orders) -------------------

def test_stop_prices_never_use_a_tick_finer_than_five_paise():
    for px in (0.5, 19.97, 87.13, 243.59, 249.99, 456.783, 999.99):
        r = dhan_client.round_stop_to_tick(px)
        assert dhan_client.is_valid_tick_price(r, tick_size=0.05), (px, r)
    assert dhan_client.round_stop_to_tick(243.59) == 243.6      # the exact Railtel price Dhan refused
    assert dhan_client.round_stop_to_tick(0) == 0 and dhan_client.round_stop_to_tick("x") == "x"
    assert dhan_client.round_stop_to_tick(1234.57) == 1234.6    # coarser bands (>=1,000: 0.10) are unchanged
    assert dhan_client.stop_tick_for_price(243.59) == 0.05 and dhan_client.stop_tick_for_price(1500.0) == 0.10


def test_place_order_stop_trigger_and_limit_are_five_paise_multiples(sdk):
    _po(order_type="STOP_LOSS", trigger_price=243.59, price=242.37)
    assert sdk.kw["trigger_price"] == 243.6 and sdk.kw["price"] == 242.35


def test_trigger_and_stop_limit_price_for_a_cheap_stock_are_valid_ticks():
    trig = B.trigger_for(244.83, 260.0)                         # 244.83 * 0.995 = 243.606 -> was 243.61 on a 0.01 tick
    assert trig == 243.6 and dhan_client.is_valid_tick_price(trig, tick_size=0.05)
    px = B.limit_price_below(trig, 0.5)
    assert px < trig and dhan_client.is_valid_tick_price(px, tick_size=0.05)


def test_tick_size_rejection_detector():
    assert B._is_tick_size_error("EXCH:16283:The order price is not multiple of the tick size.")
    assert B._is_tick_size_error("Dhan API error: price is not a multiple of the tick size")
    assert not B._is_tick_size_error(TRIG_ERR) and not B._is_tick_size_error("CDSL eDIS authorisation required") and not B._is_tick_size_error("")


def test_tick_size_rejection_backs_off_instead_of_resending_every_pass(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(config, "BROKER_STOP_EDIS_BACKOFF_S", 900)
    st.place_error = RuntimeError("EXCH:16283:The order price is not multiple of the tick size.")
    p = pos(db)
    assert sync(db, [p], now=1000.0)["failed"] == 1
    t = sync(db, [p], now=1010.0)
    assert t["backoff"] == 1 and st.n == 0                       # Dhan is not called again inside the window
    st.place_error = None
    assert sync(db, [p], now=2000.0)["placed"] == 1              # after the window it is tried once more and succeeds
