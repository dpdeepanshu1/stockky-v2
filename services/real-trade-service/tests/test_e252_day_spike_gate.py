"""E252 (2026-10-28): day-spike guard and range-used-up check for real-trade-service (ported from position-stocks E251)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import config
from entry_engine import day_spike_gate as g


def _tick(price, prev_close, hi, lo):
    return SimpleNamespace(symbol="ARVIND", price=price, prev_close=prev_close, day_high=hi, day_low=lo)


def _live(fn):
    import functools

    @functools.wraps(fn)
    def w():
        with mock.patch.object(config, "SPIKE_GUARD_MODE", "live", create=True), mock.patch.object(config, "GAIN_ROOM_MODE", "live", create=True):
            return fn()
    return w


def test_day_limit_scales_with_atr_and_clamps():
    assert g.day_limit_pct(None) == 4.0
    assert g.day_limit_pct(1.0) == 2.5       # 2 x 1.0 = 2.0 -> clamped up
    assert g.day_limit_pct(2.0) == 4.0
    assert g.day_limit_pct(5.0) == 6.0       # 10 -> clamped down


def test_spike_needs_both_a_big_day_gain_and_the_top_of_the_range():
    assert g.judge_spike(106, 100, 106.2, 99, 2.0).startswith("SPIKE_CHASE:DAY_SPIKE")
    assert g.judge_spike(106, 100, 112, 99, 2.0) is None       # big gain but well off the high
    assert g.judge_spike(102, 100, 102.1, 99, 2.0) is None     # at the high but a small gain
    assert g.judge_spike(106, 100, 112, 99, 5.0) is None       # 6 % is routine for a 5 % ATR name... below the 6.0 limit
    assert g.judge_spike(None, 100, 106, 99, 2.0) is None      # missing data = no opinion
    assert g.judge_spike(106, 100, 99, 99, 2.0) is None        # zero range = no opinion


def test_range_used_up_refuses_when_little_room_is_left():
    # low 100, ATR 2 % -> expected high 104; price 103.5 leaves 0.48 % against a 2.5 % target (needs 1.25 %)
    assert g.judge_range(103.5, 100, 2.0, 2.5).startswith("GAIN_ROOM:RANGE")
    assert g.judge_range(101.0, 100, 2.0, 2.5) is None         # 2.97 % left
    assert g.judge_range(103.5, 100, None, 2.5) is None        # no ATR = no opinion
    with mock.patch.object(config, "GAIN_ROOM_MIN_TARGET_FRAC", 0.0, create=True):
        assert g.judge_range(103.5, 100, 2.0, 2.5) is None


@_live
def test_modes_live_log_off():
    t = _tick(106, 100, 106.2, 99)
    with mock.patch.object(g, "_atr_pct", return_value=2.0):
        assert g.spike_reason_tick(t)
        with mock.patch.object(config, "SPIKE_GUARD_MODE", "log", create=True):
            assert g.spike_reason_tick(t) is None
        with mock.patch.object(config, "SPIKE_GUARD_MODE", "off", create=True):
            assert g.spike_reason_tick(t) is None
        assert g.range_reason_tick(_tick(103.5, 100, 103.6, 100), 2.5)
        with mock.patch.object(config, "GAIN_ROOM_MODE", "off", create=True):
            assert g.range_reason_tick(_tick(103.5, 100, 103.6, 100), 2.5) is None


@_live
def test_never_raises_and_fails_open():
    assert g.spike_reason_tick(None) is None and g.range_reason_tick(None, 2.5) is None
    with mock.patch.object(g, "_atr_pct", side_effect=RuntimeError("boom")):
        assert g.spike_reason_tick(_tick(106, 100, 106.2, 99)) is None


def test_gate_is_wired_into_entry_and_on_the_calibration_page():
    from pathlib import Path
    from control.catalog import CATALOG
    src = (Path(__file__).resolve().parent.parent / "entry_engine" / "entry.py").read_text(encoding="utf-8")
    assert "day_spike_gate.spike_reason_tick(tick)" in src and "day_spike_gate.range_reason_tick(tick, target_pct)" in src
    flags = {f.key: f for f in CATALOG}
    for key in ("SPIKE_GUARD_MODE", "GAIN_ROOM_MODE"):
        assert key in flags and flags[key].shipped == "live" and getattr(config, key) == "live"
