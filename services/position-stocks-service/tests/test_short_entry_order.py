"""E243: dhan_client.short_entry_order - the SELL entry is a marketable LIMIT just under the last price (Dhan turned the MARKET entry into a resting LIMIT at the last price)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from execution import dhan_client


def test_limit_sits_under_the_last_price_on_a_valid_tick_between_target_and_stop():
    t, px = dhan_client.short_entry_order(300.40, 291.40, 304.90)         # the NTPC probe's own numbers
    assert t == "LIMIT" and px < 300.40 and dhan_client.is_valid_tick_price(px) and 291.40 < px < 304.90
    assert abs(px - 300.40 * (1 - config.SHORT_ENTRY_CUSHION_PCT / 100)) <= 0.05


def test_switch_off_is_the_old_market_entry(monkeypatch):
    monkeypatch.setattr(config, "SHORT_ENTRY_MARKETABLE_LIMIT", False)
    assert dhan_client.short_entry_order(300.40, 291.40, 304.90) == ("MARKET", 300.40)


@pytest.mark.parametrize("live,target,stop", [(100.0, 99.9, 101.0), (100.0, 98.0, 99.6), (0, 98.0, 101.0)])
def test_legs_on_the_wrong_side_of_the_limit_fall_back_to_market(live, target, stop):
    assert dhan_client.short_entry_order(live, target, stop)[0] == "MARKET"


def test_cushion_is_bounded_and_the_flag_ships_on():
    assert 0.05 <= config.SHORT_ENTRY_CUSHION_PCT <= 1.0
    assert config.SHORT_ENTRY_MARKETABLE_LIMIT is True
