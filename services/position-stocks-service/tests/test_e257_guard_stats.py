"""E257: the spike-chase and gain-room guards count what they decided (held / log_only / passed / no_data / error)."""
from __future__ import annotations

import functools
from types import SimpleNamespace
from unittest import mock

import config
from orders import entry_timing, gain_room, guard_stats

NOW = 1_800_000_000.0


def _fresh(fn):
    @functools.wraps(fn)
    def w():
        guard_stats.reset()
        with mock.patch.object(config, "SPIKE_GUARD_MODE", "live", create=True), mock.patch.object(config, "GAIN_ROOM_MODE", "live", create=True):
            return fn()
    return w


@_fresh
def test_spike_counts_held_log_only_no_data_and_error():
    with mock.patch("feed.ws_client.get_tick_buffer", return_value=[], create=True), mock.patch("screening.prev_day.peek", return_value=None):
        with mock.patch.object(entry_timing, "_day_numbers", return_value=(6.0, 0.95)):
            assert entry_timing.spike_reason("A", 100.0, NOW)                              # held
            with mock.patch.object(config, "SPIKE_GUARD_MODE", "log"):
                assert entry_timing.spike_reason("A", 100.0, NOW) is None                  # log_only
        with mock.patch.object(entry_timing, "_day_numbers", return_value=(None, None)):
            assert entry_timing.spike_reason("A", 100.0, NOW) is None                      # no_data (no day numbers)
        with mock.patch.object(entry_timing, "_day_numbers", return_value=(0.5, 0.5)):
            assert entry_timing.spike_reason("A", 100.0, NOW) is None                      # no_data (cold daily-ATR cache: fixed day limit used)
            with mock.patch("screening.prev_day.peek", return_value=SimpleNamespace(atr_pct=2.0)):
                assert entry_timing.spike_reason("A", 100.0, NOW) is None                  # passed
    with mock.patch.object(entry_timing, "_day_numbers", side_effect=RuntimeError("boom")):
        assert entry_timing.spike_reason("A", 100.0, NOW) is None                          # error
    assert guard_stats.snapshot()["spike"] == {"held": 1, "log_only": 1, "no_data": 2, "passed": 1, "error": 1}


@_fresh
def test_gain_room_counts_no_data_when_no_day_range():
    with mock.patch("feed.bar_store.get_bars", return_value=[]), mock.patch("feed.ws_client.get_day_range", return_value=None, create=True), \
            mock.patch("screening.prev_day.get", return_value=None):
        assert gain_room.reject_reason("X", 100.0, 1.2, 2.4) is None
    assert guard_stats.snapshot()["gain_room"] == {"no_data": 1}


@_fresh
def test_gain_room_counts_passed_held_log_only_and_error():
    with mock.patch("feed.bar_store.get_bars", return_value=[]), mock.patch("feed.ws_client.get_day_range", return_value=(100.0, 101.0), create=True), \
            mock.patch("screening.prev_day.get", return_value=SimpleNamespace(avg_range_pct=2.0)), \
            mock.patch.object(gain_room, "odds_reason", return_value=None):
        with mock.patch.object(gain_room, "range_reason", return_value=None):
            assert gain_room.reject_reason("X", 100.0, 1.2, 2.4) is None                   # passed
        with mock.patch.object(gain_room, "range_reason", return_value="GAIN_ROOM:RANGE x"):
            assert gain_room.reject_reason("X", 100.0, 1.2, 2.4).startswith("GAIN_ROOM")   # held
            with mock.patch.object(config, "GAIN_ROOM_MODE", "log"):
                assert gain_room.reject_reason("X", 100.0, 1.2, 2.4) is None               # log_only
    # an input source that raises is an error, not a silent pass
    with mock.patch("feed.bar_store.get_bars", side_effect=RuntimeError("x")), mock.patch("feed.ws_client.get_day_range", return_value=(100.0, 101.0), create=True), \
            mock.patch("screening.prev_day.get", return_value=SimpleNamespace(avg_range_pct=2.0)), mock.patch.object(gain_room, "range_reason", return_value=None):
        assert gain_room.reject_reason("X", 100.0, 1.2, 2.4) is None
    with mock.patch("feed.bar_store.get_bars", return_value=[]), mock.patch("feed.ws_client.get_day_range", side_effect=RuntimeError("y"), create=True), \
            mock.patch.object(gain_room, "odds_reason", return_value=None):
        assert gain_room.reject_reason("X", 100.0, 1.2, 2.4) is None
    assert guard_stats.snapshot()["gain_room"] == {"passed": 1, "held": 1, "log_only": 1, "error": 2}


@_fresh
def test_off_mode_counts_nothing_and_counter_never_raises():
    with mock.patch.object(config, "SPIKE_GUARD_MODE", "off"), mock.patch.object(config, "GAIN_ROOM_MODE", "off"):
        assert entry_timing.spike_reason("A", 100.0, NOW) is None
        assert gain_room.reject_reason("A", 100.0, 1.2, 2.4) is None
    assert guard_stats.snapshot() == {}
    guard_stats.bump(None, None)       # a bad key must not raise
