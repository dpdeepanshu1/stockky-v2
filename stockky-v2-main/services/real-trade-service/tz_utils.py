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

# ── NSE/BSE trading holidays ─────────────────────────────────────────────
# AUDIT FIX (2026-09-14, live incident): is_market_open_ist() below used to
# check ONLY weekday + hours — zero exchange-holiday awareness. Today,
# 2026-09-14, is Ganesh Chaturthi (an NSE/BSE trading holiday), but since
# it's a Monday inside 09:15-15:30 IST, the old check returned True all
# day, so auto_pilot.py kept attempting cycles on a day the exchange was
# actually closed, and notification-scheduler-service separately fired its
# own scan/notify messages for the same reason (its own stale holiday
# list, see that service's scheduler/run_once.py).
#
# Root cause was two-fold: (1) this function never consulted ANY holiday
# list, and (2) the other holiday lists that DO exist elsewhere in this
# repo were themselves stale/wrong and also missing 2026-09-14. Fixed both:
# this function now consults the local set below, verified against NSE's
# official 2026 trading-holiday circular (cross-checked via the Zerodha
# holiday calendar, which mirrors NSE's list) as of 2026-09-14. This is the
# only exchange-holiday check in this service's request path, so every
# is_market_open_ist()/is_ist_weekday() call site (cycle_runner.py,
# auto_pilot.py, dynamic_universe.py, entry_engine.py, manual_engine.py)
# gets holiday-awareness "for free" from this one fix.
#
# MAINTENANCE: there is still no shared import path between services (each
# is a separately-deployed container — see config.py's isolation note), so
# this exact list is duplicated in FOUR places and must be updated in ALL
# of them every year, or this exact bug recurs:
#   - services/real-trade-service/tz_utils.py          (this file)
#   - services/position-stocks-service/tz_utils.py      (identical copy)
#   - services/api-gateway/nse_holidays.py              (_NSE_HOLIDAYS)
#   - services/notification-scheduler-service/scheduler/run_once.py (HOLIDAYS_2026)
# Run scripts/check_holiday_lists_sync.py after editing any one of them —
# it fails loudly if the four have drifted apart again.
_NSE_HOLIDAYS_2026 = {
    "2026-01-15",  # Maharashtra Municipal Corporation elections
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Holi
    "2026-03-26",  # Ram Navami
    "2026-03-31",  # Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-28",  # Bakri Eid (Eid ul-Adha)
    "2026-06-26",  # Muharram
    "2026-09-14",  # Ganesh Chaturthi — the date missing that caused this fix
    "2026-10-02",  # Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali Balipratipada
    "2026-11-24",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
}


def is_nse_holiday(now: Optional[datetime] = None) -> bool:
    """True if the given (or current) IST calendar date is an NSE/BSE
    trading holiday. See _NSE_HOLIDAYS_2026 above for source and the
    multi-file maintenance note."""
    ist_dt = (now or datetime.now(timezone.utc)).astimezone(IST)
    return ist_dt.strftime("%Y-%m-%d") in _NSE_HOLIDAYS_2026


def is_market_open_ist(now: Optional[datetime] = None) -> bool:
    """NSE market-hours check: Mon–Fri, 09:15–15:30 IST, EXCLUDING NSE/BSE
    trading holidays (_NSE_HOLIDAYS_2026 above — fixed 2026-09-14; this
    used to be weekday+hours only, see the AUDIT FIX comment above for the
    incident that caused the fix). This function is wired into every
    account-state builder in the service that feeds risk_engine.evaluate()
    — entry_engine.py, manual_engine.py, and main.py's risk_engine_check
    dry-run route — the only intentional exception is
    offline_test_harness.py's synthetic DEMO account, which fixes it True
    on purpose for deterministic offline test output."""
    ist_dt = (now or datetime.now(timezone.utc)).astimezone(IST)
    if ist_dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if ist_dt.strftime("%Y-%m-%d") in _NSE_HOLIDAYS_2026:
        return False
    return _MARKET_OPEN <= ist_dt.time() <= _MARKET_CLOSE


def as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Return dt with UTC tzinfo attached if it's naive. Every timestamp
    this service writes is UTC, so a naive value read back from the DB is
    assumed to be naive-UTC, not local time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def ist_now(now: Optional[datetime] = None) -> datetime:
    """Current wall-clock time in IST (or convert a given UTC dt to IST)."""
    return (now or datetime.now(timezone.utc)).astimezone(IST)


def ist_today_str(now: Optional[datetime] = None) -> str:
    """IST calendar date as 'YYYY-MM-DD' — used to fire a scheduled action at
    most once per trading day (store the last-fired date, compare to this)."""
    return ist_now(now).strftime("%Y-%m-%d")


def parse_hhmm(value: str, default_h: int, default_m: int) -> _time:
    """Parse an 'HH:MM' env string into a time, falling back to a default."""
    try:
        hh, mm = str(value).strip().split(":")
        return _time(int(hh), int(mm))
    except Exception:
        return _time(default_h, default_m)


def ist_time_at_or_after(target: _time, now: Optional[datetime] = None) -> bool:
    """True once the current IST clock time is at/after `target`. Callers pair
    this with a once-per-day date guard so a window (not just the exact minute)
    still fires exactly once."""
    return ist_now(now).time() >= target


def is_ist_weekday(now: Optional[datetime] = None) -> bool:
    """Mon–Fri in IST (no exchange-holiday awareness — see is_market_open_ist)."""
    return ist_now(now).weekday() < 5


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Every JSON timestamp this service sends to the frontend MUST go
    through this, not a bare `.isoformat()`. Root cause (see module
    docstring above): a DB-sourced datetime almost always comes back
    *naive* even though it was written as UTC, so `.isoformat()` on it
    prints no offset/Z suffix at all — e.g. "2026-08-28T04:07:00"
    instead of "2026-08-28T04:07:00+00:00". `new Date(...)` in the
    browser then parses that offset-less string as LOCAL time, not UTC,
    so every timestamp rendered in the dashboard (Activity log, Orders,
    Watchlist "Fetched"/"Evaluated", position opened_at, etc.) shows the
    raw UTC clock digits mislabeled as if they were already IST — off by
    exactly the IST offset (+5:30). Wrapping with as_aware() first
    guarantees the string always carries an explicit UTC offset, so the
    browser converts it to the viewer's local time correctly."""
    aware = as_aware(dt)
    return aware.isoformat() if aware else None
