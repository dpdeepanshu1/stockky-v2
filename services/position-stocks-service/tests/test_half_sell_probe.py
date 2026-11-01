"""E283 / E285 round 98: orders/half_sell_probe.py - the supervised half-sell probe, run against a stateful fake of Dhan.

The fake keeps real state (positions that follow the fills, a Super Order whose legs follow the cancels, a plain order book), so what is asserted is what would have
been sent to Dhan, in order:
  * the happy path: BUY 2 Super Order -> cancel TARGET -> cancel STOP -> SELL 1 -> SELL stop for 1 -> read -> cancel stop -> SELL 1 -> PASS, flat, nothing left
  * every refusal before an order (confirm phrase, window, not armed, kill switch, price cap, held symbol, USE_SUPER_ORDER)
  * a surprise at any step (BUY refused, no fill, a cancel refused, legs still live, the SELL refused, the stop refused, a slow window) -> FAIL, and the
    cleanup leaves nothing open: it sells only a real long, never while an exit leg is live, never more than the long, and adopts what it cannot sell
  * the answers Q1..Q7 are recorded
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from execution import dhan_client
from orders import half_sell_probe as hs

NOW = datetime(2026, 10, 26, 5, 30, tzinfo=timezone.utc)        # Monday 11:00 IST, inside the window
LATE = datetime(2026, 10, 26, 10, 30, tzinfo=timezone.utc)      # 16:00 IST, closed
SID = "1594"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeDhan:
    """Scripted, stateful Dhan. Knobs: refuse_buy, fill, refuse_legs (set of leg names that cannot be cancelled), cancel_noop (the STOP leg cancel answers OK but nothing changes),
    stop_dies_with_target, refuse_sell_n (refuse the first N plain SELLs), refuse_stop, stop_delay (seconds), preheld, positions_fail, ambiguous_buy."""

    def __init__(self, clock=None, **kw):
        self.kw = dict(refuse_buy=None, fill=True, refuse_legs=(), cancel_noop=False, stop_dies_with_target=False, refuse_sell_n=0, refuse_stop=None, stop_delay=0.0,
                       preheld=0, positions_fail=False, ambiguous_buy=False, sell_pending=False)
        self.kw.update(kw)
        self.clock = clock or Clock()
        self.nets = {SID: int(self.kw["preheld"])} if self.kw["preheld"] else {}
        self.calls = []
        self.row = None
        self.orders = []
        self.refuse_sell_n = int(self.kw["refuse_sell_n"])
        self.went_short = False
        self.round_to_tick = dhan_client.round_to_tick
        self.round_stop_to_tick = dhan_client.round_stop_to_tick
        self.clamp_super_levels = dhan_client.clamp_super_levels
        self.tick_size_for_price = dhan_client.tick_size_for_price
        self.is_ambiguous_placement_error = dhan_client.is_ambiguous_placement_error
        self.recovered = None
        self.bought = 0
        self.sold = 0

    # -- reads
    def get_security_id(self, db, sym):
        return SID if sym == "ITC" else "9999"

    def get_positions(self, db):
        self.calls.append(("get_positions", {}))
        if self.kw["positions_fail"]:
            raise RuntimeError("dhan 502")
        return [{"securityId": sid, "productType": "INTRADAY", "netQty": n, "buyAvg": 100.0 if self.bought else 0.0, "sellAvg": 100.6 if self.sold else 0.0}
                for sid, n in self.nets.items() if (self.bought or self.sold or n)]

    def get_super_order_list(self, db):
        self.calls.append(("get_super_order_list", {}))
        return [self.row] if self.row else []

    def get_order_list(self, db):
        self.calls.append(("get_order_list", {}))
        return list(self.orders)

    def get_order_row(self, db, oid):
        return next((r for r in self.orders if r["orderId"] == oid), None)

    def find_recent_buy_order(self, db, security_id, qty, super_order=True, exclude_ids=(), within_s=180.0, transaction_type="BUY"):
        self.calls.append(("find_recent_buy_order", {"side": transaction_type}))
        return self.recovered

    # -- the Super Order
    def place_super_order(self, db, **kw):
        self.calls.append(("place_super_order", kw))
        if self.kw["refuse_buy"]:
            raise RuntimeError(self.kw["refuse_buy"])
        if self.kw["ambiguous_buy"]:
            raise TimeoutError("timed out")
        if self.kw["fill"]:
            self.nets[kw["security_id"]] = self.nets.get(kw["security_id"], 0) + kw["quantity"]
            self.bought += kw["quantity"]
        t, s = kw["target_price"], kw["stop_loss_price"]
        legs = [{"legName": "ENTRY_LEG", "orderStatus": "TRADED" if self.kw["fill"] else "PENDING", "transactionType": "BUY"},
                {"legName": "TARGET_LEG", "orderStatus": "PENDING", "transactionType": "SELL", "price": t},
                {"legName": "STOP_LOSS_LEG", "orderStatus": "PENDING", "transactionType": "SELL", "triggerPrice": s}]
        self.row = {"orderId": "SO1", "orderStatus": "PENDING", "transactionType": "BUY", "quantity": kw["quantity"], "filledQty": kw["quantity"] if self.kw["fill"] else 0, "legDetails": legs}
        return {"orderId": "SO1", "orderStatus": "PENDING"}

    def cancel_super_order(self, db, *, order_id, order_leg):
        self.calls.append(("cancel_super_order", {"leg": order_leg}))
        if order_leg in self.kw["refuse_legs"]:
            raise RuntimeError("cancel refused")
        legs = {lg["legName"]: lg for lg in self.row["legDetails"]}
        lg = legs.get(order_leg)
        if lg is None or lg["orderStatus"] != "PENDING":
            raise RuntimeError(f"{order_leg} cannot be cancelled")
        if self.kw["cancel_noop"] and order_leg == "STOP_LOSS_LEG":
            return {"orderId": order_id, "orderStatus": "CANCELLED"}              # says yes, changes nothing
        lg["orderStatus"] = "CANCELLED"
        if order_leg == "TARGET_LEG" and self.kw["stop_dies_with_target"]:
            legs["STOP_LOSS_LEG"]["orderStatus"] = "CANCELLED"
        if all(g["orderStatus"] in ("CANCELLED", "TRADED") for g in self.row["legDetails"]):
            self.row["orderStatus"] = "CLOSED"
        return {"orderId": order_id, "orderStatus": "CANCELLED"}

    # -- plain orders
    def _fill(self, side, qty):
        n = self.nets.get(SID, 0) + (qty if side == "BUY" else -qty)
        self.nets[SID] = n
        if n < 0:
            self.went_short = True
        if side == "SELL":
            self.sold += qty
        else:
            self.bought += qty

    def place_order(self, db, **kw):
        self.calls.append(("place_order", kw))
        if kw["transaction_type"] == "SELL" and self.refuse_sell_n > 0:
            self.refuse_sell_n -= 1
            raise RuntimeError("RMS: quantity locked by the Super Order")
        oid = f"P{len([c for c in self.calls if c[0] == 'place_order'])}"
        pending = self.kw["sell_pending"] and kw["tag"] == "HALFPROBE_SELL"
        self.orders.append({"orderId": oid, "securityId": kw["security_id"], "transactionType": kw["transaction_type"], "orderType": kw["order_type"], "quantity": kw["quantity"],
                            "orderStatus": "PENDING" if pending else "TRADED", "correlationId": kw["tag"]})
        if not pending:
            self._fill(kw["transaction_type"], kw["quantity"])
        return {"orderId": oid}

    def place_intraday_sell_stop(self, db, **kw):
        self.calls.append(("place_intraday_sell_stop", kw))
        self.clock.advance(float(self.kw["stop_delay"]))
        if self.kw["refuse_stop"]:
            raise RuntimeError(self.kw["refuse_stop"])
        oid = "STOP1"
        self.orders.append({"orderId": oid, "securityId": kw["security_id"], "transactionType": "SELL", "orderType": "STOP_LOSS_MARKET", "quantity": kw["quantity"],
                            "orderStatus": "PENDING", "triggerPrice": kw["trigger_price"], "correlationId": kw["tag"]})
        return {"orderId": oid}

    def cancel_order(self, db, *, is_armed, dhan_order_id):
        self.calls.append(("cancel_order", {"id": dhan_order_id}))
        for r in self.orders:
            if r["orderId"] == dhan_order_id and r["orderStatus"] == "PENDING":
                r["orderStatus"] = "CANCELLED"
        return {"orderId": dhan_order_id, "orderStatus": "CANCELLED"}

    def n(self, name):
        return sum(1 for c in self.calls if c[0] == name)

    def sells(self):
        return [c[1]["quantity"] for c in self.calls if c[0] == "place_order" and c[1]["transaction_type"] == "SELL"]


@pytest.fixture()
def env(monkeypatch, tmp_path):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(models.ScalpGateState(is_armed=True, daily_loss_kill_switch_tripped=False, orders_placed_today=0))
    db.commit()
    monkeypatch.setenv("CONTROL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "USE_SUPER_ORDER", True, raising=False)
    monkeypatch.setattr(hs, "_mem", {})
    alerts = []
    clock = Clock()
    yield NS(db=db, alerts=alerts, notify=alerts.append, clock=clock)
    db.close()


def _run(env, broker, *, symbol="ITC", confirm=hs.CONFIRM_PHRASE, now=NOW, price=100.0):
    return hs.run_probe(env.db, symbol=symbol, confirm=confirm, broker=broker, price_fn=lambda s: price, now=now, sleep=env.clock.advance, clock=env.clock, poll_s=3.0,
                        notify=env.notify)


def _locks(db):
    return db.query(models.SharedSymbolLock).count()


def _order(b):
    return [c[0] if c[0] != "cancel_super_order" else f"cancel:{c[1]['leg']}" for c in b.calls
            if c[0] in ("place_super_order", "cancel_super_order", "place_order", "place_intraday_sell_stop", "cancel_order")]


# ------------------------------------------------------------------ happy path
def test_clean_half_sell_passes_in_the_protocol_order_and_leaves_nothing(env):
    b = FakeDhan(clock=env.clock)
    r = _run(env, b)
    assert r["verdict"] == "PASS", r["line"]
    assert r["line"].startswith("HALF-SELL PROBE: PASS")
    buys = [c[1] for c in b.calls if c[0] == "place_super_order"]
    assert len(buys) == 1 and buys[0]["transaction_type"] == "BUY" and buys[0]["quantity"] == 2 and buys[0]["product_type"] == "INTRADAY"
    assert buys[0]["stop_loss_price"] < 100.0 < buys[0]["target_price"], "a long: stop below, target above"
    seq = _order(b)
    # the protocol sequence; the final step cancels no leg (both are already dead)
    assert seq == ["place_super_order", "cancel:TARGET_LEG", "cancel:STOP_LOSS_LEG", "place_order", "place_intraday_sell_stop", "cancel_order", "place_order"]
    assert b.sells() == [1, 1], "sell one, then sell the last one: never more than the long"
    stop = [c[1] for c in b.calls if c[0] == "place_intraday_sell_stop"][0]
    assert stop["quantity"] == 1 and stop["trigger_price"] < 100.0, "the stop for the kept share sits BELOW the price"
    assert b.nets[SID] == 0 and not b.went_short
    assert _locks(env.db) == 0 and env.db.query(models.ScalpPosition).count() == 0
    assert hs.passed() is True and hs.summary()["passed"] is True
    assert env.alerts and "PASS" in env.alerts[-1]


def test_answers_q1_to_q7_are_recorded(env):
    r = _run(env, FakeDhan(clock=env.clock, stop_delay=1.0))
    q = r["q"]
    assert q["Q1_stop_still_active"] is True and q["Q1_stop_leg_after_target_cancel"] == "PENDING"
    assert q["Q2_parent_after_both_cancels"]["orderStatus"] == "CLOSED" and q["Q2_parent_after_both_cancels"]["quantity"] == 2
    assert q["Q3_sell_accepted"] is True
    assert q["Q4_stop_accepted"] is True and q["Q4_order_type"] == "STOP_LOSS_MARKET"
    assert 1.0 <= q["Q5_unprotected_window_s"] <= 3.0, "the window is T2 (stop cancelled) -> T4 (new stop accepted)"
    assert q["Q5_position_qty_after_sell"] == 1 and len(q["Q5_open_orders"]) == 1
    assert q["Q6_final_net_qty"] == 0 and q["Q6_orphan_orders"] == [] and q["Q6_orphan_legs"] == []
    assert q["Q6_gross_pnl_rs"] == pytest.approx(1.2) and q["Q6_charges_estimate_rs"] > 0
    assert "calls_during_probe" in q["Q7_order_rate"]
    assert set(r["timeline"]) >= {"t0_filled", "t1_target_cancelled", "t2_stop_cancelled", "t3_sell_placed", "t4_stop_accepted", "t6_flat"}
    assert any(x.startswith("Q5:") for x in r["rules"])


def test_q1_records_when_the_stop_leg_dies_with_the_target(env):
    r = _run(env, FakeDhan(clock=env.clock, stop_dies_with_target=True))
    assert r["q"]["Q1_stop_still_active"] is False and r["q"]["Q1_stop_leg_after_target_cancel"] == "CANCELLED"
    assert r["verdict"] == "PASS"
    assert r["answers"]["t2_cancel_stop"].startswith("not needed"), "no second cancel is sent for a leg that is already gone"
    assert any("died together with the target" in x for x in r["rules"])


def test_result_is_saved_and_survives_a_restart(env, monkeypatch):
    _run(env, FakeDhan(clock=env.clock))
    monkeypatch.setattr(hs, "_mem", {})
    assert hs.passed() is True and hs.last_result()["symbol"] == "ITC"


# ------------------------------------------------------------------ refusals before any order
@pytest.mark.parametrize("why,kw", [("confirm phrase", dict(confirm="yes")), ("outside the probe window", dict(now=LATE)), ("above the 500 cap", dict(price=650.0))])
def test_refusals_place_nothing_and_do_not_overwrite_a_real_result(env, why, kw):
    b = FakeDhan(clock=env.clock)
    r = _run(env, b, **kw)
    assert r["verdict"] == "SKIPPED" and why in r["reason"]
    assert not [c for c in b.calls if c[0] in ("place_super_order", "place_order", "cancel_super_order", "place_intraday_sell_stop", "cancel_order")]
    assert hs.last_result() == {} and _locks(env.db) == 0


def test_not_armed_kill_switch_and_super_order_off_skip(env, monkeypatch):
    gate = env.db.query(models.ScalpGateState).first()
    gate.is_armed = False
    env.db.commit()
    b = FakeDhan(clock=env.clock)
    assert "not armed" in _run(env, b)["reason"] and b.calls == []
    gate.is_armed, gate.daily_loss_kill_switch_tripped = True, True
    env.db.commit()
    assert "kill switch" in _run(env, b)["reason"] and b.calls == []
    gate.daily_loss_kill_switch_tripped = False
    env.db.commit()
    monkeypatch.setattr(config, "USE_SUPER_ORDER", False, raising=False)
    assert "USE_SUPER_ORDER" in _run(env, b)["reason"] and b.calls == []


def test_held_symbol_open_row_and_unreadable_positions_skip(env):
    assert "already has a position at Dhan" in _run(env, FakeDhan(clock=env.clock, preheld=3))["reason"]
    b = FakeDhan(clock=env.clock, positions_fail=True)
    assert "positions could not be read" in _run(env, b)["reason"] and b.n("place_super_order") == 0
    env.db.add(models.ScalpPosition(symbol="ITC", window_source="5m", adaptive_target_pct=2.0, adaptive_stop_pct=1.0, status="OPEN", quantity=1, entry_price=100.0, capital_risked=100.0,
                                    overnight_converted_to_cnc=False, dhan_security_id="1", target_price=102.0, stop_price=98.0, opened_at=datetime.now(timezone.utc)))
    env.db.commit()
    assert "open position" in _run(env, FakeDhan(clock=env.clock))["reason"]
    env.db.query(models.ScalpPosition).delete()
    env.db.add(models.SharedSymbolLock(symbol="ITC", held_by_service="real-trade-service", held_by_mode="REAL"))
    env.db.commit()
    assert "held by the other service" in _run(env, FakeDhan(clock=env.clock))["reason"]


# ------------------------------------------------------------------ a surprise at each step: FAIL, and nothing is left open
def test_dhan_refusing_the_buy_is_a_fail_and_nothing_else_is_sent(env):
    b = FakeDhan(clock=env.clock, refuse_buy="Invalid price for super order")
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "Dhan refused the BUY Super Order" in r["reason"]
    assert b.n("place_order") == 0 and b.n("cancel_super_order") == 0 and hs.passed() is False and _locks(env.db) == 0


def test_an_unclear_buy_error_tells_you_to_check_dhan_and_sells_nothing(env):
    b = FakeDhan(clock=env.clock, ambiguous_buy=True)
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "CHECK DHAN" in r["reason"] and b.n("place_order") == 0
    assert any("placement unclear" in a for a in env.alerts)


def test_a_buy_that_never_fills_is_cancelled_and_nothing_is_sold(env):
    b = FakeDhan(clock=env.clock, fill=False)
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "did not fill" in r["reason"]
    assert b.sells() == [], "a SELL with no long under it would open a short"
    assert b.n("cancel_super_order") == 3 and env.db.query(models.ScalpPosition).count() == 0 and _locks(env.db) == 0 and not b.went_short


def test_a_refused_target_cancel_stops_the_probe_and_adopts_the_long_instead_of_selling_under_a_live_leg(env):
    b = FakeDhan(clock=env.clock, refuse_legs=("TARGET_LEG",))
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "TARGET leg failed" in r["reason"] and "NOT FLAT" in r["reason"]
    assert b.sells() == [], "a live exit leg plus a market SELL could sell the same share twice"
    pos = env.db.query(models.ScalpPosition).one()
    assert pos.side == "LONG" and pos.quantity == 2 and pos.status == "OPEN" and pos.stop_price < pos.entry_price < pos.target_price
    assert any("not flat" in a for a in env.alerts)


def test_a_stop_cancel_that_changes_nothing_means_no_sell_is_sent(env):
    b = FakeDhan(clock=env.clock, cancel_noop=True)
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "exit legs still live" in r["reason"]
    assert b.sells() == [] and env.db.query(models.ScalpPosition).one().quantity == 2


def test_a_refused_sell_is_q3_and_the_cleanup_sells_the_whole_long_once(env):
    b = FakeDhan(clock=env.clock, refuse_sell_n=1)
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and r["q"]["Q3_sell_accepted"] is False and "quantity locked" in r["q"]["Q3_answer"]
    assert b.sells() == [1, 2], "the first SELL (1) was refused; the cleanup then sells the whole long (2) once"
    assert b.nets[SID] == 0 and not b.went_short and env.db.query(models.ScalpPosition).count() == 0 and _locks(env.db) == 0
    assert b.n("place_intraday_sell_stop") == 0


def test_a_refused_stop_is_q4_and_the_kept_share_is_sold_not_left_bare(env):
    b = FakeDhan(clock=env.clock, refuse_stop="Invalid order type for intraday")
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and r["q"]["Q4_stop_accepted"] is False and "Invalid order type" in r["q"]["Q4_answer"]
    assert b.sells() == [1, 1], "one sold at T3, the last one by the cleanup"
    assert b.nets[SID] == 0 and not b.went_short and env.db.query(models.ScalpPosition).count() == 0


def test_a_slow_window_is_a_fail_even_when_everything_was_accepted(env):
    r = _run(env, FakeDhan(clock=env.clock, stop_delay=12.0))
    assert r["verdict"] == "FAIL" and "unprotected window was" in r["reason"]
    assert r["q"]["Q5_unprotected_window_s"] > hs.WINDOW_FAIL_S


def test_a_window_between_the_target_and_the_limit_passes_with_a_note(env):
    r = _run(env, FakeDhan(clock=env.clock, stop_delay=5.0))
    assert r["verdict"] == "PASS" and any("above the 3s target" in x for x in r["rules"])


def test_a_sell_that_is_still_pending_at_cleanup_is_cancelled_before_anything_is_sold_again(env):
    # the SELL is accepted but never fills: T5 sees 2 long, the probe stops, the cleanup cancels our open orders FIRST, then sells the real long once
    b = FakeDhan(clock=env.clock, sell_pending=True)
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "expected 1" in r["reason"]
    assert b.n("cancel_order") >= 2, "our pending SELL and our stop are cancelled before the cleanup sells"
    assert b.sells()[1:] == [2] and b.nets[SID] == 0 and not b.went_short


def test_unreadable_positions_in_the_cleanup_sell_nothing_blind(env):
    class B(FakeDhan):
        def get_positions(self, db):
            if self.n("place_order") >= 1:
                self.calls.append(("get_positions", {}))
                raise RuntimeError("dhan 502")
            return super().get_positions(db)
    b = B(clock=env.clock, refuse_stop="nope")
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "NOT FLAT" in r["reason"]
    assert b.sells() == [1], "only the T3 sell: the cleanup could not read the positions and sold nothing"
    assert env.db.query(models.ScalpPosition).one().side == "LONG"


# ------------------------------------------------------------------ wiring
def test_script_and_service_share_the_confirm_phrase_and_the_route_exists():
    root = Path(__file__).resolve().parents[3]
    script = (root / "scripts" / "dhan_half_sell_probe.py").read_text(encoding="utf-8")
    assert f'CONFIRM_PHRASE = "{hs.CONFIRM_PHRASE}"' in script
    main = (root / "services" / "position-stocks-service" / "main.py").read_text(encoding="utf-8")
    assert '@app.post("/half-sell/probe")' in main and '@app.get("/half-sell/probe")' in main
    assert "Depends(require_admin)" in main.split('@app.post("/half-sell/probe")')[1].split("@app.get")[0]


def test_nothing_in_the_live_paths_calls_the_probe_or_the_sell_stop():
    root = Path(__file__).resolve().parents[1]
    for p in list(root.glob("orders/*.py")) + list(root.glob("execution/*.py")) + [root / "main.py"]:
        if p.name in ("half_sell_probe.py", "dhan_client.py"):
            continue
        txt = p.read_text(encoding="utf-8")
        assert "place_intraday_sell_stop" not in txt, f"{p.name} must not place the probe's SELL stop"
        if p.name != "main.py":
            assert "half_sell_probe" not in txt, f"{p.name} must not import the half-sell probe"


# ------------------------------------------------------------------ dhan_client.place_intraday_sell_stop (the SDK is faked)
class _Sdk:
    def __init__(self, order_type="STOP_LOSS_MARKET", status="PENDING", answer_id="S1", in_book=True, book_fails=False):
        self.sent, self.cfg = None, (order_type, status, answer_id, in_book, book_fails)

    def place_order(self, **kw):
        self.sent = kw
        return {"status": "success", "data": {"orderId": self.cfg[2]} if self.cfg[2] else {}}

    def get_order_list(self):
        if self.cfg[4]:
            raise RuntimeError("502")
        return {"status": "success", "data": [{"orderId": "S1", "orderType": self.cfg[0], "orderStatus": self.cfg[1]}] if self.cfg[3] else []}


def _stop(monkeypatch, **cfg):
    sdk = _Sdk(**cfg)
    monkeypatch.setattr(dhan_client, "_get_sdk_client", lambda db: sdk)
    monkeypatch.setattr(dhan_client.order_rate, "acquire", lambda kind, wait=False: 0.0)
    return sdk


def test_sell_stop_sends_a_sell_stop_loss_market_intraday_with_a_trigger_and_no_price(monkeypatch):
    sdk = _stop(monkeypatch)
    out = dhan_client.place_intraday_sell_stop(None, is_armed=True, security_id=SID, exchange_segment="NSE_EQ", quantity=1, trigger_price=98.987, tag="T")
    assert out["orderId"] == "S1"
    assert sdk.sent["transaction_type"] == "SELL" and sdk.sent["order_type"] == "STOP_LOSS_MARKET" and sdk.sent["product_type"] == "INTRADAY"
    assert sdk.sent["price"] == 0.0 and sdk.sent["quantity"] == 1 and sdk.sent["trigger_price"] == dhan_client.round_stop_to_tick(98.987)


@pytest.mark.parametrize("cfg,needle", [(dict(answer_id=""), "did not return an orderId"), (dict(in_book=False), "not in the order list"), (dict(book_fails=True), "could not be read"),
                                        (dict(order_type="LIMIT"), "ORDER TYPE MISMATCH"), (dict(status="REJECTED"), "not a live pending state")])
def test_sell_stop_raises_unless_the_stop_is_confirmed_live(monkeypatch, cfg, needle):
    _stop(monkeypatch, **cfg)
    with pytest.raises(RuntimeError, match=needle):
        dhan_client.place_intraday_sell_stop(None, is_armed=True, security_id=SID, exchange_segment="NSE_EQ", quantity=1, trigger_price=98.0)


def test_sell_stop_refuses_when_not_armed_or_with_no_trigger(monkeypatch):
    _stop(monkeypatch)
    with pytest.raises(dhan_client.DhanNotArmedError):
        dhan_client.place_intraday_sell_stop(None, is_armed=False, security_id=SID, exchange_segment="NSE_EQ", quantity=1, trigger_price=98.0)
    with pytest.raises(ValueError):
        dhan_client.place_intraday_sell_stop(None, is_armed=True, security_id=SID, exchange_segment="NSE_EQ", quantity=1, trigger_price=0)
