"""tests/test_group219_ranking_v2.py - scalp ranking v2 in screening/engine.py (group 219, review item 4).

v2 caps how much the size of the move counts, replaces the saturated volume weight with liquidity x volume pace, and
keeps the legacy formula behind SCAN_RANKING_V2_ENABLED=0 (that path is pinned in tests/test_screening_engine.py).
Offline: the WebSocket feed is an in-memory rig; volume snapshots are written with engine._record_volume.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_group219_ranking_v2.py -q --cov=screening --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from feed import ws_client
from screening import engine
from tz_utils import IST

NOW = 10_000.0


def ist(h, m, s=0):
    return datetime(2026, 10, 7, h, m, s, tzinfo=IST).timestamp()


class Feed:
    def __init__(self):
        self.buffers, self.volumes = {}, {}

    def add(self, symbol, ticks, volume=1_000_000):
        self.buffers[symbol] = list(ticks)
        self.volumes[symbol] = volume


@pytest.fixture()
def feed(monkeypatch):
    f = Feed()
    monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: list(f.buffers.get(s, [])))
    monkeypatch.setattr(ws_client, "get_last_volume", lambda s: f.volumes.get(s, 0))
    monkeypatch.setattr(ws_client, "get_best_bid_ask", lambda s: None)
    monkeypatch.setattr(ws_client, "_token_to_symbol", {})
    monkeypatch.setattr(ws_client, "_tick_buffers", f.buffers)
    monkeypatch.setattr(engine, "_WINDOW_THRESHOLDS", {1: 0.7, 5: 1.0, 15: 1.5, 60: 2.5})
    monkeypatch.setattr(engine, "_volume_accum", defaultdict(int))
    monkeypatch.setattr(engine, "_tick_timestamps", defaultdict(list))
    monkeypatch.setattr(engine, "_vol_hist", defaultdict(lambda: engine.deque(maxlen=engine._VOL_HIST_MAX)))
    # neutral range / VWAP / consistency so only the terms under test move the score
    monkeypatch.setattr(engine, "_RPOS_HEAVY_PENALTY", (2.0, 1.0))
    monkeypatch.setattr(engine, "_RPOS_SOFT_PENALTY", (2.0, 1.0))
    monkeypatch.setattr(engine, "_RPOS_NEAR_LOW_BONUS", (-1.0, 1.0))
    monkeypatch.setattr(engine, "_vwap_estimate", lambda buf: None)
    monkeypatch.setattr(engine, "_momentum_consistency", lambda buf, w: 0.5)
    for k, v in dict(
        MIN_AVG_VOLUME=50_000, MAX_SPREAD_PCT=0.5, MIN_PREFERRED_THRESHOLD_RELAX_PCT=15.0,
        DISABLED_SCAN_WINDOWS=frozenset(), SCAN_RANKING_V2_ENABLED=True, SCAN_PCT_CAP_MULT=3.0,
        SCAN_RVOL_MIN_WEIGHT=0.5, SCAN_RVOL_MAX_WEIGHT=2.0, SCAN_RVOL_MIN_BASELINE_MIN=10.0, SCAN_RVOL_SAMPLE_S=5.0,
    ).items():
        monkeypatch.setattr(config, k, v)
    return f


def ticks(last, ref=100.0):
    """5m reference tick at NOW-300 (price ref), last tick at NOW: pct = (last-ref)/ref*100."""
    return [(NOW - 300, ref), (NOW, last)]


def c5(cands, symbol):
    return next(c for c in cands if c.symbol == symbol and c.window_minutes == 5)


def pace(symbol, *, before=750_000, recent=150_000, ref_at=ist(10, 30), now_at=ist(10, 35)):
    """Two snapshots 5 minutes apart: `before` shares by ref_at, `before + recent` by now_at."""
    engine._record_volume(symbol, before, ref_at)
    engine._record_volume(symbol, before + recent, now_at)


# -- the size of the move is capped ---------------------------------------------------
class TestPctCap:
    def test_a_move_beyond_the_cap_scores_the_same_as_one_at_the_cap(self, feed):
        feed.add("AAA", ticks(106.0))      # +6%
        feed.add("BBB", ticks(109.0))      # +9%
        feed.add("CCC", ticks(103.0))      # +3% = exactly 3 x the 1.0% threshold
        cands = engine.scan()
        assert c5(cands, "AAA").composite_score == pytest.approx(3.0)       # liquidity 1.0, every other term 1.0
        assert c5(cands, "BBB").composite_score == pytest.approx(3.0)
        assert c5(cands, "CCC").composite_score == pytest.approx(3.0)
        assert c5(cands, "BBB").pct_change == pytest.approx(9.0)            # the real move is still reported

    def test_below_the_cap_the_score_still_follows_the_move(self, feed):
        feed.add("AAA", ticks(101.5))
        feed.add("BBB", ticks(102.5))
        cands = engine.scan()
        assert c5(cands, "AAA").composite_score == pytest.approx(1.5)
        assert c5(cands, "BBB").composite_score == pytest.approx(2.5)

    def test_cap_of_zero_restores_proportional_scoring(self, feed, monkeypatch):
        monkeypatch.setattr(config, "SCAN_PCT_CAP_MULT", 0.0)
        feed.add("AAA", ticks(109.0))
        assert c5(engine.scan(), "AAA").composite_score == pytest.approx(9.0)

    def test_cap_is_per_window_threshold(self, feed):
        # 15m threshold 1.5 -> cap 4.5; a 6% 15m move scores 4.5
        feed.add("AAA", [(NOW - 900, 100.0), (NOW, 106.0)])
        c15 = next(c for c in engine.scan() if c.window_minutes == 15)
        assert c15.composite_score == pytest.approx(4.5 * 1.05)             # x window conviction 1.05


# -- liquidity replaces the saturated volume weight -----------------------------------
class TestLiquidity:
    @pytest.mark.parametrize("volume,liq", [(50_000, 1 / 3), (75_000, 0.5), (100_000, 2 / 3), (150_000, 1.0),
                                            (10_000_000, 1.0)])
    def test_liquidity_rises_to_one_at_three_times_the_floor(self, feed, volume, liq):
        feed.add("AAA", ticks(102.0), volume=volume)
        assert c5(engine.scan(), "AAA").composite_score == pytest.approx(2.0 * liq, abs=1e-3)

    def test_every_liquid_symbol_gets_the_same_liquidity(self, feed):
        feed.add("AAA", ticks(102.0), volume=200_000)
        feed.add("BBB", ticks(102.0), volume=9_000_000)
        cands = engine.scan()
        assert c5(cands, "AAA").composite_score == c5(cands, "BBB").composite_score


# -- volume pace ------------------------------------------------------------------------
class TestRelativeVolume:
    def test_pace_is_window_rate_over_the_earlier_session_rate(self, feed):
        # 750k by 10:30 = 75 min after 09:15 -> 10,000/min; then 150k in 5 min = 30,000/min -> 3.0
        pace("AAA")
        assert engine._relative_volume("AAA", 5) == pytest.approx(3.0)

    def test_slower_than_the_session_so_far_is_below_one(self, feed):
        pace("AAA", recent=20_000)                    # 4,000/min vs 10,000/min
        assert engine._relative_volume("AAA", 5) == pytest.approx(0.4)

    def test_score_uses_the_pace_clamped_to_the_maximum(self, feed):
        pace("AAA")                                   # pace 3.0 -> clamped to 2.0
        feed.add("AAA", ticks(102.0))
        c = c5(engine.scan(), "AAA")
        assert c.rvol == pytest.approx(3.0) and c.composite_score == pytest.approx(2.0 * 2.0)

    def test_score_uses_the_pace_clamped_to_the_minimum(self, feed):
        pace("AAA", recent=20_000)                    # 0.4 -> clamped to 0.5
        feed.add("AAA", ticks(102.0))
        c = c5(engine.scan(), "AAA")
        assert c.rvol == pytest.approx(0.4) and c.composite_score == pytest.approx(2.0 * 0.5)

    def test_a_pace_inside_the_clamp_is_used_as_is(self, feed):
        pace("AAA", recent=62_500)                    # 12,500/min vs 10,000/min = 1.25
        feed.add("AAA", ticks(102.0))
        assert c5(engine.scan(), "AAA").composite_score == pytest.approx(2.0 * 1.25)

    def test_unknown_pace_is_neutral(self, feed):
        feed.add("AAA", ticks(102.0))                 # no snapshots at all
        c = c5(engine.scan(), "AAA")
        assert c.rvol is None and c.composite_score == pytest.approx(2.0)

    def test_history_that_does_not_reach_back_far_enough_is_unknown(self, feed):
        engine._record_volume("AAA", 900_000, ist(10, 35))
        engine._record_volume("AAA", 950_000, ist(10, 36))
        assert engine._relative_volume("AAA", 5) is None          # no snapshot at or before 10:31

    def test_a_window_starting_inside_the_opening_burst_is_unknown(self, feed):
        pace("AAA", before=300_000, recent=100_000, ref_at=ist(9, 20), now_at=ist(9, 25))   # baseline = 5 min
        assert engine._relative_volume("AAA", 5) is None

    def test_baseline_at_exactly_the_minimum_is_trusted(self, feed):
        pace("AAA", before=100_000, recent=50_000, ref_at=ist(9, 25), now_at=ist(9, 30))    # 10 min -> 10,000/min
        assert engine._relative_volume("AAA", 5) == pytest.approx(10_000 / 10_000)          # (50k/5)/(100k/10)

    def test_a_volume_reset_is_unknown(self, feed):
        pace("AAA", before=750_000, recent=-700_000)              # cumulative volume went DOWN
        assert engine._relative_volume("AAA", 5) is None

    def test_symbols_are_independent(self, feed):
        pace("AAA")
        assert engine._relative_volume("BBB", 5) is None

    def test_session_open_is_0915_ist(self):
        assert engine._session_open_ts(ist(11, 0)) == ist(9, 15)


class TestVolumeSnapshots:
    def test_snapshots_are_spaced_by_the_sample_interval(self, feed):
        engine._record_volume("AAA", 100, 1000.0)
        engine._record_volume("AAA", 110, 1003.0)         # < 5 s: dropped
        engine._record_volume("AAA", 120, 1005.0)         # exactly 5 s: kept
        assert list(engine._vol_hist["AAA"]) == [(1000.0, 100.0), (1005.0, 120.0)]

    @pytest.mark.parametrize("vol", [0, None, -5])
    def test_missing_or_non_positive_volume_is_ignored(self, feed, vol):
        engine._record_volume("AAA", vol, 1000.0)
        assert "AAA" not in engine._vol_hist or not engine._vol_hist["AAA"]

    def test_the_tick_hook_records_volume(self, feed):
        engine.on_tick_hook("AAA", 100.0, 5_000, 1000.0)
        assert list(engine._vol_hist["AAA"]) == [(1000.0, 5000.0)]

    def test_history_is_bounded(self, feed):
        for i in range(engine._VOL_HIST_MAX + 50):
            engine._record_volume("AAA", 1000 + i, 1000.0 + i * 10)
        assert len(engine._vol_hist["AAA"]) == engine._VOL_HIST_MAX

    def test_a_bookkeeping_error_never_reaches_the_tick_path(self, feed, monkeypatch):
        monkeypatch.setattr(config, "SCAN_RVOL_SAMPLE_S", "bad")
        engine._record_volume("AAA", 100, 1000.0)
        engine._record_volume("AAA", 100, 1001.0)         # second call compares against the bad value -> swallowed


# -- end to end ranking --------------------------------------------------------------------
class TestRankingOrder:
    def _setup(self, feed):
        feed.add("STEADY", ticks(102.5))        # +2.5%, trading faster than its own day so far
        pace("STEADY", recent=90_000)           # 18,000/min vs 10,000/min = 1.8
        feed.add("CHASED", ticks(108.0))        # +8%, volume has dried up
        pace("CHASED", recent=30_000)           # 6,000/min vs 10,000/min = 0.6

    def test_v2_prefers_the_steady_high_pace_mover_over_the_biggest_mover(self, feed):
        self._setup(feed)
        top = [c for c in engine.scan() if c.window_minutes == 5]
        assert [c.symbol for c in top] == ["STEADY", "CHASED"]
        assert top[0].composite_score == pytest.approx(2.5 * 1.8)
        assert top[1].composite_score == pytest.approx(3.0 * 0.6)     # 8% capped at 3.0

    def test_the_legacy_formula_still_chases_the_biggest_mover(self, feed, monkeypatch):
        monkeypatch.setattr(config, "SCAN_RANKING_V2_ENABLED", False)
        self._setup(feed)
        top = [c for c in engine.scan() if c.window_minutes == 5]
        assert [c.symbol for c in top] == ["CHASED", "STEADY"]
        assert top[0].composite_score == pytest.approx(8.0 * 3.0) and top[0].rvol is None   # volume 1M -> weight 3.0

    def test_rank_is_still_sorted_descending(self, feed):
        self._setup(feed)
        scores = [c.composite_score for c in engine.scan()]
        assert scores == sorted(scores, reverse=True)

    def test_the_liquidity_floor_still_applies(self, feed):
        feed.add("THIN", ticks(103.0), volume=10_000)             # below MIN_AVG_VOLUME 50,000
        assert [c for c in engine.scan() if c.symbol == "THIN"] == []

    def test_candidate_pct_change_is_the_real_move_not_the_capped_one(self, feed):
        feed.add("CHASED", ticks(108.0))
        assert c5(engine.scan(), "CHASED").pct_change == pytest.approx(8.0)
