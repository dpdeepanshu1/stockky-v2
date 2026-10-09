"""entry_engine/opening_gate.py - opening-quality gate for automatic entries (group 268, 2026-10-09).

WHY: the opening guard (group 220) used to hold every entry until 09:30. The entry window now opens at 09:15, but the scalp
service's 14-day breakdown showed entries before 10:30 won 1 of 10, so between the open and OPENING_GATE_SETTLE_IST (default
10:00) an automatic entry must pass the checks below. Afterwards nothing here applies.

CHECKS (market_feed.Tick fields only; the tick has no day-open price, so change vs previous close is the gap proxy):
  1. at least OPENING_GATE_MIN_MINUTES_AFTER_OPEN minutes since 09:15
  2. price vs previous close within [OPENING_GATE_MIN_CHANGE_PCT, OPENING_GATE_MAX_CHANGE_PCT]
  3. price not in the top (1 - OPENING_GATE_MAX_RANGE_POS) of the day high/low range (0 disables this check)
  4. (group 270) previous day closed in the upper half of its own range (entry_engine/prev_day.py, background-fetched daily
     candle; a cache miss holds the candidate back for that cycle)
  5. (group 270) the stop is not tighter than OPENING_GATE_MIN_STOP_ATR_FRAC x the daily ATR % (the tick's ATR). Here the
     stop is itself ATR-derived, so this mainly catches a flat-fallback stop (no usable ATR) or an ATR the stop logic clamped.

FAIL CLOSED: a tick without previous close (or without a usable day range while check 3 is on) is held back, not allowed.
A candidate that is held back stays queued and is looked at again next cycle (the caller does not consume it).

Pure function; never raises into the caller (an internal error holds the candidate back while the gate is active).
"""
from __future__ import annotations

import logging
from datetime import datetime, time as _time
from typing import Optional

import config
from entry_engine import opening_guard, prev_day
from tz_utils import ist_now, is_market_open_ist, parse_hhmm

logger = logging.getLogger("opening-entry-gate")

_OPEN = _time(9, 15)


def is_active(mode: str, now: Optional[datetime] = None) -> bool:
    try:
        if not config.OPENING_GATE_ENABLED or not opening_guard.covers(mode):
            return False
        if not is_market_open_ist(now):
            return False
        return _OPEN <= ist_now(now).time() < parse_hhmm(config.OPENING_GATE_SETTLE_IST, 10, 0)
    except Exception:
        return False


_shadow_seen: set = set()


def shadow_active(mode: str, now: Optional[datetime] = None) -> bool:
    try:
        return bool(config.OPENING_GATE_SHADOW) and is_active(mode, now)
    except Exception:
        return False


def shadow_first_time(mode: str, symbol: str, now: Optional[datetime] = None) -> bool:
    key = (ist_now(now).date().isoformat(), mode, symbol)
    if key in _shadow_seen:
        return False
    if len(_shadow_seen) > 5000:
        _shadow_seen.clear()
    _shadow_seen.add(key)
    return True


def reset_shadow() -> None:
    _shadow_seen.clear()


def reject_reason(mode: str, tick, now: Optional[datetime] = None, stop_pct: Optional[float] = None) -> Optional[str]:
    """None when the entry may be evaluated (or the gate is not active), else a short reason starting with OPENING_GATE."""
    try:
        if not is_active(mode, now):
            return None
        n = ist_now(now)
        minutes_open = (n.hour * 60 + n.minute + n.second / 60.0) - (9 * 60 + 15)
        if minutes_open < config.OPENING_GATE_MIN_MINUTES_AFTER_OPEN:
            return (f"OPENING_GATE:TOO_EARLY:{minutes_open:.1f}m since open "
                    f"< {config.OPENING_GATE_MIN_MINUTES_AFTER_OPEN:.0f}m")
        price = getattr(tick, "price", None)
        if not price or price <= 0:
            return "OPENING_GATE:NO_PRICE"
        prev = getattr(tick, "prev_close", None)
        if not prev or prev <= 0:
            return "OPENING_GATE:NO_PREV_CLOSE"
        chg = (price - prev) / prev * 100.0
        if chg < config.OPENING_GATE_MIN_CHANGE_PCT:
            return f"OPENING_GATE:BELOW_PREV_CLOSE:{chg:+.2f}% < {config.OPENING_GATE_MIN_CHANGE_PCT:+.2f}%"
        if chg > config.OPENING_GATE_MAX_CHANGE_PCT:
            return f"OPENING_GATE:EXTENDED:{chg:+.2f}% > {config.OPENING_GATE_MAX_CHANGE_PCT:+.2f}%"
        if config.OPENING_GATE_MAX_RANGE_POS > 0:
            dh = getattr(tick, "day_high", None)
            dl = getattr(tick, "day_low", None)
            if not dh or not dl or dh <= dl:
                return "OPENING_GATE:NO_DAY_RANGE"
            rpos = max(0.0, min(1.0, (price - dl) / (dh - dl)))
            if rpos >= config.OPENING_GATE_MAX_RANGE_POS:
                return (f"OPENING_GATE:NEAR_DAY_HIGH:range_pos={rpos:.2f} >= {config.OPENING_GATE_MAX_RANGE_POS:.2f} "
                        f"(day_low={dl:.2f} day_high={dh:.2f})")
        if config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS > 0:
            pdv = prev_day.get(getattr(tick, "symbol", "") or "")
            if pdv is None:
                return "OPENING_GATE:NO_PREV_DAY_DATA"
            cp = pdv.close_pos
            if cp is not None and cp < config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS:
                return (f"OPENING_GATE:PREV_DAY_WEAK_CLOSE:close_pos={cp:.2f} < {config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS:.2f} "
                        f"(prev {pdv.date} low={pdv.low:.2f} high={pdv.high:.2f} close={pdv.close:.2f})")
        if config.OPENING_GATE_MIN_STOP_ATR_FRAC > 0 and stop_pct is not None:
            atr = getattr(tick, "atr", None)
            if not atr or atr <= 0:
                return "OPENING_GATE:NO_ATR_DATA"
            atr_pct = atr / price * 100.0
            floor = config.OPENING_GATE_MIN_STOP_ATR_FRAC * atr_pct
            if stop_pct < floor:
                return (f"OPENING_GATE:STOP_TOO_TIGHT:stop {stop_pct:.2f}% < {config.OPENING_GATE_MIN_STOP_ATR_FRAC:.2f} x "
                        f"daily ATR {atr_pct:.2f}% = {floor:.2f}%")
        return None
    except Exception as e:
        logger.warning("opening gate error (holding candidate back): %s", e)
        return f"OPENING_GATE:ERROR:{type(e).__name__}"
