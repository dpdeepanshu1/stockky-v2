"""Offline tests for the session-72 overnight-stop / pending-reconcile / cooldown fixes.
Run:  cd services/position-stocks-service && python -m pytest tests -q
No Dhan, no network: sqlite in-memory + a fake broker patched into dhan_client.
"""
import os, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import notifier
from execution import dhan_client
from capital import ledger
from orders import overnight_stop, reconcile, eod_squareoff


class Broker:
    def __init__(self):
        self.orders = []          # rows returned by get_order_list
        self.trades = []          # rows returned by get_trade_history
        self.placed_stops = []    # (qty, trigger)
        self.next_stop_id = 1
        self.cancelled = []
        self.flat_sells = []      # quantities of place_order SELLs


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    b = Broker()
    sent = {"info": [], "critical": []}
    monkeypatch.setattr(notifier, "notify_sync", lambda m, *a, **k: sent["info"].append(m))
    monkeypatch.setattr(notifier, "notify_critical", lambda m, *a, **k: sent["critical"].append(m))
    monkeypatch.setattr(dhan_client, "get_order_list", lambda db_: list(b.orders))
    monkeypatch.setattr(dhan_client, "get_trade_history", lambda db_, f, t: list(b.trades))
    monkeypatch.setattr(dhan_client, "cancel_cnc_stop_loss_order", lambda db_, order_id: b.cancelled.append(order_id) or {})

    def _place_stop(db_, *, is_armed, security_id, exchange_segment, quantity, trigger_price, tag=None):
        b.next_stop_id += 1
        b.placed_stops.append((quantity, trigger_price))
        return {"orderId": f"S{b.next_stop_id}"}
    monkeypatch.setattr(dhan_client, "place_cnc_stop_loss_market", _place_stop)

    def _place_order(db_, **kw):
        b.flat_sells.append(kw["quantity"])
        return {"orderId": "F1"}
    monkeypatch.setattr(dhan_client, "place_order", _place_order)

    led = ledger._get_or_create(db)
    led.total_allocated_capital, led.available_capital = 100000.0, 90000.0
    db.commit()
    yield db, b, sent
    db.close()


def mkpos(db, qty=100, entry=100.0, stop_id="S1"):
    p = models.ScalpPosition(
        symbol="XYZ", window_source="5m", adaptive_target_pct=2.0, adaptive_stop_pct=1.0, status="OPEN", quantity=qty, entry_price=entry, capital_risked=entry * qty,
        overnight_converted_to_cnc=True, overnight_stop_order_id=stop_id, dhan_security_id="123",
        target_price=entry * 1.02, stop_price=entry * 0.96, opened_at=datetime.now(timezone.utc),
    )
    db.add(p); db.commit()
    return p


def row(oid, status, filled, avg, qty=100):
    return {"orderId": oid, "orderStatus": status, "filledQty": filled, "averageTradedPrice": avg, "quantity": qty}


def test_partial_then_complete_exact_pnl(env):
    db, b, _ = env
    p = mkpos(db)
    b.orders = [row("S1", "PART_TRADED", 30, 96.0)]
    assert reconcile._reconcile_overnight_stops(db) == 0
    assert (p.quantity, p.status) == (70, "OPEN")
    assert p.realized_pnl == pytest.approx(-120.0)
    # same poll again: idempotent
    reconcile._reconcile_overnight_stops(db)
    assert p.quantity == 70 and p.realized_pnl == pytest.approx(-120.0)
    # final: cumulative 100 @ avg 95.4  -> total P&L must be exactly (95.4-100)*100
    b.orders = [row("S1", "TRADED", 100, 95.4)]
    assert reconcile._reconcile_overnight_stops(db) == 1
    assert p.status == "STOP_HIT" and p.quantity == 0 and p.capital_risked == pytest.approx(0.0)
    assert p.realized_pnl == pytest.approx(-460.0)          # old cumulative-avg pricing gave -442
    assert p.realized_pnl_pct == pytest.approx(-4.6)
    assert p.overnight_stop_order_id is None and p.error_message is None
    assert ledger.get_state(db)["available_capital"] == pytest.approx(90000 + 10000 - 460)


def test_expired_partial_booked_without_seeing_part_traded(env):
    db, b, _ = env
    p = mkpos(db)
    b.orders = [row("S1", "EXPIRED", 40, 97.0)]
    reconcile._reconcile_overnight_stops(db)
    assert p.status == "OPEN" and p.quantity == 60
    assert p.realized_pnl == pytest.approx(-120.0)
    assert p.overnight_stop_order_id == "S1"      # kept so the morning recheck re-arms


def test_rearm_resets_counters_and_closes_correctly(env):
    db, b, _ = env
    p = mkpos(db)
    b.orders = [row("S1", "PART_TRADED", 30, 96.0)]
    reconcile._reconcile_overnight_stops(db)
    b.orders = [row("S1", "EXPIRED", 30, 96.0)]
    res = overnight_stop.morning_recheck(db, 4.0)
    assert res["rearmed"] == 1
    assert b.placed_stops[-1][0] == 70                       # re-armed for the REMAINING qty only
    assert (p.overnight_stop_order_id, p.overnight_stop_filled_qty_so_far, p.overnight_stop_prior_qty) == ("S2", 0, 30)
    b.orders = [row("S2", "TRADED", 70, 94.0, qty=70)]
    assert reconcile._reconcile_overnight_stops(db) == 1     # old code: 70-30=40 -> never closed
    assert p.status == "STOP_HIT" and p.quantity == 0
    assert p.realized_pnl == pytest.approx(-120 - 420)
    assert p.realized_pnl_pct == pytest.approx(-5.4)         # cost basis = ALL 100 shares


def test_part_traded_spelling_and_live_stop_left_alone(env):
    db, b, _ = env
    p = mkpos(db)
    b.orders = [row("S1", "PENDING", 0, 0)]
    res = overnight_stop.morning_recheck(db, 4.0)
    assert res["live"] == 1 and not b.placed_stops and p.quantity == 100
    b.orders = [row("S1", "TRIGGERED", 0, 0)]
    assert overnight_stop.morning_recheck(db, 4.0)["rearmed"] == 0


def test_flat_sell_books_unbooked_partial_first(env):
    db, b, _ = env
    p = mkpos(db)
    b.orders = [row("S1", "PART_TRADED", 30, 96.0)]           # reconcile has NOT seen it yet
    eod_squareoff._fire_flat_sell(db, p)
    assert b.flat_sells == [70]                               # old code sold 100 (oversell)
    assert b.cancelled == ["S1"] and p.overnight_stop_order_id is None
    assert p.realized_pnl == pytest.approx(-120.0)


def test_flat_sell_skipped_when_stop_already_sold_everything(env):
    db, b, _ = env
    p = mkpos(db)
    b.orders = [row("S1", "TRADED", 100, 95.0)]
    with pytest.raises(eod_squareoff.PositionAlreadyFlat):
        eod_squareoff._fire_flat_sell(db, p)
    assert not b.flat_sells and p.status == "STOP_HIT"


def test_partial_pnl_survives_flat_sell_resolution(env):
    db, b, _ = env
    p = mkpos(db)
    b.orders = [row("S1", "PART_TRADED", 30, 96.0)]
    reconcile._reconcile_overnight_stops(db)
    p.status, p.exit_price = "EOD_SQUAREOFF", p.entry_price
    p.error_message = "EOD_SQUAREOFF_PENDING_RECONCILE: x"
    db.commit()
    reconcile._resolve_pending_with_price(db, p, 99.0, late=False)
    assert p.realized_pnl == pytest.approx(-120 + 70 * -1.0)  # partial P&L kept, not overwritten
    assert p.error_message is None


def test_residual_when_traded_but_shares_remain(env):
    db, b, sent = env
    p = mkpos(db)
    b.orders = [row("S1", "TRADED", 60, 95.0)]
    reconcile._reconcile_overnight_stops(db)
    assert p.status == "OPEN" and p.quantity == 40 and p.overnight_stop_order_id is None
    assert any("REMAIN" in m for m in sent["critical"])


def test_trade_history_recovers_order_missing_from_book(env):
    db, b, _ = env
    p = mkpos(db)
    b.orders = []                                             # yesterday's DAY order is gone
    b.trades = [{"orderId": "S1", "tradedQuantity": 20, "tradedPrice": 95.0},
                {"orderId": "S1", "tradedQuantity": 20, "tradedPrice": 96.0},
                {"orderId": "OTHER", "tradedQuantity": 999, "tradedPrice": 1.0}]
    res = overnight_stop.morning_recheck(db, 4.0)
    assert res["settled_qty"] == 40 and res["rearmed"] == 1
    assert b.placed_stops[-1][0] == 60
    assert p.realized_pnl == pytest.approx(20 * -5 + 20 * -4)


def _stuck(db, status="MANUAL_EXIT", days_ago=2, oid="X9", qty=70):
    p = models.ScalpPosition(
        symbol="RML", window_source="5m", adaptive_target_pct=2.0, adaptive_stop_pct=1.0, status=status, quantity=qty, entry_price=100.0, exit_price=100.0, realized_pnl=0.0,
        capital_risked=7000.0, dhan_security_id="555", dhan_exit_order_id=oid, target_price=102, stop_price=96,
        error_message=f"{status}_PENDING_RECONCILE: exit_price=entry_price placeholder",
        opened_at=datetime.now(timezone.utc) - timedelta(days=days_ago + 1),
        closed_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(p); db.commit()
    return p


def test_prior_day_pending_resolved_from_trade_history_not_todays_pnl(env):
    db, b, _ = env
    p = _stuck(db)
    b.trades = [{"orderId": "X9", "tradedQuantity": 70, "tradedPrice": 98.0, "transactionType": "SELL", "securityId": "555"}]
    before = ledger.get_state(db)
    out = reconcile.resolve_stuck_pending(db, force=True)
    assert out["resolved"] == 1 and p.error_message is None
    assert p.exit_price == pytest.approx(98.0) and p.realized_pnl == pytest.approx(-140.0)
    after = ledger.get_state(db)
    assert after["realized_pnl_today"] == before["realized_pnl_today"]        # kill-switch counter untouched
    assert after["realized_pnl_total"] == pytest.approx(before["realized_pnl_total"] - 140.0)


def test_prior_day_pending_adopts_unique_order_and_ages_out(env):
    db, b, sent = env
    p = _stuck(db, oid=None)
    b.trades = [{"orderId": "Z1", "tradedQuantity": 70, "tradedPrice": 97.0, "transactionType": "SELL", "securityId": "555",
                 "createTime": (datetime.now(timezone.utc) - timedelta(days=2)).astimezone(reconcile.IST).strftime("%Y-%m-%d 15:01:00")}]
    assert reconcile.resolve_stuck_pending(db, force=True)["resolved"] == 1 and p.dhan_exit_order_id == "Z1"
    q = _stuck(db, days_ago=5, oid="NOPE")
    b.trades = []
    out = reconcile.resolve_stuck_pending(db, force=True)
    assert out["aged_out"] == 1 and "_UNRESOLVED" in q.error_message
    assert reconcile.list_pending_reconcile(db) == []


def test_self_heal_and_stale_error_cleared(env):
    db, b, _ = env
    p = _stuck(db); p.exit_price, p.realized_pnl = 97.0, -210.0; db.commit()
    assert reconcile.resolve_stuck_pending(db, force=True)["self_healed"] == 1 and p.error_message is None


def test_capital_cooldown_filter(env):
    db, _, _ = env
    try:
        import main as m
    except Exception as e:                                   # pragma: no cover - env without full deps
        pytest.skip(f"main.py not importable here: {e}")
    class C:  # minimal candidate
        def __init__(s, sym): s.symbol = sym
    m._capital_starved.clear()
    led0 = ledger._get_or_create(db); led0.available_capital = 100.0; db.commit()   # a genuinely starved pool
    m._note_capital_starved(db, "TREL")
    kept, skipped = m._capital_cooldown_filter(db, [C("TREL"), C("ABC")])
    assert [c.symbol for c in kept] == ["ABC"] and skipped == ["TREL"]
    led = ledger._get_or_create(db); led.available_capital += 5000; db.commit()   # capital freed
    kept, skipped = m._capital_cooldown_filter(db, [C("TREL")])
    assert [c.symbol for c in kept] == ["TREL"] and skipped == []
