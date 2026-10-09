"""group276 (2026-10-09 TARIL +4.0% and BLUESTONE +4.2% above catalyst when bought): rows already up more than
WATCHLIST_MAX_CHASE_PCT (default 2%) above their catalyst price wait for a pullback.
Run: python3 -m pytest tests/test_group276_chase_cap.py -q"""
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
              "WATCHLIST_TIER1_MAX_DROP_PCT", "WATCHLIST_TIER1_MIN_DAY_CHANGE_PCT", "WATCHLIST_REQUIRE_PREV_CLOSE",
              "WATCHLIST_MAX_CHASE_PCT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(entry, "_watchlist_instrument_reason", lambda s, p: None)


def reason(tier, pct_move, price=100.0, prev=None):
    return entry._watchlist_adverse_reason(NS(symbol="TESTCO", source_tier=tier), pct_move, NS(price=price, prev_close=prev))


@pytest.mark.parametrize("tier", [1, 2])
def test_four_percent_above_catalyst_is_held(tier):
    r = reason(tier, 0.0372)                       # the TARIL case
    assert r and "chase limit +2.0%" in r and "pullback" in r


def test_inside_the_limit_passes():
    assert reason(1, 0.0169) is None               # BANKINDIA +1.69% was fine
    assert reason(1, 0.02) is None                 # exactly the limit


def test_env_zero_turns_the_limit_off(monkeypatch):
    monkeypatch.setenv("WATCHLIST_MAX_CHASE_PCT", "0")
    assert reason(1, 0.04) is None


def test_env_overrides_the_limit(monkeypatch):
    monkeypatch.setenv("WATCHLIST_MAX_CHASE_PCT", "0.035")
    assert reason(1, 0.0372) is not None and reason(1, 0.03) is None


def test_master_guard_switch_disables_everything(monkeypatch):
    monkeypatch.setenv("WATCHLIST_ADVERSE_GUARD", "0")
    assert reason(1, 0.04) is None
