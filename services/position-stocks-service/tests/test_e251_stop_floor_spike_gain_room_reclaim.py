"""E251 (2026-10-28): wider volatility-aware stop, spike-chase guard, gain-room gate, one reclaim re-entry.

ARVIND 28 Oct: bought 518.15 right after a spike, 1.2 % stop hit one minute later, then the stock ran to the 2.5 % target.
Only unittest.mock is used (no fixtures), so these also run without pytest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import config
from orders import adaptive, entry_timing, gain_room, pullback_rearm, stopout_guard
from orders.adaptive import AdaptiveLevels

ENTRY_SRC = (Path(__file__).resolve().parent.parent / "orders" / "entry.py").read_text(encoding="utf-8")
NOW = 1_800_000_000.0


def _e251_on(fn):
    """The conftest switches the E251 rules off for older tests; these tests exercise them, so turn them on for the call."""
    import functools

    @functools.wraps(fn)
    def wrapper():
        with mock.patch.object(config, "SPIKE_GUARD_MODE", "live", create=True), mock.patch.object(config, "GAIN_ROOM_MODE", "live", create=True), \
             mock.patch.object(config, "ADAPTIVE_STOP_FLOOR_ENABLED", True, create=True), mock.patch.object(config, "REENTRY_RECLAIM_ENABLED", True, create=True):
            return fn()
    return wrapper


def _levels(stop=0.8, target=1.6, basis="bars"):
    return AdaptiveLevels(target_pct=target, stop_pct=stop, target_price=100 * (1 + target / 100), stop_price=100 * (1 - stop / 100),
                          breakeven_trigger_pct=0.64, basis=basis, atr_proxy_pct=0.9)


# ── 1. entry_timing: spike / thrust / 1-minute noise ──────────────────────────────────────────────────────────────────────────────────────────

@_e251_on
def test_thrust_is_a_rise_that_ends_at_the_high():
    rising = [(NOW - 170 + i * 10, 100 + i * 0.1) for i in range(18)]
    st = entry_timing.window_stats(rising, NOW, 180, min_ticks=8)
    assert st["thrust_pct"] > 1.5 and st["off_high_pct"] == 0.0
    falling = [(NOW - 170 + i * 10, 102 - i * 0.1) for i in range(18)]
    assert entry_timing.window_stats(falling, NOW, 180, min_ticks=8)["thrust_pct"] == 0.0   # a fall is not a thrust


@_e251_on
def test_thin_window_gives_no_opinion():
    assert entry_timing.window_stats([(NOW - 5, 100.0), (NOW - 2, 101.0)], NOW, 180, min_ticks=8) is None
    assert entry_timing.window_stats([], NOW, 180) is None
    assert entry_timing.window_stats(None, NOW, 180) is None


@_e251_on
def test_judge_day_spike_and_thrust_and_clean():
    r = entry_timing.judge(6.0, 0.93, None)
    assert r and r.startswith("SPIKE_CHASE:DAY_SPIKE")                      # ARVIND: up ~6 % on the day, at the top of the range
    assert entry_timing.judge(6.0, 0.60, None) is None                      # same day gain but not at the top = fine
    assert entry_timing.judge(3.0, 0.95, None) is None                      # at the top but a small day = fine
    st = {"thrust_pct": 1.8, "off_high_pct": 0.10}
    assert entry_timing.judge(1.0, 0.5, st).startswith("SPIKE_CHASE:THRUST")
    assert entry_timing.judge(1.0, 0.5, {"thrust_pct": 1.8, "off_high_pct": 0.60}) is None   # already gave some back: not mid-thrust
    assert entry_timing.judge(1.0, 0.5, {"thrust_pct": 0.9, "off_high_pct": 0.0}) is None


@_e251_on
def test_avg_1m_range_needs_enough_buckets():
    ticks = [(NOW - 290 + i * 5, 100 + (i % 7) * 0.1) for i in range(58)]
    v = entry_timing.avg_1m_range_pct(ticks, NOW)
    assert v is not None and 0.4 < v < 0.8
    assert entry_timing.avg_1m_range_pct(ticks[-10:], NOW) is None           # fewer than 3 usable minutes


@_e251_on
def test_spike_reason_modes():
    with mock.patch.object(entry_timing, "_day_numbers", return_value=(6.0, 0.95)), \
         mock.patch("feed.ws_client.get_tick_buffer", return_value=[], create=True):
        with mock.patch.object(config, "SPIKE_GUARD_MODE", "live"):
            assert entry_timing.spike_reason("ARVIND", 518.15, NOW).startswith("SPIKE_CHASE")
        with mock.patch.object(config, "SPIKE_GUARD_MODE", "log"):
            assert entry_timing.spike_reason("ARVIND", 518.15, NOW) is None
        with mock.patch.object(config, "SPIKE_GUARD_MODE", "off"):
            assert entry_timing.spike_reason("ARVIND", 518.15, NOW) is None


@_e251_on
def test_spike_reason_fails_open():
    with mock.patch.object(entry_timing, "_day_numbers", side_effect=RuntimeError("boom")):
        assert entry_timing.spike_reason("X", 100.0, NOW) is None


@_e251_on
def test_spike_refusal_arms_the_pullback_rearm():
    assert pullback_rearm.is_arming_reason("SPIKE_CHASE:DAY_SPIKE:up 6.0% ...")
    assert pullback_rearm.is_arming_reason("RANGE_GATE:NEAR_DAY_HIGH:x")      # existing behaviour kept
    assert not pullback_rearm.is_arming_reason("GAIN_ROOM:ODDS:x")


# ── 2. stop floor ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@_e251_on
def test_stop_floor_pct_takes_the_larger_part_and_caps():
    with mock.patch.object(config, "ADAPTIVE_STOP_DAILY_ATR_FRAC", 0.45), mock.patch.object(config, "ADAPTIVE_STOP_M1_MULT", 2.0), \
         mock.patch.object(config, "ADAPTIVE_STOP_FLOOR_MAX_PCT", 1.5):
        assert adaptive.stop_floor_pct(None, None) == (0.0, "")
        f, why = adaptive.stop_floor_pct(2.0, 0.3)                            # 0.9 vs 0.6
        assert abs(f - 0.9) < 1e-9 and "dailyATR" in why
        f, why = adaptive.stop_floor_pct(2.0, 0.6)                            # 0.9 vs 1.2
        assert abs(f - 1.2) < 1e-9 and "1m-range" in why
        f, why = adaptive.stop_floor_pct(6.0, 0.4)                            # 2.7 -> capped
        assert f == 1.5 and "capped" in why


@_e251_on
def test_apply_stop_floor_widens_and_moves_the_target():
    pdv = mock.Mock(atr_pct=3.0)                                              # 0.45 x 3.0 = 1.35
    with mock.patch("screening.prev_day.peek", return_value=pdv), mock.patch("orders.entry_timing.live_avg_1m_range_pct", return_value=0.4), \
         mock.patch.object(config, "ADAPTIVE_STOP_FLOOR_ENABLED", True), mock.patch.object(config, "ADAPTIVE_STOP_DAILY_ATR_FRAC", 0.45), \
         mock.patch.object(config, "ADAPTIVE_STOP_M1_MULT", 2.0), mock.patch.object(config, "ADAPTIVE_STOP_FLOOR_MAX_PCT", 1.5):
        out = adaptive.apply_stop_floor(_levels(1.24, 2.48), 100.0, "ARVIND")
    assert abs(out.stop_pct - 1.35) < 1e-6
    assert out.target_pct >= out.stop_pct * 2.0 - 1e-6                         # reward:risk kept
    assert out.stop_price < 100.0 < out.target_price
    assert len(out.basis) <= 16 and out.basis.endswith("+f")                   # scalp_positions.stop_basis is VARCHAR(16)
    assert len("opening_plan+f") <= 16


@_e251_on
def test_apply_stop_floor_never_tightens_never_touches_shorts_and_fails_open():
    pdv = mock.Mock(atr_pct=3.0)
    with mock.patch("screening.prev_day.peek", return_value=pdv), mock.patch("orders.entry_timing.live_avg_1m_range_pct", return_value=0.4):
        wide = _levels(1.45, 2.9)
        assert adaptive.apply_stop_floor(wide, 100.0, "X") is wide             # already wider than the floor
        short = AdaptiveLevels(target_pct=1.6, stop_pct=0.8, target_price=98.4, stop_price=100.8, side="SHORT")
        assert adaptive.apply_stop_floor(short, 100.0, "X", "SHORT") is short
        with mock.patch.object(config, "ADAPTIVE_STOP_FLOOR_ENABLED", False):
            tight = _levels()
            assert adaptive.apply_stop_floor(tight, 100.0, "X") is tight
    with mock.patch("screening.prev_day.peek", side_effect=RuntimeError("x")), mock.patch("orders.entry_timing.live_avg_1m_range_pct", return_value=None):
        t2 = _levels()
        assert adaptive.apply_stop_floor(t2, 100.0, "X") is t2


@_e251_on
def test_compute_keeps_the_wide_stop_through_the_target_cap():
    """The old 2.5 % cap squeezed a 1.5 % stop back to 1.25 % (target/min_rr); the 3.0 % cap lets it through at 2:1."""
    pdv = mock.Mock(atr_pct=3.4)                                              # 0.45 x 3.4 = 1.53 -> capped at 1.5
    base = _levels(0.8, 1.6)
    common = dict(ADAPTIVE_STOP_FLOOR_ENABLED=True, ADAPTIVE_STOP_DAILY_ATR_FRAC=0.45, ADAPTIVE_STOP_M1_MULT=2.0, ADAPTIVE_STOP_FLOOR_MAX_PCT=1.5,
                  SCALP_TARGET_CAP_MIN_RR=2.0, SCALP_TARGET_CAP_MIN_STOP_PCT=1.2, SCALP_TARGET_CAP_MIN_STOP_ATR_MULT=1.0,
                  ADAPTIVE_BAR_TARGET_RR=2.0, ADAPTIVE_BAR_TARGET_MAX_PCT=3.5, ENTRY_MIN_REWARD_RISK=2.0)
    def run(cap):
        patches = [mock.patch.object(config, k, v, create=True) for k, v in dict(common, SCALP_TARGET_CAP_PCT=cap).items()]
        patches += [mock.patch.object(adaptive, "_compute_levels", return_value=base), mock.patch("screening.prev_day.peek", return_value=pdv),
                    mock.patch("orders.entry_timing.live_avg_1m_range_pct", return_value=None)]
        for p in patches:
            p.start()
        try:
            return adaptive.compute(2.0, 100.0, symbol="ARVIND")
        finally:
            for p in patches:
                p.stop()
    new = run(3.0)
    assert abs(new.stop_pct - 1.5) < 1e-6 and abs(new.target_pct - 3.0) < 1e-6
    old = run(2.5)
    assert old.stop_pct < 1.3                                                  # documents why the cap moved


def test_defaults_are_the_new_wider_values():
    """Read from config.py itself: tests/conftest.py zeroes the target cap for older tests, so the live attribute is not the production default."""
    import re
    src = (Path(__file__).resolve().parent.parent / "config.py").read_text(encoding="utf-8")
    def default(name):
        return float(re.search(r'^' + name + r' = _get_float\("' + name + r'", ([0-9.]+)\)', src, re.M).group(1))
    assert default("ADAPTIVE_BAR_STOP_MIN_PCT") >= 1.2
    assert default("SCALP_TARGET_CAP_PCT") >= 3.0 and default("SCALP_TARGET_CAP_MIN_STOP_PCT") >= 1.2
    assert default("REARM_MIN_STOP_PCT") >= 1.0
    for mode in ("SPIKE_GUARD_MODE", "GAIN_ROOM_MODE"):                                  # live, not log-only
        assert re.search(r'^' + mode + r' = _get_str\("' + mode + r'", "live"\)', src, re.M)


# ── 3. gain-room gate ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _bars(prices):
    """(t, o, h, l, c, v) one-minute bars from a close series; high/low = close +/- 0.02 %."""
    return [(i * 60, p, p * 1.0002, p * 0.9998, p, 1000) for i, p in enumerate(prices)]


@_e251_on
def test_odds_reason_refuses_a_stock_that_keeps_falling_and_passes_a_trender():
    falling = _bars([100.0 * (1 - 0.0008 * i) for i in range(160)])
    rising = _bars([100.0 * (1 + 0.0008 * i) for i in range(160)])
    r = gain_room.odds_reason(falling, 1.2, 2.4)
    assert r and r.startswith("GAIN_ROOM:ODDS")
    assert gain_room.odds_reason(rising, 1.2, 2.4) is None


@_e251_on
def test_odds_reason_needs_enough_replays_and_fails_open():
    assert gain_room.odds_reason(_bars([100.0, 99.0, 98.0]), 1.2, 2.4) is None   # too few bars
    assert gain_room.odds_reason(None, 1.2, 2.4) is None
    assert gain_room.odds_reason(_bars([100.0 - i * 0.1 for i in range(60)]), 0.0, 2.4) is None


@_e251_on
def test_range_reason_blocks_a_spent_day_only():
    # ARVIND-like: low 490, normal daily range 4 %, 2 x range -> expected high 529.2; price 518 leaves 2.2 % >= 0.5 x 2.4 % -> allowed
    assert gain_room.range_reason(490.0, 518.0, 4.0, 2.4) is None
    # a stock that already ran far past two normal days: low 100, normal range 2 % -> expected high 104, price 108 -> no room
    r = gain_room.range_reason(100.0, 108.0, 2.0, 2.4)
    assert r and r.startswith("GAIN_ROOM:RANGE")
    assert gain_room.range_reason(None, 108.0, 2.0, 2.4) is None               # no data = no opinion
    assert gain_room.range_reason(100.0, 108.0, None, 2.4) is None
    with mock.patch.object(config, "GAIN_ROOM_MIN_TARGET_FRAC", 0.0):
        assert gain_room.range_reason(100.0, 108.0, 2.0, 2.4) is None          # 0 = range test off


@_e251_on
def test_gain_room_modes_and_fail_open():
    falling = _bars([100.0 * (1 - 0.0008 * i) for i in range(160)])
    with mock.patch("feed.bar_store.get_bars", return_value=falling):
        with mock.patch.object(config, "GAIN_ROOM_MODE", "live"):
            assert gain_room.reject_reason("X", 100.0, 1.2, 2.4).startswith("GAIN_ROOM")
        with mock.patch.object(config, "GAIN_ROOM_MODE", "log"):
            assert gain_room.reject_reason("X", 100.0, 1.2, 2.4) is None
        with mock.patch.object(config, "GAIN_ROOM_MODE", "off"):
            assert gain_room.reject_reason("X", 100.0, 1.2, 2.4) is None
    with mock.patch("feed.bar_store.get_bars", side_effect=RuntimeError("x")), mock.patch("feed.ws_client.get_day_range", side_effect=RuntimeError("y"), create=True):
        assert gain_room.reject_reason("X", 100.0, 1.2, 2.4) is None


# ── 4. reclaim re-entry ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _stopped(minutes_ago=10.0, pnl=-300.0, status="STOP_HIT", entry=518.15, target=530.90, now=None):
    now = now or datetime(2026, 10, 28, 7, 30, tzinfo=timezone.utc)
    return {"status": status, "closed_at": now - timedelta(minutes=minutes_ago), "realized_pnl": pnl, "entry_price": entry,
            "target_price": target, "exit_price": 511.85}, now


@_e251_on
def test_reclaim_allows_arvind_shape():
    t, now = _stopped(minutes_ago=6)
    ok, why = stopout_guard.reclaim_decision([t], 519.0, now)
    assert ok and "reclaimed" in why


@_e251_on
def test_reclaim_refusals():
    t, now = _stopped(minutes_ago=6)
    assert not stopout_guard.reclaim_decision([t], 515.0, now)[0]                  # still under the old entry
    assert not stopout_guard.reclaim_decision([t], 528.0, now)[0]                  # past half way to the old target: chasing
    t1, now = _stopped(minutes_ago=0.5)
    assert not stopout_guard.reclaim_decision([t1], 519.0, now)[0]                 # too soon (REENTRY_RECLAIM_MIN_WAIT_S)
    t2, now = _stopped(minutes_ago=300)
    assert not stopout_guard.reclaim_decision([t2], 519.0, now)[0]                 # too old
    t3, now = _stopped(status="EOD_SQUAREOFF")
    assert not stopout_guard.reclaim_decision([t3], 519.0, now)[0]                 # not a stop-out
    t4, now = _stopped(pnl=40.0)
    assert not stopout_guard.reclaim_decision([t4], 519.0, now)[0]                 # not a loss
    a, now = _stopped(minutes_ago=30)
    b, _ = _stopped(minutes_ago=6, now=now)
    assert not stopout_guard.reclaim_decision([b, a], 519.0, now)[0]               # the pass is one-time: a second trade today = blocked
    assert not stopout_guard.reclaim_decision([], 519.0, now)[0]


@_e251_on
def test_reclaim_switch_and_errors():
    t, now = _stopped(minutes_ago=6)
    with mock.patch.object(config, "REENTRY_RECLAIM_ENABLED", False):
        assert not stopout_guard.reclaim_decision([t], 519.0, now)[0]
    with mock.patch.object(config, "REENTRY_RECLAIM_MAX_PER_SYMBOL", 0):
        assert not stopout_guard.reclaim_decision([t], 519.0, now)[0]
    assert stopout_guard.reclaim_decision([{"status": "STOP_HIT"}], 519.0, now)[0] is False   # missing fields never raise


@_e251_on
def test_entry_reason_allow_symbol_lifts_the_symbol_block_but_not_the_day_pause():
    with mock.patch.object(stopout_guard, "stopouts_today", return_value=(1, {"ARVIND"})), mock.patch.object(config, "STOPOUT_GUARD_ENABLED", True), \
         mock.patch.object(config, "STOPOUT_DAY_PAUSE_N", 3), mock.patch.object(config, "STOPOUT_SYMBOL_BLOCK", True):
        assert stopout_guard.entry_reason(None, "ARVIND").startswith("STOPOUT_GUARD:SYMBOL")
        assert stopout_guard.entry_reason(None, "ARVIND", allow_symbol=True) is None
    with mock.patch.object(stopout_guard, "stopouts_today", return_value=(3, {"ARVIND"})), mock.patch.object(config, "STOPOUT_GUARD_ENABLED", True), \
         mock.patch.object(config, "STOPOUT_DAY_PAUSE_N", 3):
        assert stopout_guard.entry_reason(None, "ARVIND", allow_symbol=True).startswith("STOPOUT_GUARD:PAUSED")   # whole-day pause still wins


# ── 5. wiring in orders/entry.py ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@_e251_on
def test_entry_wiring_order():
    i = ENTRY_SRC.index
    assert i("_reclaim = stopout_guard.reclaim_allowed(") < i("stopout_guard.entry_reason(db, candidate.symbol, allow_symbol=bool(_reclaim))")
    assert i("range_reject = None if rearm") < i("spike_reject = None if rearm else entry_timing.spike_reason(") < i("opening_reject = opening_gate.reject_reason(")
    assert "_arm_pullback(candidate, spike_reject)" in ENTRY_SRC
    assert i("_rr_reject = rr_floor.reject_reason(levels)") < i("gain_room.reject_reason(candidate.symbol") < i("_depth_reject = depth_gate.reject_reason(")
    assert "reclaim=bool(_reclaim)" in ENTRY_SRC
    assert '"SPIKE_CHASE", "GAIN_ROOM"' in ENTRY_SRC                                # per-symbol skips: the next shortlisted pick is still tried
    assert "return b[:16]" in ENTRY_SRC                                             # stop_basis fits VARCHAR(16)


@_e251_on
def test_reentry_guard_reclaim_param_skips_both_blocks():
    body = ENTRY_SRC[ENTRY_SRC.index("def _reentry_guard_reject("):ENTRY_SRC.index("def _get_gate_state(")]
    assert body.index("if reclaim:") < body.index("if config.SYMBOL_BLOCK_AFTER_LOSS_TODAY:")

# ── 6. adaptive spike limits (nothing is tuned to one stock) ─────────────────────────────────────────────────────────────────────────────────

@_e251_on
def test_spike_limits_scale_with_the_stocks_own_volatility():
    # day limit = 1.2 x daily ATR clamped to 2.5..6.0; unknown ATR = the fixed 4.0
    assert entry_timing.day_limit_pct(None) == 4.0
    assert abs(entry_timing.day_limit_pct(1.5) - 2.5) < 1e-9       # calm stock: 1.8 -> clamped up to 2.5
    assert abs(entry_timing.day_limit_pct(3.0) - 3.6) < 1e-9
    assert abs(entry_timing.day_limit_pct(9.0) - 6.0) < 1e-9       # very volatile: clamped to 6.0
    assert entry_timing.judge(3.2, 0.95, None, atr_pct=1.5).startswith("SPIKE_CHASE:DAY_SPIKE")     # +3.2 % is a spike for a calm name ...
    assert entry_timing.judge(3.2, 0.95, None, atr_pct=5.0) is None                                 # ... and routine for a 5 % ATR name
    # thrust limit = 3 x average 1-minute range clamped to 1.0..2.5; unknown = the fixed 1.5
    assert entry_timing.thrust_limit_pct(None) == 1.5
    assert abs(entry_timing.thrust_limit_pct(0.2) - 1.0) < 1e-9
    assert abs(entry_timing.thrust_limit_pct(0.6) - 1.8) < 1e-9
    assert abs(entry_timing.thrust_limit_pct(2.0) - 2.5) < 1e-9
    hot = {"thrust_pct": 1.8, "off_high_pct": 0.1}
    assert entry_timing.judge(0.5, 0.5, hot, m1_range_pct=0.2).startswith("SPIKE_CHASE:THRUST")
    assert entry_timing.judge(0.5, 0.5, hot, m1_range_pct=0.8) is None                              # a stock that moves 0.8 %/minute: +1.8 % in 3 min is normal


@_e251_on
def test_spike_limit_switches_turn_each_test_off():
    with mock.patch.object(config, "SPIKE_DAY_PCT", 0.0):
        assert entry_timing.judge(9.0, 0.99, None, atr_pct=2.0) is None
    with mock.patch.object(config, "SPIKE_THRUST_PCT", 0.0):
        assert entry_timing.judge(0.0, 0.5, {"thrust_pct": 3.0, "off_high_pct": 0.0}) is None


def test_switches_are_in_the_calibration_catalog_and_live():
    from control.catalog import CATALOG
    by_key = {f.key: f for f in CATALOG}
    for key in ("ADAPTIVE_STOP_FLOOR_ENABLED", "ADAPTIVE_STOP_DAILY_ATR_FRAC", "ADAPTIVE_STOP_M1_MULT", "ADAPTIVE_STOP_FLOOR_MAX_PCT",
                "SPIKE_GUARD_MODE", "GAIN_ROOM_MODE", "REENTRY_RECLAIM_ENABLED"):
        assert key in by_key and by_key[key].live, key
        assert hasattr(config, key), key
