"""tests/test_group471_stopout_guard.py - entry_engine/stopout_guard.py (group 471). Pure parts; wiring is covered by the evaluate_mode suite on CI."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from entry_engine import stopout_guard as sg

SINCE = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
T = datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)
OLD = datetime(2026, 10, 19, 5, 0, tzinfo=timezone.utc)


def test_tally_counts_only_losing_stops_since_midnight():
    rows = [("stop_hit: x", T, -50.0, "AAA"), ("broker_stop", T, -10.0, "bbb"), ("emergency_gap_down", T, -5.0, "CCC"),
            ("stop_hit", T, 12.0, "WIN"), ("eod_squareoff", T, -30.0, "EOD"), ("stop_hit", OLD, -40.0, "OLD"), ("stop_hit", None, -1.0, "NOW")]
    n, syms = sg.tally(rows, SINCE)
    assert n == 3 and syms == {"AAA", "BBB", "CCC"}


def test_tally_never_raises():
    assert sg.tally([("stop_hit", T, "bad", "A")], SINCE) == (0, set())
    assert sg.tally(None, SINCE) == (0, set())


def test_pause_reason(monkeypatch):
    monkeypatch.setattr(config, "STOPOUT_DAY_PAUSE_N", 3)
    assert sg.pause_reason_from(2) is None
    assert sg.pause_reason_from(3).startswith("STOPOUT_GUARD:PAUSED")
    monkeypatch.setattr(config, "STOPOUT_DAY_PAUSE_N", 0)
    assert sg.pause_reason_from(99) is None


def test_symbol_reason(monkeypatch):
    monkeypatch.setattr(config, "STOPOUT_SYMBOL_BLOCK", True)
    assert sg.symbol_reason("aaa", {"AAA"}).startswith("STOPOUT_GUARD:SYMBOL")
    assert sg.symbol_reason("ZZZ", {"AAA"}) is None
    monkeypatch.setattr(config, "STOPOUT_SYMBOL_BLOCK", False)
    assert sg.symbol_reason("AAA", {"AAA"}) is None


def test_covers_and_failopen(monkeypatch):
    monkeypatch.setattr(config, "STOPOUT_GUARD_ENABLED", True)
    assert sg.covers("REAL") and sg.covers("demo")
    monkeypatch.setattr(config, "STOPOUT_GUARD_ENABLED", False)
    assert not sg.covers("REAL")
    assert sg.stopouts_today(None, "REAL") == (0, set())      # db error -> no pause
