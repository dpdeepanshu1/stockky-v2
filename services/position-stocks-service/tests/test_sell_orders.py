"""E243: orders/sell_orders.py - the read-only view behind the SELL orders card (no database needed: rows are plain objects)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orders import sell_orders

NTPC_ROW = {"orderId": "34326102815775", "orderStatus": "PENDING", "transactionType": "SELL", "productType": "INTRADAY", "orderType": "LIMIT", "quantity": 1, "filledQty": 0,
            "price": 300.4, "legDetails": [{"legName": "STOP_LOSS_LEG", "orderStatus": "PENDING", "price": 304.9}, {"legName": "TARGET_LEG", "orderStatus": "PENDING", "price": 291.4}]}


def test_state_words():
    assert sell_orders.state_of(NTPC_ROW) == "WAITING"                                    # resting, nothing filled: the exact NTPC row
    assert sell_orders.state_of({**NTPC_ROW, "orderStatus": "CANCELLED"}) == "CANCELLED"
    assert sell_orders.state_of({**NTPC_ROW, "filledQty": 1}) == "FILLED"
    assert sell_orders.state_of({**NTPC_ROW, "quantity": 3, "filledQty": 1}) == "PARTIAL"
    assert sell_orders.state_of({**NTPC_ROW, "orderStatus": "REJECTED"}) == "REJECTED"
    assert sell_orders.state_of(None) == "UNKNOWN"


def test_probe_that_never_filled_is_listed_with_dhans_row_attached():
    hist = [{"at": "2026-10-28T04:44:00+00:00", "symbol": "NTPC", "order_id": "34326102815775", "verdict": "FAIL", "entry_order_type": "LIMIT", "entry_price_sent": 299.5, "ref_price": 300.4}]
    out = sell_orders.build(None, super_orders=[NTPC_ROW], super_error=None, probe_history=hist, short_rows=[])
    assert len(out["probes"]) == 1 and out["probes"][0]["broker"]["state"] == "WAITING" and out["probes"][0]["broker"]["order_type"] == "LIMIT"
    s = out["summary"]
    assert (s["probe_runs"], s["shorts_open"], s["sell_orders_at_dhan"], s["sell_orders_waiting"], s["sell_orders_untracked"]) == (1, 0, 1, 1, 0)


def test_only_our_sell_orders_are_shown_not_other_rows_in_the_book():
    other = {**NTPC_ROW, "orderId": "777"}
    long_row = {**NTPC_ROW, "orderId": "888", "transactionType": "BUY"}
    hist = [{"order_id": "34326102815775", "symbol": "NTPC", "verdict": "FAIL"}, {"order_id": "888", "symbol": "X", "verdict": "PASS"}]
    out = sell_orders.build(None, super_orders=[NTPC_ROW, other, long_row], super_error=None, probe_history=hist, short_rows=[])
    assert [b["order_id"] for b in out["broker_rows"]] == ["34326102815775", "777"]          # E244: every SELL in the book is listed, the BUY row is not
    assert [b["tracked"] for b in out["broker_rows"]] == [True, False] and out["summary"]["sell_orders_untracked"] == 1


def test_live_short_rows_carry_their_levels_and_broker_state():
    row = NS(id=5, symbol="SBIN", status="OPEN", quantity=2, entry_price=800.0, stop_price=808.0, target_price=780.0, exit_price=None, dhan_super_order_id="34326102815775",
             plain_stop_order_id=None, opened_at=None)
    out = sell_orders.build(None, super_orders=[NTPC_ROW], super_error=None, probe_history=[], short_rows=[row])
    assert out["shorts"][0]["symbol"] == "SBIN" and out["shorts"][0]["broker"]["state"] == "WAITING" and out["summary"]["shorts_open"] == 1


def test_unreadable_dhan_book_is_reported_not_hidden():
    out = sell_orders.build(None, super_orders=None, super_error="RuntimeError: dhan 502", probe_history=[{"order_id": "1", "symbol": "ITC"}], short_rows=[])
    assert out["errors"] and "502" in out["errors"][0] and out["probes"][0]["broker"] is None


def test_a_short_dhan_holds_that_stockky_never_recorded_is_listed_as_untracked():
    """The 3-share ITC short from the first probe run: open at Dhan, no row here, so it must not be invisible."""
    pos = [{"securityId": "1594", "tradingSymbol": "ITC", "netQty": -3, "sellAvg": 254.03, "productType": "INTRADAY"}, {"securityId": "5", "tradingSymbol": "ABC", "netQty": 4}]
    out = sell_orders.build(None, super_orders=[], super_error=None, probe_history=[], short_rows=[], positions=pos)
    assert [(p["symbol"], p["net_qty"], p["tracked"]) for p in out["dhan_shorts"]] == [("ITC", -3, False)]
    assert out["summary"]["dhan_shorts_open"] == 1 and out["summary"]["dhan_shorts_untracked"] == 1


def test_a_short_stockky_owns_is_tracked_and_unreadable_positions_are_reported():
    row = NS(id=5, symbol="ITC", status="OPEN", quantity=3, entry_price=254.0, stop_price=258.0, target_price=246.0, exit_price=None, dhan_super_order_id=None,
             dhan_security_id="1594", plain_stop_order_id=None, opened_at=None)
    out = sell_orders.build(None, super_orders=[], super_error=None, probe_history=[], short_rows=[row], positions=[{"securityId": "1594", "tradingSymbol": "ITC", "netQty": -3}])
    assert out["dhan_shorts"][0]["tracked"] is True and out["summary"]["dhan_shorts_untracked"] == 0
    bad = sell_orders.build(None, super_orders=[], super_error=None, probe_history=[], short_rows=[], positions=None, positions_error="RuntimeError: 502")
    assert "positions could not be read" in bad["errors"][0]
