"""orders/calendar_gate.py - expiry-day, Friday and pre-holiday rules (group 336, 2026-10-12).

WHY: three kinds of session behave differently from an ordinary one and nothing in the entry path knew about them:
  * EXPIRY day (index weekly / monthly derivatives expiry): wide, fast swings in the last hours as positions are squared off or rolled.
  * PRE-HOLIDAY day (the next trading day is not tomorrow because an exchange holiday - not only a weekend - sits in between): thin, drifting
    afternoon, and anything still held faces several days of news with no way to exit.
  * FRIDAY (pre-weekend): a milder version of the same, two closed days ahead.
RULES (all config, every one can be switched off; a blank cut-off time turns that cut-off off)
  * entry cut-off : no NEW entry from CALENDAR_EXPIRY_ENTRY_CUTOFF_IST / CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST / CALENDAR_FRIDAY_ENTRY_CUTOFF_IST
                    on that kind of day (the earliest that applies wins).
  * size          : position size x CALENDAR_EXPIRY_SIZE_MULT / CALENDAR_PRE_HOLIDAY_SIZE_MULT / CALENDAR_FRIDAY_SIZE_MULT (the smallest that applies).
  * carry cap     : (real-trade only) overnight carry is limited to CALENDAR_*_CARRY_MAX positions on those days (never raised above the normal cap).
EXPIRY DAY: the weekly expiry weekday is CALENDAR_EXPIRY_WEEKDAY (0=Mon .. 4=Fri; default 1 = Tuesday, the NSE index-derivatives weekly expiry day
since Sept 2025 - CHECK it against the current NSE circular and set the variable if the exchange changes it). When that date is an exchange
holiday the expiry moves to the previous trading day. The monthly expiry is the last such weekday of the month (same roll-back).
FAIL OPEN: any error = ordinary day. Pure date maths over this service's own holiday list (tz_utils.is_nse_holiday). Never raises.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, Optional

import config
from tz_utils import IST, ist_now, ist_time_at_or_after, parse_hhmm, is_nse_holiday

logger = logging.getLogger("calendar-gate")


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_nse_holiday(datetime(d.year, d.month, d.day, 12, 0, tzinfo=IST))


def _next_trading_day(d: date) -> date:
    n = d + timedelta(days=1)
    for _ in range(14):                       # a fortnight with no session is not a calendar this code can reason about
        if _is_trading_day(n):
            return n
        n += timedelta(days=1)
    return d + timedelta(days=1)


def _prev_trading_day(d: date) -> date:
    n = d - timedelta(days=1)
    for _ in range(14):
        if _is_trading_day(n):
            return n
        n -= timedelta(days=1)
    return d - timedelta(days=1)


def _roll_back(d: date) -> date:
    return d if _is_trading_day(d) else _prev_trading_day(d)


def _expiry_weekday() -> int:
    try:
        w = int(getattr(config, "CALENDAR_EXPIRY_WEEKDAY", 1))
        return w if 0 <= w <= 4 else 1
    except (TypeError, ValueError):
        return 1


def is_expiry_day(d: date) -> Optional[str]:
    """'monthly' / 'weekly' when d is an index-derivatives expiry session, else None. Never raises."""
    try:
        wd = _expiry_weekday()
        if not _is_trading_day(d):
            return None
        week_expiry = _roll_back(d + timedelta(days=(wd - d.weekday())))
        if week_expiry != d:
            return None
        nxt_month = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = nxt_month - timedelta(days=1)
        last_wd = last - timedelta(days=(last.weekday() - wd) % 7)      # last `wd` weekday of the month, rolled back = monthly expiry
        return "monthly" if _roll_back(last_wd) == d else "weekly"
    except Exception:  # noqa: BLE001
        return None


def is_pre_holiday(d: date) -> bool:
    """True when d is a trading day and an exchange HOLIDAY (not just the weekend) falls between d and the next session. Never raises."""
    try:
        if not _is_trading_day(d):
            return False
        n = d + timedelta(days=1)
        nxt = _next_trading_day(d)
        while n < nxt:
            if n.weekday() < 5:               # a weekday that is not a session = an exchange holiday
                return True
            n += timedelta(days=1)
        return False
    except Exception:  # noqa: BLE001
        return False


def context(now: Optional[datetime] = None) -> Dict[str, object]:
    """{'date', 'expiry': 'weekly'|'monthly'|None, 'pre_holiday': bool, 'friday': bool, 'tags': [...]} for the IST day of `now`. Never raises."""
    out: Dict[str, object] = {"date": None, "expiry": None, "pre_holiday": False, "friday": False, "tags": []}
    try:
        d = ist_now(now).date()
        out["date"] = d.isoformat()
        if not _is_trading_day(d):
            return out
        exp = is_expiry_day(d)
        pre = is_pre_holiday(d)
        fri = d.weekday() == 4
        out.update(expiry=exp, pre_holiday=pre, friday=fri)
        out["tags"] = [t for t, on in (("expiry", bool(exp)), ("pre_holiday", pre), ("friday", fri)) if on]
    except Exception as e:  # noqa: BLE001
        logger.debug("calendar context failed (ordinary day): %s", e)
    return out


def _on() -> bool:
    return bool(getattr(config, "CALENDAR_RULES_ENABLED", True))


def _cutoff_for(tag: str) -> str:
    return str({"expiry": getattr(config, "CALENDAR_EXPIRY_ENTRY_CUTOFF_IST", "14:30"),
                "pre_holiday": getattr(config, "CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST", "14:30"),
                "friday": getattr(config, "CALENDAR_FRIDAY_ENTRY_CUTOFF_IST", "")}[tag] or "").strip()


def entry_block_reason(now: Optional[datetime] = None) -> Optional[str]:
    """'CALENDAR_<KIND>_CUTOFF:...' once the earliest applicable cut-off has passed today, else None. Never raises."""
    try:
        if not _on():
            return None
        ctx = context(now)
        hits = []
        for tag in ctx["tags"]:                                           # type: ignore[union-attr]
            raw = _cutoff_for(tag)
            if raw:
                hits.append((parse_hhmm(raw, 14, 30), tag, raw))
        if not hits:
            return None
        hits.sort(key=lambda h: (h[0].hour, h[0].minute))
        t, tag, raw = hits[0]
        if ist_time_at_or_after(t, now):
            label = {"expiry": f"{ctx['expiry']} expiry day", "pre_holiday": "the session before an exchange holiday", "friday": "Friday"}[tag]
            return f"CALENDAR_{tag.upper()}_CUTOFF:no new entry after {raw} IST on {label}"
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("calendar entry_block_reason failed (allowing): %s", e)
        return None


def scalp_cutoff_reason(now: Optional[datetime] = None) -> Optional[str]:
    """group 340: 'SCALP_ENTRY_CUTOFF:...' once config.SCALP_ENTRY_CUTOFF_IST has passed, else None. Blank = off (default). Fails open."""
    try:
        raw = (getattr(config, "SCALP_ENTRY_CUTOFF_IST", "") or "").strip()
        if not raw:
            return None
        if ist_time_at_or_after(parse_hhmm(raw, 14, 15), now):
            return f"SCALP_ENTRY_CUTOFF:no new scalp entry after {raw} IST"
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("scalp cutoff check failed (allowing): %s", e)
        return None


def size_mult(now: Optional[datetime] = None) -> float:
    """1.0 on an ordinary day; the smallest applicable CALENDAR_*_SIZE_MULT (clamped 0.1..1.0) on an expiry / pre-holiday / Friday session."""
    try:
        if not _on():
            return 1.0
        mults = []
        for tag in context(now)["tags"]:                                  # type: ignore[union-attr]
            m = float({"expiry": getattr(config, "CALENDAR_EXPIRY_SIZE_MULT", 0.8),
                       "pre_holiday": getattr(config, "CALENDAR_PRE_HOLIDAY_SIZE_MULT", 0.8),
                       "friday": getattr(config, "CALENDAR_FRIDAY_SIZE_MULT", 0.9)}[tag])
            mults.append(min(1.0, max(0.1, m)))
        return min(mults) if mults else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def carry_max_positions(normal_cap: int, now: Optional[datetime] = None) -> int:
    """The overnight-carry position cap for today: the smallest applicable CALENDAR_*_CARRY_MAX, never above `normal_cap`. Never raises."""
    try:
        if not _on():
            return normal_cap
        caps = [int(normal_cap)]
        for tag in context(now)["tags"]:                                  # type: ignore[union-attr]
            c = {"expiry": getattr(config, "CALENDAR_EXPIRY_CARRY_MAX", 2),
                 "pre_holiday": getattr(config, "CALENDAR_PRE_HOLIDAY_CARRY_MAX", 1),
                 "friday": getattr(config, "CALENDAR_FRIDAY_CARRY_MAX", 2)}[tag]
            caps.append(max(0, int(c)))
        return min(caps)
    except Exception:  # noqa: BLE001
        return normal_cap
