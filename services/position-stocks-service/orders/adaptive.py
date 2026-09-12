"""
orders/adaptive.py — Adaptive target and stoploss computation.

Given a candidate's pct_change and current LTP, computes an adaptive
target and stoploss that:
  - Fall within the configured bands (MIN_TARGET_PCT to MAX_TARGET_PCT,
    MIN_STOP_PCT to MAX_STOP_PCT).
  - Scale with momentum: a stronger move gets a wider target but also a
    tighter stoploss (the stock has shown momentum and shouldn't need much
    room to breathe). A weaker qualifying move gets a proportional target.
  - Are validated as valid tick multiples before being returned (Dhan
    will reject legs with invalid prices — see dhan_client.round_to_tick).

Formula (first pass — can be calibrated later):
  target_pct = clip(pct_change * MOMENTUM_TARGET_MULTIPLIER,
                    MIN_TARGET_PCT, MAX_TARGET_PCT)
  stop_pct   = clip(pct_change * MOMENTUM_STOP_FRACTION,
                    MIN_STOP_PCT, MAX_STOP_PCT)

MOMENTUM_TARGET_MULTIPLIER: if pct_change=1.5, target=3% (2× move).
MOMENTUM_STOP_FRACTION: stop = 1.5× the triggering move size, capped.
"""
from __future__ import annotations

from dataclasses import dataclass

import config
from execution.dhan_client import round_to_tick

MOMENTUM_TARGET_MULTIPLIER = 2.0    # target = 2× the triggering pct_change
MOMENTUM_STOP_FRACTION     = 1.5    # stop   = 1.5× the triggering pct_change


@dataclass
class AdaptiveLevels:
    target_pct:   float
    stop_pct:     float
    target_price: float
    stop_price:   float


def compute(pct_change: float, current_ltp: float) -> AdaptiveLevels:
    """Compute adaptive target/stop for a BUY scalp trade.
    pct_change: the rolling-window % that triggered the signal (positive).
    current_ltp: live price at the moment of signal (used as entry reference).
    """
    raw_target = pct_change * MOMENTUM_TARGET_MULTIPLIER
    raw_stop   = pct_change * MOMENTUM_STOP_FRACTION

    target_pct = max(config.MIN_TARGET_PCT, min(raw_target, config.MAX_TARGET_PCT))
    stop_pct   = max(config.MIN_STOP_PCT,   min(raw_stop,   config.MAX_STOP_PCT))

    target_price = round_to_tick(current_ltp * (1 + target_pct / 100))
    stop_price   = round_to_tick(current_ltp * (1 - stop_pct   / 100))

    return AdaptiveLevels(
        target_pct=round(target_pct, 4),
        stop_pct=round(stop_pct, 4),
        target_price=target_price,
        stop_price=stop_price,
    )
