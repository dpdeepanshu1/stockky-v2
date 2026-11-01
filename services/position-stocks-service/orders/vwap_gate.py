"""orders/vwap_gate.py - VWAP confirmation before a scalp BUY (group 333, 2026-10-12; real-trade-service has had the same gate since group 330).

WHY: the day's volume-weighted average price is where the average buyer today is in profit or loss. A long taken BELOW it buys into a stock whose
sellers are in control. Dhan's `average_price` is that value and rides on the market-data /quote row as `avg_price`. The scan engine only had a
tick-average ESTIMATE (a score penalty far ABOVE it); nothing refused a buy under the real VWAP.

RULES (config)
  price < vwap * (1 - ENTRY_VWAP_TOLERANCE_PCT/100)            -> skip (VWAP_BELOW)                  default tolerance 0.15 %
  price > vwap * (1 + ENTRY_VWAP_MAX_EXTENSION_PCT/100)        -> skip (VWAP_EXTENDED), 0 = off      default off (the engine already penalises it)
  Only a fresh quote (the depth gate's age check) and a VWAP within 15 % of the price are judged (beyond that it is a stale / wrong value, not an
  average). Before ENTRY_VWAP_MIN_TIME_IST (09:45) the average rests on too few trades: no opinion.
FAIL OPEN: no body / no / non-positive / NaN avg_price / old quote / any error = no opinion. Never raises. Off with ENTRY_VWAP_GATE_ENABLED=0.
Reads the same cached /quote row as the depth and circuit gates (orders.depth_gate._fresh_quote): no extra network call.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

import config
from orders import depth_gate
from tz_utils import ist_time_at_or_after, parse_hhmm

logger = logging.getLogger("position-stocks-vwap-gate")

_MAX_SANE_DISTANCE = 0.15


def _pos(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def decide_scored(price, vwap):
    """(reason, penalty) from numbers. E273 round 9: the penalty is the distance over its own limit: for VWAP_BELOW the % under the VWAP divided by the tolerance
    (ENTRY_VWAP_TOLERANCE_PCT, floor 0.05), for VWAP_EXTENDED the % over divided by ENTRY_VWAP_MAX_EXTENSION_PCT; capped (orders/soft_gates.py). (None, None) = fine. Never raises."""
    try:
        from orders import soft_gates
        p, v = _pos(price), _pos(vwap)
        if p is None or v is None or abs(p - v) / v > _MAX_SANE_DISTANCE:
            return None, None
        tol = max(0.0, float(getattr(config, "ENTRY_VWAP_TOLERANCE_PCT", 0.15) or 0.0))
        if p < v * (1.0 - tol / 100.0):
            gap = (v - p) / v * 100.0
            return (f"VWAP_BELOW:price {p:.2f} is {gap:.2f}% under the day's VWAP {v:.2f} (sellers in control) - not a long entry",
                    soft_gates.ratio_penalty(gap, max(tol, 0.05)) or 1.0)
        ext = max(0.0, float(getattr(config, "ENTRY_VWAP_MAX_EXTENSION_PCT", 0.0) or 0.0))
        if ext > 0 and p > v * (1.0 + ext / 100.0):
            over = (p - v) / v * 100.0
            return (f"VWAP_EXTENDED:price {p:.2f} is {over:.2f}% over the day's VWAP {v:.2f} (> {ext:.1f}%) - extended, not chasing",
                    soft_gates.ratio_penalty(over, ext) or 1.0)
        return None, None
    except Exception as e:  # noqa: BLE001
        logger.debug("vwap gate decide failed (allowing): %s", e)
        return None, None


def decide(price, vwap) -> Optional[str]:
    """Pure decision from numbers: a "VWAP_BELOW:..." / "VWAP_EXTENDED:..." reason or None. Never raises."""
    return decide_scored(price, vwap)[0]


def reject_reason(symbol: str, ltp, now: Optional[datetime] = None) -> Optional[str]:
    """A skip reason or None when the entry may go ahead. Reads the cached /quote row. Never raises."""
    try:
        if not getattr(config, "ENTRY_VWAP_GATE_ENABLED", True):
            return None
        if not ist_time_at_or_after(parse_hhmm(str(getattr(config, "ENTRY_VWAP_MIN_TIME_IST", "09:45")), 9, 45), now):
            return None
        q = depth_gate._fresh_quote(symbol)
        return decide(ltp, q.get("avg_price")) if q else None
    except Exception as e:  # noqa: BLE001
        logger.debug("vwap gate %s failed (allowing): %s", symbol, e)
        return None


def penalty(symbol: str, ltp, now: Optional[datetime] = None) -> Optional[dict]:
    """E273 round 9: {"penalty": float, "why": reason} when the price is past the VWAP limits (same switch, time and quote rules as reject_reason), else None. Never raises."""
    try:
        if not getattr(config, "ENTRY_VWAP_GATE_ENABLED", True):
            return None
        if not ist_time_at_or_after(parse_hhmm(str(getattr(config, "ENTRY_VWAP_MIN_TIME_IST", "09:45")), 9, 45), now):
            return None
        q = depth_gate._fresh_quote(symbol)
        why, pen = decide_scored(ltp, q.get("avg_price")) if q else (None, None)
        return {"penalty": float(pen), "why": why} if why and pen else None
    except Exception as e:  # noqa: BLE001
        logger.debug("vwap gate penalty %s failed (no opinion): %s", symbol, e)
        return None
