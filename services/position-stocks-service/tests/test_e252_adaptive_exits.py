"""E252 (2026-10-28): adaptive trailing stop, adaptive no-follow-through / flat band, hold-or-close for a flat trade.

SYNGENE 28 Oct: bought 389.45, trail exited 391.15 (+0.44 %), stock then ran to 412.7. HEGAM: 20-minute no-follow-through exit while fading.
Only unittest.mock is used.
"""
from __future__ import annotations

import functools
from unittest import mock

import config
from orders import exit_adaptive
from orders.trailing import compute_trailing_stop


def _on(fn):
    @functools.wraps(fn)
    def wrapper():
        with mock.patch.object(config, "TRAIL_ADAPTIVE_ENABLED", True, create=True), mock.patch.object(config, "EXIT_ADAPTIVE_ENABLED", True, create=True):
            return fn()
    return wrapper


def _pts(start, per_min_ranges, base=100.0):
    """(ts, high, low, close) points, one per minute, for 5-minute-bucket tests."""
    out = []
    for i, r in enumerate(per_min_ranges):
        out.append((start + i * 60.0, base * (1 + r / 200.0), base * (1 - r / 200.0), base))
    return out


@_on
def test_trail_activation_is_half_the_target_but_never_under_the_old_floor():
    assert exit_adaptive.trail_activation_pct(2.5) == 1.25
    assert exit_adaptive.trail_activation_pct(1.2) == 1.0          # old floor
    assert exit_adaptive.trail_activation_pct(10.0) == 2.0         # capped
    assert exit_adaptive.trail_activation_pct(None) == 1.0


@_on
def test_trail_distance_follows_the_stocks_bar_range_but_never_exceeds_the_stop():
    assert exit_adaptive.trail_distance_pct(1.3, None) == max(0.4, 1.3 * 0.6)
    assert exit_adaptive.trail_distance_pct(1.3, 1.1) == 1.1       # wider than the old 0.78
    assert exit_adaptive.trail_distance_pct(1.3, 3.0) == 1.3       # capped at the stop
    assert exit_adaptive.trail_distance_pct(1.3, 0.2) == max(0.4, 1.3 * 0.6)


@_on
def test_syngene_case_trail_no_longer_sits_inside_normal_noise():
    # peak ~394.2 (+1.2 %), 2.5 % target: the old trail (0.78 %) would put the stop at 391.1; the new one waits for +1.25 % and sits a bar range lower
    old = None
    with mock.patch.object(config, "TRAIL_ADAPTIVE_ENABLED", False, create=True):
        old = compute_trailing_stop(entry=389.45, peak=394.2, ltp=393.5, current_stop=384.0, target=399.2, stop_pct=1.3)
    new = compute_trailing_stop(entry=389.45, peak=394.2, ltp=393.5, current_stop=384.0, target=399.2, stop_pct=1.3, bar_range_pct=1.0)
    assert old is not None and old > 390.0
    assert new is None                                              # +1.22 % is below the 1.25 % activation: the stop is not lifted yet
    later = compute_trailing_stop(entry=389.45, peak=395.5, ltp=395.0, current_stop=384.0, target=399.2, stop_pct=1.3, bar_range_pct=1.0)
    assert later is not None and later < 395.5 * (1 - 0.0099) and later > 389.45


@_on
def test_short_trail_uses_the_same_adaptive_numbers():
    new = compute_trailing_stop(entry=100.0, peak=98.9, ltp=99.0, current_stop=101.5, target=97.0, stop_pct=1.5, side="SHORT", bar_range_pct=1.0)
    assert new is None                                              # 1.1 % < 1.5 % activation (3 % target x 0.5)
    new2 = compute_trailing_stop(entry=100.0, peak=97.9, ltp=98.1, current_stop=101.5, target=96.0, stop_pct=1.5, side="SHORT", bar_range_pct=1.0)
    assert new2 is not None and 97.9 < new2 < 100.0 and abs(new2 - 97.9 * 1.01) < 0.06


@_on
def test_nft_gain_and_flat_band_scale_with_target_and_clamp():
    assert exit_adaptive.nft_min_gain_pct(2.5) == 0.5
    assert exit_adaptive.nft_min_gain_pct(1.0) == 0.4
    assert exit_adaptive.nft_min_gain_pct(6.0) == 0.8
    assert exit_adaptive.nft_min_gain_pct(None) == config.NO_FOLLOWTHROUGH_MIN_GAIN_PCT
    assert abs(exit_adaptive.flat_band_pct(2.5) - 0.375) < 1e-9
    assert exit_adaptive.flat_band_pct(0.5) == 0.25 and exit_adaptive.flat_band_pct(9.0) == 0.6


def test_adaptive_off_gives_back_the_fixed_numbers():
    with mock.patch.object(config, "EXIT_ADAPTIVE_ENABLED", False, create=True), mock.patch.object(config, "TRAIL_ADAPTIVE_ENABLED", False, create=True):
        assert exit_adaptive.nft_min_gain_pct(3.0) == config.NO_FOLLOWTHROUGH_MIN_GAIN_PCT
        assert exit_adaptive.flat_band_pct(3.0) == config.STAGNATION_EXIT_BAND_PCT
        assert exit_adaptive.trail_activation_pct(3.0) == config.TRAIL_ACTIVATE_PCT
        assert exit_adaptive.trail_distance_pct(1.3, 2.0) == max(config.TRAIL_MIN_DISTANCE_PCT, 1.3 * config.TRAIL_DISTANCE_STOP_FRACTION)
        assert exit_adaptive.stagnation_decision(age_min=25, fading=False, pressure=None) == (True, "adaptive exits off")


@_on
def test_avg_range_ignores_the_forming_bucket_and_needs_three_buckets():
    pts = _pts(0.0, [1.0] * 5 + [1.0] * 5 + [1.0] * 5 + [9.0])      # three full 5-minute buckets + one minute of a fourth (huge) bucket
    assert abs(exit_adaptive._avg_range_from_points(pts) - 1.0) < 1e-9
    assert exit_adaptive._avg_range_from_points(_pts(0.0, [1.0] * 10)) is None
    assert exit_adaptive._avg_range_from_points([]) is None


def _fake_ws(vwap, ticks):
    """A stand-in feed.ws_client (works whether or not the real module imports here)."""
    import contextlib
    import sys
    import types

    @contextlib.contextmanager
    def cm():
        fake = types.SimpleNamespace(get_exchange_vwap=lambda _s: vwap, get_tick_buffer=lambda _s: ticks)
        pkg = sys.modules.get("feed")
        with mock.patch.dict(sys.modules, {"feed.ws_client": fake}):
            if pkg is not None:
                with mock.patch.object(pkg, "ws_client", fake, create=True):
                    yield
            else:
                yield
    return cm()


@_on
def test_fading_below_vwap_or_lower_highs_and_unknown_without_data():
    ticks_down = [(1000.0 + i * 30.0, 100.0 - i * 0.05) for i in range(21)]       # 10 minutes, falling: lower highs
    with _fake_ws(None, ticks_down):
        assert exit_adaptive.is_fading("X", 99.0) is True
    ticks_up = [(1000.0 + i * 30.0, 100.0 + i * 0.05) for i in range(21)]
    with _fake_ws(None, ticks_up):
        assert exit_adaptive.is_fading("X", 101.0) is False
        assert exit_adaptive.is_fading("X", 101.0, short=True) is True          # a short: higher lows
    with _fake_ws(100.5, []):
        assert exit_adaptive.is_fading("X", 100.0) is True                      # under VWAP
        assert exit_adaptive.is_fading("X", 101.0) is False
    with _fake_ws(None, []):
        assert exit_adaptive.is_fading("X", 100.0) is None


@_on
def test_stagnation_decision_close_only_when_fading_or_slot_wanted_or_hard_max():
    assert exit_adaptive.stagnation_decision(age_min=25, fading=False, pressure=None)[0] is False
    assert exit_adaptive.stagnation_decision(age_min=25, fading=None, pressure=None)[0] is False
    assert exit_adaptive.stagnation_decision(age_min=25, fading=True, pressure=None)[0] is True
    assert exit_adaptive.stagnation_decision(age_min=25, fading=False, pressure="TREL: MAX_POSITIONS")[0] is True
    assert exit_adaptive.stagnation_decision(age_min=95, fading=False, pressure=None)[0] is True


@_on
def test_slot_pressure_reads_recent_capital_and_slot_skips_only():
    from datetime import datetime, timedelta, timezone

    class Row:
        def __init__(self, symbol, reason, age_min):
            self.symbol, self.reason = symbol, reason
            self.created_at = datetime.now(timezone.utc) - timedelta(minutes=age_min)

    def db_with(rows):
        q = mock.MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.limit.return_value = q
        q.all.return_value = rows
        db = mock.MagicMock()
        db.query.return_value = q
        return db

    class _Col:
        def __eq__(self, _o):
            return True

        def __ge__(self, _o):
            return True

        def desc(self):
            return self

    fake_model = mock.MagicMock()
    fake_model.decision, fake_model.created_at = _Col(), _Col()
    with mock.patch("models.ScalpCandidateLog", fake_model, create=True):
        assert exit_adaptive.slot_pressure(db_with([Row("TREL", "INSUFFICIENT_CAPITAL", 2)])) == "TREL: INSUFFICIENT_CAPITAL"
        assert exit_adaptive.slot_pressure(db_with([Row("TREL", "MAX_POSITIONS:3", 2)])) == "TREL: MAX_POSITIONS"
        assert exit_adaptive.slot_pressure(db_with([Row("TREL", "SPIKE_CHASE:x", 2)])) is None
        assert exit_adaptive.slot_pressure(db_with([])) is None
        bad = mock.MagicMock()
        bad.query.side_effect = RuntimeError("db down")
        assert exit_adaptive.slot_pressure(bad) is None


def test_stagnation_exit_is_wired_to_the_adaptive_module_and_switches_are_on_the_calibration_page():
    from pathlib import Path
    from control.catalog import CATALOG
    svc = Path(__file__).resolve().parent.parent
    eod = (svc / "orders" / "eod_squareoff.py").read_text(encoding="utf-8")
    assert "exit_adaptive.stagnation_decision" in eod and "exit_adaptive.flat_band_pct" in eod and "exit_adaptive.nft_min_gain_pct" in eod
    assert "exit_adaptive.trail_distance_pct" in (svc / "orders" / "trailing.py").read_text(encoding="utf-8")
    flags = {f.key: f for f in CATALOG}
    # conftest.py turns both switches off for every older test, so the shipped default is read from a fresh copy of config.py (env unset in CI)
    import importlib.util
    spec = importlib.util.spec_from_file_location("_fresh_config_e252_exits", Path(config.__file__))
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    for key in ("TRAIL_ADAPTIVE_ENABLED", "EXIT_ADAPTIVE_ENABLED"):
        assert key in flags and flags[key].live is True and flags[key].shipped == "true"
        assert getattr(fresh, key) is True
