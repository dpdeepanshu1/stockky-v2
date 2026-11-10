"""F&O round 231: the REAL path with PARTIAL fills and orders whose state is in doubt. A scriptable fake broker that CAN confirm (can_confirm = True), so a missing answer is a
network miss, never "assume it went through". Nothing here talks to Dhan."""
from __future__ import annotations

from datetime import timedelta

import pytest

from fno import execution, paper, store as st
from fno.execution import OrderResult, OrderStatus
from test_engine_paper import NOW, FakeProvider, engine, make_cfg


class RB(execution.Broker):
    """What a new BUY / SELL becomes is set in `buy` / `sell`: {"state", "filled" (qty; None = all for TRADED, else 0), "cancel_to" (state after a cancel; default CANCELLED)}.
    state "SILENT" = order_status answers None (no answer at all). `miss` = {oid: n}: the next n status calls for that order answer None (a network blip)."""
    can_confirm = True

    def __init__(self, net=None):
        self.sent, self.states, self.cancelled, self.net = [], {}, [], net
        self.buy, self.sell = {"state": "TRADED"}, {"state": "TRADED"}
        self.cancel_to, self.miss = {}, {}

    def _mk(self, plan, payload):
        q = payload["quantity"]
        if plan["state"] == "SILENT":
            return None
        f = plan.get("filled")
        filled = q if (plan["state"] == "TRADED" and f is None) else (f or 0)
        return OrderStatus(plan["state"], round(payload["price"] - 0.5, 2) if filled else 0.0, filled)

    def place(self, payload):
        self.sent.append(payload)
        oid = "O%d" % len(self.sent)
        if payload.get("orderType") == "STOP_LOSS":
            self.states[oid] = OrderStatus("PENDING")
        else:
            plan = self.buy if payload["transactionType"] == "BUY" else self.sell
            s = self._mk(plan, payload)
            if s is not None:
                self.states[oid] = s
            self.cancel_to[oid] = plan.get("cancel_to", "CANCELLED")
        return OrderResult(True, order_id=oid, status="TRANSIT")

    def order_status(self, oid):
        if self.miss.get(oid, 0) > 0:
            self.miss[oid] -= 1
            return None
        return self.states.get(oid)

    def cancel(self, oid):
        self.cancelled.append(oid)
        s = self.states.get(oid)
        if s is not None and s.state == "PENDING":
            self.states[oid] = OrderStatus(self.cancel_to.get(oid, "CANCELLED"), s.avg_price, s.filled_qty)
        return OrderResult(True, order_id=oid)

    def net_positions(self):
        return self.net


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def real(db, broker, **kw):
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_MODE="REAL", FNO_REAL_ENABLED=True, DHAN_CLIENT_ID="1000", DHAN_ACCESS_TOKEN="tok",
                   FNO_RISK_PER_TRADE_PCT=3.0, FNO_MAX_LOTS=4, **kw)
    e = engine(db, FakeProvider(), cfg, broker=broker)
    e.sleep = lambda s: None
    db.kv_set("fno_armed_day", "2026-11-10")
    return e


def sells(b):
    return [o for o in b.sent if o["transactionType"] == "SELL" and o["orderType"] == "LIMIT"]


def stops(b):
    return [o for o in b.sent if o["orderType"] == "STOP_LOSS"]


def entered(db, b, e):
    e.tick(NOW)
    ps = db.open_positions("REAL")
    assert ps and b.sent[0]["quantity"] >= 130, "the tests need an entry of two lots or more"
    return ps[0]


# ---- entry ---------------------------------------------------------------------------------------------------------------------
def test_entry_filled_in_part_keeps_the_filled_lots_with_a_stop_for_exactly_those(db):
    b = RB()
    b.buy = {"state": "PENDING", "filled": 65}                       # one lot of several fills, the rest never does
    e = real(db, b)
    p = entered(db, b, e)
    assert p.lots == 1 and p.qty == 65 and "PARTIAL fill: 65 of" in p.note
    assert p.entry_premium == round(b.sent[0]["price"] - 0.5, 2)
    assert b.cancelled == ["O1"] and p.broker_order_ids.get("entry_state") is None
    assert len(stops(b)) == 1 and stops(b)[0]["quantity"] == 65 and p.broker_order_ids["sl"]
    assert any(x["kind"] == "reconcile" and "only in part" in x.get("msg", "") for x in db.events())


def test_entry_filled_less_than_one_lot_keeps_no_position(db):
    b = RB()
    b.buy = {"state": "PENDING", "filled": 30}
    e = real(db, b)
    e.tick(NOW)
    assert db.open_positions() == [] and not stops(b)
    assert any("less than one lot" in x.get("message", "") for x in db.events())


def test_partial_entry_whose_cancel_did_not_land_is_flagged_and_gets_no_stop(db):
    b = RB()
    b.buy = {"state": "PENDING", "filled": 65, "cancel_to": "PENDING"}
    e = real(db, b)
    p = entered(db, b, e)
    assert p.lots == 1 and p.broker_order_ids["entry_state"] == "UNKNOWN" and not stops(b)


def test_entry_with_no_answer_is_flagged_unknown_gets_no_stop_and_is_never_sold_blind(db):
    b = RB()
    b.buy = {"state": "SILENT"}
    e = real(db, b)
    p = entered(db, b, e)
    assert p.broker_order_ids["entry_state"] == "UNKNOWN" and not stops(b) and "UNKNOWN" in p.note
    e.squareoff_all(now=NOW + timedelta(seconds=30))                 # Dhan's positions are unreachable: no SELL (it could open a short)
    assert not sells(b) and db.get_position(p.id).status == "OPEN"
    b.net = {}                                                       # Dhan answers, and shows nothing held: still no SELL
    e.squareoff_all(now=NOW + timedelta(seconds=60))
    assert not sells(b) and db.get_position(p.id).status == "OPEN"


def test_unknown_entry_is_confirmed_by_dhans_net_position_then_gets_its_stop_and_can_exit(db):
    b = RB()
    b.buy = {"state": "SILENT"}
    e = real(db, b)
    p = entered(db, b, e)
    b.net = {str(p.security_id): p.qty}
    e.tick(NOW + timedelta(seconds=10))
    q = db.get_position(p.id)
    assert q.broker_order_ids.get("entry_state") is None and "now confirmed" in q.note and len(stops(b)) == 1
    e.squareoff_all(now=NOW + timedelta(seconds=20))
    assert len(sells(b)) == 1 and db.get_position(p.id).status == "CLOSED"


def test_unknown_entry_that_turns_out_rejected_is_closed_as_not_filled(db):
    b = RB()
    b.buy = {"state": "SILENT"}
    e = real(db, b)
    p = entered(db, b, e)
    b.states["O1"] = OrderStatus("REJECTED")
    e.tick(NOW + timedelta(seconds=10))
    q = db.get_position(p.id)
    assert q.status == "CLOSED" and q.exit_reason == "NOT_FILLED" and q.net_pnl == 0.0 and not sells(b)


def test_a_network_miss_while_waiting_for_the_fill_is_retried_not_assumed(db):
    b = RB()
    b.miss = {"O1": 2}                                               # the first two status calls get no answer
    e = real(db, b)
    p = entered(db, b, e)
    assert p.entry_premium == round(b.sent[0]["price"] - 0.5, 2) and "unconfirmed" not in p.note and p.broker_order_ids.get("entry_state") is None


# ---- exit ----------------------------------------------------------------------------------------------------------------------
def test_exit_filled_in_part_books_the_sold_lots_and_keeps_the_rest_with_a_new_stop(db):
    b = RB()
    e = real(db, b)
    p = entered(db, b, e)
    lots0, sl0 = p.lots, p.broker_order_ids["sl"]
    b.sell = {"state": "PENDING", "filled": 65}
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    q = db.get_position(p.id)
    kids = [x for x in db.positions("REAL") if x.parent_id == p.id]
    assert q.status == "OPEN" and q.lots == lots0 - 1 and len(kids) == 1
    assert kids[0].status == "CLOSED" and kids[0].lots == 1 and kids[0].exit_reason == paper.MANUAL and kids[0].net_pnl is not None
    assert q.broker_order_ids["sl"] != sl0 and stops(b)[-1]["quantity"] == q.qty and "exit" not in q.broker_order_ids
    s = db.day_summary("2026-11-10", "REAL")
    assert s["trades"] == 1 and s["open"] == 1                       # the child row is not a second trade


def test_the_remaining_lots_are_sold_on_the_next_try(db):
    b = RB()
    e = real(db, b)
    p = entered(db, b, e)
    b.sell = {"state": "PENDING", "filled": 65}
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    b.sell = {"state": "TRADED"}
    e.squareoff_all(now=NOW + timedelta(seconds=60))
    rows = [x for x in db.positions("REAL") if x.id == p.id or x.parent_id == p.id]
    assert all(x.status == "CLOSED" for x in rows) and sum(x.lots for x in rows) == b.sent[0]["quantity"] // 65
    bought = b.sent[0]["quantity"]
    assert sells(b)[0]["quantity"] == bought and sells(b)[1]["quantity"] == bought - 65     # the retry sells only what is left (65 of the first SELL had filled), never the whole again


def test_exit_in_doubt_is_never_repeated_until_it_is_known(db):
    b = RB()
    e = real(db, b)
    p = entered(db, b, e)
    b.sell = {"state": "PENDING", "cancel_to": "PENDING"}            # the cancel does not land: the SELL may still fill
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    q = db.get_position(p.id)
    assert q.status == "OPEN" and q.broker_order_ids.get("exit") and "UNKNOWN" in q.note and len(sells(b)) == 1
    n_sent = len(b.sent)
    e.squareoff_all(now=NOW + timedelta(seconds=60))                 # still in doubt: no second SELL, no new stop
    assert len(b.sent) == n_sent
    oid = q.broker_order_ids["exit"]
    b.states[oid] = OrderStatus("TRADED", 99.0, q.qty)               # it did fill after all
    e.squareoff_all(now=NOW + timedelta(seconds=90))
    z = db.get_position(p.id)
    assert z.status == "CLOSED" and z.exit_premium == 99.0 and len(b.sent) == n_sent


def test_stop_part_filled_before_the_exit_is_booked_and_only_the_rest_is_sold(db):
    b = RB()
    e = real(db, b)
    p = entered(db, b, e)
    lots0 = p.lots
    b.states[p.broker_order_ids["sl"]] = OrderStatus("PENDING", 70.0, 65)    # the stop sold one lot before the engine got to it
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    kids = [x for x in db.positions("REAL") if x.parent_id == p.id]
    assert len(kids) == 1 and kids[0].exit_reason == paper.SL and kids[0].exit_premium == 70.0 and kids[0].lots == 1
    assert sells(b)[-1]["quantity"] == (lots0 - 1) * 65 and db.get_position(p.id).status == "CLOSED"


def test_exit_with_a_stop_whose_state_is_unreadable_waits_instead_of_selling(db):
    b = RB()
    e = real(db, b)
    p = entered(db, b, e)
    b.miss = {p.broker_order_ids["sl"]: 50}                          # round 294: every read of the stop gets no answer, also the retries after the cancel
    n = len(sells(b))
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    assert len(sells(b)) == n and db.get_position(p.id).status == "OPEN"
    assert any("stop order state is unknown" in x.get("msg", "") for x in db.events())


# ---- plumbing ------------------------------------------------------------------------------------------------------------------
def test_old_rows_load_as_core_and_children_round_trip(db):
    old = paper.Position.from_json({"underlying": "NIFTY", "expiry": "2026-11-17", "strike": 24500.0, "opt_type": "CE", "lot_size": 65, "lots": 1, "entry_ts": NOW.timestamp(),
                                    "entry_premium": 100.0, "sl_price": 75.0, "target_price": 150.0})
    assert old.strategy == "CORE" and old.parent_id is None
    with pytest.raises(ValueError):
        paper.split_closed(old, 1, 90.0, NOW.timestamp(), paper.SL, paper.Params() if hasattr(paper, "Params") else None)


def test_dhan_broker_can_confirm_and_the_base_cannot():
    assert execution.DhanBroker("https://x/v2", "1", "t").can_confirm is True and execution.Broker.can_confirm is False


def test_unknown_entry_confirmed_by_its_order_status_is_rebased_on_the_real_price(db):
    from fno.params import Params
    b = RB()
    b.buy = {"state": "SILENT"}
    e = real(db, b)
    p = entered(db, b, e)
    b.states["O1"] = OrderStatus("TRADED", 80.0, p.qty)
    e.tick(NOW + timedelta(seconds=10))
    q = db.get_position(p.id)
    pp = Params.from_config(e.cfg)
    assert q.entry_premium == 80.0 and q.sl_price == paper.round_tick(80.0 * (1 - pp.sl_pct / 100.0), 0.05, up=True)
    assert q.target_price == paper.round_tick(80.0 * (1 + pp.target_pct / 100.0), 0.05, up=True) and q.broker_order_ids.get("entry_state") is None
    assert stops(b)[-1]["triggerPrice"] == q.sl_price if "triggerPrice" in stops(b)[-1] else True


def test_a_partial_exit_does_not_pay_the_entry_brokerage_twice_and_a_trade_is_one_win_or_loss(db):
    from fno import costs
    from fno.params import Params
    b = RB()
    e = real(db, b)
    p = entered(db, b, e)
    b.sell = {"state": "PENDING", "filled": 65}
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    kid = [x for x in db.positions("REAL") if x.parent_id == p.id][0]
    pp = Params.from_config(e.cfg)
    full = costs.round_trip(kid.underlying, kid.entry_premium, kid.exit_premium, kid.qty, pp).total
    assert kid.charges < round(full, 2) - pp.brokerage_per_order * 0.99
    b.sell = {"state": "TRADED"}
    e.squareoff_all(now=NOW + timedelta(seconds=60))
    s = db.day_summary("2026-11-10", "REAL")
    assert s["wins"] + s["losses"] == 1 and s["trades"] == 1
