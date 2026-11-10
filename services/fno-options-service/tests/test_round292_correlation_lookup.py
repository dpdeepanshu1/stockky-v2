"""Round 292: an order whose POST failed in a way that may have reached Dhan (network, HTTP 5xx) is looked up by the correlationId we sent (GET /orders/external/{id}, read only).
Exit SELL: what Dhan says decides (traded: booked at Dhan's price; resting: wait; rejected / cancelled: the mark is cleared). Stop: no second stop while one may be resting; a found stop is kept as the stop.
A lookup that cannot answer (network, 5xx, 400 ...) is never read as "not there". Nothing here talks to Dhan."""
from __future__ import annotations

import io
import json
import urllib.error
from datetime import timedelta

from fno import execution, paper
from fno.execution import DhanBroker, OrderResult, OrderStatus
from test_engine_paper import NOW
from test_real_partial import RB, db, real, entered, sells, stops  # noqa: F401  (db is a fixture)


# ---- the Dhan call --------------------------------------------------------------------------------------------------------------
class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def _broker(reply, seen=None):
    def opener(req, timeout=0):
        if seen is not None:
            seen.append((req.get_method(), req.full_url))
        if isinstance(reply, Exception):
            raise reply
        return _Resp(reply)
    return DhanBroker("https://x/v2", "1", "t", opener=opener)


def _http(code):
    return urllib.error.HTTPError("https://x", code, "m", {}, io.BytesIO(b"{}"))


def test_the_lookup_reads_the_order_by_its_correlation_id_with_a_get():
    seen = []
    st = _broker({"orderId": "77", "orderStatus": "TRADED", "averageTradedPrice": 101.5, "filledQty": 65}, seen).order_by_correlation("abc123")
    assert seen == [("GET", "https://x/v2/orders/external/abc123")]
    assert st.state == "TRADED" and st.avg_price == 101.5 and st.filled_qty == 65 and st.raw["orderId"] == "77"


def test_a_pending_order_and_a_list_reply_are_read():
    assert _broker({"orderId": "1", "orderStatus": "PENDING"}).order_by_correlation("a").state == "PENDING"
    assert _broker([{"orderId": "1", "orderStatus": "TRIGGER_PENDING"}]).order_by_correlation("a").state == "PENDING"


def test_only_a_404_is_not_found_and_everything_else_is_cannot_tell():
    assert _broker(_http(404)).order_by_correlation("a").state == "NOT_FOUND"
    for bad in (_http(400), _http(401), _http(429), _http(500), _http(504), TimeoutError(), urllib.error.URLError("down")):
        assert _broker(bad).order_by_correlation("a") is None, bad
    for empty in ({}, [], {"errorCode": "DH-906"}, "x"):
        assert _broker(empty).order_by_correlation("a") is None


def test_the_id_is_quoted_and_a_blank_id_asks_nothing():
    seen = []
    _broker({"orderStatus": "PENDING", "orderId": "1"}, seen).order_by_correlation("a/b c")
    assert seen[0][1].endswith("/orders/external/a%2Fb%20c")
    assert _broker({}, seen).order_by_correlation("  ") is None and len(seen) == 1


def test_the_base_broker_cannot_tell():
    assert execution.Broker().order_by_correlation("a") is None


# ---- the engine: exit SELL ------------------------------------------------------------------------------------------------------
class Look(RB):
    """Every LIMIT SELL call fails with NETWORK (it may have reached Dhan); `lookup(cid)` is what Dhan answers to the correlationId lookup."""
    def __init__(self, lookup=None):
        super().__init__()
        self.fail, self.lookup, self.asked = False, lookup, []

    def place(self, payload):
        if self.fail and payload["transactionType"] == "SELL" and payload["orderType"] == "LIMIT":
            self.sent.append(payload)
            return OrderResult(False, status="NETWORK", message="boom")
        return super().place(payload)

    def order_by_correlation(self, cid):
        self.asked.append(cid)
        return self.lookup(cid) if self.lookup else None


def _doubt(db, b):
    e = real(db, b)
    p = entered(db, b, e)
    b.fail = True
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    q = db.get_position(p.id)
    assert q.broker_order_ids["exit_doubt"] and len(sells(b)) == 1
    return e, q


def test_a_sell_that_dhan_shows_as_traded_is_booked_at_dhans_price_even_while_positions_show_it_held(db):
    b = Look(lambda cid: OrderStatus("TRADED", 98.25, 130, {"orderId": "S9"}))
    e, q = _doubt(db, b)
    b.net = {str(q.security_id): q.qty}                                          # the positions call still shows it: only the order lookup is believed
    e.squareoff_all(now=NOW + timedelta(seconds=60))
    r = db.get_position(q.id)
    assert r.status == "CLOSED" and r.exit_reason == paper.MANUAL and abs(r.exit_premium - 98.25) < 1e-9
    assert "exit_doubt" not in r.broker_order_ids and "correlationId" in r.note and len(sells(b)) == 1
    assert b.asked == [q.broker_order_ids["exit_doubt"]]


def test_a_sell_resting_at_dhan_keeps_the_mark_and_sends_nothing(db):
    b = Look(lambda cid: OrderStatus("PENDING", 0.0, 0, {"orderId": "S9"}))
    e, q = _doubt(db, b)
    n = len(stops(b))
    for k in (60, 90, 120):
        e.squareoff_all(now=NOW + timedelta(seconds=k))
    r = db.get_position(q.id)
    assert r.status == "OPEN" and r.broker_order_ids.get("exit_doubt") and len(sells(b)) == 1 and len(stops(b)) == n
    assert any("is at Dhan" in x.get("msg", "") for x in db.events("reconcile"))


def test_a_sell_dhan_says_was_rejected_clears_the_mark_and_the_engine_sells_again(db):
    b = Look(lambda cid: OrderStatus("REJECTED", 0.0, 0, {"orderId": "S9"}))
    e, q = _doubt(db, b)
    b.fail = False
    e.squareoff_all(now=NOW + timedelta(seconds=60))                             # the lookup clears the mark ...
    assert "exit_doubt" not in db.get_position(q.id).broker_order_ids and db.get_position(q.id).status == "OPEN"
    e.squareoff_all(now=NOW + timedelta(seconds=90))                             # ... and the next cycle sells
    assert len(sells(b)) == 2 and db.get_position(q.id).status == "CLOSED"
    assert any("was rejected" in x.get("msg", "") for x in db.events("reconcile"))


def test_a_lookup_that_cannot_answer_changes_nothing(db):
    for answer in (None, OrderStatus("NOT_FOUND"), OrderStatus("UNKNOWN")):
        with_db = type(db)(":memory:")
        b = Look(lambda cid, a=answer: a)
        e, q = _doubt(with_db, b)
        b.net = {str(q.security_id): q.qty}
        for k in (60, 90):
            e.squareoff_all(now=NOW + timedelta(seconds=k))
        r = with_db.get_position(q.id)
        assert r.status == "OPEN" and r.broker_order_ids.get("exit_doubt") and len(sells(b)) == 1, answer
        with_db.close()


def test_a_mark_without_a_correlation_id_asks_nothing(db):
    b = Look(lambda cid: OrderStatus("TRADED", 98.25, 130, {"orderId": "S9"}))
    e, q = _doubt(db, b)
    q.broker_order_ids["exit_doubt"] = "?"
    db.update_position(q)
    b.asked.clear()
    e.squareoff_all(now=NOW + timedelta(seconds=60))
    assert b.asked == [] and db.get_position(q.id).status == "OPEN"


# ---- the engine: stop orders ----------------------------------------------------------------------------------------------------
class StopFails(RB):
    """STOP_LOSS calls fail with `status` while `fail_stop` is on; `lookup(cid)` answers the correlationId lookup."""
    def __init__(self, status="NETWORK", lookup=None):
        super().__init__()
        self.status, self.fail_stop, self.lookup, self.asked = status, True, lookup, []

    def place(self, payload):
        if self.fail_stop and payload["orderType"] == "STOP_LOSS":
            self.sent.append(payload)
            return OrderResult(False, status=self.status, message="boom")
        return super().place(payload)

    def order_by_correlation(self, cid):
        self.asked.append(cid)
        return self.lookup(cid) if self.lookup else None


def _entered_with_failed_stop(db, b):
    e = real(db, b)
    p = entered(db, b, e)
    q = db.get_position(p.id)
    return e, q


def test_a_stop_call_that_may_have_reached_dhan_is_remembered_with_its_correlation_id(db):
    b = StopFails()
    e, q = _entered_with_failed_stop(db, b)
    assert q.broker_order_ids.get("sl_doubt") == stops(b)[0]["correlationId"] and q.broker_order_ids.get("sl_doubt_ts")
    assert "sl" not in q.broker_order_ids
    assert any("MAY be live" in str(x) for x in db.events("order_failed"))


def test_while_the_stop_is_in_doubt_and_the_lookup_cannot_answer_no_second_stop_is_placed(db):
    b = StopFails()
    e, q = _entered_with_failed_stop(db, b)
    for k in (30, 60, 90, 120, 150):
        e.tick(NOW + timedelta(seconds=k))
    assert len(stops(b)) == 1 and db.get_position(q.id).broker_order_ids.get("sl_doubt")


def test_a_stop_found_at_dhan_is_kept_and_no_second_one_is_placed(db):
    b = StopFails(lookup=lambda cid: OrderStatus("PENDING", 0.0, 0, {"orderId": "S1"}))
    b.states["S1"] = OrderStatus("PENDING")
    e, q = _entered_with_failed_stop(db, b)
    b.fail_stop = False
    e.tick(NOW + timedelta(seconds=30))
    r = db.get_position(q.id)
    assert r.broker_order_ids.get("sl") == "S1" and "sl_doubt" not in r.broker_order_ids and len(stops(b)) == 1


def test_a_stop_dhan_says_was_rejected_is_placed_again(db):
    b = StopFails(lookup=lambda cid: OrderStatus("REJECTED", 0.0, 0, {"orderId": "S1"}))
    e, q = _entered_with_failed_stop(db, b)
    b.fail_stop = False
    e.tick(NOW + timedelta(seconds=30))
    r = db.get_position(q.id)
    assert "sl_doubt" not in r.broker_order_ids and r.broker_order_ids.get("sl") and len(stops(b)) == 2


def test_not_found_is_trusted_only_after_two_minutes(db):
    b = StopFails(lookup=lambda cid: OrderStatus("NOT_FOUND"))
    e, q = _entered_with_failed_stop(db, b)
    b.fail_stop = False
    e.tick(NOW + timedelta(seconds=60))
    assert len(stops(b)) == 1 and db.get_position(q.id).broker_order_ids.get("sl_doubt")       # too early: a delayed order may still land
    e.tick(NOW + timedelta(seconds=400))
    r = db.get_position(q.id)
    assert "sl_doubt" not in r.broker_order_ids and r.broker_order_ids.get("sl") and len(stops(b)) == 2


def test_a_plain_4xx_refusal_of_a_stop_is_not_in_doubt_and_retries_as_before(db):
    b = StopFails(status="HTTP 400")
    e, q = _entered_with_failed_stop(db, b)
    assert "sl_doubt" not in q.broker_order_ids
    e.tick(NOW + timedelta(seconds=30))
    assert len(stops(b)) >= 2


def test_an_exit_is_not_held_back_by_a_stop_in_doubt_but_says_to_check_the_app(db):
    b = StopFails()
    e, q = _entered_with_failed_stop(db, b)
    e.squareoff_all(now=NOW + timedelta(seconds=60))
    r = db.get_position(q.id)
    assert len(sells(b)) == 1 and r.status == "CLOSED"
    assert any("cancel any stop" in x.get("msg", "") for x in db.events("reconcile"))


def test_clearing_the_stop_mark_lets_the_engine_place_a_stop_again(db):
    b = StopFails()
    e, q = _entered_with_failed_stop(db, b)
    b.fail_stop = False
    e.tick(NOW + timedelta(seconds=30))
    assert len(stops(b)) == 1                                                                   # still waiting
    assert e.clear_stop_doubt(q.id) == {"position": q.id, "cleared": True}
    e.tick(NOW + timedelta(seconds=60))
    assert len(stops(b)) == 2 and db.get_position(q.id).broker_order_ids.get("sl")
    assert e.clear_stop_doubt(99999)["error"]
    assert any(x.get("action") == "clear_stop_doubt" for x in db.events("manual"))
