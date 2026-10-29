"""E257: the day-spike and gain-room guards count what they decided, so a cold ATR cache or a missing day high/low is visible."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import config
from entry_engine import day_spike_gate as g


def _tick(price, prev_close, hi, lo):
    return SimpleNamespace(symbol="ARVIND", price=price, prev_close=prev_close, day_high=hi, day_low=lo)


def _run(mode, fn):
    g.reset_stats()
    with mock.patch.object(config, "SPIKE_GUARD_MODE", mode, create=True), mock.patch.object(config, "GAIN_ROOM_MODE", mode, create=True), \
            mock.patch.object(g, "_atr_pct", return_value=None):
        return fn()


def test_spike_guard_counts_held_passed_and_no_data():
    def go():
        assert g.spike_reason_tick(_tick(106.0, 100.0, 106.0, 100.0))          # +6% at the top of the range -> held
        assert g.spike_reason_tick(_tick(101.0, 100.0, 106.0, 100.0)) is None   # calm -> passed
        assert g.spike_reason_tick(_tick(101.0, None, 106.0, 100.0)) is None    # no previous close -> no data
        return g.guard_stats()["spike"]
    assert _run("live", go) == {"held": 1, "passed": 1, "no_data": 1}


def test_log_mode_counts_log_only_not_held():
    def go():
        assert g.spike_reason_tick(_tick(106.0, 100.0, 106.0, 100.0)) is None
        return g.guard_stats()["spike"]
    assert _run("log", go) == {"log_only": 1}


def test_range_guard_reports_a_cold_atr_cache_as_no_data():
    def go():
        assert g.range_reason_tick(_tick(103.0, 100.0, 104.0, 100.0), 2.4) is None   # ATR unavailable (patched None)
        return g.guard_stats()["range"]
    assert _run("live", go) == {"no_data": 1}


def test_off_mode_counts_nothing():
    def go():
        g.spike_reason_tick(_tick(106.0, 100.0, 106.0, 100.0))
        return g.guard_stats()
    assert _run("off", go) == {"spike": {}, "range": {}}
