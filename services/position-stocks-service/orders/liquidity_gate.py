"""orders/liquidity_gate.py - volume / turnover floor on the entry path (group 333, 2026-10-12).

WHY: the only liquidity checks before a BUY were the Rs 20 price floor, the market-cap floor and the best-5 book check (spread / book value). Nothing asked whether the
NAME TRADES: how much it normally turns over, and whether it is trading near that today. A thin name passes every price / depth test at 10:30 and then
cannot be sold at the stop. Three tests on numbers that are already cached (no new call):
  1. AVG TURNOVER   normal daily turnover (mean volume x close over up to 20 completed sessions, screening/prev_day.py) >= ENTRY_MIN_AVG_TURNOVER_CR
                    crore Rs (default 3).                                                          -> LIQUIDITY_AVG_TURNOVER
  2. DAY TURNOVER   from ENTRY_LIQ_MIN_TIME_IST (10:00): today's turnover so far (day volume x price) >= ENTRY_MIN_DAY_TURNOVER_CR crore Rs (1.0).
                                                                                                   -> LIQUIDITY_DAY_TURNOVER
  3. RELATIVE VOLUME from ENTRY_RVOL_MIN_TIME_IST (09:45): day volume / (normal daily volume x share of the session elapsed) >= ENTRY_MIN_RVOL (0.35);
                    the elapsed share is front-loaded: (minutes since 09:15 / 375) ** 0.6.             -> LIQUIDITY_RVOL
A test with unknown inputs (zero / missing volume, no previous-day facts yet) is skipped = no opinion; a floor of 0 turns that test off. FAIL OPEN,
never raises. Off with ENTRY_LIQUIDITY_GATE_ENABLED=0.
Reads the cached /quote row (orders.depth_gate._fresh_quote) and cached previous-day facts (screening.prev_day.get: a miss starts a background
fetch and is "unknown" this cycle, like the opening gate).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

import config
from orders import depth_gate
from tz_utils import ist_now, ist_time_at_or_after, parse_hhmm

logger = logging.getLogger("position-stocks-liquidity-gate")

_CRORE = 1e7
_SESSION_MINUTES = 375.0          # 09:15 - 15:30
_MAX_ELAPSED = 1.0


def _pos(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _cfg(name: str, dflt: float) -> float:
    try:
        return float(getattr(config, name, dflt))
    except (TypeError, ValueError):
        return float(dflt)


def elapsed_fraction(now: Optional[datetime] = None) -> float:
    """Share of the normal day's volume expected to have traded by now (front-loaded, 0..1)."""
    n = ist_now(now)
    mins = (n.hour * 60 + n.minute + n.second / 60.0) - (9 * 60 + 15)
    return 0.0 if mins <= 0 else min(_MAX_ELAPSED, mins / _SESSION_MINUTES) ** 0.6


def decide(price, day_volume, avg_volume, avg_turnover, now: Optional[datetime] = None) -> Optional[str]:
    """Pure decision from numbers: a "LIQUIDITY_*:..." reason or None. Never raises."""
    try:
        p, dv, av, at = _pos(price), _pos(day_volume), _pos(avg_volume), _pos(avg_turnover)
        min_avg = _cfg("ENTRY_MIN_AVG_TURNOVER_CR", 3.0)
        if min_avg > 0 and at is not None and at < min_avg * _CRORE:
            return f"LIQUIDITY_AVG_TURNOVER:normal daily turnover Rs {at / _CRORE:.2f} cr < floor Rs {min_avg:.2f} cr - too thin to scalp"
        min_day = _cfg("ENTRY_MIN_DAY_TURNOVER_CR", 1.0)
        if (min_day > 0 and p is not None and dv is not None
                and ist_time_at_or_after(parse_hhmm(str(getattr(config, "ENTRY_LIQ_MIN_TIME_IST", "10:00")), 10, 0), now)
                and dv * p < min_day * _CRORE):
            return f"LIQUIDITY_DAY_TURNOVER:traded only Rs {dv * p / _CRORE:.2f} cr today < floor Rs {min_day:.2f} cr - no depth to exit into"
        min_rv = _cfg("ENTRY_MIN_RVOL", 0.35)
        if (min_rv > 0 and dv is not None and av is not None
                and ist_time_at_or_after(parse_hhmm(str(getattr(config, "ENTRY_RVOL_MIN_TIME_IST", "09:45")), 9, 45), now)):
            frac = elapsed_fraction(now)
            if frac > 0 and dv / (av * frac) < min_rv:
                return (f"LIQUIDITY_RVOL:volume is {dv / (av * frac):.2f}x its normal pace for this time (< {min_rv:.2f}x) - "
                        "the name is not trading today")
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("liquidity gate decide failed (allowing): %s", e)
        return None


def reject_reason(symbol: str, ltp, now: Optional[datetime] = None) -> Optional[str]:
    """A skip reason or None when the entry may go ahead. Never raises."""
    try:
        if not getattr(config, "ENTRY_LIQUIDITY_GATE_ENABLED", True):
            return None
        from screening import prev_day
        pdv = prev_day.get(str(symbol or "").upper().replace(".NS", "").strip())
        q = depth_gate._fresh_quote(symbol)
        return decide(ltp, (q or {}).get("volume"), getattr(pdv, "avg_volume", None), getattr(pdv, "avg_turnover", None), now)
    except Exception as e:  # noqa: BLE001
        logger.debug("liquidity gate %s failed (allowing): %s", symbol, e)
        return None
