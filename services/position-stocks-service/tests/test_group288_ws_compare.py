"""Group 288: read-only comparison of order-update WebSocket events with Dhan's order book."""
from __future__ import annotations

import time

import pytest

import config
from execution import dhan_client, order_ws
from test_main import client, db, m  # noqa: F401


def _ev(oid, status="TRADED", txn="B", qty=10, avg=100.0, symbol="ABC"):
    return {"order_id": oid, "status": status, "symbol": symbol, "txn": txn, "traded_qty": qty,
            "avg_traded_price": avg, "traded_price": avg, "received_at": time.time()}


def _row(oid, status="TRADED", side="BUY", qty=10, avg=100.0, sym="ABC-EQ"):
    return {"orderId": oid, "orderStatus": status, "transactionType": side, "filledQty": qty,
            "averageTradedPrice": avg, "tradingSymbol": sym}


@pytest.fixture()
def st():
    return order_ws.OrderEventStore()


class TestCompare:
    def test_all_match_is_safe(self, st):
        for i in "123":
            st.add(_ev(i))
        r = order_ws.compare_with_order_book([_row(i) for i in "123"], st)
        assert r["compared"] == 3 and r["matched"] == 3 and r["safe_to_enable"] is True

    def test_too_few_orders_is_not_safe(self, st):
        st.add(_ev("1"))
        r = order_ws.compare_with_order_book([_row("1")], st)
        assert r["matched"] == 1 and r["safe_to_enable"] is False and "not enough" in r["verdict"]

    @pytest.mark.parametrize("ev_kw,field", [({"status": "PENDING"}, "status"), ({"txn": "S"}, "side"),
                                             ({"qty": 7}, "traded_qty"), ({"avg": 101.0}, "avg_price"),
                                             ({"symbol": "XYZ"}, "symbol")])
    def test_each_field_mismatch_is_reported(self, st, ev_kw, field):
        st.add(_ev("1", **ev_kw))
        st.add(_ev("2")); st.add(_ev("3"))
        r = order_ws.compare_with_order_book([_row("1"), _row("2"), _row("3")], st)
        assert r["safe_to_enable"] is False
        assert r["mismatched"][0]["order_id"] == "1" and field in r["mismatched"][0]["diffs"]
        assert "DISAGREE" in r["verdict"]

    def test_price_within_a_paisa_matches(self, st):
        st.add(_ev("1", avg=100.005))
        assert order_ws.compare_with_order_book([_row("1")], st, min_orders=1)["safe_to_enable"] is True

    def test_missing_in_ws_is_counted_not_a_mismatch(self, st):
        r = order_ws.compare_with_order_book([_row("9"), {"x": 1}], st)
        assert r["missing_in_ws"] == 1 and r["compared"] == 0 and r["mismatched"] == []

    def test_unfilled_book_row_skips_price_check(self, st):
        st.add(_ev("1", status="PENDING", qty=0, avg=None))
        r = order_ws.compare_with_order_book([_row("1", status="PENDING", qty=0, avg=None)], st, min_orders=1)
        assert r["matched"] == 1

    def test_never_raises(self, st):
        r = order_ws.compare_with_order_book([object()], st)
        assert r["safe_to_enable"] is False and "error" in r


class TestRoute:
    def test_listener_off(self, client, monkeypatch):
        monkeypatch.setattr(config, "DHAN_ORDER_WS_ENABLED", False)
        r = client.get("/orders/ws-compare").json()
        assert r["available"] is False

    def test_book_fetch_failure(self, client, monkeypatch):
        import main
        monkeypatch.setattr(config, "DHAN_ORDER_WS_ENABLED", True)
        monkeypatch.setattr(main, "_order_ws_listener", order_ws.OrderUpdateListener(lambda: None))
        monkeypatch.setattr(dhan_client, "get_order_list", lambda db: (_ for _ in ()).throw(RuntimeError("boom")))
        r = client.get("/orders/ws-compare").json()
        assert r["available"] is False and "boom" in r["reason"]

    def test_compares_when_running(self, client, monkeypatch):
        import main
        s = order_ws.OrderEventStore()
        for i in "123":
            s.add(_ev(i))
        monkeypatch.setattr(order_ws, "store", s)
        monkeypatch.setattr(config, "DHAN_ORDER_WS_ENABLED", True)
        monkeypatch.setattr(main, "_order_ws_listener", order_ws.OrderUpdateListener(lambda: None))
        monkeypatch.setattr(dhan_client, "get_order_list", lambda db: [_row(i) for i in "123"])
        r = client.get("/orders/ws-compare").json()
        assert r["available"] is True and r["book_orders"] == 3 and r["safe_to_enable"] is True
