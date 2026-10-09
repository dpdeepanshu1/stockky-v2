"""Group 280: execution/order_ws - read-only Dhan order-update listener (parse, store, reconnect, routes)."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from execution import order_ws

SAMPLE = {"Type": "order_alert", "Data": {
    "OrderNo": "112111182198", "ExchOrderNo": "X1", "Status": "Traded", "TradedQty": "10", "AvgTradedPrice": "107.17",
    "TradedPrice": 107.17, "RemainingQuantity": 0, "Symbol": "aaatech", "TxnType": "B", "ProductName": "INTRADAY",
    "LegNo": 1, "CorrelationId": "ps1", "ReasonDescription": "", "Quantity": 10}}


def test_parse_normalises_a_real_shaped_message():
    ev = order_ws.parse_message(json.dumps(SAMPLE))
    assert ev["order_id"] == "112111182198" and ev["status"] == "TRADED" and ev["symbol"] == "AAATECH"
    assert ev["txn"] == "B" and ev["traded_qty"] == 10 and ev["avg_traded_price"] == 107.17
    assert ev["remaining_qty"] == 0 and ev["leg_no"] == 1 and ev["reason"] is None and ev["product"] == "INTRADAY"


def test_parse_accepts_bytes_and_dicts_and_rejects_the_rest():
    assert order_ws.parse_message(json.dumps(SAMPLE).encode())["order_id"] == "112111182198"
    assert order_ws.parse_message(SAMPLE)["status"] == "TRADED"
    for bad in ("not json", "[]", "{}", json.dumps({"Data": {}}), json.dumps({"Data": {"OrderNo": ""}}),
                json.dumps({"Data": "x"}), None, 5):
        assert order_ws.parse_message(bad) is None


def test_parse_keeps_a_rejection_reason_and_bad_numbers_become_none():
    ev = order_ws.parse_message({"Data": {"OrderNo": "9", "Status": "REJECTED", "ReasonDescription": "RMS: no funds",
                                          "TradedQty": "abc", "AvgTradedPrice": "nan"}})
    assert ev["status"] == "REJECTED" and ev["reason"] == "RMS: no funds"
    assert ev["traded_qty"] is None and ev["avg_traded_price"] is None


def test_store_latest_history_and_bound():
    st = order_ws.OrderEventStore(max_orders=3)
    for s, q in (("TRANSIT", 0), ("PENDING", 0), ("TRADED", 10)):
        st.add(order_ws.parse_message({"Data": {"OrderNo": "1", "Status": s, "TradedQty": q}}))
    assert st.latest("1")["status"] == "TRADED" and [h["status"] for h in st.history("1")] == ["TRANSIT", "PENDING", "TRADED"]
    for i in range(2, 6):
        st.add(order_ws.parse_message({"Data": {"OrderNo": str(i), "Status": "PENDING"}}))
    assert len(st) == 3 and st.latest("1") is None and st.latest("5") is not None
    assert st.latest("nope") is None and st.history("nope") == []
    assert [e["order_id"] for e in st.recent(2)] == ["5", "4"]


def test_login_message_shape():
    m = json.loads(order_ws.login_message(1000000001, "JWT.x"))
    assert m == {"LoginReq": {"MsgCode": 42, "ClientId": "1000000001", "Token": "JWT.x"}, "UserType": "SELF"}


class FakeWS:
    def __init__(self, msgs, then=None):
        self.sent, self.msgs, self.then, self.closed = [], list(msgs), then, False

    async def send(self, m):
        self.sent.append(m)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self._it()

    async def _it(self):
        for m in self.msgs:
            yield m
        if self.then:
            raise self.then


def run(coro):
    return asyncio.run(coro)


def test_listener_logs_in_stores_events_and_reconnects():
    st = order_ws.OrderEventStore()
    sockets = [FakeWS([json.dumps(SAMPLE), "garbage"], then=ConnectionError("dropped")),
               FakeWS([json.dumps({"Data": {"OrderNo": "2", "Status": "PENDING"}})])]
    seen = []

    async def connect(url):
        seen.append(url)
        if not sockets:
            lst.stop()
            raise ConnectionError("done")
        return sockets.pop(0)
    lst = order_ws.OrderUpdateListener(lambda: ("1000000001", "SECRET-TOKEN"), connect=connect, event_store=st,
                                      backoff_min=0.0, backoff_max=0.0)
    run(lst.run())
    assert seen[0] == order_ws.WS_URL and len(seen) == 3
    assert st.latest("112111182198")["status"] == "TRADED" and st.latest("2")["status"] == "PENDING"
    s = lst.status()
    assert s["connects"] == 2 and s["messages"] == 3 and s["order_events"] == 2 and s["connected"] is False
    assert s["orders_tracked"] == 2


def test_login_is_sent_first_and_the_token_never_reaches_logs_or_status(caplog):
    ws = FakeWS([])
    lst_holder = {}

    async def connect(url):
        if lst_holder["l"].state["connects"] >= 1:
            lst_holder["l"].stop()
            raise RuntimeError("boom")
        return ws
    lst = order_ws.OrderUpdateListener(lambda: ("42", "SECRET-TOKEN"), connect=connect,
                                      event_store=order_ws.OrderEventStore(), backoff_min=0.0, backoff_max=0.0)
    lst_holder["l"] = lst
    with caplog.at_level(logging.DEBUG):
        run(lst.run())
    assert json.loads(ws.sent[0])["LoginReq"]["Token"] == "SECRET-TOKEN" and ws.closed
    assert "SECRET-TOKEN" not in caplog.text and "SECRET-TOKEN" not in json.dumps(lst.status())


def test_no_credentials_does_not_connect_and_is_reported():
    calls = []

    async def connect(url):
        calls.append(url)

    holder = {}
    n = {"i": 0}

    def creds():
        n["i"] += 1
        if n["i"] >= 3:
            holder["l"].stop()
        return None
    lst = order_ws.OrderUpdateListener(creds, connect=connect, event_store=order_ws.OrderEventStore(),
                                      backoff_min=0.0, backoff_max=0.0)
    holder["l"] = lst
    run(lst.run())
    assert calls == [] and lst.state["no_credentials"] >= 2 and "no Dhan credentials" in lst.state["last_error"]


def test_a_connect_error_is_recorded_and_backed_off():
    holder = {}

    async def connect(url):
        holder["l"].stop()
        raise OSError("refused")
    lst = order_ws.OrderUpdateListener(lambda: ("1", "t"), connect=connect, event_store=order_ws.OrderEventStore(),
                                      backoff_min=0.0, backoff_max=0.0)
    holder["l"] = lst
    run(lst.run())
    assert lst.state["last_error"].startswith("OSError") and lst.state["connected"] is False


def test_cancel_propagates():
    async def connect(url):
        await asyncio.sleep(10)

    async def go():
        lst = order_ws.OrderUpdateListener(lambda: ("1", "t"), connect=connect, event_store=order_ws.OrderEventStore())
        t = asyncio.create_task(lst.run())
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    run(go())
