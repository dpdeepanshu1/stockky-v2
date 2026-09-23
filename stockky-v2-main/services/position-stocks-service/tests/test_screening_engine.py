"""
tests/test_screening_engine.py — offline tests for screening/engine.py, the
rolling-window momentum scanner that ranks every candidate the bot may buy.

Every gate (volume floor, spread cap, open-symbol exclusion, per-window
threshold) and every score multiplier (day-range, VWAP extension, momentum
consistency, volume weight, window conviction) is pinned with a scenario that
isolates it, and the final ranking is checked end to end.

Offline: the WebSocket tick feed is replaced by an in-memory rig.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_screening_engine.py -q --cov=screening --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from feed import ws_client
from screening import engine

NOW = 10_000.0


class Feed:
    def __init__(self):
        self.buffers: dict[str, list] = {}
        self.volumes: dict[str, int] = {}
        self.quotes: dict[str, tuple] = {}

    def add(self, symbol, ticks, volume=100_000, quote=None):
        self.buffers[symbol] = list(ticks)
        self.volumes[symbol] = volume
        if quote is not None:
            self.quotes[symbol] = quote
        return symbol


@pytest.fixture()
def feed(monkeypatch):
    f = Feed()
    monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: list(f.buffers.get(s, [])))
    monkeypatch.setattr(ws_client, "get_last_volume", lambda s: f.volumes.get(s, 0))
    monkeypatch.setattr(ws_client, "get_best_bid_ask", lambda s: f.quotes.get(s))
    monkeypatch.setattr(ws_client, "_token_to_symbol", {})
    monkeypatch.setattr(ws_client, "_tick_buffers", f.buffers)
    monkeypatch.setattr(engine, "_WINDOW_THRESHOLDS", {1: 0.7, 5: 1.0, 15: 1.5, 60: 2.5})
    monkeypatch.setattr(engine, "_volume_accum", defaultdict(int))
    monkeypatch.setattr(engine, "_tick_timestamps", defaultdict(list))
    for k, v in dict(MIN_AVG_VOLUME=50_000, MAX_SPREAD_PCT=0.5, MIN_PREFERRED_THRESHOLD_RELAX_PCT=15.0).items():
        monkeypatch.setattr(config, k, v)
    return f


def neutral(monkeypatch, cons=0.5, vwap=None):
    """Isolate one multiplier at a time: fix consistency and VWAP."""
    monkeypatch.setattr(engine, "_momentum_consistency", lambda buf, w: cons)
    monkeypatch.setattr(engine, "_vwap_estimate", lambda buf: vwap)


def five_min(last, ref=100.0, mid=None):
    """A buffer whose 5m reference tick is at NOW-300 (price `ref`) and last tick `last`.
    Range position of `last` = (last-min)/(max-min) over all ticks."""
    ticks = [(NOW - 300, ref)]
    if mid is not None:
        ticks.append((NOW - 150, mid))
    ticks.append((NOW, last))
    return ticks


def only(cands, window):
    return [c for c in cands if c.window_minutes == window]


def syms(cands, window=5):
    """Sorted symbols that surfaced in one window. Sparse test buffers also trip the 1m window
    (its reference is the oldest tick before the cutoff), so gate tests look at the 5m window."""
    return sorted(c.symbol for c in cands if c.window_minutes == window)


# ── volume tracker ───────────────────────────────────────────────────────────
class TestVolumeTracker:
    def test_counts_ticks_inside_the_five_minute_window(self, feed):
        for t in (0.0, 100.0, 200.0):
            engine._update_volume("ABC", t)
        assert engine._volume_accum["ABC"] == 3

    def test_old_ticks_fall_out(self, feed):
        for t in (0.0, 100.0, 250.0, 301.0):
            engine._update_volume("ABC", t)
        assert engine._volume_accum["ABC"] == 3                      # t=0 is older than 301-300

    def test_tick_exactly_at_the_cutoff_is_kept(self, feed):
        engine._update_volume("ABC", 0.0)
        engine._update_volume("ABC", 300.0)
        assert engine._volume_accum["ABC"] == 2

    def test_symbols_are_independent(self, feed):
        engine._update_volume("AAA", 1.0)
        engine._update_volume("BBB", 2.0)
        engine._update_volume("BBB", 3.0)
        assert (engine._volume_accum["AAA"], engine._volume_accum["BBB"]) == (1, 2)

    def test_on_tick_hook_feeds_the_tracker(self, feed):
        engine.on_tick_hook("ABC", 100.0, 5, 10.0)
        assert engine._volume_accum["ABC"] == 1


# ── pure helpers ─────────────────────────────────────────────────────────────
class TestSpread:
    def test_percent_of_ltp(self, feed):
        feed.quotes["ABC"] = (99.9, 100.1)
        assert engine._spread_pct("ABC", 100.0) == pytest.approx(0.2)

    @pytest.mark.parametrize("quote", [None, (0, 100.0), (100.0, 0), (100.0, 100.0), (101.0, 100.0)])
    def test_unusable_depth_is_unknown_never_zero(self, feed, quote):
        if quote is not None:
            feed.quotes["ABC"] = quote
        assert engine._spread_pct("ABC", 100.0) is None

    def test_non_positive_ltp_is_unknown(self, feed):
        feed.quotes["ABC"] = (99.0, 101.0)
        assert engine._spread_pct("ABC", 0.0) is None


class TestRollingPctChange:
    def test_change_versus_the_tick_at_the_window_start(self, feed):
        feed.add("ABC", [(NOW - 300, 100.0), (NOW - 100, 101.0), (NOW, 103.0)])
        assert engine._rolling_pct_change("ABC", 5) == pytest.approx(3.0)

    def test_reference_is_the_latest_tick_at_or_before_the_cutoff(self, feed):
        feed.add("ABC", [(NOW - 500, 90.0), (NOW - 301, 100.0), (NOW - 299, 50.0), (NOW, 110.0)])
        assert engine._rolling_pct_change("ABC", 5) == pytest.approx(10.0)

    def test_tick_exactly_at_the_cutoff_counts(self, feed):
        feed.add("ABC", [(NOW - 300, 100.0), (NOW, 102.0)])
        assert engine._rolling_pct_change("ABC", 5) == pytest.approx(2.0)

    def test_history_shorter_than_the_window_is_unknown(self, feed):
        feed.add("ABC", [(NOW - 299, 100.0), (NOW, 110.0)])
        assert engine._rolling_pct_change("ABC", 5) is None

    def test_needs_two_ticks(self, feed):
        feed.add("ABC", [(NOW, 100.0)])
        assert engine._rolling_pct_change("ABC", 5) is None

    def test_non_positive_reference_is_unknown(self, feed):
        feed.add("ABC", [(NOW - 300, 0.0), (NOW, 100.0)])
        assert engine._rolling_pct_change("ABC", 5) is None

    def test_falls_are_negative(self, feed):
        feed.add("ABC", [(NOW - 300, 100.0), (NOW, 95.0)])
        assert engine._rolling_pct_change("ABC", 5) == pytest.approx(-5.0)


class TestMomentumConsistency:
    def buf(self, prices, start=NOW - 250, step=10.0):
        return [(start + i * step, p) for i, p in enumerate(prices)]

    def test_too_few_ticks_is_neutral(self):
        assert engine._momentum_consistency(self.buf([100, 101, 102]), 5) == 0.5

    def test_too_few_prices_inside_the_window_is_neutral(self):
        old = [(NOW - 5000 + i, 100.0) for i in range(5)] + [(NOW, 101.0)]
        assert engine._momentum_consistency(old, 5) == 0.5

    def test_sustained_uptrend_scores_high(self):
        # open 100, close 110, midpoint 105: 8 of 10 ticks are above it after a quick jump
        b = self.buf([100, 106, 107, 107, 108, 108, 109, 109, 110, 110])
        assert engine._momentum_consistency(b, 5) == pytest.approx(0.9)

    def test_spike_then_drop_scores_low(self):
        # rockets then fades to just above the open: most ticks sit below the midpoint
        b = self.buf([100, 100, 100, 100, 100, 100, 100, 100, 130, 101])
        assert engine._momentum_consistency(b, 5) == pytest.approx(0.2)

    def test_a_tick_exactly_at_the_midpoint_counts_as_above(self):
        # open 100, close 110 -> midpoint 105; the two 105 ticks and the 110 are "above" (>=): 3/4
        assert engine._momentum_consistency(self.buf([100, 105, 105, 110]), 5) == pytest.approx(0.75)

    def test_result_is_always_a_fraction(self):
        for prices in ([1, 2, 3, 4], [4, 3, 2, 1], [5, 5, 5, 5], [1, 9, 1, 9, 1]):
            v = engine._momentum_consistency(self.buf(prices), 5)
            assert 0.0 <= v <= 1.0


class TestVwap:
    def test_mean_of_prices(self):
        assert engine._vwap_estimate([(1, 100.0), (2, 110.0)]) == 105.0

    def test_ignores_non_positive_prices(self):
        assert engine._vwap_estimate([(1, 0.0), (2, 110.0), (3, -4.0)]) == 110.0

    def test_empty_is_none(self):
        assert engine._vwap_estimate([]) is None
        assert engine._vwap_estimate([(1, 0.0)]) is None


def test_candidate_label():
    c = engine.Candidate("ABC", 15, 2.0, 100.0, 10)
    assert c.window_label == "15m" and c.composite_score == 0.0


# ── scan: gates ──────────────────────────────────────────────────────────────
class TestScanGates:
    def test_a_qualifying_symbol_is_returned_with_its_fields(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("ABC", five_min(103.0, mid=101.0))
        (c,) = only(engine.scan(), 5)
        assert (c.symbol, c.window_minutes, c.current_ltp) == ("ABC", 5, 103.0)
        assert c.pct_change == pytest.approx(3.0)

    def test_below_threshold_is_not_a_candidate(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("ABC", five_min(100.6))                              # below BOTH the 0.7 and 1.0 thresholds
        assert engine.scan() == []

    def test_exactly_at_threshold_is_a_candidate(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("ABC", five_min(101.0))
        assert syms(engine.scan()) == ["ABC"]

    def test_falling_stock_is_never_a_candidate(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("ABC", five_min(90.0))
        assert engine.scan() == []

    def test_symbols_already_held_are_excluded(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("HELD", five_min(103.0))
        feed.add("FREE", five_min(103.0))
        assert {c.symbol for c in engine.scan(open_symbols={"HELD"})} == {"FREE"}

    def test_short_buffer_and_dead_price_are_skipped(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("ONE", [(NOW, 103.0)])
        feed.add("ZERO", [(NOW - 300, 100.0), (NOW, 0.0)])
        assert engine.scan() == []

    def test_volume_floor(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("THIN", five_min(103.0), volume=49_999)
        feed.add("EDGE", five_min(103.0), volume=50_000)
        assert {c.symbol for c in engine.scan()} == {"EDGE"}

    def test_volume_floor_can_be_disabled(self, feed, monkeypatch):
        neutral(monkeypatch)
        monkeypatch.setattr(config, "MIN_AVG_VOLUME", 0)
        feed.add("THIN", five_min(103.0), volume=0)
        assert syms(engine.scan()) == ["THIN"]

    def test_spread_cap(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("WIDE", five_min(103.0), quote=(102.0, 103.6))             # 1.55%
        feed.add("EDGE", five_min(103.0), quote=(102.75, 103.25))            # 0.5% exactly
        feed.add("TIGHT", five_min(103.0), quote=(102.9, 103.1))
        assert syms(engine.scan()) == ["EDGE", "TIGHT"]

    def test_unknown_spread_fails_open(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("NODEPTH", five_min(103.0))                                 # no quote at all
        assert syms(engine.scan()) == ["NODEPTH"]

    def test_spread_cap_can_be_disabled(self, feed, monkeypatch):
        neutral(monkeypatch)
        monkeypatch.setattr(config, "MAX_SPREAD_PCT", 0)
        feed.add("WIDE", five_min(103.0), quote=(90.0, 110.0))
        assert syms(engine.scan()) == ["WIDE"]

    def test_falls_back_to_buffer_keys_when_no_subscription_map(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("ABC", five_min(103.0))
        assert ws_client._token_to_symbol == {} and syms(engine.scan()) == ["ABC"]

    def test_subscription_map_wins_when_present(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("ABC", five_min(103.0))
        feed.add("XYZ", five_min(103.0))
        monkeypatch.setattr(ws_client, "_token_to_symbol", {"1": "ABC"})
        assert {c.symbol for c in engine.scan()} == {"ABC"}


# ── scan: multipliers ────────────────────────────────────────────────────────
def score_of(feed, monkeypatch, ticks, cons=0.5, vwap=None, volume=100_000, window=5):
    neutral(monkeypatch, cons=cons, vwap=vwap)
    feed.add("ABC", ticks, volume=volume)
    (c,) = only(engine.scan(), window)
    return c


class TestScoreMultipliers:
    # ticks: ref 100 @-300, mid 110 @-150, last X -> range position (X-100)/10, pct = X-100 (%)
    @pytest.mark.parametrize("last,rpos_mult", [
        (108.4, 0.75),     # 0.84  (soft band starts at 0.70)
        (108.5, 0.50),     # 0.85  heavy penalty (>=)
        (107.0, 0.75),     # 0.70  soft penalty (>=)
        (106.9, 1.0),      # 0.69
        (105.0, 1.0),      # 0.50
        (102.1, 1.0),      # 0.21
        (102.0, 1.20),     # 0.20  near-low bonus (<=)
    ])
    def test_day_range_multiplier(self, feed, monkeypatch, last, rpos_mult):
        c = score_of(feed, monkeypatch, five_min(last, mid=110.0))
        pct = (last - 100.0)
        assert c.composite_score == pytest.approx(pct * 2.0 * rpos_mult, abs=1e-3)   # volume 100k/50k = 2.0

    def test_vwap_extension_penalty_at_exactly_two_percent(self, feed, monkeypatch):
        # ltp 102, vwap 100 -> +2.0% (>=): penalised. rpos 0.2 -> 1.2x range bonus. pct 2.0, volume 2.0
        c = score_of(feed, monkeypatch, five_min(102.0, mid=110.0), vwap=100.0)
        assert c.composite_score == pytest.approx(2.0 * 2.0 * 1.2 * 0.80)

    def test_vwap_just_below_the_extension_line_is_not_penalised(self, feed, monkeypatch):
        c = score_of(feed, monkeypatch, five_min(101.99, mid=110.0), vwap=100.0)
        assert c.composite_score == pytest.approx(1.99 * 2.0 * 1.2, abs=1e-3)

    def test_missing_vwap_is_not_a_penalty(self, feed, monkeypatch):
        c = score_of(feed, monkeypatch, five_min(105.0, mid=110.0), vwap=None)
        assert c.composite_score == pytest.approx(10.0)

    @pytest.mark.parametrize("cons,mult", [(0.65, 1.15), (0.9, 1.15), (0.6499, 1.0), (0.5, 1.0),
                                           (0.35, 1.0), (0.3499, 0.80), (0.0, 0.80)])
    def test_momentum_consistency_multiplier(self, feed, monkeypatch, cons, mult):
        c = score_of(feed, monkeypatch, five_min(105.0, mid=110.0), cons=cons)
        assert c.composite_score == pytest.approx(5.0 * 2.0 * mult)

    @pytest.mark.parametrize("volume,weight", [(50_000, 1.0), (75_000, 1.5), (100_000, 2.0),
                                               (150_000, 3.0), (10_000_000, 3.0)])
    def test_volume_weight_scales_then_caps_at_three(self, feed, monkeypatch, volume, weight):
        c = score_of(feed, monkeypatch, five_min(105.0, mid=110.0), volume=volume)
        assert c.composite_score == pytest.approx(5.0 * weight)

    def test_volume_weight_without_a_floor_uses_one_share_as_the_base(self, feed, monkeypatch):
        monkeypatch.setattr(config, "MIN_AVG_VOLUME", 0)
        c = score_of(feed, monkeypatch, five_min(105.0, mid=110.0), volume=0)
        assert c.composite_score == pytest.approx(5.0 * 1.0)


class TestWindows:
    def ladder(self, feed):
        n = NOW
        feed.add("ABC", [(n - 3600, 100.0), (n - 900, 100.0), (n - 300, 100.0), (n - 60, 100.0), (n, 110.0)],
                 volume=200_000)

    def test_each_window_has_its_own_conviction_and_volume_cap(self, feed, monkeypatch):
        neutral(monkeypatch)                                          # last tick is the day high -> range 0.5x
        self.ladder(feed)
        by_w = {c.window_minutes: c.composite_score for c in engine.scan()}
        assert by_w == {
            1: pytest.approx(10 * 2.0 * 0.5 * 0.90),                   # 1m volume weight capped at 2.0
            5: pytest.approx(10 * 3.0 * 0.5 * 1.00),
            15: pytest.approx(10 * 3.0 * 0.5 * 1.05),
            60: pytest.approx(10 * 3.0 * 0.5 * 1.10),
        }

    def test_results_are_sorted_best_first(self, feed, monkeypatch):
        neutral(monkeypatch)
        self.ladder(feed)
        cands = engine.scan()
        assert [c.window_minutes for c in cands] == [60, 15, 5, 1]
        assert [c.composite_score for c in cands] == sorted((c.composite_score for c in cands), reverse=True)

    def test_best_symbol_wins_across_windows_and_symbols(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("SMALL", five_min(102.0, mid=110.0))
        feed.add("BIG", five_min(106.0, mid=110.0))
        assert [c.symbol for c in engine.scan()] == ["BIG", "SMALL"]

    def test_one_minute_window_never_uses_a_threshold_below_the_noise_floor(self, feed, monkeypatch):
        # import-time rule: max(MIN_PCT_CHANGE_1M, 0.7)
        monkeypatch.setattr(config, "MIN_PCT_CHANGE_1M", 0.1)
        import importlib
        reloaded = importlib.reload(engine)
        try:
            assert reloaded._WINDOW_THRESHOLDS[1] == 0.7
        finally:
            monkeypatch.setattr(config, "MIN_PCT_CHANGE_1M", 0.5)
            importlib.reload(engine)


class TestUnderPreferred:
    def test_relaxed_thresholds_admit_borderline_candidates(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("ABC", five_min(100.9, mid=110.0))                    # 0.9% < 1.0% but >= 0.85%
        assert engine.scan() == []
        assert len(engine.scan(under_preferred=True)) == 1

    def test_one_minute_stays_at_the_noise_floor_even_when_relaxed(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("ABC", [(NOW - 60, 100.0), (NOW, 100.65)])
        assert only(engine.scan(under_preferred=True), 1) == []

    def test_relaxation_is_capped_at_fifty_percent(self, feed, monkeypatch):
        neutral(monkeypatch)
        monkeypatch.setattr(config, "MIN_PREFERRED_THRESHOLD_RELAX_PCT", 90.0)   # clamps to 50% -> 0.5%
        feed.add("LOW", five_min(100.4, mid=110.0))
        feed.add("OK", five_min(100.5, mid=110.0))
        assert [c.symbol for c in engine.scan(under_preferred=True)] == ["OK"]

    def test_negative_relaxation_is_ignored(self, feed, monkeypatch):
        neutral(monkeypatch)
        monkeypatch.setattr(config, "MIN_PREFERRED_THRESHOLD_RELAX_PCT", -20.0)
        feed.add("ABC", five_min(101.1, mid=110.0))                      # passes 1.0, would fail a "tightened" 1.2
        assert syms(engine.scan(under_preferred=True)) == ["ABC"]

    def test_relaxing_never_touches_the_other_gates(self, feed, monkeypatch):
        neutral(monkeypatch)
        feed.add("THIN", five_min(100.9, mid=110.0), volume=10)
        feed.add("WIDE", five_min(100.9, mid=110.0), quote=(90.0, 110.0))
        assert engine.scan(under_preferred=True) == []
