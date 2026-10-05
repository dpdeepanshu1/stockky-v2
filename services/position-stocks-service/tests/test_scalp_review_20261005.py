"""
tests/test_scalp_review_20261005.py - offline tests for the 2026-10-05 scalp review changes
(position-stocks-service): config defaults, exchange day-stats parsing, breakeven cap,
bar-ATR default, paused scan windows, exchange-range use in adaptive + screener.

Entry guards, the no-follow-through exit and the reconcile fill fixes are tested next to
their modules (test_entry.py, test_eod_squareoff.py, test_reconcile.py).

Run from services/position-stocks-service:
    python3 -m pytest tests/test_scalp_review_20261005.py -q
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from feed import ws_client
from orders import adaptive
from screening import engine

SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# -- config defaults (fresh interpreter, none of the env vars set) ----------------
class TestConfigDefaults:
    NAMES = ["ADAPTIVE_BAR_ATR_ENABLED", "STAGNATION_EXIT_MINUTES", "NO_FOLLOWTHROUGH_EXIT_ENABLED",
             "NO_FOLLOWTHROUGH_EXIT_MINUTES", "NO_FOLLOWTHROUGH_MIN_GAIN_PCT", "BREAKEVEN_TRIGGER_MAX_PCT",
             "ENTRY_MAX_SLIPPAGE_PCT", "ENTRY_MAX_TICK_AGE_S", "MAX_DAY_GAIN_PCT",
             "ENTRY_FILL_SLIPPAGE_ALERT_PCT", "DISABLED_SCAN_WINDOWS"]

    def _run(self, **env):
        base = {k: v for k, v in os.environ.items() if k not in self.NAMES}
        base.update(env)
        code = "import config;print(repr([getattr(config,n) for n in %r]))" % (self.NAMES,)
        out = subprocess.run([sys.executable, "-c", code], cwd=SERVICE_DIR, env=base,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return eval(out.stdout.strip(), {"frozenset": frozenset})

    def test_shipped_defaults(self):
        vals = dict(zip(self.NAMES, self._run()))
        assert vals["ADAPTIVE_BAR_ATR_ENABLED"] is True
        assert vals["STAGNATION_EXIT_MINUTES"] == 30.0
        assert vals["NO_FOLLOWTHROUGH_EXIT_ENABLED"] is True
        assert vals["NO_FOLLOWTHROUGH_EXIT_MINUTES"] == 20.0 and vals["NO_FOLLOWTHROUGH_MIN_GAIN_PCT"] == 0.5
        assert vals["BREAKEVEN_TRIGGER_MAX_PCT"] == 1.0
        assert vals["ENTRY_MAX_SLIPPAGE_PCT"] == 0.5 and vals["ENTRY_MAX_TICK_AGE_S"] == 45.0
        assert vals["MAX_DAY_GAIN_PCT"] == 7.0 and vals["ENTRY_FILL_SLIPPAGE_ALERT_PCT"] == 1.0
        assert vals["DISABLED_SCAN_WINDOWS"] == frozenset({1, 15})

    def test_env_can_turn_the_new_behaviour_back_off(self):
        vals = dict(zip(self.NAMES, self._run(ADAPTIVE_BAR_ATR_ENABLED="false", DISABLED_SCAN_WINDOWS="none",
                                              NO_FOLLOWTHROUGH_EXIT_ENABLED="0")))
        assert vals["ADAPTIVE_BAR_ATR_ENABLED"] is False and vals["NO_FOLLOWTHROUGH_EXIT_ENABLED"] is False
        assert vals["DISABLED_SCAN_WINDOWS"] == frozenset()

    def test_blank_env_values_fall_back_to_defaults(self):
        vals = dict(zip(self.NAMES, self._run(DISABLED_SCAN_WINDOWS="  ", ENTRY_MAX_SLIPPAGE_PCT="")))
        assert vals["DISABLED_SCAN_WINDOWS"] == frozenset({1, 15}) and vals["ENTRY_MAX_SLIPPAGE_PCT"] == 0.5

    @pytest.mark.parametrize("raw,expected", [("1,15", {1, 15}), (" 5 , 60 ", {5, 60}), ("", set()),
                                              (None, set()), ("none", set()), ("5,x,,60", {5, 60})])
    def test_parse_int_set(self, raw, expected):
        assert config._parse_int_set(raw) == frozenset(expected)


# -- ws_client day stats -----------------------------------------------------------
def _frame(open_p=0, high=0, low=0, close=0, length=123):
    head = bytes(91) + struct.pack("<qqqq", open_p, high, low, close)
    return head[:length] if length <= len(head) else head + bytes(length - len(head))


class TestParseDayStats:
    def test_parses_paise_to_rupees(self):
        assert ws_client._parse_day_stats(_frame(5000, 5300, 4900, 4950)) == (50.0, 53.0, 49.0, 49.5)

    def test_zero_fields_become_none(self):
        assert ws_client._parse_day_stats(_frame(5000, 5300, 0, 0)) == (50.0, 53.0, None, None)

    def test_all_zero_is_none(self):
        assert ws_client._parse_day_stats(_frame()) is None

    @pytest.mark.parametrize("length", [0, 51, 122])
    def test_short_frame_is_none(self, length):
        assert ws_client._parse_day_stats(bytes(length)) is None

    def test_negative_values_become_none(self):
        assert ws_client._parse_day_stats(_frame(-5, 5300, 4900, 4950))[0] is None


class TestDayRangeGetters:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.setattr(ws_client, "_day_stats", {})

    def test_unknown_symbol(self):
        assert ws_client.get_day_stats("X") is None and ws_client.get_day_range("X") is None

    def test_range_is_low_high(self):
        ws_client._day_stats["X"] = (150.0, 200.0, 100.0, 140.0)
        assert ws_client.get_day_range("X") == (100.0, 200.0)

    @pytest.mark.parametrize("stats", [(1, None, 5, 1), (1, 5, None, 1), (1, 5, 5, 1), (1, 4, 5, 1)])
    def test_unusable_range_is_none(self, stats):
        ws_client._day_stats["X"] = stats
        assert ws_client.get_day_range("X") is None


# -- adaptive: breakeven cap, bar-ATR on, exchange range -----------------------------
class TestBreakevenCap:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        for k, v in dict(MIN_STOP_PCT=2.0, MAX_STOP_PCT=5.0, MIN_TARGET_PCT=3.0, MAX_TARGET_PCT=8.0,
                         ADAPTIVE_BAR_ATR_ENABLED=False, BREAKEVEN_TRIGGER_MAX_PCT=1.0).items():
            monkeypatch.setattr(config, k, v)
        monkeypatch.setattr(adaptive, "_atr_proxy_pct", lambda s, p: 2.0)
        monkeypatch.setattr(adaptive, "_intraday_range_position", lambda s, p: None)

    def test_trigger_is_capped_at_one_percent(self):
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")        # target 6.6% -> 40% = 2.64%
        assert lv.target_pct == pytest.approx(6.6) and lv.breakeven_trigger_pct == 1.0

    def test_small_target_is_still_forty_percent(self):
        assert adaptive._breakeven_trigger(2.0) == pytest.approx(0.8)

    def test_exactly_at_cap(self):
        assert adaptive._breakeven_trigger(2.5) == 1.0

    def test_zero_cap_disables_the_cap(self, monkeypatch):
        monkeypatch.setattr(config, "BREAKEVEN_TRIGGER_MAX_PCT", 0.0)
        assert adaptive._breakeven_trigger(6.6) == pytest.approx(2.64)

    def test_bar_levels_use_the_cap_too(self, monkeypatch):
        monkeypatch.setattr(config, "ADAPTIVE_BAR_TARGET_MAX_PCT", 3.5)
        monkeypatch.setattr(config, "ADAPTIVE_BAR_TARGET_RR", 1.8)
        monkeypatch.setattr(config, "ADAPTIVE_BAR_STOP_MULT", 1.3)
        monkeypatch.setattr(config, "ADAPTIVE_BAR_STOP_MIN_PCT", 0.8)
        monkeypatch.setattr(config, "ADAPTIVE_BAR_STOP_MAX_PCT", 2.0)
        lv = adaptive._compute_bar_levels(1.5, 100.0, "ABC")     # stop 1.95, target 3.5 -> 40% = 1.4
        assert lv.breakeven_trigger_pct == 1.0


class TestAdaptiveUsesExchangeRange:
    def test_range_position_prefers_exchange_range_over_buffer(self, monkeypatch):
        # buffer alone says LTP 190 is at the top (range 188..190); exchange day range 100..200 says 0.9
        monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: [(i, p) for i, p in enumerate([188.0, 190.0])])
        monkeypatch.setitem(ws_client._day_stats, "ZZ", (150.0, 200.0, 100.0, 140.0))
        try:
            assert adaptive._intraday_range_position("ZZ", 190.0) == pytest.approx(0.9)
        finally:
            ws_client._day_stats.pop("ZZ", None)

    def test_falls_back_to_buffer_without_exchange_stats(self, monkeypatch):
        monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: [(i, p) for i, p in enumerate([100.0, 200.0])])
        assert adaptive._intraday_range_position("NOSTATS", 150.0) == pytest.approx(0.5)


# -- screener: paused windows + exchange range ----------------------------------------
NOW = 10_000.0


class _Feed:
    def __init__(self):
        self.buffers, self.volumes = {}, {}


@pytest.fixture()
def scan_env(monkeypatch):
    f = _Feed()
    monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: list(f.buffers.get(s, [])))
    monkeypatch.setattr(ws_client, "get_last_volume", lambda s: f.volumes.get(s, 0))
    monkeypatch.setattr(ws_client, "get_best_bid_ask", lambda s: None)
    monkeypatch.setattr(ws_client, "_token_to_symbol", {})
    monkeypatch.setattr(ws_client, "_tick_buffers", f.buffers)
    monkeypatch.setattr(ws_client, "_day_stats", {})
    monkeypatch.setattr(engine, "_WINDOW_THRESHOLDS", {1: 0.7, 5: 1.0, 15: 1.5, 60: 2.5})
    monkeypatch.setattr(engine, "_volume_accum", defaultdict(int))
    monkeypatch.setattr(engine, "_tick_timestamps", defaultdict(list))
    monkeypatch.setattr(engine, "_momentum_consistency", lambda buf, w: 0.5)
    monkeypatch.setattr(engine, "_vwap_estimate", lambda buf: None)
    for k, v in dict(MIN_AVG_VOLUME=50_000, MAX_SPREAD_PCT=0.5, MIN_PREFERRED_THRESHOLD_RELAX_PCT=15.0,
                     DISABLED_SCAN_WINDOWS=frozenset()).items():
        monkeypatch.setattr(config, k, v)
    return f


def _add(f, sym, last, ref=100.0):
    f.buffers[sym] = [(NOW - 300, ref), (NOW, last)]
    f.volumes[sym] = 100_000


def _windows(cands, sym="ABC"):
    return sorted(c.window_minutes for c in cands if c.symbol == sym)


class TestPausedWindows:
    def test_all_windows_surface_when_none_paused(self, scan_env):
        _add(scan_env, "ABC", 103.0)
        assert 5 in _windows(engine.scan())

    def test_paused_window_never_surfaces(self, scan_env, monkeypatch):
        _add(scan_env, "ABC", 103.0)
        base = _windows(engine.scan())
        monkeypatch.setattr(config, "DISABLED_SCAN_WINDOWS", frozenset({5}))
        paused = _windows(engine.scan())
        assert 5 in base and 5 not in paused and set(paused) == set(base) - {5}

    def test_default_pause_set_removes_1m_and_15m_only(self, scan_env, monkeypatch):
        _add(scan_env, "ABC", 103.0)
        scan_env.buffers["ABC"] = [(NOW - 3600, 100.0), (NOW - 900, 100.0), (NOW - 300, 100.0), (NOW - 60, 100.0), (NOW, 103.0)]
        monkeypatch.setattr(config, "DISABLED_SCAN_WINDOWS", frozenset({1, 15}))
        assert set(_windows(engine.scan())) == {5, 60}

    def test_under_preferred_relaxation_also_respects_the_pause(self, scan_env, monkeypatch):
        _add(scan_env, "ABC", 100.9)                                # only clears the relaxed 5m threshold
        monkeypatch.setattr(config, "DISABLED_SCAN_WINDOWS", frozenset({5}))
        assert 5 not in _windows(engine.scan(under_preferred=True))


class TestScreenerUsesExchangeRange:
    def test_exchange_range_removes_the_buffer_only_high_penalty(self, scan_env):
        # buffer range is 100..103 so LTP sits AT the high (x0.5); exchange range 90..130 puts it at 0.325 (x1.0)
        _add(scan_env, "ABC", 103.0)
        only5 = lambda: next(c for c in engine.scan() if c.symbol == "ABC" and c.window_minutes == 5)
        penalised = only5().composite_score
        ws_client._day_stats["ABC"] = (100.0, 130.0, 90.0, 99.0)
        assert only5().composite_score == pytest.approx(penalised * 2.0, rel=1e-3)


# -- day-stats plausibility check (group 163) ------------------------------------------
class TestDayStatsPlausibility:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.setattr(ws_client, "_day_stats", {})
        monkeypatch.setattr(ws_client, "_day_stats_accepted", 0)
        monkeypatch.setattr(ws_client, "_day_stats_rejected", 0)
        monkeypatch.setattr(ws_client, "_day_stats_warned", False)

    @pytest.mark.parametrize("ds", [
        (100.0, 105.0, 98.0, 99.0),        # normal
        (None, 105.0, None, None),         # partial frame
        (100.0, 100.3, 98.0, 99.0),        # ltp 100.5 slightly above high: tick timing slack
    ])
    def test_plausible(self, ds):
        assert ws_client._day_stats_plausible(ds, 100.5) is True

    @pytest.mark.parametrize("ds", [
        (1e9, 105.0, 98.0, 99.0),          # garbage open
        (100.0, 105.0, 98.0, 0.01),        # garbage prev close
        (100.0, 97.0, 98.0, 99.0),         # high < low
        (100.0, 90.0, 80.0, 85.0),         # ltp far above high
        (100.0, 130.0, 120.0, 99.0),       # ltp far below low
    ])
    def test_implausible(self, ds):
        assert ws_client._day_stats_plausible(ds, 100.5) is False

    def test_missing_ltp_does_not_reject(self):
        assert ws_client._day_stats_plausible((1e9, 1.0, 5.0, 2.0), 0) is True

    def test_accept_stores_and_counts(self):
        assert ws_client._accept_day_stats("X", (100.0, 105.0, 98.0, 99.0), 100.5)
        assert ws_client.get_day_stats("X") == (100.0, 105.0, 98.0, 99.0)
        assert ws_client._day_stats_accepted == 1

    def test_reject_keeps_previous_and_warns_once(self, caplog):
        ws_client._day_stats["X"] = (100.0, 105.0, 98.0, 99.0)
        bad = (1e9, 105.0, 98.0, 99.0)
        with caplog.at_level("WARNING"):
            assert not ws_client._accept_day_stats("X", bad, 100.5)
            assert not ws_client._accept_day_stats("Y", bad, 100.5)
        assert ws_client.get_day_stats("X") == (100.0, 105.0, 98.0, 99.0)
        assert ws_client.get_day_stats("Y") is None and ws_client.get_day_range("Y") is None
        assert ws_client._day_stats_rejected == 2
        assert len([r for r in caplog.records if "look wrong" in r.getMessage()]) == 1

    def test_status_reports_counters(self):
        ws_client._accept_day_stats("X", (100.0, 105.0, 98.0, 99.0), 100.5)
        ws_client._accept_day_stats("Y", (1e9, 105.0, 98.0, 99.0), 100.5)
        st = ws_client.ws_status()
        assert (st["day_stats_symbols"], st["day_stats_accepted"], st["day_stats_rejected"]) == (1, 1, 1)
