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

GROUP 390 (09:15 entries): the gate no longer blocks until 09:20. Checks that need a built-up range apply only once there is one:
the day-range position (4), the opening-range tick count / position (5) and Nifty vs ITS day open (6) apply from
OPENING_GATE_OR_MINUTES after the open. Before that, price vs previous close / day open, the gap, the previous-day candle and
Nifty vs ITS previous close still apply.

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
from orders import gate_policy
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


def _opening_range(symbol: str, now_ist: datetime):
    """(or_low, or_high, tick_count, complete) from the tick buffer, or None. `complete` is True once the OR window has ended."""
    from feed import ws_client
    start = _open_ts(now_ist)
    end = start + config.OPENING_GATE_OR_MINUTES * 60.0
    prices = [p for ts, p in ws_client.get_tick_buffer(symbol) if p and p > 0 and start <= ts < end]
    if not prices:
        return None
    return min(prices), max(prices), len(prices), now_ist.timestamp() >= end


def below_open_mode() -> str:
    """veto | penalty | off. E285 batch 5: the adaptive gate policy (orders/gate_policy.py) decides it - HARD = veto, SOFT = penalty, OFF = no test; with no policy row it is
    OPENING_BELOW_OPEN_MODE as before (anything else there reads as penalty)."""
    st = gate_policy.state(gate_policy.BELOW_OPEN)
    return "veto" if st == gate_policy.HARD else ("off" if st == gate_policy.OFF else "penalty")


def below_open_penalty(symbol: str, ltp: float, now: Optional[datetime] = None) -> Optional[dict]:
    """E273: BELOW_OPEN as a soft factor. {"penalty": z-units, "pct": how far under the day open, in %} when the gate is active, in penalty mode and the
    price is under the day's open; else None. The unit is the stock's own noise: OPENING_BELOW_OPEN_UNIT_ATR_FRAC (0.1) x its daily ATR %, or
    OPENING_BELOW_OPEN_FALLBACK_UNIT_PCT (0.3 %) when there is no ATR. Price under the PREVIOUS close is a penalty down to OPENING_GATE_HARD_BELOW_PREV_PCT (round 30), a refusal below it. Never raises."""
    try:
        if below_open_mode() != "penalty" or not ltp or ltp <= 0 or not is_active(now):
            return None
        from feed import ws_client
        ds = ws_client.get_day_stats(symbol)
        if not ds or not ds[0] or ltp >= ds[0]:
            return None
        pct = (ds[0] - ltp) / ds[0] * 100.0
        unit = float(getattr(config, "OPENING_BELOW_OPEN_FALLBACK_UNIT_PCT", 0.3))
        pdv = prev_day.peek(symbol)
        if pdv is not None and pdv.atr_pct:
            unit = float(getattr(config, "OPENING_BELOW_OPEN_UNIT_ATR_FRAC", 0.1)) * pdv.atr_pct
        return {"penalty": round(pct / max(unit, 0.05), 3), "pct": round(pct, 3)}
    except Exception:  # noqa: BLE001
        return None


def parts_mode() -> str:
    """E273 round 12: penalty (default) | veto. In penalty mode the day-high position, opening-range position, previous-day close, price under the previous close (round 30, refused past OPENING_GATE_HARD_BELOW_PREV_PCT) and Nifty parts shrink the size instead of refusing."""
    m = str(getattr(config, "OPENING_PARTS_MODE", "penalty") or "penalty").strip().lower()
    return m if m in ("penalty", "veto") else "penalty"


def _hard_below_prev() -> float:
    try:
        return max(0.05, float(getattr(config, "OPENING_GATE_HARD_BELOW_PREV_PCT", 2.0)))
    except (TypeError, ValueError):
        return 2.0


def _cap() -> float:
    try:
        return max(0.1, float(getattr(config, "SOFT_GATE_MAX_PENALTY", 3.0)))
    except (TypeError, ValueError):
        return 3.0


def shortfall_penalty(value: Optional[float], floor: Optional[float], full_scale: Optional[float]) -> Optional[float]:
    """None when value >= floor (or an input is unusable); 1.0 just under the floor, rising linearly to the cap once the shortfall reaches full_scale."""
    try:
        v, f, sc = float(value), float(floor), float(full_scale)
        if v >= f or sc <= 0:
            return None
        top = _cap()
        return round(min(top, 1.0 + (top - 1.0) * (f - v) / sc), 4)
    except (TypeError, ValueError):
        return None


def parts_penalties(symbol: str, ltp: float, now: Optional[datetime] = None) -> dict:
    """E273 round 12: {key: {"penalty": float, "why": reason text}} for the opening-gate parts that used to refuse on their own: near_day_high (OPENING_GATE_MAX_RANGE_POS),
    below_or_mid / extended_or (opening range), prev_day_close, nifty_open / nifty_prev. Same windows and data as reject_reason; a part whose data is missing gives nothing
    here (reject_reason still refuses on missing data). Empty in veto mode or when the gate is not active. Never raises."""
    out: dict = {}
    try:
        pm = parts_mode() == "penalty"
        ndh_soft = gate_policy.state(gate_policy.NEAR_DAY_HIGH) == gate_policy.SOFT        # E285 batch 5: this part's own state (the policy may differ from the other parts)
        gd_soft = gate_policy.state(gate_policy.GAP_DOWN) == gate_policy.SOFT
        if (not pm and not ndh_soft and not gd_soft) or not ltp or ltp <= 0 or not is_active(now):
            return out
        n = ist_now(now)
        from feed import ws_client
        from screening import trade_gates
        minutes_open = (n.timestamp() - _open_ts(n)) / 60.0
        or_settled = minutes_open >= config.OPENING_GATE_OR_MINUTES
        top = _cap()
        ds = ws_client.get_day_stats(symbol)
        if ds and gd_soft and config.OPENING_GATE_MAX_GAP_DOWN_PCT > 0 and ds[0] and ds[3]:
            # E285 batch 5: the policy moved the gap-down gate to SOFT: it no longer refuses (reject_reason), the entry trades at the SOFT size penalty instead.
            gap = (ds[0] - ds[3]) / ds[3] * 100.0
            if gap < -config.OPENING_GATE_MAX_GAP_DOWN_PCT:
                out["gap_down"] = {"penalty": gate_policy.soft_penalty(), "why": f"OPENING_GATE:GAP_DOWN:{gap:+.2f}% < -{config.OPENING_GATE_MAX_GAP_DOWN_PCT:.2f}%", "policy": True}
        if ds and ndh_soft and or_settled and config.OPENING_GATE_MAX_RANGE_POS > 0:
            _o, hi, lo, _pc = ds
            if hi and lo and hi > lo:
                rpos = max(0.0, min(1.0, (ltp - lo) / (hi - lo)))
                fl = config.OPENING_GATE_MAX_RANGE_POS
                if rpos >= fl:
                    # static SOFT (penalty mode): the usual linear penalty; a gate the policy moved from HARD to SOFT: the flat SOFT size penalty (half size)
                    _static_soft = gate_policy.static_state(gate_policy.NEAR_DAY_HIGH) == gate_policy.SOFT
                    out["near_day_high"] = {"penalty": round(min(top, 1.0 + (top - 1.0) * (rpos - fl) / max(1.0 - fl, 0.01)), 4) if _static_soft else gate_policy.soft_penalty(),
                                            "why": f"OPENING_GATE:NEAR_DAY_HIGH:range_pos={rpos:.2f} >= {fl:.2f}", "policy": True}
        if not pm:
            return out
        if ds and ds[3] and ltp < ds[3]:                      # E273 round 30: under the previous close (down to OPENING_GATE_HARD_BELOW_PREV_PCT) is a penalty, not a refusal
            chg = (ltp - ds[3]) / ds[3] * 100.0
            p = shortfall_penalty(chg, 0.0, _hard_below_prev())
            if p:
                out["below_prev_close"] = {"penalty": p, "why": f"OPENING_GATE:BELOW_PREV_CLOSE:ltp {ltp:.2f} < prev close {ds[3]:.2f} ({chg:+.2f}%)"}
        orng = _opening_range(symbol, n)
        if orng is not None and orng[3] and orng[2] >= config.OPENING_GATE_MIN_OR_TICKS:
            or_low, or_high = orng[0], orng[1]
            span = or_high - or_low
            if span > 1e-9:
                or_pos = (ltp - or_low) / span
                p = shortfall_penalty(or_pos, config.OPENING_GATE_MIN_OR_POS, 1.0)      # one full opening range under the mid = the cap
                if p:
                    out["below_or_mid"] = {"penalty": p, "why": f"OPENING_GATE:BELOW_OR_MID:or_pos={or_pos:.2f} < {config.OPENING_GATE_MIN_OR_POS:.2f}"}
            if config.OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT > 0 and or_high > 0:
                ext = (ltp - or_high) / or_high * 100.0
                lim = config.OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT
                if ext > lim:
                    out["extended_or"] = {"penalty": round(min(top, ext / lim), 4), "why": f"OPENING_GATE:EXTENDED_ABOVE_OR:{ext:.2f}% over OR high (max {lim:.2f}%)"}
        if config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS > 0:
            pdv = prev_day.peek(symbol)
            cp = getattr(pdv, "close_pos", None) if pdv is not None else None
            fl = config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS
            p = shortfall_penalty(cp, fl, fl) if cp is not None else None             # a close at the very low = the cap
            if p:
                out["prev_day_close"] = {"penalty": p, "why": f"OPENING_GATE:PREV_DAY_WEAK_CLOSE:close_pos={cp:.2f} < {fl:.2f}"}
        if config.MARKET_GATE_ENABLED:
            nifty = trade_gates.last_nifty_change_pct()
            nprev = trade_gates.last_nifty_prev_close_pct()
            scale = max(0.05, float(getattr(config, "OPENING_NIFTY_FULL_PENALTY_PCT", 0.5)))
            if nifty is not None and or_settled:
                p = shortfall_penalty(nifty, config.OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT, scale)
                if p:
                    out["nifty_open"] = {"penalty": p, "why": f"OPENING_GATE:NIFTY_BELOW_OPEN:{nifty:+.2f}% < {config.OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT:+.2f}%"}
            if nprev is not None:
                p = shortfall_penalty(nprev, config.OPENING_GATE_NIFTY_MIN_VS_PREV_PCT, scale)
                if p:
                    out["nifty_prev"] = {"penalty": p, "why": f"OPENING_GATE:NIFTY_BELOW_PREV_CLOSE:{nprev:+.2f}% < {config.OPENING_GATE_NIFTY_MIN_VS_PREV_PCT:+.2f}%"}
    except Exception:  # noqa: BLE001
        return {}
    return out


def gap_up_limit(symbol: str) -> float:
    """E285 round 128: the largest gap-up (open vs previous close, %) an entry may follow. The fixed OPENING_GATE_MAX_GAP_UP_PCT, or - with OPENING_GATE_GAP_ADAPTIVE - the
    larger of it and the stock's own PREOPEN_GAP_ATR_MULT x daily ATR % (preopen.atr_gap_threshold), never above OPENING_GATE_GAP_UP_HARD_PCT. 0 = the gate is off. Never raises."""
    try:
        fixed = float(config.OPENING_GATE_MAX_GAP_UP_PCT or 0.0)
        if fixed <= 0 or not getattr(config, "OPENING_GATE_GAP_ADAPTIVE", False):
            return fixed
        from screening import preopen
        thr = preopen.atr_gap_threshold(symbol)
        lim = max(fixed, float(thr)) if thr else fixed
        hard = float(getattr(config, "OPENING_GATE_GAP_UP_HARD_PCT", 0.0) or 0.0)
        if hard > 0:
            lim = min(lim, max(hard, fixed))
        return lim
    except Exception:  # noqa: BLE001
        return float(getattr(config, "OPENING_GATE_MAX_GAP_UP_PCT", 0.0) or 0.0)


def reject_reason(symbol: str, ltp: float, now: Optional[datetime] = None, ignore_gap: bool = False) -> Optional[str]:
    """None when the entry may go ahead (or the gate is not active), else a skip reason starting with a stable code.
    E273 round 12: in OPENING_PARTS_MODE=penalty (default) the day-high position, opening-range position, previous-day close and Nifty tests no longer refuse (see parts_penalties());
    missing data still refuses. ignore_gap=True skips the two gap tests (an event-explained gap-up, round 11) so everything else still runs."""
    try:
        soft = parts_mode() == "penalty"
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

        or_settled = minutes_open >= config.OPENING_GATE_OR_MINUTES     # group 390: range-based checks need a built-up range
        gap = (day_open - prev_close) / prev_close * 100.0
        _gap_up_lim = gap_up_limit(symbol)
        if not ignore_gap and _gap_up_lim > 0 and gap > _gap_up_lim:
            return f"OPENING_GATE:GAP_UP:{gap:+.2f}% > +{_gap_up_lim:.2f}%"
        if (not ignore_gap and config.OPENING_GATE_MAX_GAP_DOWN_PCT > 0 and gap < -config.OPENING_GATE_MAX_GAP_DOWN_PCT
                and gate_policy.state(gate_policy.GAP_DOWN) == gate_policy.HARD):        # E285 batch 5: SOFT / OFF = no refusal here (SOFT is a size penalty in parts_penalties)
            return f"OPENING_GATE:GAP_DOWN:{gap:+.2f}% < -{config.OPENING_GATE_MAX_GAP_DOWN_PCT:.2f}%"
        if ltp < day_open and below_open_mode() == "veto":     # E273: default "penalty" - the scorecard showed this gate blocks winners; see below_open_penalty()
            return f"OPENING_GATE:BELOW_OPEN:ltp {ltp:.2f} < open {day_open:.2f}"
        if ltp < prev_close and (not soft or (ltp - prev_close) / prev_close * 100.0 <= -_hard_below_prev()):      # E273 round 30: penalty mode refuses only past the depth below
            return f"OPENING_GATE:BELOW_PREV_CLOSE:ltp {ltp:.2f} < prev close {prev_close:.2f}"

        if config.OPENING_GATE_MAX_RANGE_POS > 0 and or_settled:
            if not day_high or not day_low or day_high <= day_low:
                return "OPENING_GATE:NO_DAY_RANGE"
            rpos = max(0.0, min(1.0, (ltp - day_low) / (day_high - day_low)))
            if rpos >= config.OPENING_GATE_MAX_RANGE_POS and gate_policy.state(gate_policy.NEAR_DAY_HIGH) == gate_policy.HARD:      # E285 batch 5: was `not soft`; same with no policy row
                return (f"OPENING_GATE:NEAR_DAY_HIGH:range_pos={rpos:.2f} >= {config.OPENING_GATE_MAX_RANGE_POS:.2f} "
                        f"(day_low={day_low:.2f} day_high={day_high:.2f})")

        orng = _opening_range(symbol, n)
        if or_settled and (orng is None or orng[2] < config.OPENING_GATE_MIN_OR_TICKS):
            return "OPENING_GATE:NO_OPENING_RANGE_DATA"    # group 390: a thin/missing range only matters once the range is complete
        if orng is not None and orng[3]:
            or_low, or_high = orng[0], orng[1]
            span = or_high - or_low
            if span > 1e-9:
                or_pos = (ltp - or_low) / span
                if or_pos < config.OPENING_GATE_MIN_OR_POS and not soft:
                    return (f"OPENING_GATE:BELOW_OR_MID:or_pos={or_pos:.2f} < {config.OPENING_GATE_MIN_OR_POS:.2f} "
                            f"(or_low={or_low:.2f} or_high={or_high:.2f})")
            if config.OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT > 0 and or_high > 0:
                ext = (ltp - or_high) / or_high * 100.0
                if ext > config.OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT and not soft:
                    return (f"OPENING_GATE:EXTENDED_ABOVE_OR:{ext:.2f}% over OR high {or_high:.2f} "
                            f"(max {config.OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT:.2f}%)")

        if config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS > 0 or config.OPENING_GATE_MIN_STOP_ATR_FRAC > 0:
            pdv = prev_day.get(symbol)
            if pdv is None:
                return "OPENING_GATE:NO_PREV_DAY_DATA"
            if config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS > 0:
                cp = pdv.close_pos
                if cp is not None and cp < config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS and not soft:
                    return (f"OPENING_GATE:PREV_DAY_WEAK_CLOSE:close_pos={cp:.2f} < {config.OPENING_GATE_MIN_PREVDAY_CLOSE_POS:.2f} "
                            f"(prev {pdv.date} low={pdv.low:.2f} high={pdv.high:.2f} close={pdv.close:.2f})")

        if config.MARKET_GATE_ENABLED:
            nifty = trade_gates.last_nifty_change_pct()
            nprev = trade_gates.last_nifty_prev_close_pct()
            if nifty is None or nprev is None:
                return "OPENING_GATE:NO_NIFTY_DATA"
            if or_settled and nifty < config.OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT and not soft:    # group 390: vs its own open is noise at 09:15-09:30
                return f"OPENING_GATE:NIFTY_BELOW_OPEN:{nifty:+.2f}% < {config.OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT:+.2f}%"
            if nprev < config.OPENING_GATE_NIFTY_MIN_VS_PREV_PCT and not soft:
                return f"OPENING_GATE:NIFTY_BELOW_PREV_CLOSE:{nprev:+.2f}% < {config.OPENING_GATE_NIFTY_MIN_VS_PREV_PCT:+.2f}%"
        return None
    except Exception as e:  # fail CLOSED: an error while the gate is active skips the entry
        logger.warning("opening gate error for %s (skipping entry): %s", symbol, e)
        return f"OPENING_GATE:ERROR:{type(e).__name__}"


def stop_reject(symbol: str, stop_pct: float, now: Optional[datetime] = None) -> Optional[str]:
    """Group 270: inside the gate window an entry whose stop is tighter than OPENING_GATE_MIN_STOP_ATR_FRAC x the daily ATR %
    is noise-sized at the open and is skipped. Fails closed without ATR data. None when the gate is not active."""
    try:
        if config.OPENING_GATE_MIN_STOP_ATR_FRAC <= 0:
            return None
        in_window = is_active(now)
        if not in_window and not config.STOP_ATR_FLOOR_AFTER_WINDOW:
            return None
        pdv = prev_day.get(symbol)
        if pdv is None or pdv.atr_pct is None:
            # E229: inside the window fail closed (as before); after it fail open - never newly block a trade on missing history.
            return "OPENING_GATE:NO_ATR_DATA" if in_window else None
        floor = config.OPENING_GATE_MIN_STOP_ATR_FRAC * pdv.atr_pct
        if stop_pct < floor:
            return (f"{'OPENING_GATE' if in_window else 'STOP_ATR_FLOOR'}:STOP_TOO_TIGHT:stop {stop_pct:.2f}% < {config.OPENING_GATE_MIN_STOP_ATR_FRAC:.2f} x "
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
