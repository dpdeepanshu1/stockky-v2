"""
tests/test_cost_model.py — automated coverage for cost_model.py (2026-09-18
follow-on item #10: "No automated tests for cost_model.py or
_select_overnight_holds — verified by code review + compile checks only").

Pure unit tests, no DB/network required — cost_model.py itself has zero
side effects (see its own module docstring: "does not place orders, touch
the DB, or import anything from this service besides config").

Run from services/real-trade-service:
    python -m pytest tests/test_cost_model.py -v
or, with no pytest available:
    python tests/test_cost_model.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import cost_model


def test_round_trip_cost_is_positive_and_grows_with_trade_value():
    small = cost_model.estimate_round_trip_cost(100.0, 10)   # ₹1,000 trade
    large = cost_model.estimate_round_trip_cost(100.0, 1000)  # ₹100,000 trade
    assert small.total > 0
    assert large.total > small.total


def test_delivery_vs_intraday_stt_differs():
    """CNC (delivery) charges STT on both legs; INTRADAY only on the sell
    leg — delivery should always cost at least as much on STT alone."""
    delivery = cost_model.estimate_round_trip_cost(500.0, 20, product_type="CNC")
    intraday = cost_model.estimate_round_trip_cost(500.0, 20, product_type="INTRADAY")
    assert delivery.stt >= intraday.stt


def test_dp_charge_only_on_real_delivery_sell():
    no_dp = cost_model.estimate_round_trip_cost(500.0, 20, product_type="CNC", is_delivery_sell=False)
    with_dp = cost_model.estimate_round_trip_cost(500.0, 20, product_type="CNC", is_delivery_sell=True)
    assert no_dp.dp_charge == 0.0
    assert with_dp.dp_charge > 0.0
    assert with_dp.total > no_dp.total


def test_exit_price_defaults_to_entry_price():
    a = cost_model.estimate_round_trip_cost(200.0, 15)
    b = cost_model.estimate_round_trip_cost(200.0, 15, exit_price=200.0)
    assert a.total == b.total


def test_gate_uses_config_defaults_when_no_override_given():
    """A tiny trade_value below config.MIN_TRADE_VALUE must fail the gate
    with no explicit min_trade_value/min_edge_to_cost_ratio passed in —
    this is the exact call signature entry_engine used before follow-on
    item #5 added the per-mode override params."""
    result = cost_model.evaluate_entry_cost_gate(
        entry_price=50.0, qty=1, target_pct=2.0, product_type="CNC",
    )
    assert result.trade_value == 50.0
    assert result.trade_value < config.MIN_TRADE_VALUE
    assert result.passes_min_value is False
    assert result.passes is False


def test_gate_passes_a_large_high_edge_trade():
    result = cost_model.evaluate_entry_cost_gate(
        entry_price=1000.0, qty=100, target_pct=5.0, product_type="CNC",
    )
    assert result.trade_value == 100_000.0
    assert result.passes_min_value is True
    # 5% target on a ₹1L position is a large edge relative to round-trip
    # costs (brokerage/STT/etc are all fractions of a percent) — should
    # clear the default 3.0x floor comfortably.
    assert result.ratio is not None and result.ratio >= config.MIN_EDGE_TO_COST_RATIO
    assert result.passes is True


def test_gate_respects_explicit_min_trade_value_override():
    """2026-09-18 fix (follow-on item #5): a per-mode TradeRiskConfig
    override should be honored exactly, independent of config.py's
    env-var default."""
    # A ₹4,000 trade fails a ₹5,000 floor...
    fails = cost_model.evaluate_entry_cost_gate(
        entry_price=400.0, qty=10, target_pct=5.0, product_type="CNC",
        min_trade_value=5000.0,
    )
    assert fails.passes_min_value is False
    # ...but the same trade passes a ₹1,000 floor.
    passes = cost_model.evaluate_entry_cost_gate(
        entry_price=400.0, qty=10, target_pct=5.0, product_type="CNC",
        min_trade_value=1000.0,
    )
    assert passes.passes_min_value is True


def test_gate_respects_explicit_min_edge_to_cost_ratio_override():
    # A thin 0.5% target on a modest position should fail a strict 10x
    # ratio floor but pass a permissive 0.1x floor.
    strict = cost_model.evaluate_entry_cost_gate(
        entry_price=500.0, qty=20, target_pct=0.5, product_type="CNC",
        min_trade_value=0.0, min_edge_to_cost_ratio=10.0,
    )
    lenient = cost_model.evaluate_entry_cost_gate(
        entry_price=500.0, qty=20, target_pct=0.5, product_type="CNC",
        min_trade_value=0.0, min_edge_to_cost_ratio=0.1,
    )
    assert strict.passes_min_ratio is False
    assert lenient.passes_min_ratio is True


def test_zero_qty_never_raises():
    result = cost_model.estimate_round_trip_cost(100.0, 0)
    assert result.total >= 0.0
    gate = cost_model.evaluate_entry_cost_gate(100.0, 0, 2.0)
    # ratio is None when cost.total == 0 (can happen at qty=0) — the gate
    # must not divide by zero or raise.
    assert gate.passes_min_ratio in (True, False)


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(_TESTS) - failures}/{len(_TESTS)} passed")
    if failures:
        sys.exit(1)
