"""Group 208 (item 13): normalised broker positions."""
from __future__ import annotations

import pytest

from portfolio import broker_view as bv


def test_closed_position_is_not_open_and_has_no_ltp_or_unrealised():
    r = bv.normalize([{"tradingSymbol": "KRBL", "netQty": 0, "buyAvg": 300.0, "realizedProfit": 12.5}],
                     {"KRBL": 310.0})[0]
    assert r["is_open"] is False and r["ltp"] is None and r["unrealized_pnl"] is None
    assert r["realized_pnl"] == 12.5 and r["cost_value"] is None


def test_open_long_pnl_is_computed_from_ltp_not_the_cost_value():
    r = bv.normalize([{"tradingSymbol": "ABSLAMC", "netQty": 5, "buyAvg": 405.4, "dayBuyValue": 2027.0,
                       "unrealizedProfit": 0}], {"ABSLAMC": 410.0})[0]
    assert r["ltp"] == 410.0 and r["unrealized_pnl"] == pytest.approx(23.0)
    assert r["cost_value"] == pytest.approx(2027.0) and r["unrealized_pnl"] != r["cost_value"]


def test_missing_ltp_gives_none_never_zero_or_cost():
    r = bv.normalize([{"tradingSymbol": "ABSLAMC", "netQty": 5, "buyAvg": 405.4, "dayBuyValue": 2027.0}])[0]
    assert r["ltp"] is None and r["unrealized_pnl"] is None


def test_zero_or_negative_ltp_is_treated_as_missing():
    r = bv.normalize([{"tradingSymbol": "X", "netQty": 1, "buyAvg": 10.0}], {"X": 0.0})[0]
    assert r["ltp"] is None


def test_dhans_own_unrealised_figure_is_used_when_no_ltp_is_available():
    r = bv.normalize([{"tradingSymbol": "X", "netQty": 2, "buyAvg": 10.0, "unrealizedProfit": 4.5}])[0]
    assert r["unrealized_pnl"] == 4.5 and r["ltp"] is None


def test_short_position_pnl_sign():
    r = bv.normalize([{"tradingSymbol": "S", "netQty": -4, "sellAvg": 100.0}], {"S": 98.0})[0]
    assert r["is_open"] is True and r["avg_price"] == 100.0 and r["unrealized_pnl"] == pytest.approx(8.0)


def test_net_qty_derived_from_legs_when_the_field_is_absent():
    assert bv.net_qty({"buyQty": 5, "sellQty": 5}) == 0
    assert bv.net_qty({"buyQty": 5, "sellQty": 2}) == 3
    assert bv.net_qty({}) == 0


def test_open_symbols_only_lists_distinct_open_rows():
    raw = [{"tradingSymbol": "A", "netQty": 1}, {"tradingSymbol": "A", "netQty": 2},
           {"tradingSymbol": "B", "netQty": 0}, {"netQty": 3}]
    assert bv.open_symbols(raw) == ["A"]


def test_empty_and_none_inputs():
    assert bv.normalize([]) == [] and bv.normalize(None) == [] and bv.open_symbols(None) == []
