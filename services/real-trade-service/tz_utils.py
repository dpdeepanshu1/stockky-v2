"""tz_utils.py — single place to fix the offset-naive vs offset-aware
datetime crash across this service.

Root cause: models.py declares timestamp columns as plain `DateTime`
(no `timezone=True`). Every write goes in as tz-aware UTC
(`datetime.now(timezone.utc)`), but most DB drivers (SQLite always,
Postgres unless the column is TIMESTAMPTZ) hand it back on read as a
*naive* datetime. Comparing that naive value against a fresh
`datetime.now(timezone.utc)` raises:
    TypeError: can't compare offset-naive and offset-aware datetimes

This is exactly what was crashing GET /status/REAL and POST /dhan/connect
(_check_and_expire_gates in main.py and connection_status/is_token_valid
in auth/dhan_credentials.py).

Fix: always pass DB-sourced datetimes through `as_aware()` before
comparing them to `datetime.now(timezone.utc)`.
"""
from __future__ import annotations

from datetime import datetime, time as _time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_MARKET_OPEN = _time(9, 15)
_MARKET_CLOSE = _time(15, 30)


def is_market_open_ist(now: Optional[datetime] = None) -> bool:
    """Best-effort NSE market-hours check: Mon–Fri, 09:15–15:30 IST.
    Deliberately does NOT know about exchange holidays (that list lives in
    notification-scheduler-service/scheduler/run_once.py's HOLIDAYS_2026,
    a separate service with no shared import path here) — auto-pilot will
    still attempt a cycle on a holiday, but every order it could place
    still goes through market_feed's live quote + the risk engine, so a
    holiday with no ticks simply produces WAIT/no-price outcomes rather
    than a bad order. Good enough for gating a background loop; NOT a
    substitute for a real trading-calendar check inside the risk engine
    itself (that remains a Phase 3 TODO, same as the other
    `market_is_open=True` placeholders across this service)."""
    ist_now = (now or datetime.now(timezone.utc)).astimezone(IST)
    if ist_now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return _MARKET_OPEN <= ist_now.time() <= _MARKET_CLOSE


def as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Return dt with UTC tzinfo attached if it's naive. Every timestamp
    this service writes is UTC, so a naive value read back from the DB is
    assumed to be naive-UTC, not local time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
