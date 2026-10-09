"""screening/opening_gate.py - opening-quality gate for scalp entries (group 268, 2026-10-09).

WHY: the entry window now opens at 09:15. The 14-day trade breakdown (33 trades) showed entries before 10:30 won 1 of 10
(-Rs 154 gross) and 10:30 onwards won 11 of 23 (+Rs 116). A fixed clock block throws away the good opening trades too, so
between the open and OPENING_GATE_SETTLE_IST (default 10:00) an entry must pass ALL of these; afterwards nothing here applies.

CHECKS (each can be switched off by setting its number to 0 / disabling the gate):
  1. at least OPENING_GATE_MIN_MINUTES_AFTER_OPEN minutes since 09:15
  2. gap: day open vs previous close, not above +OPENING_GATE_MAX_GAP_UP_PCT and not below -OPENING_GATE_MAX_GAP_DOWN_PCT
  3. holding: price at or above the day's open AND at or above the previous close
  4. day range: price not in the top (1 - OPENING_GATE_MAX_RANGE_POS) of the exchange's day high/low
  5. opening range (first OPENING_GATE_OR_MINUTES minutes from 09:15, built from the tick buffer): once it is complete, price in
     its upper half (OPENING_GATE_MIN_OR_POS) or above it, but not more than OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT over the OR high
  6. Nifty: change vs its day open and vs the previous close both >= the configured minimums (skipped when MARKET_GATE_ENABLED=0)

FAIL CLOSED: unlike every other gate in this service, missing data (no exchange day stats, no previous close, no Nifty reading,
too few opening-range ticks) skips the entry while the gate is active. An internal error also skips it.

  7. previous day (group 270, screening/prev_day.py): it closed in the upper half of its own range
     (OPENING_GATE_MIN_PREVDAY_CLOSE_POS), and the entry's stop is not tighter than OPENING_GATE_MIN_STOP_ATR_FRAC x the daily
     ATR % (stop_reject(), called once the adaptive levels are known).

The previous-day candles come from a per-day cache filled by a background thread, so there is no network call on the entry
path; a cache miss fails closed for that scan and the next scan finds the data.

entry_features() builds a short text of the same numbers for the ENTERED candidate-log row at any time of day, so winners and
losers can be compared later. Pure reads of ws_client / trade_gates state; neither function raises.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as _time
from typing import Optional

import config
from screening import prev_day
from tz_utils import ist_now, is_market_open_ist, parse_hhmm

logger = logging.getLogger("position-stocks-opening-gate")

_OPEN = _time(9, 15)


def _open_ts(now_ist: datetime) -> float:
    return now_ist.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()


def is_active(now: Optional[datetime] = None) -> bool:
    """True while the gate applies: enabled, market open, clock before the settle time."""
    try:
        if not config.OPENING_GATE_ENABLED:
            return False
        if not is_market_open_ist(now):
            return False
        n = ist_now(now)
        return _OPEN <= n.time() < parse_hhmm(config.OPENING_GATE_SETTLE_IST, 10, 0)
    except Exception:
        return False


_shadow_seen: set = set()


def shadow_active(now: Optional[datetime] = None) -> bool:
    """True while OPENING_GATE_SHADOW is on and the gate window is open: nothing may be ordered."""
    try:
        return bool(config.OPENING_GATE_SHADOW) and is_active(now)
    except Exception:
        return False


def shadow_first_time(symbol: str, now: Optional[datetime] = None) -> bool:
    """True the first time a symbol would-have-entered today (so the candidate log gets one row, not one per 10 s scan)."""
    key = (ist_now(now).date().isoformat(), symbol)
    if key in _shadow_seen:
        return False
    if len(_shadow_seen) > 5000:
        _shadow_seen.clear()
    _shadow_seen.add(key)
    return True


def reset_shadow() -> None:
    _shadow_seen.clear()


def _opening_range(symbol: str, now_ist: datetime):
    """(or_low, or_high, tick_count, complete) from the tick buffer, or None. `complete` is True once the OR window has ended."""
    from feed import ws_client
    start = _open_ts(now_ist)
    end = start + config.OPENING_GATE_OR_MINUTES * 60.0
    prices = [p for ts, p in ws_client.get_tick_buffer(symbol) if p and p > 0 and start <= ts < end]
    if not prices:
        return None
    return min(prices), max(prices), len(prices), now_ist.timestamp() >= end


def reject_reason(symbol: str, ltp: float, now: Optional[datetime] = None) -> Optional[str]:
    """None when the entry may go ahead (or the gate is not active), else a skip reason starting with a stable code."""
    try:
        if not is_active(now):
            return None
        n = ist_now(now)
        from feed import ws_client
        from screening import trade_gates

        if not ltp or ltp <= 0:
            return "OPENING_GATE:NO_PRICE"
        prev_day.get(symbol)          # warm the previous-day cache early (background fetch, never blocks)
        minutes_open = (n.timestamp() - _open_ts(n)) / 60.0
        if minutes_open < config.OPENING_GATE_MIN_MINUTES_AFTER_OPEN:
            return f"OPENING_GATE:TOO_EARLY:{minutes_open:.1f}m since open < {config.OPENING_GATE_MIN_MINUTES_AFTER_OPEN:.0f}m"

        ds = ws_client.get_day_stats(symbol)
        if not ds:
            return "OPENING_GATE:NO_DAY_STATS"
        day_open, day_high, day_low, prev_close = ds
        if not day_open or not prev_close:
            return "OPENING_GATE:NO_OPEN_OR_PREV_CLOSE"

        gap = (day_open - prev_close) / prev_close * 100.0
        if config.OPENING_GATE_MAX_GAP_UP_PCT > 0 and gap > config.OPENING_GATE_MAX_GAP_UP_PCT:
            return f"OPENING_GATE:GAP_UP:{gap:+.2f}% > +{config.OPENING_GATE_MAX_GAP_UP_PCT:.2f}%"
        if config.OPENING_GATE_MAX_GAP_DOWN_PCT > 0 and gap < -config.OPENING_GATE_MAX_GAP_DOWN_PCT:
            return f"OPENING_GATE:GAP_DOWN:{gap:+.2f}% < -{config.OPENING_GATE_MAX_GAP_DOWN_PCT:.2f}%"
        if ltp < day_open:
            return f"OPENING_GATE:BELOW_OPEN:ltp {ltp:.2f} < open {day_open:.2f}"
        if ltp < prev_close:
            return f"OPENING_GATE:BELOW_PREV_CLOSE:ltp {ltp:.2f} < prev close {prev_close:.2f}"

        if config.OPENING_GATE_MAX_RANGE_POS > 0:
            if not day_high or not day_low or day_high <= day_low:
                return "OPENING_GATE:NO_DAY_RANGE"
            rpos = max(0.0, min(1.0, (ltp - day_low) / (day_high - day_low)))
            if rpos >= config.OPENING_GATE_MAX_RANGE_POS:
                return (f"OPENING_GATE:NEAR_DAY_HIGH:range_pos={rpos:.2f} >= {config.OPENING_GATE_MAX_RANGE_POS:.2f} "
                        f"(day_low={day_low:.2f} day_high={day_high:.2f})")

        orng = _opening_range(symbol, n)
        if orng is None or orng[2] < config.OPENING_GATE_MIN_OR_TICKS:
            return "OPENING_GATE:NO_OPENING_RANGE_DATA"
        or_low, or_high, _cnt, complete = orng
        if complete:
            span = or_high - or_low
            if span > 1e-9:
                or_pos = (ltp - or_low) / span
                if or_pos < config.OPENING_GATE_MIN_OR_POS:
                    return (f"OPENING_GATE:BELOW_OR_MID:or_pos={or_pos:.2f} < {config.OPENING_GATE_MIN_OR_POS:.2f} "
                            f"(or_low={or_low:.2f} or_high={or_high:.2f})")
            if config.OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT > 0 and or_high > 0:
                ext = (ltp - or_high) / or_high * 100.0
                if ext > config.OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT:
                    return (f"OPENING_GATE:EXTENDED_ABOVE_OR:{ext:.2f}% over OR high {or_high:.2f} "
                            f"(max {config.OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT:.2f}%)")

        if config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS > 0 or config.OPENING_GATE_MIN_STOP_ATR_FRAC > 0:
            pdv = prev_day.get(symbol)
            if pdv is None:
                return "OPENING_GATE:NO_PREV_DAY_DATA"
            if config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS > 0:
                cp = pdv.close_pos
                if cp is not None and cp < config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS:
                    return (f"OPENING_GATE:PREV_DAY_WEAK_CLOSE:close_pos={cp:.2f} < {config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS:.2f} "
                            f"(prev {pdv.date} low={pdv.low:.2f} high={pdv.high:.2f} close={pdv.close:.2f})")

        if config.MARKET_GATE_ENABLED:
            nifty = trade_gates.last_nifty_change_pct()
            nprev = trade_gates.last_nifty_prev_close_pct()
            if nifty is None or nprev is None:
                return "OPENING_GATE:NO_NIFTY_DATA"
            if nifty < config.OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT:
                return f"OPENING_GATE:NIFTY_BELOW_OPEN:{nifty:+.2f}% < {config.OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT:+.2f}%"
            if nprev < config.OPENING_GATE_NIFTY_MIN_VS_PREV_PCT:
                return f"OPENING_GATE:NIFTY_BELOW_PREV_CLOSE:{nprev:+.2f}% < {config.OPENING_GATE_NIFTY_MIN_VS_PREV_PCT:+.2f}%"
        return None
    except Exception as e:  # fail CLOSED: an error while the gate is active skips the entry
        logger.warning("opening gate error for %s (skipping entry): %s", symbol, e)
        return f"OPENING_GATE:ERROR:{type(e).__name__}"


def stop_reject(symbol: str, stop_pct: float, now: Optional[datetime] = None) -> Optional[str]:
    """Group 270: inside the gate window an entry whose stop is tighter than OPENING_GATE_MIN_STOP_ATR_FRAC x the daily ATR %
    is noise-sized at the open and is skipped. Fails closed without ATR data. None when the gate is not active."""
    try:
        if not is_active(now) or config.OPENING_GATE_MIN_STOP_ATR_FRAC <= 0:
            return None
        pdv = prev_day.get(symbol)
        if pdv is None or pdv.atr_pct is None:
            return "OPENING_GATE:NO_ATR_DATA"
        floor = config.OPENING_GATE_MIN_STOP_ATR_FRAC * pdv.atr_pct
        if stop_pct < floor:
            return (f"OPENING_GATE:STOP_TOO_TIGHT:stop {stop_pct:.2f}% < {config.OPENING_GATE_MIN_STOP_ATR_FRAC:.2f} x "
                    f"daily ATR {pdv.atr_pct:.2f}% = {floor:.2f}%")
        return None
    except Exception as e:
        logger.warning("opening gate stop check error for %s (skipping entry): %s", symbol, e)
        return f"OPENING_GATE:ERROR:{type(e).__name__}"


def entry_features(symbol: str, ltp: float, now: Optional[datetime] = None) -> str:
    """' gap=+0.40% vs_open=+0.9% range_pos=0.82 or_pos=1.10 nifty_prev=+0.31' for the ENTERED log row; '' when nothing is known."""
    parts = []
    try:
        from feed import ws_client
        from screening import trade_gates
        ds = ws_client.get_day_stats(symbol)
        if ds:
            day_open, day_high, day_low, prev_close = ds
            if day_open and prev_close:
                parts.append(f"gap={(day_open - prev_close) / prev_close * 100.0:+.2f}%")
            if day_open and ltp:
                parts.append(f"vs_open={(ltp - day_open) / day_open * 100.0:+.2f}%")
            if day_high and day_low and day_high > day_low and ltp:
                parts.append(f"range_pos={max(0.0, min(1.0, (ltp - day_low) / (day_high - day_low))):.2f}")
        orng = _opening_range(symbol, ist_now(now))
        if orng and orng[1] > orng[0] and ltp:
            parts.append(f"or_pos={(ltp - orng[0]) / (orng[1] - orng[0]):.2f}")
        nprev = trade_gates.last_nifty_prev_close_pct()
        if nprev is not None:
            parts.append(f"nifty_prev={nprev:+.2f}")
        pdv = prev_day.peek(symbol)
        if pdv is not None:
            if pdv.close_pos is not None:
                parts.append(f"pd_close_pos={pdv.close_pos:.2f}")
            if pdv.atr_pct is not None:
                parts.append(f"atr_pct={pdv.atr_pct:.2f}")
    except Exception as e:
        logger.debug("entry_features %s: %s", symbol, e)
    return (" " + " ".join(parts)) if parts else ""
