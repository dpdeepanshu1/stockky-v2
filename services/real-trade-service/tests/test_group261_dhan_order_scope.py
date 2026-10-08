"""group 261: /dhan/orders tags which Dhan orders this service placed (shared account with position-stocks)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dhan_order_scope as S


def test_order_id_reads_either_spelling_and_strips():
    assert S.order_id_of({"orderId": " 123 "}) == "123"
    assert S.order_id_of({"order_id": 77}) == "77"
    assert S.order_id_of({}) == ""
    assert S.order_id_of("junk") == ""


def test_tag_marks_ours_and_not_ours_without_mutating_input():
    orders = [{"orderId": "1", "tradingSymbol": "AURIONPRO"}, {"orderId": "2", "tradingSymbol": "TRIVENI"}]
    out = S.tag_orders_ours(orders, {"1"})
    assert [o["ours"] for o in out] == [True, False]
    assert "ours" not in orders[0]


def test_order_without_an_id_is_not_ours():
    assert S.tag_orders_ours([{"tradingSymbol": "X"}], {"1"})[0]["ours"] is False


def test_failed_lookup_leaves_orders_untagged():
    out = S.tag_orders_ours([{"orderId": "1"}], None)
    assert out == [{"orderId": "1"}] and "ours" not in out[0]


def test_non_dict_rows_pass_through_and_empty_input_is_ok():
    assert S.tag_orders_ours(["x"], {"1"}) == ["x"]
    assert S.tag_orders_ours(None, {"1"}) == []


def test_tag_dhan_orders_never_raises_when_db_breaks():
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    orders = [{"orderId": "1"}]
    assert S.tag_dhan_orders(Boom(), orders) == orders


def test_tag_dhan_orders_marks_via_db_lookup(monkeypatch):
    monkeypatch.setattr(S, "known_dhan_order_ids", lambda db, ids, mode="REAL": {"1"})
    out = S.tag_dhan_orders(object(), [{"orderId": "1"}, {"orderId": "9"}])
    assert [o["ours"] for o in out] == [True, False]
