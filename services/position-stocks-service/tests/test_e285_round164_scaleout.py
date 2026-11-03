"""E285 round 164: MOMENTUM_SCALEOUT (orders/scaleout.py). A fake broker records every call; nothing touches Dhan or a database."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from orders import overnight_stop, scaleout

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent


class DB:
    def __init__(self): self.commits = 0; self.rollbacks = 0
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


class Broker:
    def __init__(self, net=8, legs=None, sell_status="TRADED", sell_price=105.0, sell_error=None, stop_error=None, cancel_leaves_live=False, positions=True):
        self.calls = []; self.net = net; self.sell_status = sell_status; self.sell_price = sell_price; self.sell_error = sell_error; self.stop_error = stop_error
        self.legs = legs or {"TARGET_LEG": "PENDING", "STOP_LOSS_LEG": "PENDING"}; self.cancel_leaves_live = cancel_leaves_live; self.positions = positions
        self.orders = {}; self.n = 0; self.stop_status = "PENDING"
    def tick_size_for_price(self, p): return 0.05
    def round_stop_to_tick(self, p): return round(round(p / 0.05) * 0.05, 2)
    def is_ambiguous_placement_error(self, e): return "timeout" in str(e)
    def get_positions(self, db):
        if not self.positions: raise RuntimeError("down")
        return [{"securityId": "1", "netQty": self.net}]
    def get_super_order_list(self, db): return [{"orderId": "SUP", "legDetails": [{"legName": k, "orderStatus": v} for k, v in self.legs.items()]}]
    def cancel_super_order(self, db, *, order_id, order_leg):
        self.calls.append(("cancel_leg", order_leg))
        if not self.cancel_leaves_live and self.legs.get(order_leg) not in ("TRADED",): self.legs[order_leg] = "CANCELLED"
        return {}
    def place_order(self, db, **kw):
        self.calls.append(("sell", kw["quantity"], kw["transaction_type"], kw["order_type"]))
        if self.sell_error: raise RuntimeError(self.sell_error)
        self.n += 1; oid = f"S{self.n}"
        self.orders[oid] = {"orderId": oid, "orderStatus": self.sell_status, "filledQty": kw["quantity"], "averageTradedPrice": self.sell_price, "quantity": kw["quantity"],
                            "securityId": "1", "transactionType": "SELL", "tag": kw.get("tag")}
        return {"orderId": oid}
    def place_intraday_sell_stop(self, db, **kw):
        self.calls.append(("stop", kw["quantity"], kw["trigger_price"]))
        if self.stop_error: raise RuntimeError(self.stop_error)
        self.n += 1; oid = f"T{self.n}"
        self.orders[oid] = {"orderId": oid, "orderStatus": self.stop_status, "quantity": kw["quantity"], "securityId": "1", "transactionType": "SELL", "tag": kw.get("tag")}
        return {"orderId": oid, "order_type_sent": "STOP_LOSS_MARKET"}
    def get_order_row(self, db, oid): return self.orders.get(oid)
    def get_order_list(self, db): return list(self.orders.values())
    def cancel_cnc_stop_loss_order(self, db, *, order_id):
        self.calls.append(("cancel_order", order_id)); self.orders.get(order_id, {})["orderStatus"] = "CANCELLED"; return {}


def mk(qty=8, **kw):
    d = dict(id=1, symbol="MSUMI", status="OPEN", side="LONG", quantity=qty, entry_price=100.0, capital_risked=100.0 * qty, realized_pnl=0.0, stop_price=98.0, target_price=110.0,
             dhan_security_id="1", dhan_super_order_id="SUP", plain_stop_order_id=None, overnight_converted_to_cnc=False, overnight_stop_prior_qty=0, overnight_stop_filled_qty_so_far=0,
             overnight_stop_filled_notional_so_far=0.0, overnight_stop_order_id=None, flow_json=None, stop_moved_to_breakeven=False, exit_price=None, realized_pnl_pct=None,
             dhan_exit_order_id=None, error_message=None, closed_at=None)
    d.update(kw)
    return SimpleNamespace(**d)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    scaleout.reset_state()
    monkeypatch.setattr(config, "MOMENTUM_SCALEOUT", True, raising=False)
    monkeypatch.setattr(config, "SCALEOUT_FILL_WAIT_S", 1.0, raising=False)
    released = []
    monkeypatch.setattr(overnight_stop.ledger, "release_capital", lambda db, **k: released.append(k))
    monkeypatch.setattr(scaleout, "_alert", lambda t: None)
    return released


def run(pos, broker, ltp=106.0):
    t = [0.0]
    def clock(): return t[0]
    def sleep(s): t[0] += s
    return scaleout.run_half_sell(DB(), pos, ltp=ltp, decision={"trail_gap_pct": 0.8, "signals": ["a"], "reason": "x"}, broker=broker, sleep=sleep, clock=clock)


def test_happy_path_splits_and_books_the_half(env):
    p, b = mk(8), Broker()
    r = run(p, b)
    assert r["ok"] and r["fallback"] == "NONE" and r["qty_sold"] == 4
    kinds = [c[0] for c in b.calls]
    assert kinds == ["cancel_leg", "cancel_leg", "sell", "stop"] and b.calls[0][1] == "TARGET_LEG" and b.calls[1][1] == "STOP_LOSS_LEG"
    assert b.calls[2][1] == 4 and b.calls[3][1] == 4
    assert p.quantity == 4 and p.dhan_super_order_id is None and p.plain_stop_order_id == "T2"
    assert p.realized_pnl == pytest.approx(20.0) and p.overnight_stop_prior_qty == 4 and p.capital_risked == pytest.approx(400.0)
    assert p.stop_price >= 98.0 and p.stop_price < 106.0 and p.exit_price is None
    st = scaleout.read_stamp(p.flow_json)
    assert st["booked"] and st["sell_price"] == 105.0 and scaleout.is_remainder(p)
    assert env and env[0]["realized_pnl"] == pytest.approx(20.0)


def test_odd_quantity_keeps_the_larger_half():
    p, b = mk(7), Broker(net=7)
    r = run(p, b)
    assert r["ok"] and [c[1] for c in b.calls if c[0] in ("sell", "stop")] == [3, 4]


def test_stop_is_placed_before_any_fill_poll_and_never_below_old_stop():
    p, b = mk(8, stop_price=104.0), Broker()
    run(p, b, ltp=106.0)
    assert p.stop_price >= 104.0


def test_cancel_failure_leaves_nothing_sold_and_falls_back_to_full():
    p, b = mk(8), Broker(cancel_leaves_live=True)
    r = run(p, b)
    assert r["fallback"] == "FULL" and not any(c[0] in ("sell", "stop") for c in b.calls) and p.quantity == 8 and p.dhan_super_order_id == "SUP"


def test_filled_leg_sends_nothing():
    p, b = mk(8), Broker(legs={"TARGET_LEG": "TRADED", "STOP_LOSS_LEG": "CANCELLED"})
    r = run(p, b)
    assert r["fallback"] == "NONE" and not r["ok"] and not any(c[0] in ("sell", "stop") for c in b.calls)


def test_dhan_shows_less_than_row_sends_nothing_at_all():
    p, b = mk(8), Broker(net=3)
    r = run(p, b)
    assert r["fallback"] == "NONE" and b.calls == []


def test_unreadable_positions_fall_back_to_full_before_any_cancel():
    p, b = mk(8), Broker(positions=False)
    r = run(p, b)
    assert r["fallback"] == "FULL" and b.calls == []


def test_sell_refused_falls_back_to_full():
    p, b = mk(8), Broker(sell_error="RMS: rejected")
    r = run(p, b)
    assert r["fallback"] == "FULL" and not any(c[0] == "stop" for c in b.calls) and p.quantity == 8


def test_stop_failure_books_half_and_sells_the_rest(monkeypatch):
    closed = []
    monkeypatch.setattr(scaleout, "_full_close", lambda db, pos, reason, d: closed.append((pos.quantity, reason)))
    p, b = mk(8), Broker(stop_error="Dhan said no")
    r = run(p, b)
    assert r["fallback"] == "HALF_THEN_FULL" and closed == [(4, "MOMENTUM_EXIT")] and p.quantity == 4 and p.realized_pnl == pytest.approx(20.0)


def test_unconfirmed_sell_is_booked_later_and_flat_sell_is_blocked_meanwhile():
    p, b = mk(8), Broker(sell_status="PENDING")
    r = run(p, b)
    assert r["ok"] and p.quantity == 8 and not scaleout.read_stamp(p.flow_json)["booked"]
    with pytest.raises(RuntimeError):
        scaleout.settle_before_flat_sell(DB(), p, b)
    b.orders["S1"]["orderStatus"] = "TRADED"
    scaleout.settle_before_flat_sell(DB(), p, b)
    assert p.quantity == 4 and scaleout.read_stamp(p.flow_json)["booked"]
    scaleout.settle_before_flat_sell(DB(), p, b)          # idempotent
    assert p.quantity == 4 and p.realized_pnl == pytest.approx(20.0)


def test_dead_sell_undoes_to_full(monkeypatch):
    closed = []
    monkeypatch.setattr(scaleout, "_full_close", lambda db, pos, reason, d: closed.append(pos.quantity))
    p, b = mk(8), Broker(sell_status="REJECTED")
    for o in ():  # noqa
        pass
    b_orders_fix = b
    b.sell_price = 0
    b_orders_fix.orders = {}
    # make the order read as dead with no fill
    orig = b.place_order
    def po(db, **kw):
        res = orig(db, **kw); b.orders[res["orderId"]]["filledQty"] = 0; return res
    b.place_order = po
    r = run(p, b)
    assert not r["ok"] and r["stage"] == "sell_dead" and closed == [8] and ("cancel_order", "T2") in b.calls


def test_second_split_is_refused_and_inflight_guard():
    p, b = mk(8), Broker()
    run(p, b)
    assert scaleout.eligible(p, "10:00")[0] is False
    q = mk(8, id=2)
    scaleout._inflight.add(2)
    assert run(q, Broker())["stage"] == "inflight"


def test_eligibility_rules(monkeypatch):
    assert scaleout.eligible(mk(8), "10:00") == (True, "")
    assert not scaleout.eligible(mk(3), "10:00")[0]
    assert not scaleout.eligible(mk(8), "14:30")[0]
    assert not scaleout.eligible(mk(8, side="SHORT"), "10:00")[0]
    assert not scaleout.eligible(mk(8, dhan_super_order_id=None), "10:00")[0]
    monkeypatch.setattr(config, "MOMENTUM_SCALEOUT", False)
    assert scaleout.eligible(mk(8), "10:00") == (False, "off")


def test_guard_books_stop_fill_with_prior_pnl(env, monkeypatch):
    from capital import shared_symbol_lock
    monkeypatch.setattr(shared_symbol_lock, "release", lambda db, sym: None)
    p, b = mk(8), Broker()
    run(p, b)
    monkeypatch.setattr(scaleout, "_split_longs", lambda db: [p])
    b.orders["T2"].update(orderStatus="TRADED", filledQty=4, averageTradedPrice=101.0)
    n = scaleout.run_scaleout_guard(DB(), now_mono=1000.0, broker=b)
    assert n == 1 and p.status == "STOP_HIT" and p.exit_price == 101.0
    assert p.realized_pnl == pytest.approx(24.0) and p.realized_pnl_pct == pytest.approx(24.0 / 800.0 * 100.0)
    assert [k for k in env if k.get("realized_pnl") == pytest.approx(4.0)]


def test_stamp_helpers_never_raise():
    assert scaleout.read_stamp("not json") is None and scaleout.read_stamp(None) is None
    assert scaleout.write_stamp("garbage", {"a": 1}) is not None


def test_catalog_and_config_agree():
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    assert 'Flag("MOMENTUM_SCALEOUT"' in cat and re.search(r'Flag\("MOMENTUM_SCALEOUT".*?shipped="false"', cat, re.S)
    assert config.__dict__.get("MOMENTUM_SCALEOUT") in (True, False) and config.SCALEOUT_STOP_GAP_PCT >= 0.3


def test_flat_sell_and_main_are_wired():
    eod = (ROOT / "orders" / "eod_squareoff.py").read_text(encoding="utf-8")
    assert "scaleout.settle_before_flat_sell" in eod and "release_stop_before_cover" in eod
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "scaleout.run_scaleout_guard" in main
    hook = (ROOT / "orders" / "momentum_hook.py").read_text(encoding="utf-8")
    assert "_apply_half" in hook and "scaleout=_scaleout_ok(pos)" in hook


def test_guard_replaces_a_dead_stop_once_then_sells(monkeypatch):
    p, b = mk(8), Broker()
    run(p, b)
    sold = []
    monkeypatch.setattr(scaleout, "_split_longs", lambda db: [p])
    monkeypatch.setattr(scaleout, "_sell_now", lambda db, pos, reason, now: sold.append(reason) or True)
    b.orders["T2"]["orderStatus"] = "CANCELLED"
    scaleout.run_scaleout_guard(DB(), now_mono=1000.0, broker=b)
    assert p.plain_stop_order_id == "T3" and sold == []
    b.orders["T3"]["orderStatus"] = "REJECTED"
    scaleout.run_scaleout_guard(DB(), now_mono=1001.0, broker=b)
    assert sold == ["STOP_BREACH_EXIT"] and len([c for c in b.calls if c[0] == "stop"]) == 2


def test_guard_never_sells_while_half_sell_unconfirmed(monkeypatch):
    p, b = mk(8), Broker(sell_status="PENDING")
    run(p, b)
    sold = []
    monkeypatch.setattr(scaleout, "_split_longs", lambda db: [p])
    monkeypatch.setattr(scaleout, "_sell_now", lambda db, pos, reason, now: sold.append(reason) or True)
    b.orders["T2"]["orderStatus"] = "CANCELLED"
    scaleout.run_scaleout_guard(DB(), now_mono=1000.0, broker=b)
    assert sold == [] and p.quantity == 8
