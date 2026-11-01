"""entry_engine/opening_gate.py - opening-quality gate for automatic entries (group 268, 2026-10-09).

WHY: the opening guard (group 220) used to hold every entry until 09:30. The entry window now opens at 09:15, but the scalp
service's 14-day breakdown showed entries before 10:30 won 1 of 10, so between the open and OPENING_GATE_SETTLE_IST (default
10:00) an automatic entry must pass the checks below. Afterwards nothing here applies.

CHECKS (market_feed.Tick fields only; the tick has no day-open price, so change vs previous close is the gap proxy):
  1. at least OPENING_GATE_MIN_MINUTES_AFTER_OPEN minutes since 09:15
  2. price vs previous close within [OPENING_GATE_MIN_CHANGE_PCT, OPENING_GATE_MAX_CHANGE_PCT]; E273 round 27: below the lower limit down to OPENING_GATE_HARD_CHANGE_PCT is a size penalty, not a refusal
  3. (group 390: only from OPENING_GATE_RANGE_AFTER_MIN minutes after the open) price not in the top (1 - OPENING_GATE_MAX_RANGE_POS) of the day high/low range (0 disables this check)
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


def _scale(x: float, lo: float, hi: float) -> float:
    """0 at lo, 1 at hi, clamped; a zero-width span counts as 1."""
    return 1.0 if hi <= lo else max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _down_floor() -> float:
    """Lowest price change vs the previous close that is still only penalised (E273 round 27); with the penalty switch off it is the old limit."""
    lim = float(config.OPENING_GATE_MIN_CHANGE_PCT)
    if not getattr(config, "OPENING_GATE_PENALTY_ENABLED", True):
        return lim
    return min(lim, float(getattr(config, "OPENING_GATE_HARD_CHANGE_PCT", -2.0)))


def soft_down_reason(mode: str, tick, now: Optional[datetime] = None) -> Optional[str]:
    """E273 round 27: the BELOW_PREV_CLOSE text when the price is under the limit but not past the hard ceiling (a size penalty, not a refusal), else None. Never raises."""
    try:
        if not getattr(config, "OPENING_GATE_PENALTY_ENABLED", True) or not is_active(mode, now):
            return None
        price, prev = getattr(tick, "price", None), getattr(tick, "prev_close", None)
        if not price or not prev or price <= 0 or prev <= 0:
            return None
        chg = (price - prev) / prev * 100.0
        if _down_floor() <= chg < config.OPENING_GATE_MIN_CHANGE_PCT:
            return f"OPENING_GATE:BELOW_PREV_CLOSE:{chg:+.2f}% < {config.OPENING_GATE_MIN_CHANGE_PCT:+.2f}% (sized down)"
        return None
    except Exception:  # noqa: BLE001
        return None


def penalty(mode: str, tick, now: Optional[datetime] = None) -> dict:
    """E273 round 17: the SIZE penalty for the two soft opening tests, as {"mult": 0.25..1.0, "reasons": [...]}. mult 1.0 and no reasons when the gate is not active,
    the penalty switch is off, the data is missing (reject_reason already holds those back) or both tests pass. Between the old limit and the hard ceiling the
    multiplier falls from 0.85 to 0.40 per test; the two multiply, floor OPENING_GATE_PENALTY_FLOOR. Never raises."""
    out: dict = {"mult": 1.0, "reasons": []}
    try:
        if not getattr(config, "OPENING_GATE_PENALTY_ENABLED", True) or not is_active(mode, now):
            return out
        n = ist_now(now)
        minutes_open = (n.hour * 60 + n.minute + n.second / 60.0) - (9 * 60 + 15)
        price, mult = getattr(tick, "price", None), 1.0
        if config.OPENING_GATE_MAX_RANGE_POS > 0 and minutes_open >= config.OPENING_GATE_RANGE_AFTER_MIN and price:
            dh, dl = getattr(tick, "day_high", None), getattr(tick, "day_low", None)
            if dh and dl and dh > dl:
                rpos = max(0.0, min(1.0, (price - dl) / (dh - dl)))
                if rpos >= config.OPENING_GATE_MAX_RANGE_POS:
                    m = 0.85 - 0.45 * _scale(rpos, config.OPENING_GATE_MAX_RANGE_POS, float(config.OPENING_GATE_HARD_RANGE_POS))
                    mult *= m
                    out["reasons"].append(f"near_day_high:range_pos={rpos:.2f} x{m:.2f}")
        if config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS > 0:
            pdv = prev_day.get(getattr(tick, "symbol", "") or "")
            cp = getattr(pdv, "close_pos", None) if pdv is not None else None
            if cp is not None and cp < config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS:
                m = 0.85 - 0.45 * _scale(config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS - cp, 0.0, config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS - float(config.OPENING_GATE_HARD_PREVDAY_CLOSE_POS))
                mult *= m
                out["reasons"].append(f"prev_day_weak_close:close_pos={cp:.2f} x{m:.2f}")
        prev = getattr(tick, "prev_close", None)
        if price and prev and soft_down_reason(mode, tick, now):           # E273 round 27: under the previous close but above the hard ceiling
            chg = (price - prev) / prev * 100.0
            m = 0.85 - 0.45 * _scale(config.OPENING_GATE_MIN_CHANGE_PCT - chg, 0.0, config.OPENING_GATE_MIN_CHANGE_PCT - _down_floor())
            mult *= m
            out["reasons"].append(f"below_prev_close:chg={chg:+.2f}% x{m:.2f}")
        out["mult"] = round(max(float(config.OPENING_GATE_PENALTY_FLOOR), mult), 3) if out["reasons"] else 1.0
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("opening gate penalty failed (no penalty): %s", e)
        return {"mult": 1.0, "reasons": []}


def reject_reason(mode: str, tick, now: Optional[datetime] = None, stop_pct: Optional[float] = None, waive_gap_up: bool = False) -> Optional[str]:
    """None when the entry may be evaluated (or the gate is not active), else a short reason starting with OPENING_GATE. waive_gap_up (E273 round 20) skips only the EXTENDED check."""
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
        _down = _down_floor()                  # E273 round 27: between the limit and this ceiling the entry is only sized down (penalty())
        if chg < _down:
            return f"OPENING_GATE:BELOW_PREV_CLOSE:{chg:+.2f}% < {_down:+.2f}%"
        if chg > config.OPENING_GATE_MAX_CHANGE_PCT and not waive_gap_up:      # E273 round 20: an event-explained gap up (gap_event) is not refused here
            return f"OPENING_GATE:EXTENDED:{chg:+.2f}% > {config.OPENING_GATE_MAX_CHANGE_PCT:+.2f}%"
        # group 390: the day range only means something once the opening range has built up (at 09:15:30 high ~ low ~ price)
        if config.OPENING_GATE_MAX_RANGE_POS > 0 and minutes_open >= config.OPENING_GATE_RANGE_AFTER_MIN:
            dh = getattr(tick, "day_high", None)
            dl = getattr(tick, "day_low", None)
            if not dh or not dl or dh <= dl:
                return "OPENING_GATE:NO_DAY_RANGE"
            rpos = max(0.0, min(1.0, (price - dl) / (dh - dl)))
            _near_floor = float(config.OPENING_GATE_HARD_RANGE_POS) if getattr(config, "OPENING_GATE_PENALTY_ENABLED", True) else config.OPENING_GATE_MAX_RANGE_POS
            if rpos >= _near_floor:
                return (f"OPENING_GATE:NEAR_DAY_HIGH:range_pos={rpos:.2f} >= {config.OPENING_GATE_MAX_RANGE_POS:.2f} "
                        f"(day_low={dl:.2f} day_high={dh:.2f})")
        if config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS > 0:
            pdv = prev_day.get(getattr(tick, "symbol", "") or "")
            if pdv is None:
                return "OPENING_GATE:NO_PREV_DAY_DATA"
            cp = pdv.close_pos
            _weak_floor = float(config.OPENING_GATE_HARD_PREVDAY_CLOSE_POS) if getattr(config, "OPENING_GATE_PENALTY_ENABLED", True) else config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS
            if cp is not None and cp < _weak_floor:
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
