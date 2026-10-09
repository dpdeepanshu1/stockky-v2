"""group 265: exit-side DP guard - skip a tiny-gain target sale of a carried CNC holding."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import cost_model
from exit_engine import exit as ex

OLD = datetime.now(timezone.utc) - timedelta(days=2)


def _pos(product="CNC", opened=OLD, entry=100.0):
    return SimpleNamespace(entry_product_type=product, opened_at=opened, avg_entry_price=entry)


def test_sell_cost_includes_dp_only_when_carried():
    carried = cost_model.estimate_delivery_sell_cost(100.0, 10, carried=True)
    same_day = cost_model.estimate_delivery_sell_cost(100.0, 10, carried=False)
    assert carried - same_day == pytest.approx(config.DP_CHARGE_FLAT * (1 + config.GST_PCT / 100), abs=0.02)


def test_tiny_gain_on_carried_cnc_is_blocked():
    reason = ex._dp_guard_blocks_target_sale(_pos(), 100.5, 4)          # gain Rs 2 vs ~Rs 15 DP
    assert reason and "holding" in reason


def test_large_gain_goes_ahead():
    assert ex._dp_guard_blocks_target_sale(_pos(), 140.0, 20) is None   # gain Rs 800


def test_mis_and_same_day_positions_are_never_blocked():
    assert ex._dp_guard_blocks_target_sale(_pos(product="INTRADAY"), 100.5, 4) is None
    assert ex._dp_guard_blocks_target_sale(_pos(opened=datetime.now(timezone.utc)), 100.5, 4) is None


def test_switch_off_and_failure_never_block(monkeypatch):
    monkeypatch.setattr(config, "EXIT_DP_GUARD_ENABLED", False)
    assert ex._dp_guard_blocks_target_sale(_pos(), 100.5, 4) is None
    monkeypatch.setattr(config, "EXIT_DP_GUARD_ENABLED", True)
    assert ex._dp_guard_blocks_target_sale(SimpleNamespace(), 100.5, 4) is None
