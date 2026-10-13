"""Group 349: one place that decides whether the Market Board zones/swings come from the legacy code or from the new engine adapter.

BOARD_USE_MS_ENGINE=0 (default) -> legacy `screening.market_structure` (unchanged behaviour).
BOARD_USE_MS_ENGINE=1           -> `stockky_ms.compat_board` (same output shape; one documented one-bar difference in zone status).
Any import or runtime error in the new path falls back to the legacy result for that call, so the board never breaks because of it.
"""
from __future__ import annotations

import logging
from typing import List, Tuple

import config
from screening import market_structure as ms

logger = logging.getLogger("position-stocks.ms_switch")

try:
    from stockky_ms import compat_board as _cb
except Exception:  # noqa: BLE001 - package not vendored in this image
    _cb = None


def engine_active() -> bool:
    return bool(getattr(config, "BOARD_USE_MS_ENGINE", False)) and _cb is not None


def zones_and_swings(bars, atr_mult: float, full_range: bool) -> Tuple[List[dict], List[dict]]:
    if engine_active():
        try:
            return _cb.find_zones(bars, atr_mult=atr_mult, full_range=full_range), _cb.swings(bars)
        except Exception as e:  # noqa: BLE001
            logger.warning("ms engine board path failed (%s); using legacy", e)
    return ms.find_zones(bars, atr_mult=atr_mult, full_range=full_range), ms.swings(bars)
