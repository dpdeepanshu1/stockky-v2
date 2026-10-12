"""group335 - a symbol-specific entry skip no longer ends the cycle: the next shortlisted pick is tried.
Run from services/position-stocks-service:  python3 -m pytest tests/test_group335_shortlist_fallthrough.py -q"""
from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from orders import entry


@pytest.mark.parametrize("reason", [
    "VWAP_BELOW:price 99.00 is 0.50% under the day's VWAP 99.50", "VWAP_EXTENDED:x", "LIQUIDITY_AVG_TURNOVER:x", "LIQUIDITY_DAY_TURNOVER:x",
    "LIQUIDITY_RVOL:x", "DEPTH_SPREAD:1.2% > 0.6%", "DEPTH_THIN_BOOK:x", "DEPTH_MISSING:x", "CIRCUIT_UPPER:x", "CIRCUIT_LOWER:x",
    "PREOPEN_GAP_UP:x", "PREOPEN_GAP_DOWN:x", "BOARD_CHASE:2.0 ATR", "REENTRY_COOLDOWN:x", "REENTRY_BLOCKED_AFTER_LOSS:x",
    "ENTRY_REJECTED_TODAY:1", "PENNY_STOCK:ltp=1", "RANGE_GATE:x", "OPENING_GATE:BELOW_OPEN:x", "PRICE_GUARD:STALE_TICK:last tick 90s old",
    "PRICE_GUARD:ENTRY_SLIPPAGE:x", "PRICE_GUARD:DAY_GAIN_TOO_HIGH:x", "ALREADY_OPEN_HERE:id=1", "SYMBOL_HELD_BY_OTHER_SERVICE",
    "COST_GATE:x", "SECURITY_NOT_FOUND:x", "ENTRY_ORDER_FAILED_COOLDOWN:X order placement failed",
])
def test_symbol_specific_reasons_fall_through(reason):
    assert entry.is_symbol_specific_skip(reason) is True


@pytest.mark.parametrize("reason", [
    None, "", "SERVICE_NOT_ARMED", "DAILY_LOSS_KILL_SWITCH", "ORDER_BUDGET_EXHAUSTED:40", "MAX_POSITIONS:3",
    "ENTRY_MARGIN_PAUSE:new entries paused 4.0m more", "OPENING_SHADOW:WOULD_ENTER ltp=1", "INSUFFICIENT_CAPITAL",
    "SHARED_ORDER_BUDGET_EXHAUSTED", "QUALITY_GATE:x",
])
def test_global_reasons_still_end_the_cycle(reason):
    assert entry.is_symbol_specific_skip(reason) is False


def test_log_candidate_records_skip_reason_only_for_skips():
    class DB:
        def add(self, x): pass
        def commit(self): pass
        def rollback(self): pass
    cand = SimpleNamespace(symbol="AAA", window_label="w", pct_change=1.0, composite_score=5.0)
    entry.reset_last_skip()
    entry._log_candidate(DB(), cand, "SKIPPED", "VWAP_BELOW:x")
    assert entry.last_skip_reason() == "VWAP_BELOW:x"
    entry._log_candidate(DB(), cand, "ENTERED", "ok")
    assert entry.last_skip_reason() == "VWAP_BELOW:x"      # an ENTERED row does not overwrite it
    entry.reset_last_skip()
    assert entry.last_skip_reason() is None


def test_last_skip_is_per_thread():
    entry.reset_last_skip()
    entry._skip_state.reason = "MAIN"
    seen = {}
    def other():
        seen["start"] = entry.last_skip_reason()
        entry._skip_state.reason = "OTHER"
    t = threading.Thread(target=other); t.start(); t.join()
    assert seen["start"] is None and entry.last_skip_reason() == "MAIN"


def test_wrapper_reports_reason_and_uses_patched_attempt_entry(monkeypatch):
    import main
    cand = SimpleNamespace(symbol="AAA")

    def fake(db, c, quality=None):
        entry._skip_state.reason = "LIQUIDITY_RVOL:x"
        return None
    monkeypatch.setattr(main, "attempt_entry", fake)
    assert main._attempt_entry_with_skip_reason(None, cand, None) == (None, "LIQUIDITY_RVOL:x")

    monkeypatch.setattr(main, "attempt_entry", lambda db, c, quality=None: "POS")
    assert main._attempt_entry_with_skip_reason(None, cand, None) == ("POS", None)

    entry._skip_state.reason = "STALE"
    monkeypatch.setattr(main, "attempt_entry", lambda db, c, quality=None: None)   # no skip row written this call
    assert main._attempt_entry_with_skip_reason(None, cand, None) == (None, None)


def test_flag_default_on():
    import config
    assert config.SHORTLIST_FALLTHROUGH is True
