"""E359: a broker stop whose placement call failed without a clear refusal may be live at Dhan with no TradeOrder row.
The order book is checked and the live order adopted (or nothing is sent) instead of placing a second stop."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import models
from execution import broker_stop as B
from execution import dhan_client
from test_group322_broker_stop import rig, pos, sync  # noqa: F401  (rig is a fixture)


def live(oid="X1", qty=10, sym="ABC", tag="BSTOP", status="TRIGGER_PENDING", otype="STOP_LOSS_MARKET", trig=97.5):
    return {"orderId": oid, "transactionType": "SELL", "orderType": otype, "orderStatus": status, "quantity": qty,
            "tradingSymbol": sym, "correlationId": tag, "triggerPrice": trig}


def test_lost_response_adopts_the_live_stop_and_sends_no_second(rig):
    db, st = rig
    st.place_error = TimeoutError("read timed out")
    st.book = [live()]
    p = pos(db)
    assert sync(db, [p])["placed"] == 1
    o = B.open_stop(db, "ABC")
    assert o is not None and o.dhan_order_id == "X1" and o.limit_price == 97.5 and o.exit_reason == "broker_stop"
    st.place_error = None
    assert sync(db, [p], now=1010.0)["kept"] == 1
    assert st.placed == []


def test_reply_without_order_id_is_recovered_the_same_way(rig, monkeypatch):
    db, st = rig
    monkeypatch.setattr(dhan_client, "place_order", lambda db_, **kw: {})
    st.book = [live("X2")]
    p = pos(db)
    assert sync(db, [p])["placed"] == 1
    assert B.open_stop(db, "ABC").dhan_order_id == "X2"


def test_unreadable_book_sends_nothing_and_adopts_on_a_later_pass(rig, monkeypatch):
    db, st = rig
    st.place_error = TimeoutError("read timed out")
    st.book = [live()]
    good = lambda db_: list(st.book)  # noqa: E731

    def bad(db_):
        raise RuntimeError("order book down")

    monkeypatch.setattr(dhan_client, "get_order_list", bad)
    p = pos(db)
    assert sync(db, [p])["failed"] == 1
    assert B.open_stop(db, "ABC") is None and B._unverified.get("ABC")
    monkeypatch.setattr(dhan_client, "get_order_list", good)
    st.place_error = None
    assert sync(db, [p], now=10_000_000.0)["placed"] == 1
    assert B.open_stop(db, "ABC").dhan_order_id == "X1"
    assert st.placed == [] and not B._unverified.get("ABC")


def test_no_stop_at_dhan_means_the_failure_was_real_and_next_pass_places(rig):
    db, st = rig
    st.place_error = TimeoutError("read timed out")
    st.book = []
    p = pos(db)
    assert sync(db, [p])["failed"] == 1
    assert B.open_stop(db, "ABC") is None and not B._unverified.get("ABC")
    st.place_error = None
    assert sync(db, [p], now=1010.0)["placed"] == 1 and len(st.placed) == 1


def test_clear_refusal_does_not_read_the_order_book(rig, monkeypatch):
    db, st = rig
    calls = []
    monkeypatch.setattr(dhan_client, "get_order_list", lambda db_: calls.append(1) or [live()])
    st.place_error = RuntimeError("RMS: Margin shortfall")
    assert sync(db, [pos(db)])["failed"] == 1
    assert calls == [] and B.open_stop(db, "ABC") is None


def test_only_our_open_matching_unbooked_stop_is_adopted(rig):
    db, st = rig
    p = pos(db)
    sec = "123"
    cases = [live("A1", tag="OTHER"), live("A2", qty=7), live("A3", sym="XYZ"), live("A4", status="CANCELLED"),
             live("A5", status="TRADED"), dict(live("A6"), transactionType="BUY"), live("A7", otype="LIMIT")]
    st.book = cases
    row, ok = B._find_live_stop(db, p, sec)
    assert row is None and ok is True
    db.add(models.TradeOrder(mode="REAL", symbol="ABC", side="SELL", order_type="STOP_LOSS", qty=10, status="CANCELLED",
                             dhan_order_id="K1", execution_source="AUTO", exit_reason=B.BROKER_STOP_REASON))
    db.commit()
    st.book = [live("K1")]
    assert B._find_live_stop(db, p, sec) == (None, True)          # already booked: not adopted again
    st.book = [live("K1"), live("N1", tag="")]
    row, ok = B._find_live_stop(db, p, sec)
    assert ok and row["orderId"] == "N1"                          # an empty tag still matches on symbol + qty + type


def test_failed_adoption_commit_keeps_the_flag_and_does_not_raise(rig, monkeypatch):
    db, st = rig
    st.place_error = TimeoutError("read timed out")
    st.book = [live()]
    p = pos(db)
    real_commit = db.commit
    n = {"c": 0}

    def flaky():
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("db down")
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky)
    assert sync(db, [p])["failed"] == 1
    assert B._unverified.get("ABC") and B.open_stop(db, "ABC") is None
    assert sync(db, [p], now=10_000_000.0)["placed"] == 1
    assert B.open_stop(db, "ABC").dhan_order_id == "X1" and st.placed == []
