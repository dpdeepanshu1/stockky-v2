"""group231 (log review item 5): Tier 1 watchlist rows use a tighter drop limit (1.5%) and must not be down on the day.
Run: python3 -m pytest tests/test_group231_tier1_guard.py -q"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from entry_engine import entry


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("WATCHLIST_ADVERSE_GUARD", "WATCHLIST_MAX_DROP_PCT", "WATCHLIST_TIER3_MIN_DAY_CHANGE_PCT",
              "WATCHLIST_TIER1_MAX_DROP_PCT", "WATCHLIST_TIER1_MIN_DAY_CHANGE_PCT", "WATCHLIST_REQUIRE_PREV_CLOSE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(entry, "_watchlist_instrument_reason", lambda s, p: None)


def row(tier):
    return NS(symbol="TESTCO", source_tier=tier)


def tick(price, prev=None):
    return NS(price=price, prev_close=prev)


def reason(tier, pct_move, price=100.0, prev=None):
    return entry._watchlist_adverse_reason(row(tier), pct_move, tick(price, prev))


def test_tier1_drop_limit_is_one_and_a_half_percent():
    assert reason(1, -0.0233) and "limit -1.5%" in reason(1, -0.0233)   # the ABCAPITAL case
    assert reason(1, -0.014) is None
    assert reason(1, -0.016) is not None


def test_tier2_and_tier3_keep_the_three_percent_limit():
    assert reason(2, -0.0233) is None
    assert "limit -3.0%" in reason(2, -0.035)
    assert reason(3, -0.0233, prev=95.0) is None


def test_tier1_down_on_the_day_is_held():
    r = reason(1, 0.0, price=99.0, prev=100.0)
    assert r and "tier 1 stock is -1.00% on the day" in r


def test_tier1_flat_or_up_on_the_day_passes():
    assert reason(1, 0.0, price=100.0, prev=100.0) is None
    assert reason(1, 0.0, price=103.0, prev=100.0) is None


def test_tier1_unknown_day_change_still_passes():
    assert reason(1, 0.0, price=99.0, prev=None) is None


def test_tier2_and_tier3_day_check_unchanged():
    assert reason(2, 0.0, price=99.0, prev=100.0) is None
    assert reason(3, 0.0, price=100.5, prev=100.0) and "volume-shock" in reason(3, 0.0, price=100.5, prev=100.0)


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("WATCHLIST_TIER1_MAX_DROP_PCT", "0.03")
    assert reason(1, -0.0233) is None
    monkeypatch.setenv("WATCHLIST_TIER1_MAX_DROP_PCT", "0")          # 0 = use WATCHLIST_MAX_DROP_PCT
    monkeypatch.setenv("WATCHLIST_MAX_DROP_PCT", "0.02")
    assert reason(1, -0.0233) and "limit -2.0%" in reason(1, -0.0233)
    monkeypatch.setenv("WATCHLIST_TIER1_MIN_DAY_CHANGE_PCT", "-2")
    assert reason(1, 0.0, price=99.0, prev=100.0) is None
    monkeypatch.setenv("WATCHLIST_TIER1_MIN_DAY_CHANGE_PCT", "off")
    assert reason(1, 0.0, price=90.0, prev=100.0) is None


def test_master_switch_turns_both_off(monkeypatch):
    monkeypatch.setenv("WATCHLIST_ADVERSE_GUARD", "0")
    assert reason(1, -0.05, price=90.0, prev=100.0) is None
