"""tests/test_group218_cost_gate.py - orders/cost_gate.py (group 218, review item 5).

Pure maths, no DB. Expected numbers are worked by hand from the rates pinned in the fixture.
Run from services/position-stocks-service:
    python3 -m pytest tests/test_group218_cost_gate.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import cost_gate


@pytest.fixture(autouse=True)
def rates(monkeypatch):
    for k, v in dict(
        SCALP_COST_GATE_ENABLED=True, SCALP_MIN_EDGE_TO_COST_RATIO=3.0, SCALP_COST_SLIPPAGE_ALLOWANCE_PCT=0.10,
        BROKERAGE_PER_ORDER=0.0, STT_INTRADAY_SELL_PCT=0.025, EXCHANGE_TXN_PCT=0.00325, IPFT_PCT=0.0, SEBI_TURNOVER_PCT=0.0001,
        GST_PCT=18.0, STAMP_DUTY_BUY_PCT_INTRADAY=0.003,
    ).items():
        monkeypatch.setattr(config, k, v)


class TestEstimateLevies:
    def test_zero_brokerage_levies_for_a_1000_buy_sold_at_1010(self):
        # stt 1010*0.025% = 0.2525; stamp 1000*0.003% = 0.03; exchange 2010*0.00325% = 0.065325;
        # sebi 2010*0.0001% = 0.00201; gst (0.065325+0.00201)*18% = 0.0121203  -> 0.3619553
        assert cost_gate.estimate_levies(100.0, 10, exit_price=101.0) == pytest.approx(0.3619553, abs=1e-6)

    def test_exit_defaults_to_entry(self):
        a = cost_gate.estimate_levies(100.0, 10)
        b = cost_gate.estimate_levies(100.0, 10, exit_price=100.0)
        assert a == b and a > 0

    def test_flat_brokerage_counts_two_legs_and_carries_gst(self):
        base = cost_gate.estimate_levies(100.0, 10, exit_price=101.0)
        config.BROKERAGE_PER_ORDER = 20.0
        # +40 brokerage, +40*18% = 7.2 gst
        assert cost_gate.estimate_levies(100.0, 10, exit_price=101.0) == pytest.approx(base + 40.0 + 7.2)

    def test_stt_is_on_the_sell_leg_only(self):
        config.EXCHANGE_TXN_PCT = config.SEBI_TURNOVER_PCT = config.STAMP_DUTY_BUY_PCT_INTRADAY = 0.0
        # exit 0 -> nothing sold -> no stt
        assert cost_gate.estimate_levies(100.0, 10, exit_price=0.0) == pytest.approx(0.0)

    def test_scales_linearly_with_quantity_when_brokerage_is_zero(self):
        assert cost_gate.estimate_levies(100.0, 20, 101.0) == pytest.approx(2 * cost_gate.estimate_levies(100.0, 10, 101.0))


class TestEvaluate:
    def test_numbers_for_a_normal_scalp(self):
        r = cost_gate.evaluate(100.0, 10, 1.0)
        assert r.trade_value == pytest.approx(1000.0) and r.expected_edge == pytest.approx(10.0)
        assert r.slippage_allowance == pytest.approx(1.0)              # 0.10% of 1000
        assert r.cost == pytest.approx(0.3619553 + 1.0, abs=1e-6)
        assert r.ratio == pytest.approx(10.0 / 1.3619553, abs=1e-4) and r.passes
        assert r.cost_pct == pytest.approx(0.13619553, abs=1e-6)

    def test_zero_cost_means_ratio_none_and_passes(self):
        for k in ("STT_INTRADAY_SELL_PCT", "EXCHANGE_TXN_PCT", "IPFT_PCT", "SEBI_TURNOVER_PCT", "STAMP_DUTY_BUY_PCT_INTRADAY",
                  "SCALP_COST_SLIPPAGE_ALLOWANCE_PCT"):
            setattr(config, k, 0.0)
        r = cost_gate.evaluate(100.0, 10, 1.0)
        assert r.cost == 0 and r.ratio is None and r.passes

    def test_exactly_at_the_minimum_ratio_passes(self):
        r = cost_gate.evaluate(100.0, 10, 1.0)
        config.SCALP_MIN_EDGE_TO_COST_RATIO = r.ratio
        assert cost_gate.evaluate(100.0, 10, 1.0).passes

    def test_tiny_target_fails(self):
        # target 0.15% -> edge 1.5 vs cost ~1.36 -> ratio ~1.1
        r = cost_gate.evaluate(100.0, 10, 0.15)
        assert not r.passes and r.ratio < 3.0


class TestRejectReason:
    def test_normal_scalp_with_zero_brokerage_is_allowed(self):
        assert cost_gate.reject_reason(500.0, 40, 1.4) is None

    def test_flat_brokerage_on_a_small_position_is_rejected_with_a_readable_reason(self):
        config.BROKERAGE_PER_ORDER = 20.0
        r = cost_gate.reject_reason(100.0, 10, 1.0)
        # cost = 0.3619553 + 47.2 (brokerage+gst) -> 47.56 + 1.0 allowance = 48.56; edge 10 -> ratio 0.21
        assert r.startswith("edge_to_cost=0.21<3.00 ") and "edge=Rs10.00" in r and "cost=Rs48.56(4.86%)" in r
        assert "qty=10" in r and "value=Rs1000.00" in r and "target=1.00%" in r

    def test_same_brokerage_is_fine_on_a_big_enough_position(self):
        config.BROKERAGE_PER_ORDER = 20.0
        # value 100,000, target 3% -> edge 3000 vs cost ~ 47.5 + 100 + 25 levies ~ 175
        assert cost_gate.reject_reason(500.0, 200, 3.0) is None

    def test_switch_off_allows_everything(self):
        config.BROKERAGE_PER_ORDER = 20.0
        config.SCALP_COST_GATE_ENABLED = False
        assert cost_gate.reject_reason(100.0, 10, 1.0) is None

    def test_a_higher_slippage_allowance_can_flip_the_result(self):
        assert cost_gate.reject_reason(100.0, 10, 1.0) is None
        config.SCALP_COST_SLIPPAGE_ALLOWANCE_PCT = 0.5          # 5 of edge 10 -> ratio < 3
        assert cost_gate.reject_reason(100.0, 10, 1.0) is not None

    @pytest.mark.parametrize("args", [(0.0, 10, 1.0), (None, 10, 1.0), (100.0, 0, 1.0), (100.0, None, 1.0), (100.0, 10, None)])
    def test_unusable_inputs_fail_open(self, args):
        config.BROKERAGE_PER_ORDER = 20.0
        assert cost_gate.reject_reason(*args) is None

    def test_a_config_error_fails_open(self):
        config.SCALP_MIN_EDGE_TO_COST_RATIO = "bad"
        config.BROKERAGE_PER_ORDER = 20.0
        assert cost_gate.reject_reason(100.0, 10, 1.0) is None
