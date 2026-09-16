"""
screening/engine.py — in-memory rolling-window screener.

Consumes ticks from feed/ws_client.py ring buffers.
Computes rolling pct-change over 1m/5m/15m/60m windows O(1) per tick
(scan the deque backward until we find the reference price at the window
boundary — no DB read on the hot path).

Runs ALL FOUR windows simultaneously and feeds one shared ranking step
(composite score) so the best candidate wins regardless of which window
surfaced it.

Gates applied per candidate:
  1. Min pct-change per window (config.MIN_PCT_CHANGE_*M)
  2. Min average volume floor (MIN_AVG_VOLUME — crude proxy, traded
     volume from the WS feed accumulates in _volume_accum)
  3. Not already in an open scalp position (open_symbols set, passed in)
  4. Momentum consistency: require the move to be SUSTAINED across the
     window, not a single-tick spike — see _momentum_consistency() below.
  5. Range-position multiplier on composite_score: demotes candidates
     already near their day-high (less upside room).

Composite score formula:
  score = pct_change * volume_weight * range_mult * consistency_mult
  volume_weight   = min(tick_count / floor, 3.0)   — cap at 3× floor
  range_mult      = 0.50 / 0.75 / 1.0 / 1.20       — day-range regime
  consistency_mult= 0.80 / 1.0 / 1.15               — momentum quality

2026-09-15 calibration (session42 — "dig more, calibrate more efficient"):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROBLEMS FOUND IN DEEP AUDIT:

1. 1m threshold too low (0.5%): fired on single-tick noise constantly.
   Fix: raised to 0.7%. Still the lowest window threshold, but requires
   a more real move to surface a candidate.

2. No momentum consistency check: a stock could have pct_change=+1.2%
   over 5m because of ONE big candle at t=4m59s and then immediately
   reversing. We were buying AFTER the move, not INTO a rising trend.
   Fix: _momentum_consistency() checks what fraction of the window the
   price was ABOVE the midpoint of open/close. If price spiked and
   dropped, the fraction is low → consistency_mult penalises it.

3. VWAP-proxy deviation: added _vwap_estimate() — simple price-weighted
   average of all ticks in the buffer. If LTP is already >2% above VWAP,
   the stock is extended vs session average → apply range_mult penalty
   even if it hasn't hit day-high yet.

4. 60m window composite_score was equal-weighted with shorter windows.
   A 60m signal implies a sustained multi-hour move — it's higher-
   conviction. Bonus multiplier 1.10 applied to 60m candidates.

5. score = pct_change * volume_weight was unbounded for high-pct/high-
   volume stocks and made tiny-cap illiquid stocks (many ticks = high
   tick_count) look great. Capped volume_weight at 2.0 for 1m window
   (noisier signal) and 3.0 for 5m/15m/60m (unchanged).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import config
from feed import ws_client

logger = logging.getLogger("position-stocks-screener")

# Per-window thresholds keyed by window minutes.
# 2026-09-15 calibration: 1m raised 0.5→0.7% to reduce single-tick noise.
_WINDOW_THRESHOLDS = {
    1:  max(config.MIN_PCT_CHANGE_1M,  0.7),   # noise floor: never below 0.7%
    5:  config.MIN_PCT_CHANGE_5M,
    15: config.MIN_PCT_CHANGE_15M,
    60: config.MIN_PCT_CHANGE_60M,
}

# Volume accumulator: tick count in last 5m as liquidity proxy
# (WS mode-1 gives no real volume — tick frequency is the best proxy)
_volume_accum: Dict[str, int] = defaultdict(int)
_volume_window_s = 300  # 5 minutes
_tick_timestamps: Dict[str, list] = defaultdict(list)

# 60m window conviction bonus — sustained multi-hour trend is higher quality
_WINDOW_CONVICTION_MULT = {1: 0.90, 5: 1.0, 15: 1.05, 60: 1.10}

# Volume weight cap per window: tighter for 1m (noisy), normal for longer
_VOLUME_WEIGHT_CAP = {1: 2.0, 5: 3.0, 15: 3.0, 60: 3.0}

# Range-position multipliers (applied to composite_score)
_RPOS_HEAVY_PENALTY   = (0.85, 0.50)  # >= 0.85 of day range → 0.50×
_RPOS_SOFT_PENALTY    = (0.70, 0.75)  # >= 0.70 → 0.75×
_RPOS_NEAR_LOW_BONUS  = (0.20, 1.20)  # <= 0.20 → 1.20×

# VWAP extension penalty: if LTP > VWAP by this %, apply soft penalty
_VWAP_EXTENDED_PCT    = 2.0   # 2% above VWAP → penalise
_VWAP_EXTENDED_MULT   = 0.80

# Momentum consistency thresholds
_CONSISTENCY_STRONG   = 0.65   # > 65% of window was rising → bonus
_CONSISTENCY_WEAK     = 0.35   # < 35% → penalty (spike-and-drop)
_CONSISTENCY_STRONG_MULT = 1.15
_CONSISTENCY_WEAK_MULT   = 0.80


def _update_volume(symbol: str, ts: float) -> None:
    """Track tick count in the last _volume_window_s as activity proxy."""
    tl = _tick_timestamps[symbol]
    tl.append(ts)
    cutoff = ts - _volume_window_s
    while tl and tl[0] < cutoff:
        tl.pop(0)
    _volume_accum[symbol] = len(tl)


def _rolling_pct_change(symbol: str, window_minutes: int) -> Optional[float]:
    """O(scan) — scan deque backward to find the reference price at the
    start of the window. Returns None if insufficient data."""
    buf = ws_client.get_tick_buffer(symbol)
    if len(buf) < 2:
        return None
    now_ts = buf[-1][0]
    current_ltp = buf[-1][1]
    cutoff_ts = now_ts - (window_minutes * 60)
    ref_price = None
    for ts, ltp in reversed(buf):
        if ts <= cutoff_ts:
            ref_price = ltp
            break
    if ref_price is None or ref_price <= 0:
        return None
    return ((current_ltp - ref_price) / ref_price) * 100.0


def _momentum_consistency(buf, window_minutes: int) -> float:
    """Return fraction of ticks in the window that were above the
    window's midpoint price (open+close)/2.

    A genuine trending move has most ticks above the midpoint.
    A spike-and-drop (enter at peak) has most ticks at or below it.

    Returns a float in [0, 1]. High = consistent uptrend. Low = spike.
    """
    if len(buf) < 4:
        return 0.5   # not enough data — neutral

    now_ts  = buf[-1][0]
    cutoff  = now_ts - (window_minutes * 60)
    window_prices = [ltp for ts, ltp in buf if ts >= cutoff and ltp > 0]

    if len(window_prices) < 2:
        return 0.5

    midpoint = (window_prices[0] + window_prices[-1]) / 2.0
    above    = sum(1 for p in window_prices if p >= midpoint)
    return above / len(window_prices)


def _vwap_estimate(buf) -> Optional[float]:
    """Simple arithmetic mean of all LTPs in buffer as VWAP proxy.
    WS mode-1 gives no volume, so we can't do a true volume-weighted avg.
    Price-average is a reasonable intraday approximation."""
    prices = [ltp for _ts, ltp in buf if ltp > 0]
    if not prices:
        return None
    return sum(prices) / len(prices)


@dataclass
class Candidate:
    symbol: str
    window_minutes: int
    pct_change: float
    current_ltp: float
    tick_activity: int           # tick count in last 5m (volume proxy)
    composite_score: float = 0.0
    window_label: str = field(init=False)

    def __post_init__(self):
        self.window_label = f"{self.window_minutes}m"


def scan(open_symbols: Optional[Set[str]] = None) -> List[Candidate]:
    """Run a full scan across all subscribed symbols and all four windows.
    Returns a list of Candidate objects sorted by composite_score descending.
    open_symbols: set of symbol strings already holding a scalp position.

    Composite score = pct_change × volume_weight × range_mult
                                 × consistency_mult × window_conviction_mult

    All multipliers documented at the top of this file.
    """
    open_symbols = open_symbols or set()
    candidates: List[Candidate] = []

    subscribed = list(ws_client._token_to_symbol.values()) or list(
        ws_client._tick_buffers.keys()
    )

    for symbol in subscribed:
        if symbol in open_symbols:
            continue

        buf = ws_client.get_tick_buffer(symbol)
        if len(buf) < 2:
            continue

        current_ltp = buf[-1][1]
        if current_ltp <= 0:
            continue

        tick_count = _volume_accum.get(symbol, 0)
        if tick_count < max(1, int(config.MIN_AVG_VOLUME / 5000)):
            continue

        # ── Range-position multiplier (computed once, shared across windows) ──
        _prices     = [p for _t, p in buf if p > 0]
        _range_mult = 1.0
        if len(_prices) >= 2:
            _day_low  = min(_prices)
            _day_high = max(_prices)
            _span     = _day_high - _day_low
            if _span > 1e-6:
                _rpos = max(0.0, min(1.0, (current_ltp - _day_low) / _span))
                if _rpos >= _RPOS_HEAVY_PENALTY[0]:
                    _range_mult = _RPOS_HEAVY_PENALTY[1]
                elif _rpos >= _RPOS_SOFT_PENALTY[0]:
                    _range_mult = _RPOS_SOFT_PENALTY[1]
                elif _rpos <= _RPOS_NEAR_LOW_BONUS[0]:
                    _range_mult = _RPOS_NEAR_LOW_BONUS[1]

        # ── VWAP-extension penalty (if LTP >> session average → already extended) ──
        _vwap = _vwap_estimate(buf)
        _vwap_mult = 1.0
        if _vwap and _vwap > 0:
            _vwap_dev_pct = (current_ltp - _vwap) / _vwap * 100.0
            if _vwap_dev_pct >= _VWAP_EXTENDED_PCT:
                _vwap_mult = _VWAP_EXTENDED_MULT   # extended vs session avg

        for win_minutes, threshold in _WINDOW_THRESHOLDS.items():
            pct = _rolling_pct_change(symbol, win_minutes)
            if pct is None or pct < threshold:
                continue

            # ── Momentum consistency gate ──────────────────────────────────
            consistency = _momentum_consistency(buf, win_minutes)
            if consistency >= _CONSISTENCY_STRONG:
                _cons_mult = _CONSISTENCY_STRONG_MULT   # steady uptrend
            elif consistency < _CONSISTENCY_WEAK:
                _cons_mult = _CONSISTENCY_WEAK_MULT     # spike-and-drop
            else:
                _cons_mult = 1.0

            # ── Volume weight (per-window cap for 1m noise control) ────────
            vol_floor  = max(config.MIN_AVG_VOLUME / 5000, 1)
            vol_cap    = _VOLUME_WEIGHT_CAP.get(win_minutes, 3.0)
            volume_weight = min(max(tick_count, 1) / vol_floor, vol_cap)

            # ── Window conviction bonus ────────────────────────────────────
            win_mult = _WINDOW_CONVICTION_MULT.get(win_minutes, 1.0)

            score = (
                pct
                * volume_weight
                * _range_mult
                * _vwap_mult
                * _cons_mult
                * win_mult
            )

            if score <= 0:
                continue

            candidates.append(Candidate(
                symbol=symbol,
                window_minutes=win_minutes,
                pct_change=round(pct, 4),
                current_ltp=current_ltp,
                tick_activity=tick_count,
                composite_score=round(score, 4),
            ))

    candidates.sort(key=lambda c: c.composite_score, reverse=True)
    return candidates


def on_tick_hook(symbol: str, ltp: float, volume: int, ts: float) -> None:
    """Called by ws_client on every incoming tick. Updates volume proxy."""
    _update_volume(symbol, ts)
