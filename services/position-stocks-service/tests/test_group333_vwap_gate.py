"""tests/test_group333_vwap_gate.py - orders/vwap_gate.py (group 333): no scalp BUY below the day's VWAP; everything fails open."""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import vwap_gate
from tz_utils import IST


def at(h, m):
    return datetime(2026, 10, 13, h, m, tzinfo=IST)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_VWAP_GATE_ENABLED", True)     # conftest switches it off for the older suites
    monkeypatch.setattr(config, "ENTRY_VWAP_MAX_EXTENSION_PCT", 0.0)


def test_below_vwap_refused_inside_tolerance_allowed():
    assert vwap_gate.decide(99.0, 100.0).startswith("VWAP_BELOW")
    assert vwap_gate.decide(99.9, 100.0) is None            # 0.1 % under, tolerance 0.15 %
    assert vwap_gate.decide(101.0, 100.0) is None


def test_extension_side_off_by_default_then_on(monkeypatch):
    assert vwap_gate.decide(110.0, 100.0) is None
    monkeypatch.setattr(config, "ENTRY_VWAP_MAX_EXTENSION_PCT", 3.0)
    assert vwap_gate.decide(104.0, 100.0).startswith("VWAP_EXTENDED")
    assert vwap_gate.decide(102.0, 100.0) is None


def test_unknown_or_insane_vwap_is_no_opinion():
    for v in (None, 0, -5, "x", float("nan")):
        assert vwap_gate.decide(100.0, v) is None
    assert vwap_gate.decide(100.0, 50.0) is None            # 100 % away: not a real average
    assert vwap_gate.decide(None, 100.0) is None


def test_reject_reason_reads_cached_quote_and_honours_time(monkeypatch):
    monkeypatch.setattr(vwap_gate.depth_gate, "_fresh_quote", lambda s: {"avg_price": 100.0})
    assert vwap_gate.reject_reason("A", 98.0, at(10, 30)).startswith("VWAP_BELOW")
    assert vwap_gate.reject_reason("A", 98.0, at(9, 30)) is None        # before 09:45
    assert vwap_gate.reject_reason("A", 98.0, at(9, 45)).startswith("VWAP_BELOW")
    monkeypatch.setattr(config, "ENTRY_VWAP_MIN_TIME_IST", "10:00")
    assert vwap_gate.reject_reason("A", 98.0, at(9, 50)) is None


def test_off_switch_stale_quote_and_errors_never_block(monkeypatch):
    monkeypatch.setattr(vwap_gate.depth_gate, "_fresh_quote", lambda s: {"avg_price": 100.0})
    monkeypatch.setattr(config, "ENTRY_VWAP_GATE_ENABLED", False)
    assert vwap_gate.reject_reason("A", 98.0, at(10, 30)) is None
    monkeypatch.setattr(config, "ENTRY_VWAP_GATE_ENABLED", True)
    monkeypatch.setattr(vwap_gate.depth_gate, "_fresh_quote", lambda s: None)       # no fresh quote (too old / no answer)
    assert vwap_gate.reject_reason("A", 98.0, at(10, 30)) is None
    monkeypatch.setattr(vwap_gate.depth_gate, "_fresh_quote", lambda s: {})
    assert vwap_gate.reject_reason("A", 98.0, at(10, 30)) is None                   # quote without avg_price
    def boom(s):
        raise RuntimeError("x")
    monkeypatch.setattr(vwap_gate.depth_gate, "_fresh_quote", boom)
    assert vwap_gate.reject_reason("A", 98.0, at(10, 30)) is None


def test_zero_tolerance_blocks_any_dip(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_VWAP_TOLERANCE_PCT", 0.0)
    assert vwap_gate.decide(99.99, 100.0).startswith("VWAP_BELOW")
    assert vwap_gate.decide(100.0, 100.0) is None


def test_entry_wiring_runs_before_capital_is_reserved():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders", "entry.py"), encoding="utf-8").read()
    assert "vwap_gate.reject_reason(candidate.symbol, candidate.current_ltp)" in src
    assert src.index("vwap_gate.reject_reason(") < src.index("ledger.reserve_capital(")
    assert config.ENTRY_VWAP_TOLERANCE_PCT == 0.15 and config.ENTRY_VWAP_MIN_TIME_IST == "09:45"
