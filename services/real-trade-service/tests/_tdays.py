"""Test helper (E207): an entry timestamp that is exactly N NSE trading sessions old, independent of the weekday the suite runs on."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tz_utils import IST, is_nse_holiday


def opened_at_trading_days_ago(n: int, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if n <= 0:
        return now
    cursor = now.astimezone(IST).date()
    remaining = n
    while remaining > 0:
        if cursor.weekday() < 5 and not is_nse_holiday(datetime(cursor.year, cursor.month, cursor.day, 12, 0, tzinfo=IST)):
            remaining -= 1
        cursor -= timedelta(days=1)
    return datetime(cursor.year, cursor.month, cursor.day, 12, 0, tzinfo=IST).astimezone(timezone.utc)
