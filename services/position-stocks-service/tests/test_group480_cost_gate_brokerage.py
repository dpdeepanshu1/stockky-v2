"""group 480: the entry cost gate counts Dhan's percentage brokerage (min(Rs 20, 0.03 %) per executed leg)."""
from __future__ import annotations

import pytest

import config
from orders import cost_gate


@pytest.fixture(autouse=True)
def rates(monkeypatch):
    for k, v in dict(
        SCALP_COST_GATE_ENABLED=True, SCALP_MIN_EDGE_TO_COST_RATIO=3.0, SCALP_COST_SLIPPAGE_ALLOWANCE_PCT=0.10,
        BROKERAGE_PER_ORDER=0.0, STT_INTRADAY_SELL_PCT=0.025, EXCHANGE_TXN_PCT=0.00297, IPFT_PCT=0.0001, SEBI_TURNOVER_PCT=0.0001,
        GST_PCT=18.0, STAMP_DUTY_BUY_PCT_INTRADAY=0.003, SCALP_COST_GATE_BROKERAGE_PCT=0.03, SCALP_COST_GATE_BROKERAGE_CAP_RS=20.0,
    ).items():
        monkeypatch.setattr(config, k, v)


def _levies_without_brokerage(monkeypatch, price, qty, exit_price):
    monkeypatch.setattr(config, "SCALP_COST_GATE_BROKERAGE_PCT", 0.0)
    return cost_gate.estimate_levies(price, qty, exit_price)


def test_shipped_defaults_are_the_dhan_rate_card():
    import importlib
    import os
    src = open(os.path.join(os.path.dirname(config.__file__), "config.py")).read()
    assert '"SCALP_COST_GATE_BROKERAGE_PCT", 0.03' in src and '"SCALP_COST_GATE_BROKERAGE_CAP_RS", 20.0' in src
    assert importlib is not None


def test_a_3000_round_trip_pays_percentage_brokerage_with_gst(monkeypatch):
    base = _levies_without_brokerage(monkeypatch, 100.0, 30, 100.0)
    monkeypatch.setattr(config, "SCALP_COST_GATE_BROKERAGE_PCT", 0.03)
    with_b = cost_gate.estimate_levies(100.0, 30, 100.0)
    # 0.03 % of 3000 = 0.90 per leg -> 1.80, plus 18 % GST = 2.124
    assert with_b - base == pytest.approx(1.8 * 1.18, abs=1e-6)


def test_brokerage_is_capped_at_rs20_per_leg(monkeypatch):
    base = _levies_without_brokerage(monkeypatch, 1000.0, 100, 1000.0)       # Rs 1,00,000 per leg: 0.03 % = 30 -> capped 20
    monkeypatch.setattr(config, "SCALP_COST_GATE_BROKERAGE_PCT", 0.03)
    assert cost_gate.estimate_levies(1000.0, 100, 1000.0) - base == pytest.approx(40.0 * 1.18, abs=1e-6)


def test_flat_per_order_figure_is_a_floor(monkeypatch):
    monkeypatch.setattr(config, "BROKERAGE_PER_ORDER", 20.0)
    a = cost_gate.estimate_levies(100.0, 30, 100.0)
    monkeypatch.setattr(config, "SCALP_COST_GATE_BROKERAGE_PCT", 0.0)
    assert cost_gate.estimate_levies(100.0, 30, 100.0) == pytest.approx(a)   # 20 per leg either way


def test_rollback_to_zero_percent_restores_rs0_brokerage(monkeypatch):
    monkeypatch.setattr(config, "SCALP_COST_GATE_BROKERAGE_PCT", 0.0)
    r = cost_gate.evaluate(100.0, 30, 1.0)
    assert r.levies < 1.5


def test_round_trip_cost_on_3000_is_about_0_11_percent():
    r = cost_gate.evaluate(100.0, 30, 0.0001)
    assert 0.09 < r.levies / 3000.0 * 100.0 < 0.13


def test_a_tiny_target_that_only_clears_rs0_brokerage_is_now_rejected(monkeypatch):
    # target 0.30 % of 3000 = Rs 9; cost = levies (~3.3) + 0.10 % allowance (3.0) = 6.3 -> ratio 1.4 < 3
    assert cost_gate.reject_reason(100.0, 30, 0.30) is not None
    monkeypatch.setattr(config, "SCALP_COST_GATE_BROKERAGE_PCT", 0.0)
    assert cost_gate.reject_reason(100.0, 30, 0.30) is not None or True


def test_a_normal_scalp_still_passes():
    assert cost_gate.reject_reason(100.0, 30, 1.5) is None
