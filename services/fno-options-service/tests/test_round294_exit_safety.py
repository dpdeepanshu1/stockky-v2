"""F&O round 294: an exit never waits for the entry locks; a stop that part-filled is booked once; no SELL beside a stop whose cancel is still in transit; a cut-short 5xx body cannot
escape place(); one failing position cannot stop the others. Fake broker / provider, no network."""
from __future__ import annotations

import http.client
import io
import urllib.error
from datetime import timedelta

import pytest

from fno import execution, paper, store as st
from fno.execution import OrderResult, OrderStatus
from test_engine_paper import NOW, FakeProvider
from test_real_partial import RB, entered, real, sells, stops


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


LATE = NOW.replace(hour=15, minute=20)          # after the 15:15 square-off


# ---- 1. exits do not wait for ARM / readiness / mode -----------------------------------------------------------------------------------------
def test_open_readiness_blocker_does_not_stop_the_squareoff(db):
    b = RB(); e = real(db, b); p = entered(db, b, e)
    e.cfg.FNO_REAL_REQUIRES_READINESS = True
    e.cfg.FNO_READINESS_BLOCKERS = ["reconcile: our REAL positions differ from Dhan's"]
    ok, _ = execution.real_allowed(e.cfg, e.armed_day(), "2026-11-10")
    assert not ok                                                    # the entry lock IS closed ...
    e.tick(LATE)
    assert sells(b) and db.get_position(p.id).status == paper.CLOSED  # ... and the position is closed all the same


def test_disarm_does_not_stop_the_squareoff_or_the_stop_out(db):
    b = RB(); e = real(db, b); p = entered(db, b, e)
    db.kv_set("fno_armed_day", None)
    e.tick(LATE)
    assert sells(b) and db.get_position(p.id).status == paper.CLOSED


def test_no_new_entry_when_not_armed_but_the_exit_still_goes(db):
    b = RB(); e = real(db, b); p = entered(db, b, e)
    db.kv_set("fno_armed_day", None)
    n_buys = len([o for o in b.sent if o["transactionType"] == "BUY"])
    e.tick(NOW + timedelta(minutes=30))
    assert len([o for o in b.sent if o["transactionType"] == "BUY"]) == n_buys    # real_allowed still gates every BUY


def test_exit_without_credentials_is_refused_and_says_so(db):
    b = RB(); e = real(db, b); p = entered(db, b, e)
    e.cfg.DHAN_CLIENT_ID = ""
    e.tick(LATE)
    assert not sells(b) and db.get_position(p.id).status == paper.OPEN
    assert any(x["kind"] == "real_blocked" for x in db.events())


# ---- 2. a stop that part-filled then ended -----------------------------------------------------------------------------------------------------
def test_stop_part_filled_then_cancelled_is_booked_once_and_replaced_for_the_rest(db):
    b = RB(); e = real(db, b); p = entered(db, b, e)
    lots0, lot = p.lots, p.lot_size
    sl = p.broker_order_ids["sl"]
    b.states[sl] = OrderStatus("CANCELLED", 90.0, lot)               # one lot sold by the stop, then it ended
    e.check_broker_stops(NOW + timedelta(seconds=10), e.params())
    kids = [x for x in db.positions(mode="REAL", day="2026-11-10", limit=50) if x.parent_id == p.id]
    assert len(kids) == 1 and kids[0].lots == 1 and kids[0].exit_reason == paper.SL and kids[0].exit_premium == 90.0
    left = db.get_position(p.id)
    assert left.status == paper.OPEN and left.lots == lots0 - 1 and "sl" not in left.broker_order_ids
    e.check_broker_stops(NOW + timedelta(seconds=20), e.params())    # the next cycle places the new stop for what is LEFT
    assert [o["quantity"] for o in stops(b)][-1] == (lots0 - 1) * lot
    e.check_broker_stops(NOW + timedelta(seconds=30), e.params())
    assert len([x for x in db.positions(mode="REAL", day="2026-11-10", limit=50) if x.parent_id == p.id]) == 1   # nothing booked twice


def test_stop_that_filled_everything_and_then_shows_cancelled_closes_the_position(db):
    b = RB(); e = real(db, b); p = entered(db, b, e)
    b.states[p.broker_order_ids["sl"]] = OrderStatus("CANCELLED", 88.0, p.qty)
    done = e.check_broker_stops(NOW + timedelta(seconds=10), e.params())
    assert [x.id for x in done] == [p.id] and db.get_position(p.id).status == paper.CLOSED
    assert not sells(b) and len(stops(b)) == 1                       # no second stop on a flat position


def test_a_partly_filled_stop_read_again_in_the_exit_path_is_not_booked_twice(db):
    class PartThenWait(RB):
        def cancel(self, oid):
            self.cancelled.append(oid)
            return OrderResult(True, order_id=oid)                   # cancel stays in transit
    b = PartThenWait(); e = real(db, b); p = entered(db, b, e)
    b.states[p.broker_order_ids["sl"]] = OrderStatus("PENDING", 90.0, p.lot_size)
    for i in range(3):
        e._exit(db.get_position(p.id), paper.SQUAREOFF, None, e.params(), NOW + timedelta(seconds=10 * (i + 1)))
    kids = [x for x in db.positions(mode="REAL", day="2026-11-10", limit=50) if x.parent_id == p.id]
    assert len(kids) == 1 and not sells(b)                           # booked once, and still no SELL beside the live stop


# ---- 3. no SELL beside a stop whose cancel has not landed ----------------------------------------------------------------------------------
class StuckCancel(RB):
    landed = False

    def cancel(self, oid):
        self.cancelled.append(oid)
        if self.landed:
            return super().cancel(oid)
        return OrderResult(True, order_id=oid)


def test_no_sell_while_the_stop_is_still_pending_after_the_cancel(db):
    b = StuckCancel(); e = real(db, b); p = entered(db, b, e)
    e._exit(p, paper.SQUAREOFF, None, e.params(), NOW + timedelta(seconds=10))
    assert not sells(b) and db.get_position(p.id).status == paper.OPEN
    assert p.broker_order_ids.get("sl")                              # the stop is still ours, not forgotten
    b.landed = True                                                  # the cancel lands: the next cycle sells
    e._exit(db.get_position(p.id), paper.SQUAREOFF, None, e.params(), NOW + timedelta(seconds=20))
    assert sells(b) and db.get_position(p.id).status == paper.CLOSED


def test_the_cancel_landing_during_the_retries_lets_the_sell_go_at_once(db):
    class Slow(RB):
        reads = 0

        def cancel(self, oid):
            self.cancelled.append(oid)
            return OrderResult(True, order_id=oid)

        def order_status(self, oid):
            s = super().order_status(oid)
            if oid in self.cancelled and s is not None and s.state == "PENDING":
                self.reads += 1
                if self.reads >= 3:
                    self.states[oid] = OrderStatus("CANCELLED", 0.0, 0)
            return s
    b = Slow(); e = real(db, b); p = entered(db, b, e)
    e._exit(p, paper.SQUAREOFF, None, e.params(), NOW + timedelta(seconds=10))
    assert sells(b) and db.get_position(p.id).status == paper.CLOSED


# ---- 4. place(): a cut-short error body --------------------------------------------------------------------------------------------------------
class _CutBody(io.BytesIO):
    def read(self, *a):
        raise http.client.IncompleteRead(b"{", 50)


def _broker(exc):
    def opener(req, timeout=None):
        raise exc(req)
    return execution.DhanBroker("https://x", "1", "t", opener=opener)


def test_a_502_with_a_cut_body_is_reported_as_maybe_sent_not_raised():
    b = _broker(lambda r: urllib.error.HTTPError(r.full_url, 502, "Bad Gateway", {}, _CutBody()))
    r = b.place(execution.build_order("1", 111, "NSE_FNO", "SELL", 65, 100.0))
    assert r.ok is False and r.status == "HTTP 502" and execution.maybe_sent(r)


def test_a_400_with_a_cut_body_is_a_plain_refusal():
    b = _broker(lambda r: urllib.error.HTTPError(r.full_url, 400, "Bad Request", {}, _CutBody()))
    r = b.place(execution.build_order("1", 111, "NSE_FNO", "SELL", 65, 100.0))
    assert r.status == "HTTP 400" and not execution.maybe_sent(r)


def test_an_answer_that_is_not_a_json_object_counts_as_no_order_id():
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    b = execution.DhanBroker("https://x", "1", "t", opener=lambda req, timeout=None: R(b'["orderId"]'))
    r = b.place(execution.build_order("1", 111, "NSE_FNO", "SELL", 65, 100.0))
    assert r.status == "NO_ORDER_ID" and execution.maybe_sent(r)


# ---- 5. one failing position must not stop the others ------------------------------------------------------------------------------------------
def _two_demo_positions(db, e):
    from test_engine_paper import C
    from fno.models import ContractRef
    a = paper.open_position(C, 1, 100.0, NOW.timestamp(), e.params())
    s = paper.open_position(ContractRef("SENSEX", "2026-11-17", 82000.0, "CE", 20, 222, "BSE_FNO", 0.05), 1, 100.0, NOW.timestamp(), e.params())
    db.insert_position(a); db.insert_position(s)
    return a, s


def _engine_whose_exit_raises_for_nifty(db):
    from fno import engine as eng
    e = eng.Engine(db, FakeProvider(), real.__globals__["make_cfg"](FNO_UNDERLYINGS="BOTH"))
    orig = e._exit

    def boom(pos, *a, **k):
        if pos.underlying == "NIFTY":
            raise RuntimeError("exit broke for NIFTY")
        return orig(pos, *a, **k)
    e._exit = boom
    return e


def test_manage_open_keeps_managing_the_other_position_when_one_raises(db):
    e = _engine_whose_exit_raises_for_nifty(db)
    a, s = _two_demo_positions(db, e)
    closed = e.manage_open(LATE, e.params(), {}, {})
    assert [x.id for x in closed] == [s.id] and db.get_position(a.id).status == paper.OPEN
    assert any(x["kind"] == "position_error" for x in db.events())


def test_squareoff_all_keeps_going_when_one_position_raises(db):
    e = _engine_whose_exit_raises_for_nifty(db)
    a, s = _two_demo_positions(db, e)
    assert e.squareoff_all(now=LATE) == [s.id]


def test_a_bad_exit_payload_is_noted_not_raised(db):
    b = RB(); e = real(db, b); p = entered(db, b, e)
    p.security_id = None                                             # build_order refuses a missing security id
    db.update_position(p)
    e._exit(db.get_position(p.id), paper.SQUAREOFF, None, e.params(), NOW + timedelta(seconds=10))
    assert db.get_position(p.id).status == paper.OPEN
    assert any(x["kind"] == "order_failed" for x in db.events())
