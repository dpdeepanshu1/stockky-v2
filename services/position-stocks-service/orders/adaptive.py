"""
orders/adaptive.py — Adaptive target and stop-loss computation.

2026-09-15 calibration (session42 — "dig more, calibrate more efficient"):
══════════════════════════════════════════════════════════════════════════

PROBLEM WITH PREVIOUS APPROACH:
  Target and stop were computed as fixed multiples of pct_change:
    target = pct_change × 2.0
    stop   = pct_change × 1.5
  This is wrong in two key ways:

  1. When a 1m window fires at exactly its 0.7% threshold:
       target = 1.4%,  stop = 1.05%  → R:R = 1.33 (below 1.8 floor)
     The R:R enforcement then bumps target to 1.89%, but that 1.89% target
     for a position-stock (held overnight) is far too tight — the stock
     will hit that in 30 minutes of normal noise and then reverse.

  2. When a 60m window fires at 2.5%+:
       target = 5.0%,  stop = 3.75%  → reasonable, but ignores whether
     the stock has 5% room left within today's price range. It doesn't.

  REAL-TRADE-SERVICE's approach (entry_engine/entry.py) is correct:
     stop_pct   = clamp(ATR_pct × 1.5, MIN_STOP, MAX_STOP)
     target_pct = stop_pct × (3.0 / 1.5)   → always 2.0:1 R:R by design

  We apply the same here — derive a VOLATILITY-BASED stop from the
  intraday tick buffer's own price standard deviation (ATR proxy), then
  set target as stop × TARGET_RR_RATIO.

WHAT THIS MODULE NOW DOES:

1. ATR-PROXY FROM TICK BUFFER:
   Compute the rolling true-range proxy from the last N ticks in the
   ws_client buffer. Take the average of abs(tick[i] - tick[i-1]) across
   the last 20 ticks → ATR_proxy in ₹ → ATR_pct = ATR_proxy / LTP × 100.
   If buffer too short, fall back to pct_change × 0.60 as stop (tighter
   than before, since we know the stock just moved pct_change already).

2. ATR-BASED STOP & TARGET:
     stop_pct   = clamp(ATR_pct × ATR_STOP_MULT, MIN_STOP, MAX_STOP)
     target_pct = stop_pct × TARGET_RR_RATIO     (2.2:1 — slightly above
                                                   real-trade's 2.0 because
                                                   position stocks are held
                                                   longer, need more reward)

3. RANGE-POSITION ADJUSTMENT (carried from session41b, recalibrated):
   range_pos ≥ 0.80 → tighten stop×0.80, target×0.75 (less room up)
   range_pos ≤ 0.20 → widen  target×1.15 (room to bounce)
   Critically: after adjustment, re-enforce MIN_RR so tightening never
   produces a bad R:R trade.

4. MINIMUM R:R = 2.0 (raised from 1.8 — matching real-trade-service).

5. BREAKEVEN TRIGGER at 40% of target (was 50%).
   At 40% of target the position is far enough in profit that moving the
   stop to entry is the right call — locking in the free-ride sooner
   than the previous 50% threshold that was letting profits evaporate.

6. Returns AdaptiveLevels with both pct and ₹ prices, plus range_regime
   and the ATR_proxy used (for dashboard visibility / debugging).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import config
from execution.dhan_client import round_to_tick

logger = logging.getLogger("position-stocks-adaptive")

# ── Core calibration constants ────────────────────────────────────────────────
ATR_STOP_MULT   = 1.5    # stop  = ATR_pct × 1.5   (mirrors real-trade-service)
TARGET_RR_RATIO = 2.2    # target = stop × 2.2      (slightly above RT's 2.0)
MIN_REWARD_RISK = 2.0    # floor — never enter < 2:1 R:R
ATR_LOOKBACK    = 20     # tick samples for ATR proxy (last 20 ticks ≈ 3-5 min)
BREAKEVEN_FRAC  = 0.40   # move stop to entry once unrealised gain ≥ 40% of target

# ── Range-position regime adjustments ────────────────────────────────────────
_NEAR_HIGH_THRESHOLD   = 0.80
_NEAR_LOW_THRESHOLD    = 0.20
_NEAR_HIGH_TARGET_MULT = 0.75  # tighten target: less upside room
_NEAR_HIGH_STOP_MULT   = 0.80  # tighten stop: protect sooner on reversal
_NEAR_LOW_TARGET_MULT  = 1.15  # widen target: room to bounce from day-low


@dataclass
class AdaptiveLevels:
    target_pct:            float
    stop_pct:              float
    target_price:          float
    stop_price:            float
    breakeven_trigger_pct: float = 0.0
    range_regime:          str   = "neutral"
    range_position:        Optional[float] = None
    atr_proxy_pct:         Optional[float] = None   # for dashboard/debug


def _atr_proxy_pct(symbol: str, current_ltp: float) -> Optional[float]:
    """Compute ATR proxy from last ATR_LOOKBACK ticks in the tick buffer.
    Returns ATR as % of current_ltp, or None if insufficient data."""
    try:
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        prices = [ltp for _ts, ltp in buf if ltp > 0]
        if len(prices) < 4:
            return None
        # Take last ATR_LOOKBACK prices
        sample = prices[-ATR_LOOKBACK:]
        if len(sample) < 2:
            return None
        tick_ranges = [abs(sample[i] - sample[i - 1]) for i in range(1, len(sample))]
        avg_range   = sum(tick_ranges) / len(tick_ranges)
        if avg_range <= 0 or current_ltp <= 0:
            return None
        return (avg_range / current_ltp) * 100.0
    except Exception as e:
        logger.debug("adaptive: ATR proxy fetch failed for %s: %s", symbol, e)
        return None


def _intraday_range_position(symbol: str, current_ltp: float) -> Optional[float]:
    """Return range_position in [0, 1]: where LTP sits within today's
    intraday high/low derived from the tick buffer. None if unavailable."""
    try:
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        prices = [p for _t, p in buf if p > 0]
        if len(prices) < 2:
            return None
        day_low  = min(prices)
        day_high = max(prices)
        span     = day_high - day_low
        if span <= 1e-6:
            return None
        return max(0.0, min(1.0, (current_ltp - day_low) / span))
    except Exception as e:
        logger.debug("adaptive: range position fetch failed for %s: %s", symbol, e)
        return None


def compute(
    pct_change: float,
    current_ltp: float,
    symbol: Optional[str] = None,
) -> AdaptiveLevels:
    """Compute adaptive target/stop for a BUY scalp trade.

    pct_change:  rolling-window % that triggered the signal (positive).
    current_ltp: live price at the moment of signal (entry reference).
    symbol:      if given, reads tick buffer for ATR proxy and range position.
                 Falls back gracefully if buffer unavailable.
    """
    # ── Step 1: ATR-proxy based stop and target ────────────────────────────
    atr_pct = _atr_proxy_pct(symbol, current_ltp) if symbol else None

    if atr_pct is not None and atr_pct > 0:
        raw_stop = atr_pct * ATR_STOP_MULT
    else:
        # Fallback: use 60% of pct_change as stop.
        # Rationale: stock just moved pct_change%; our stop should sit
        # below the current price by a smaller margin than the move itself
        # (otherwise we're stopping out on a normal retracement of the
        # very signal that triggered us). 60% of the move as stop gives
        # ~1.67× the signal move as target headroom after R:R enforcement.
        raw_stop = pct_change * 0.60

    stop_pct   = max(config.MIN_STOP_PCT, min(raw_stop, config.MAX_STOP_PCT))
    target_pct = stop_pct * TARGET_RR_RATIO
    target_pct = min(target_pct, config.MAX_TARGET_PCT)

    # ── Step 2: Range-position adjustment ─────────────────────────────────
    range_regime  = "neutral"
    range_pos_val: Optional[float] = None

    if symbol:
        range_pos_val = _intraday_range_position(symbol, current_ltp)
        if range_pos_val is not None:
            if range_pos_val >= _NEAR_HIGH_THRESHOLD:
                range_regime = "near_high"
                # Stock is near today's peak — less room to run, higher
                # reversal risk. Tighten both target (be less greedy) and
                # stop (exit faster on reversal).
                target_pct = max(
                    config.MIN_TARGET_PCT,
                    min(target_pct * _NEAR_HIGH_TARGET_MULT, config.MAX_TARGET_PCT),
                )
                stop_pct = max(
                    config.MIN_STOP_PCT,
                    min(stop_pct * _NEAR_HIGH_STOP_MULT, config.MAX_STOP_PCT),
                )
                logger.debug(
                    "adaptive: %s near_high (pos=%.2f) → target=%.2f%% stop=%.2f%%",
                    symbol, range_pos_val, target_pct, stop_pct,
                )
            elif range_pos_val <= _NEAR_LOW_THRESHOLD:
                range_regime = "near_low"
                # Stock is near today's trough — momentum buy at a relative
                # discount means more room upward. Widen target.
                target_pct = max(
                    config.MIN_TARGET_PCT,
                    min(target_pct * _NEAR_LOW_TARGET_MULT, config.MAX_TARGET_PCT),
                )
                logger.debug(
                    "adaptive: %s near_low (pos=%.2f) → target=%.2f%% stop=%.2f%%",
                    symbol, range_pos_val, target_pct, stop_pct,
                )

    # ── Step 3: Enforce minimum R:R = 2.0 ─────────────────────────────────
    if stop_pct > 0 and (target_pct / stop_pct) < MIN_REWARD_RISK:
        target_pct = round(stop_pct * MIN_REWARD_RISK, 4)
        target_pct = min(target_pct, config.MAX_TARGET_PCT)
        logger.debug(
            "adaptive: %s R:R enforcement → target bumped to %.2f%% (stop=%.2f%%)",
            symbol or "?", target_pct, stop_pct,
        )

    # ── Step 4: Breakeven trigger at 40% of target ────────────────────────
    breakeven_trigger_pct = round(target_pct * BREAKEVEN_FRAC, 4)

    # ── Step 5: Convert to prices ──────────────────────────────────────────
    target_price = round_to_tick(current_ltp * (1 + target_pct / 100))
    stop_price   = round_to_tick(current_ltp * (1 - stop_pct   / 100))

    return AdaptiveLevels(
        target_pct=round(target_pct, 4),
        stop_pct=round(stop_pct, 4),
        target_price=target_price,
        stop_price=stop_price,
        breakeven_trigger_pct=breakeven_trigger_pct,
        range_regime=range_regime,
        range_position=range_pos_val,
        atr_proxy_pct=round(atr_pct, 4) if atr_pct is not None else None,
    )
