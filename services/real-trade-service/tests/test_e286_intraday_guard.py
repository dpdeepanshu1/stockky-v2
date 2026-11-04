"""E286 round 214: failed-entry early exit + intraday profit-lock trail (exit_engine/intraday_guard.py). Plain runner: python3 tests/test_e286_intraday_guard.py."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from exit_engine import intraday_guard as ig


# ── failed entry ──────────────────────────────────────────────────────────────────────────────────────────────
def test_failed_entry_exits_when_never_worked_and_losing():
    # COFORGE-like: R = 4.5 %, never above entry, now -1.8 % (0.4 R) after 30 minutes
    v = ig.failed_entry(1000.0, 955.0, 982.0, 1000.0, 30, 45.0)
    assert v["action"] == "EXIT" and "failed entry" in v["reason"]


def test_failed_entry_holds_when_it_followed_through():
    v = ig.failed_entry(1000.0, 955.0, 982.0, 1010.0, 30, 45.0)          # best +10 = 0.22 R > 0.15 R
    assert v["action"] == "HOLD"


def test_failed_entry_holds_when_too_young_or_loss_small():
    assert ig.failed_entry(1000.0, 955.0, 982.0, 1000.0, 5, 45.0)["action"] == "HOLD"
    assert ig.failed_entry(1000.0, 955.0, 990.0, 1000.0, 30, 45.0)["action"] == "HOLD"     # -10 = 0.22 R < 0.35 R


def test_failed_entry_uses_stop_when_initial_distance_missing():
    assert ig.failed_entry(1000.0, 955.0, 980.0, 1000.0, 30, None)["action"] == "EXIT"


def test_failed_entry_bad_data_is_hold_never_raises():
    for args in ((None, 955, 980, 1000, 30), (1000, 955, "x", 1000, 30), (1000, 955, 980, None, 30), (1000, 1010, 980, 1000, 30), (1000, 955, 980, 1000, None)):
        assert ig.failed_entry(*args)["action"] == "HOLD"


# ── profit lock ───────────────────────────────────────────────────────────────────────────────────────────────
def test_profit_lock_mrpl_case_locks_gain_instead_of_breakeven():
    # +3.3 % best, 1-minute range 0.12 % -> gap max(0.35, 0.36) = 0.36 %; the old stop sat at breakeven (100)
    v = ig.profit_lock(100.0, 103.3, 103.2, 100.0, 0.12, 0.05)
    assert v and v["new_stop"] > 102.5 and v["new_stop"] < 103.2


def test_profit_lock_waits_for_min_gain():
    assert ig.profit_lock(100.0, 100.5, 100.5, 95.0, 0.1, 0.05) is None


def test_profit_lock_never_loosens_and_needs_two_ticks():
    v = ig.profit_lock(100.0, 103.3, 103.3, 100.0, 0.12, 0.05)
    assert ig.profit_lock(100.0, 103.3, 103.3, v["new_stop"], 0.12, 0.05) is None                 # same level: nothing
    assert ig.profit_lock(100.0, 103.3, 103.3, v["new_stop"] + 0.05, 0.12, 0.05) is None            # under two ticks better
    assert ig.profit_lock(100.0, 103.3, 103.3, v["new_stop"] + 5.0, 0.12, 0.05) is None             # a higher stop is never lowered


def test_profit_lock_gap_is_capped_and_floored():
    wide = ig.profit_lock(100.0, 105.0, 105.0, 100.0, 2.0, 0.05)                                   # 3 x 2 % = 6 % -> capped at 1.5 %
    assert wide and wide["gap_pct"] == 1.5
    tight = ig.profit_lock(100.0, 105.0, 105.0, 100.0, 0.01, 0.05)                                 # floored at 0.35 %
    assert tight and tight["gap_pct"] == 0.35


def test_profit_lock_no_bars_uses_fallback_gap():
    v = ig.profit_lock(100.0, 103.0, 103.0, 100.0, None, 0.05)
    assert v and v["gap_pct"] == 0.6


def test_profit_lock_stop_stays_below_live_price_and_above_entry():
    v = ig.profit_lock(100.0, 103.0, 101.2, 100.0, 0.5, 0.05)          # price already gave most of it back: stop just under the live price
    assert v is None or (v["new_stop"] < 101.2 and v["new_stop"] >= 100.1)
    assert ig.profit_lock(100.0, 100.9, 100.9, 100.0, 0.5, 0.05) is None   # gap would put the stop below entry + 0.1 %


def test_profit_lock_bad_data_never_raises():
    assert ig.profit_lock(None, 103, 103, 100, 0.1, 0.05) is None
    assert ig.profit_lock(100, 103, 103, "x", 0.1, 0.05) is not None      # unreadable old stop = no floor, still a valid lock
    assert ig.profit_lock(100, 103, 103, 100, 0.1, 0) is None


def _fresh_config(monkeypatch, *names):
    """tests/conftest.py switches several modes off on the shared config module, so the SHIPPED defaults are read from a fresh copy of config.py with these env vars cleared."""
    import importlib.util
    import config
    for k in names:
        monkeypatch.delenv(k, raising=False)
    spec = importlib.util.spec_from_file_location("config_fresh_e286", config.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    return fresh


def test_config_defaults(monkeypatch):
    config = _fresh_config(monkeypatch, "FAILED_ENTRY_EXIT_MODE", "PROFIT_LOCK_MODE", "FAILED_ENTRY_LOSS_FRAC", "PROFIT_LOCK_RANGE_MULT")
    assert config.FAILED_ENTRY_EXIT_MODE == "live" and config.PROFIT_LOCK_MODE == "live"
    assert config.FAILED_ENTRY_LOSS_FRAC == 0.35 and config.PROFIT_LOCK_RANGE_MULT == 3.0


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            n += 1
    print(f"{n} passed")
