"""Round 293: an entry BUY whose POST failed in a way that may have reached Dhan (network, HTTP 5xx) is looked up by the correlationId we sent (GET /orders/external/{id}, read only).
Found: handled as placed (settle, stop). Cannot tell: kept as a flagged UNKNOWN entry (no stop, never sold, blocks a second BUY) and looked up each cycle; resolved from Dhan's answer.
A part-filled entry whose rest was cancelled is kept at the filled whole lots (before: flagged forever, no stop). Nothing here talks to Dhan."""
from __future__ import annotations

from datetime import timedelta

from fno.execution import OrderResult, OrderStatus
from test_engine_paper import NOW
from test_real_partial import RB, db, real, stops  # noqa: F401  (db is a fixture)


class LostBuy(RB):
    """The BUY call fails with `status`; `lookup(cid)` is what Dhan answers to the correlationId lookup. `landed` = the BUY did reach Dhan as an order of that state / fill."""
    def __init__(self, lookup=None, status="NETWORK", net=None):
        super().__init__(net=net)
        self.lookup, self.status, self.asked, self.landed = lookup, status, [], {}

    def place(self, payload):
        if payload["transactionType"] == "BUY" and payload["orderType"] == "LIMIT":
            self.sent.append(payload)
            return OrderResult(False, status=self.status, message="boom")
        return super().place(payload)

    def order_by_correlation(self, cid):
        self.asked.append(cid)
        return self.lookup(cid) if self.lookup else None


def _found(state, filled=0, price=0.0, oid="LB1"):
    return lambda cid: OrderStatus(state, price, filled, {"orderId": oid})


def buys(b):
    return [o for o in b.sent if o["transactionType"] == "BUY"]


def _kept(db):
    ps = db.open_positions("REAL")
    assert len(ps) == 1
    return ps[0]


# ---- at entry --------------------------------------------------------------------------------------------------------------------
def test_the_buy_carries_our_own_correlation_id_and_it_is_the_one_looked_up(db):
    b = LostBuy()
    real(db, b).tick(NOW)
    cid = buys(b)[0]["correlationId"]
    assert cid.startswith("bu") and len(cid) <= 30 and b.asked == [cid]


def test_a_buy_that_did_reach_dhan_and_traded_is_booked_with_a_stop(db):
    b = LostBuy(_found("TRADED", filled=260, price=101.5))
    b.states["LB1"] = OrderStatus("TRADED", 101.5, 260)
    p = (real(db, b).tick(NOW), _kept(db))[1]
    assert p.broker_order_ids["entry"] == "LB1" and "entry_state" not in p.broker_order_ids and "entry_cid" not in p.broker_order_ids
    assert p.entry_premium == 101.5 and len(stops(b)) == 1 and p.broker_order_ids.get("sl")


def test_a_buy_found_resting_is_cancelled_like_any_unfilled_entry(db):
    b = LostBuy(_found("PENDING"))
    b.states["LB1"] = OrderStatus("PENDING")
    b.cancel_to["LB1"] = "CANCELLED"
    real(db, b).tick(NOW)
    assert b.cancelled == ["LB1"] and not db.open_positions("REAL") and not stops(b)


def test_a_buy_found_rejected_or_cancelled_with_nothing_filled_keeps_no_row(db):
    for state in ("REJECTED", "CANCELLED"):
        b = LostBuy(_found(state))
        real(db, b).tick(NOW)
        assert not db.open_positions("REAL"), state


def test_a_buy_that_cannot_be_read_is_kept_unknown_with_no_stop_and_the_event_says_so(db):
    for bad in (None, lambda cid: OrderStatus("NOT_FOUND"), lambda cid: OrderStatus("UNKNOWN")):
        b = LostBuy(bad)
        with_db = type(db)(":memory:")
        real(with_db, b).tick(NOW)
        p = _kept(with_db)
        assert p.broker_order_ids["entry_state"] == "UNKNOWN" and p.broker_order_ids["entry_cid"] == buys(b)[0]["correlationId"] and "entry" not in p.broker_order_ids
        assert not stops(b)
        assert any("kept as an unknown entry" in x.get("msg", "") for x in with_db.events("reconcile"))
        with_db.close()


def test_a_5xx_is_treated_like_a_network_error(db):
    b = LostBuy(status="HTTP 502")
    real(db, b).tick(NOW)
    assert _kept(db).broker_order_ids["entry_state"] == "UNKNOWN" and len(b.asked) == 1


def test_a_plain_refusal_asks_nothing_and_keeps_no_row(db):
    b = LostBuy(status="HTTP 400")
    real(db, b).tick(NOW)
    assert not db.open_positions("REAL") and b.asked == []


def test_an_unknown_entry_blocks_a_second_buy_in_the_next_cycles(db):
    b = LostBuy()
    e = real(db, b)
    e.tick(NOW)
    e.tick(NOW + timedelta(seconds=60))
    e.tick(NOW + timedelta(seconds=120))
    assert len(buys(b)) == 1 and len(db.open_positions("REAL")) == 1


# ---- the next cycles -------------------------------------------------------------------------------------------------------------
def _unknown(db, b):
    e = real(db, b)
    e.tick(NOW)
    return e, _kept(db)


def test_it_stays_unknown_while_the_lookup_cannot_answer(db):
    b = LostBuy()
    e, p = _unknown(db, b)
    e.resolve_unknown_entries(NOW + timedelta(seconds=300))
    p = _kept(db)
    assert p.broker_order_ids["entry_state"] == "UNKNOWN" and not stops(b)


def test_a_404_is_waited_out_and_then_the_row_is_closed_as_never_placed(db):
    b = LostBuy(lambda cid: OrderStatus("NOT_FOUND"))
    e, p = _unknown(db, b)
    e.resolve_unknown_entries(NOW + timedelta(seconds=30))               # too early: a delayed order may still land
    assert _kept(db).broker_order_ids["entry_state"] == "UNKNOWN"
    e.resolve_unknown_entries(NOW + timedelta(seconds=130))
    assert not db.open_positions("REAL") and not stops(b)
    assert db.get_position(p.id).exit_reason == "NOT_FILLED" and db.get_position(p.id).net_pnl == 0.0


def test_a_late_fill_is_confirmed_at_the_real_price_and_gets_its_stop(db):
    answer = {"v": None}
    b = LostBuy(lambda cid: answer["v"])
    e, p = _unknown(db, b)
    answer["v"] = OrderStatus("TRADED", 99.0, 260, {"orderId": "LB9"})
    b.states["LB9"] = OrderStatus("TRADED", 99.0, 260)
    e.resolve_unknown_entries(NOW + timedelta(seconds=60))
    p = _kept(db)
    assert p.broker_order_ids["entry"] == "LB9" and "entry_state" not in p.broker_order_ids and "entry_cid" not in p.broker_order_ids
    assert p.entry_premium == 99.0 and len(stops(b)) == 1 and stops(b)[0]["quantity"] == p.qty


def test_a_late_resting_order_is_cancelled_and_what_filled_decides(db):
    answer = {"v": None}
    b = LostBuy(lambda cid: answer["v"])
    e, p = _unknown(db, b)
    answer["v"] = OrderStatus("PENDING", 0.0, 0, {"orderId": "LB5"})
    b.states["LB5"] = OrderStatus("PENDING")
    e.resolve_unknown_entries(NOW + timedelta(seconds=60))
    assert b.cancelled == ["LB5"] and not db.open_positions("REAL") and not stops(b)


def test_a_late_partial_fill_keeps_exactly_the_filled_lots_with_a_stop_for_them(db):
    answer = {"v": None}
    b = LostBuy(lambda cid: answer["v"])
    e, p = _unknown(db, b)
    assert p.lots >= 2
    answer["v"] = OrderStatus("PENDING", 0.0, 65, {"orderId": "LB6"})
    b.states["LB6"] = OrderStatus("PENDING", 98.0, 65)
    e.resolve_unknown_entries(NOW + timedelta(seconds=60))
    p = _kept(db)
    assert b.cancelled == ["LB6"] and p.lots == 1 and p.qty == 65 and p.entry_premium == 98.0 and "PARTIAL fill" in p.note
    assert len(stops(b)) == 1 and stops(b)[0]["quantity"] == 65


# ---- the older gap closed on the way: a part-filled entry whose cancel was not confirmed ------------------------------------------
def test_an_entry_flagged_unknown_after_a_partial_fill_is_settled_when_the_cancel_lands(db):
    b = RB()
    b.buy = {"state": "PENDING", "filled": 65, "cancel_to": "PENDING"}   # one lot fills; the cancel is not confirmed
    e = real(db, b)
    e.tick(NOW)
    p = _kept(db)
    assert p.lots == 1 and p.broker_order_ids["entry_state"] == "UNKNOWN" and not stops(b)
    b.states["O1"] = OrderStatus("CANCELLED", 98.0, 65)                    # Dhan now shows the cancel
    e.resolve_unknown_entries(NOW + timedelta(seconds=60))
    p = _kept(db)
    assert "entry_state" not in p.broker_order_ids and len(stops(b)) == 1 and stops(b)[0]["quantity"] == 65


def test_a_part_fill_of_less_than_one_lot_stays_flagged_and_loud(db):
    b = RB()
    b.buy = {"state": "PENDING", "filled": 65, "cancel_to": "PENDING"}
    e = real(db, b)
    e.tick(NOW)
    b.states["O1"] = OrderStatus("CANCELLED", 98.0, 30)
    e.resolve_unknown_entries(NOW + timedelta(seconds=60))
    assert _kept(db).broker_order_ids["entry_state"] == "UNKNOWN" and not stops(b)
    assert any("less than one lot" in x.get("msg", "") for x in db.events("reconcile"))
