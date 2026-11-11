"""E427: one rule for what a `next_earnings_date` means to the gateway.

Before round 410 every gateway site that read `next_earnings_date` treated any non-empty value as "results are coming": it set `event_risk`
and wrote an "Earnings due" reason. The event service keeps returning the last date it knew, so a date that had already passed kept a symbol
flagged as event risk (and as a "results" signal in Hot Picks) indefinitely, and a date 25 days out blocked entries although the decision
service (`EARNINGS_RISK_DAYS`) only calls 0-3 days out a risk.

Rules (calendar days, IST today, same as the decision service's 0 <= days <= EARNINGS_RISK_DAYS):
  * event_risk      : 0 <= days_out <= EARNINGS_RISK_DAYS (3, the decision service's constant)
  * "due" / signal  : 0 <= days_out <= EARNINGS_DUE_DAYS (default 30)
  * a past date, a missing value or text that is not a date: neither.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Tuple

_IST = timezone(timedelta(hours=5, minutes=30))


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


EARNINGS_RISK_DAYS = 3   # same constant as decision-prediction-service/decision/main.py; not an env switch here so the two can never disagree
EARNINGS_DUE_DAYS = max(_int_env("EARNINGS_DUE_DAYS", 30), EARNINGS_RISK_DAYS)


def earnings_days_out(value: Any, today: Optional[date] = None) -> Optional[int]:
    """Calendar days from today (IST) to the date in `value`; negative if past; None if `value` holds no usable date."""
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    elif isinstance(value, str) and value.strip():
        try:
            d = date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    else:
        return None
    return (d - (today or datetime.now(_IST).date())).days


def earnings_due(value: Any, today: Optional[date] = None) -> bool:
    """True when the date is today or up to EARNINGS_DUE_DAYS ahead."""
    n = earnings_days_out(value, today)
    return n is not None and 0 <= n <= EARNINGS_DUE_DAYS


def earnings_event_risk(value: Any, today: Optional[date] = None) -> Tuple[bool, Optional[str]]:
    """(event_risk, reason line). The reason is given for every "due" date; event_risk only inside the risk window."""
    n = earnings_days_out(value, today)
    if n is None or n < 0 or n > EARNINGS_DUE_DAYS:
        return False, None
    return n <= EARNINGS_RISK_DAYS, f"Earnings due: {value}"
