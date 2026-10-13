"""orders/sizing.py - whole-share quantity for a risk-based position budget (group 340, 2026-10-12).
WHY: max(1, int(budget / ltp)) floored (1.9 shares -> 1: CLEANMAX) and forced a share even when ONE share cost several times the budget.
RULES (SIZING_ROUNDING_ENABLED=0 restores the old behaviour): round UP one share when within SIZING_MAX_OVERSHOOT_PCT of the budget;
when one share costs more than SIZING_ONE_SHARE_MAX_MULT x the budget -> None (skip); otherwise floor / one share. Never raises.
"""
from __future__ import annotations

import logging
from typing import Optional

import config

logger = logging.getLogger("position-stocks-sizing")


def legacy_quantity(position_value: float, price: float) -> int:
    return max(1, int(position_value / price))


def compute_quantity(position_value: float, price: float) -> Optional[int]:
    """Whole shares for `position_value` rupees at `price`, or None when the entry should be skipped."""
    try:
        if price <= 0:
            return 1
        if position_value <= 0:
            return legacy_quantity(0.0, price)
        if not getattr(config, "SIZING_ROUNDING_ENABLED", True):
            return legacy_quantity(position_value, price)
        floor_qty = int(position_value / price)
        over = 1.0 + max(0.0, float(config.SIZING_MAX_OVERSHOOT_PCT)) / 100.0
        if floor_qty >= 1:
            return floor_qty + 1 if (floor_qty + 1) * price <= position_value * over else floor_qty
        if price <= position_value * over:
            return 1
        mult = float(config.SIZING_ONE_SHARE_MAX_MULT)
        if mult > 0 and price > position_value * mult:
            return None
        return 1
    except Exception as e:  # noqa: BLE001
        logger.debug("sizing failed (using floor): %s", e)
        try:
            return legacy_quantity(position_value, price)
        except Exception:  # noqa: BLE001
            return 1
