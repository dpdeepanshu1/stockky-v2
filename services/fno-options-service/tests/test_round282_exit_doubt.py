"""Round 282: a failed exit SELL (network, HTTP 5xx, 2xx without an order id) may be live at Dhan: no second SELL, no new stop on top of it; Dhan's positions decide.
Before this, the position stayed OPEN and the next cycle sent another SELL: if both had filled, the second opened a short. Nothing here talks to Dhan."""
from __future__ import annotations

import json
from datetime import timedelta

from fno import execution, paper
from fno.execution import DhanBroker, OrderResult
from test_engine_paper import NOW
from test_real_partial import RB, db, real, entered, sells, stops  # noqa: F401  (db is a fixture)


class SellFails(RB):
    """Every LIMIT SELL call fails with `status` (the order may have reached Dhan: `reaches`); everything else behaves like RB."""
    def __init__(self, status="NETWORK", reaches=False, net=None):
        super().__init__(net=net)
        self.status, self.reaches, self.fail = status, reaches, False

    def place(self, payload):
        if self.fail and payload["transactionType"] == "SELL" and payload["orderType"] == "LIMIT":
            self.sent.append(payload)
            return OrderResult(False, status=self.status, message="boom")
        return super().place(payload)


def _held(p):
    return {str(p.security_id): p.qty}


def _doubt_events(db):
    return [x for x in db.events("reconcile") if "may have reached Dhan" in x.get("msg", "") or "may have reached Dhan" in x.get("msg", "")]


# ---- the classifier -------------------------------------------------------------------------------------------------------------
def test_maybe_sent_covers_network_5xx_408_and_no_order_id_but_not_a_plain_refusal():
    f = lambda s: execution.maybe_sent(OrderResult(False, status=s))   # noqa: E731
    assert f("NETWORK") and f("HTTP 500") and f("HTTP 502") and f("HTTP 504") and f("HTTP 408") and f("NO_ORDER_ID")
    assert not f("HTTP 400") and not f("HTTP 401") and not f("HTTP 429") and not f("TRANSIT") and not f("") and not f("HTTP x")
    assert not execution.maybe_sent(OrderResult(True, order_id="1", status="HTTP 500")) and not execution.maybe_sent(None)


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def test_a_2xx_without_an_order_id_and_without_error_text_is_in_doubt_but_with_error_text_it_is_a_refusal():
    b = DhanBroker("https://x/v2", "1", "t", opener=lambda req, timeout=0: _Resp({}))
    r = b.place({"a": 1})
    assert (r.ok, r.status) == (False, "NO_ORDER_ID") and execution.maybe_sent(r)
    b = DhanBroker("https://x/v2", "1", "t", opener=lambda req, timeout=0: _Resp({"errorCode": "DH-906", "errorMessage": "bad"}))
    r = b.place({"a": 1})
    assert r.ok is False and r.status != "NO_ORDER_ID" and not execution.maybe_sent(r)
    b = DhanBroker("https://x/v2", "1", "t", opener=lambda req, timeout=0: _Resp({"orderId": "9", "orderStatus": "TRANSIT"}))
    assert b.place({"a": 1}).ok is True


# ---- the engine -----------------------------------------------------------------------------------------------------------------
def test_a_failed_sell_marks_the_position_and_sends_no_stop_and_no_second_sell(db):
    b = SellFails()
    e = real(db, b)
    p = entered(db, b, e)
    n_stops = len(stops(b))
    b.fail = True
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    q = db.get_position(p.id)
    assert q.status == "OPEN" and q.broker_order_ids["exit_doubt"] and q.broker_order_ids["exit_doubt_reason"] == paper.MANUAL
    assert len(sells(b)) == 1 and len(stops(b)) == n_stops                      # one SELL attempt, and the cancelled stop was NOT put back
    b.net = _held(q)
    for k in (60, 90, 120):                                                     # later cycles: Dhan still shows the quantity -> it may be resting: still no second SELL
        e.squareoff_all(now=NOW + timedelta(seconds=k))
    assert len(sells(b)) == 1 and len(stops(b)) == n_stops and db.get_position(p.id).status == "OPEN"
    assert any("resting at Dhan" in x.get("msg", "") for x in db.events("reconcile"))


def test_an_http_5xx_on_the_sell_is_treated_like_a_network_failure(db):
    b = SellFails(status="HTTP 504")
    e = real(db, b)
    p = entered(db, b, e)
    b.fail = True
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    e.squareoff_all(now=NOW + timedelta(seconds=60))
    assert len(sells(b)) == 1 and db.get_position(p.id).broker_order_ids.get("exit_doubt")
    assert _doubt_events(db)


def test_a_plain_4xx_refusal_of_the_sell_still_puts_the_stop_back_and_retries(db):
    b = SellFails(status="HTTP 400")
    e = real(db, b)
    p = entered(db, b, e)
    n_stops = len(stops(b))
    b.fail = True
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    q = db.get_position(p.id)
    assert "exit_doubt" not in q.broker_order_ids and len(stops(b)) == n_stops + 1
    e.squareoff_all(now=NOW + timedelta(seconds=60))
    assert len(sells(b)) == 2


def test_when_dhan_shows_the_contract_flat_the_position_is_booked_as_sold(db):
    b = SellFails(reaches=True)
    e = real(db, b)
    p = entered(db, b, e)
    b.fail = True
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    limit = sells(b)[0]["price"]
    b.net = {}                                                                  # Dhan shows nothing held: the SELL did go through
    e.tick(NOW + timedelta(seconds=90))                                         # a normal cycle resolves it, whatever the premium does
    q = db.get_position(p.id)
    assert q.status == "CLOSED" and q.exit_reason == paper.MANUAL and abs(q.exit_premium - limit) < 1e-9
    assert "exit_doubt" not in q.broker_order_ids and "Dhan shows the contract flat" in q.note
    assert len(sells(b)) == 1


def test_unreadable_positions_keep_waiting_and_send_nothing(db):
    b = SellFails()
    e = real(db, b)
    p = entered(db, b, e)
    b.fail = True
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    b.net = None
    e.squareoff_all(now=NOW + timedelta(seconds=60))
    e.tick(NOW + timedelta(seconds=90))
    assert len(sells(b)) == 1 and db.get_position(p.id).status == "OPEN"


def test_clearing_the_mark_lets_the_engine_sell_again_and_is_logged(db):
    b = SellFails()
    e = real(db, b)
    p = entered(db, b, e)
    b.fail = True
    e.squareoff_all(now=NOW + timedelta(seconds=30))
    assert e.clear_exit_doubt(p.id) == {"position": p.id, "cleared": True}
    assert any(x.get("action") == "clear_exit_doubt" for x in db.events("manual"))
    b.fail = False
    b.net = _held(db.get_position(p.id))
    e.squareoff_all(now=NOW + timedelta(seconds=60))
    assert len(sells(b)) == 2 and db.get_position(p.id).status == "CLOSED"
    assert e.clear_exit_doubt(99999).get("error")


def test_an_entry_buy_failing_with_a_5xx_is_flagged_as_possibly_sent(db):
    class Gw(RB):
        def place(self, payload):
            self.sent.append(payload)
            return OrderResult(False, status="HTTP 502", message="bad gateway")
    b = Gw(net={})
    e = real(db, b)
    e.tick(NOW)
    ps = db.open_positions("REAL")                                   # round 293: kept as a flagged unknown entry until the correlationId lookup answers
    assert len(ps) == 1 and ps[0].broker_order_ids["entry_state"] == "UNKNOWN" and _doubt_events(db)


# ---- the manual probe ------------------------------------------------------------------------------------------------------------
from test_round261 import FakeBroker, go  # noqa: E402


class GwBroker(FakeBroker):
    """The n-th place() call (1-based) answers with an HTTP 5xx failure."""
    def __init__(self, script, fail_at, status="HTTP 504"):
        super().__init__(script)
        self.fail_at, self.status, self.calls = fail_at, status, 0

    def place(self, payload):
        self.calls += 1
        if self.calls == self.fail_at:
            self.placed.append(payload)
            return execution.OrderResult(False, status=self.status, message="gateway timeout")
        return super().place(payload)


def test_the_probe_treats_a_5xx_on_the_buy_as_state_in_doubt():
    b = GwBroker([], fail_at=1)
    r = go(b)
    text = "\n".join(r["lines"])
    assert r["exit_code"] == 4 and len(b.placed) == 1 and "MAY have reached Dhan" in text and "Nothing is open" not in text and "HTTP 504" in text


def test_the_probe_sends_no_second_sell_after_a_5xx_on_the_sell():
    b = GwBroker(["fill"], fail_at=2, status="HTTP 502")
    r = go(b)
    assert r["exit_code"] == 4 and [p["transactionType"] for p in b.placed] == ["BUY", "SELL"] and r["bought"] == 65 and r["sold"] == 0


def test_the_probe_still_retries_after_a_4xx_refusal_of_the_sell():
    b = GwBroker(["fill", "fill"], fail_at=2, status="HTTP 400")
    r = go(b)
    assert r["exit_code"] == 0 and [p["transactionType"] for p in b.placed] == ["BUY", "SELL", "SELL"]
