"""
tests/test_adaptive.py — offline tests for orders/adaptive.py.

adaptive.compute() decides the stop and target of EVERY entry (and therefore,
through entry.py's sizing, how many shares are bought). The invariants that
must hold for any input: target above entry, stop below entry, stop inside
[MIN_STOP_PCT, MAX_STOP_PCT], target never above MAX_TARGET_PCT, and a
breakeven trigger of exactly 40% of the target.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_adaptive.py -q --cov=orders --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from feed import ws_client
from orders import adaptive


@pytest.fixture(autouse=True)
def pin_config(monkeypatch):
    for k, v in dict(MIN_STOP_PCT=2.0, MAX_STOP_PCT=5.0, MIN_TARGET_PCT=3.0, MAX_TARGET_PCT=8.0).items():
        monkeypatch.setattr(config, k, v)


@pytest.fixture()
def feed(monkeypatch):
    ticks: dict[str, list[float]] = {}
    monkeypatch.setattr(ws_client, "get_tick_buffer",
                        lambda sym: [(i, p) for i, p in enumerate(ticks.get(sym, []))])
    return ticks


def fixed(monkeypatch, atr=None, rng=None):
    monkeypatch.setattr(adaptive, "_atr_proxy_pct", lambda s, p: atr)
    monkeypatch.setattr(adaptive, "_intraday_range_position", lambda s, p: rng)


# ── _atr_proxy_pct ───────────────────────────────────────────────────────────
class TestAtrProxy:
    def test_average_absolute_tick_move_as_percent_of_ltp(self, feed):
        feed["ABC"] = [100.0, 101.0, 100.0, 101.0]                  # ranges 1,1,1 -> avg 1
        assert adaptive._atr_proxy_pct("ABC", 100.0) == pytest.approx(1.0)

    def test_needs_at_least_four_valid_prices(self, feed):
        feed["ABC"] = [100.0, 101.0, 100.0]
        assert adaptive._atr_proxy_pct("ABC", 100.0) is None

    def test_zero_and_negative_prices_are_dropped_before_counting(self, feed):
        feed["ABC"] = [100.0, 0.0, 101.0, -5.0, 100.0]              # only 3 valid
        assert adaptive._atr_proxy_pct("ABC", 100.0) is None

    def test_only_the_last_twenty_prices_count(self, feed):
        feed["ABC"] = [1.0, 500.0] * 5 + [100.0, 102.0] * 10        # old wild ticks then calm ones
        assert adaptive._atr_proxy_pct("ABC", 100.0) == pytest.approx(2.0)

    def test_window_is_exactly_twenty_prices(self, feed):
        # last 20 = ten wide (±4) then ten calm (±2) prices: 19 ranges = 9*4 + 2 + 9*2 = 56 -> avg 2.947
        # (a shorter window would see only the calm half and report 2.0)
        feed["ABC"] = [1.0, 500.0, 1.0, 500.0, 1.0] + [98.0, 102.0] * 5 + [100.0, 102.0] * 5
        assert adaptive._atr_proxy_pct("ABC", 100.0) == pytest.approx(56 / 19)

    def test_flat_prices_give_no_signal(self, feed):
        feed["ABC"] = [100.0] * 10
        assert adaptive._atr_proxy_pct("ABC", 100.0) is None

    def test_non_positive_ltp_gives_no_signal(self, feed):
        feed["ABC"] = [100.0, 101.0, 100.0, 101.0]
        assert adaptive._atr_proxy_pct("ABC", 0.0) is None

    def test_feed_error_gives_no_signal(self, monkeypatch):
        monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: (_ for _ in ()).throw(RuntimeError("ws")))
        assert adaptive._atr_proxy_pct("ABC", 100.0) is None


# ── _intraday_range_position ─────────────────────────────────────────────────
class TestRangePosition:
    def test_position_within_the_days_range(self, feed):
        feed["ABC"] = [100.0, 200.0, 150.0]
        assert adaptive._intraday_range_position("ABC", 175.0) == pytest.approx(0.75)

    @pytest.mark.parametrize("ltp,expected", [(300.0, 1.0), (50.0, 0.0)])
    def test_clamped_to_zero_one(self, feed, ltp, expected):
        feed["ABC"] = [100.0, 200.0]
        assert adaptive._intraday_range_position("ABC", ltp) == expected

    def test_needs_two_prices(self, feed):
        feed["ABC"] = [100.0]
        assert adaptive._intraday_range_position("ABC", 100.0) is None

    def test_flat_range_is_unknown(self, feed):
        feed["ABC"] = [100.0, 100.0, 100.0]
        assert adaptive._intraday_range_position("ABC", 100.0) is None

    def test_non_positive_prices_ignored(self, feed):
        feed["ABC"] = [0.0, 100.0]
        assert adaptive._intraday_range_position("ABC", 100.0) is None

    def test_feed_error_is_unknown(self, monkeypatch):
        monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: (_ for _ in ()).throw(RuntimeError("ws")))
        assert adaptive._intraday_range_position("ABC", 100.0) is None


# ── compute ──────────────────────────────────────────────────────────────────
class TestComputeStopAndTarget:
    @pytest.mark.parametrize("pct,stop", [(1.0, 2.0), (5.0, 3.0), (20.0, 5.0)])
    def test_without_atr_stop_is_sixty_percent_of_the_move_within_bounds(self, monkeypatch, pct, stop):
        fixed(monkeypatch)
        lv = adaptive.compute(pct, 500.0, symbol="ABC")
        assert lv.stop_pct == pytest.approx(stop) and lv.atr_proxy_pct is None

    def test_no_symbol_means_no_feed_lookup_at_all(self, monkeypatch):
        monkeypatch.setattr(adaptive, "_atr_proxy_pct", lambda s, p: pytest.fail("should not be called"))
        monkeypatch.setattr(adaptive, "_intraday_range_position", lambda s, p: pytest.fail("should not be called"))
        lv = adaptive.compute(5.0, 500.0)
        assert lv.stop_pct == pytest.approx(3.0) and lv.range_regime == "neutral" and lv.range_position is None

    @pytest.mark.parametrize("atr,stop", [(0.5, 2.0), (2.0, 3.0), (3.0, 4.5), (9.0, 5.0)])
    def test_atr_drives_the_stop_at_1_5x_clamped(self, monkeypatch, atr, stop):
        fixed(monkeypatch, atr=atr)
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.stop_pct == pytest.approx(stop) and lv.atr_proxy_pct == pytest.approx(atr)

    def test_target_is_2_2x_the_stop(self, monkeypatch):
        fixed(monkeypatch, atr=2.0)                                  # stop 3.0
        assert adaptive.compute(10.0, 500.0, symbol="ABC").target_pct == pytest.approx(6.6)

    def test_breakeven_trigger_is_40_percent_of_target(self, monkeypatch):
        fixed(monkeypatch, atr=2.0)
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.breakeven_trigger_pct == pytest.approx(6.6 * 0.4)

    def test_prices_are_derived_from_the_percentages(self, monkeypatch):
        fixed(monkeypatch, atr=2.0)                                  # stop 3.0%, target 6.6%
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.stop_price == pytest.approx(485.0, abs=0.06)
        assert lv.target_price == pytest.approx(533.0, abs=0.06)

    def test_result_fields_are_rounded_to_four_places(self, monkeypatch):
        fixed(monkeypatch, atr=1.7777777)
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.atr_proxy_pct == 1.7778 and lv.stop_pct == round(lv.stop_pct, 4)


class TestRangeRegime:
    @pytest.mark.parametrize("rng,regime", [(0.79, "neutral"), (0.80, "near_high"), (1.0, "near_high"),
                                            (0.5, "neutral"), (0.21, "neutral"), (0.20, "near_low"), (0.0, "near_low")])
    def test_regime_boundaries(self, monkeypatch, rng, regime):
        fixed(monkeypatch, atr=2.0, rng=rng)
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.range_regime == regime and lv.range_position == rng

    def test_near_high_tightens_both_target_and_stop(self, monkeypatch):
        fixed(monkeypatch, atr=2.0, rng=0.9)                         # 6.6/3.0 -> 4.95/2.4
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.target_pct == pytest.approx(4.95) and lv.stop_pct == pytest.approx(2.4)

    def test_near_low_widens_only_the_target(self, monkeypatch):
        fixed(monkeypatch, atr=2.0, rng=0.1)
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.target_pct == pytest.approx(6.6 * 1.15) and lv.stop_pct == pytest.approx(3.0)

    def test_near_high_cannot_push_the_stop_below_the_minimum(self, monkeypatch):
        fixed(monkeypatch, atr=1.0, rng=0.95)                        # stop 2.0*0.8 -> floored back to 2.0
        assert adaptive.compute(10.0, 500.0, symbol="ABC").stop_pct == pytest.approx(2.0)

    def test_near_high_cannot_push_the_target_below_the_minimum(self, monkeypatch):
        fixed(monkeypatch, atr=1.0, rng=0.95)                        # 4.4*0.75 = 3.3 >= 3.0 ; make it bite:
        monkeypatch.setattr(config, "MIN_TARGET_PCT", 4.0)
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.target_pct >= 4.0

    def test_near_low_widening_is_capped_at_the_maximum_target(self, monkeypatch):
        fixed(monkeypatch, atr=3.2, rng=0.1)                         # stop 4.8, target 10.56 -> 8.0 cap, *1.15 -> cap
        assert adaptive.compute(10.0, 500.0, symbol="ABC").target_pct == pytest.approx(8.0)


class TestRewardRisk:
    def test_min_2_to_1_restored_after_a_tightened_near_high_target(self, monkeypatch):
        fixed(monkeypatch, atr=1.0, rng=0.9)                         # stop 2.0, target 4.4*0.75=3.3 -> 1.65:1
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.target_pct / lv.stop_pct >= 2.0 - 1e-9
        assert lv.target_pct == pytest.approx(4.0)

    def test_ratio_is_not_touched_when_already_above_the_floor(self, monkeypatch):
        fixed(monkeypatch, atr=2.0)
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.target_pct / lv.stop_pct == pytest.approx(2.2)

    def test_floor_is_NOT_guaranteed_when_the_target_cap_binds__CURRENT_BEHAVIOUR(self, monkeypatch):
        # module docstring: "never enter < 2:1 R:R". With MAX_STOP 5% and MAX_TARGET 8% a wide-ATR
        # stock gets stop 5.0% / target 8.0% = 1.6:1 — the cap wins over the floor. Pinned so a
        # config change or a fix is a conscious decision (see the audit notes).
        fixed(monkeypatch, atr=9.0)
        lv = adaptive.compute(10.0, 500.0, symbol="ABC")
        assert lv.stop_pct == 5.0 and lv.target_pct == 8.0
        assert lv.target_pct / lv.stop_pct == pytest.approx(1.6)


class TestPriceClamps:
    def test_rounding_collapse_is_corrected_by_one_tick_each_side(self, monkeypatch):
        # tiny percentages on a ₹20 stock round straight back to the LTP; Dhan rejects that.
        for k, v in dict(MIN_STOP_PCT=0.001, MAX_STOP_PCT=5.0, MIN_TARGET_PCT=0.001, MAX_TARGET_PCT=8.0).items():
            monkeypatch.setattr(config, k, v)
        fixed(monkeypatch, atr=0.0005)
        lv = adaptive.compute(0.01, 20.0, symbol="ABC")
        assert lv.target_price == pytest.approx(20.01) and lv.stop_price == pytest.approx(19.99)

    def test_normal_case_needs_no_clamp(self, monkeypatch):
        fixed(monkeypatch, atr=2.0)
        lv = adaptive.compute(10.0, 20.0, symbol="ABC")
        assert lv.target_price > 20.0 > lv.stop_price


# ── invariants over a grid of inputs ─────────────────────────────────────────
@pytest.mark.parametrize("ltp", [20.0, 47.35, 99.95, 250.0, 1234.5, 2999.0])
@pytest.mark.parametrize("pct", [0.3, 0.7, 1.5, 4.0, 12.0])
@pytest.mark.parametrize("atr", [None, 0.2, 1.0, 2.5, 8.0])
@pytest.mark.parametrize("rng", [None, 0.0, 0.5, 1.0])
def test_output_invariants_hold_for_any_input(monkeypatch, ltp, pct, atr, rng):
    fixed(monkeypatch, atr=atr, rng=rng)
    lv = adaptive.compute(pct, ltp, symbol="ABC")
    assert lv.target_price > ltp > lv.stop_price > 0
    assert config.MIN_STOP_PCT - 1e-9 <= lv.stop_pct <= config.MAX_STOP_PCT + 1e-9
    assert lv.target_pct <= config.MAX_TARGET_PCT + 1e-9
    assert lv.breakeven_trigger_pct == pytest.approx(lv.target_pct * 0.4, abs=1e-3)
    assert lv.range_regime in ("neutral", "near_high", "near_low")


# ── defensive branch: len(sample) < 2 (round-30) ────────────────────────────
class TestAtrProxyShortWindowBranch:
    """ATR_LOOKBACK=20 makes the `if len(sample) < 2` guard unreachable under
    normal conditions (prices >= 4 → sample >= 4).  Patching ATR_LOOKBACK=1
    means sample = prices[-1:] = 1 item, which is < 2, so the early-return
    fires.  This is the only way to cover adaptive.py line 113 without
    touching source code."""

    def test_sample_shorter_than_two_ticks_returns_none(self, feed, monkeypatch):
        monkeypatch.setattr(adaptive, "ATR_LOOKBACK", 1)
        feed["XYZ"] = [100.0, 101.0, 100.0, 101.0]   # 4 valid prices → sample[-1:] = [101.0]
        assert adaptive._atr_proxy_pct("XYZ", 100.0) is None
