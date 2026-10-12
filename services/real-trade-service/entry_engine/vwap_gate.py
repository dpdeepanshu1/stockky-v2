"""entry_engine/vwap_gate.py - VWAP confirmation before a BUY (group 330, 2026-10-12).

WHY: the day's volume-weighted average price is where the average buyer today is in profit or loss. A long taken BELOW it is buying
into a stock whose sellers are in control; every bounce meets sellers who want out at VWAP. Dhan's `average_price` is that value and
rides on the market-data row as `avg_price` (Tick.vwap).

RULE: price < vwap * (1 - config.ENTRY_VWAP_TOLERANCE_PCT / 100) -> reject (VWAP_BELOW).
FAIL OPEN: no / non-positive / NaN VWAP, or a VWAP more than 15 % away from the price (a stale or wrong value, not a real average) is
"no opinion". Pure, never raises. Off with ENTRY_VWAP_GATE_ENABLED=false.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import config

logger = logging.getLogger("real-trade-vwap-gate")

_MAX_SANE_DISTANCE = 0.15


def _pos(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def reject_reason(price, vwap) -> Optional[str]:
    try:
        if not config.ENTRY_VWAP_GATE_ENABLED:
            return None
        p, v = _pos(price), _pos(vwap)
        if p is None or v is None:
            return None
        if abs(p - v) / v > _MAX_SANE_DISTANCE:
            return None
        tol = max(0.0, float(config.ENTRY_VWAP_TOLERANCE_PCT or 0.0))
        if p < v * (1.0 - tol / 100.0):
            return (f"VWAP_BELOW: price {p:.2f} is {(v - p) / v * 100:.2f}% under the day's VWAP {v:.2f} "
                    "(sellers in control) - not a long entry")
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("vwap gate failed (allowing): %s", e)
        return None


def reject_reason_tick(tick) -> Optional[str]:
    try:
        return reject_reason(getattr(tick, "price", None), getattr(tick, "vwap", None))
    except Exception:  # noqa: BLE001
        return None
