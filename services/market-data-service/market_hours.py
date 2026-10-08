"""market_hours.py — self-contained NSE market-hours (IST) check for
market-data-service.

Why this exists (2026-09-01 incident): angelone_ws_feed.py's
_poll_forever() and yahoo_ws_feed.py's feed loop are unconditional
`while True`/`while _running` background threads started once at
service boot — they poll AngelOne's quote API for the whole scan
universe every ~3s and hold a Yahoo streaming socket open, 24/7, with
no market-hours awareness anywhere in that loop. That's a different
loop from real-trade-service's Auto-Pilot FULL CYCLE loop, which IS
correctly gated ("market-hours only, IST") — this module gives the
background feed threads in *this* service the same gate.

Deliberately self-contained rather than importing
real-trade-service/tz_utils.py: these are separate deployed services
with no shared import path (same constraint noted in that file's own
_get_live_quotes_engine()-style comments elsewhere in this service).
"""
from __future__ import annotations

import os
from datetime import datetime, time as _time, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_MARKET_OPEN = _time(9, 15)
_MARKET_CLOSE = _time(15, 30)

# NSE/BSE trading holidays for 2026 (IST calendar dates). Group 139: this service had NO holiday
# awareness, so on a weekday holiday (e.g. Fri 2026-10-02, Gandhi Jayanti) the AngelOne and Yahoo
# feeds still polled the whole universe 09:05-15:35 for prices that cannot change, and the quote /
# history caches used the 5-15 minute open-session TTLs. Same literal, same dates, as the other
# copies (no shared import path between services): services/real-trade-service/tz_utils.py and
# position-stocks-service/tz_utils.py (_NSE_HOLIDAYS_2026), api-gateway/nse_holidays.py
# (_NSE_HOLIDAYS), notification-scheduler-service/scheduler/run_once.py (HOLIDAYS_2026).
# Run scripts/check_holiday_lists_sync.py after editing any one of them. Extend each year.
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
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-02",  # Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali Balipratipada
    "2026-11-24",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
}

# A few minutes of slack on each side so a feed doesn't stop/start right
# at the bell — lets it warm up just before open and finish flushing just
# after close. Configurable without a code change.
_PRE_OPEN_SLACK_MIN = int(((os.getenv("MARKET_HOURS_PRE_OPEN_SLACK_MIN") or "").strip() or "10"))
_POST_CLOSE_SLACK_MIN = int(((os.getenv("MARKET_HOURS_POST_CLOSE_SLACK_MIN") or "").strip() or "5"))

# Escape hatch: force the feeds to poll 24/7 anyway (e.g. local dev/testing
# outside market hours). Off by default.
_ALWAYS_ON = os.getenv("MARKET_HOURS_FEED_ALWAYS_ON", "false").strip().lower() in (
    "1", "true", "yes", "on",
)


def is_nse_holiday_ist(now: datetime | None = None) -> bool:
    """True if the given (or current) IST calendar date is an NSE/BSE trading holiday
    (_NSE_HOLIDAYS_2026 above). A naive datetime is taken as IST wall-clock time."""
    if now is None:
        now = datetime.now(timezone.utc)
    ist_now = now.astimezone(IST) if now.tzinfo is not None else now
    return ist_now.strftime("%Y-%m-%d") in _NSE_HOLIDAYS_2026


def is_feed_window_ist(now: datetime | None = None) -> bool:
    """True during Mon-Fri, roughly 09:05-15:35 IST (open/close +/- slack), except on NSE/BSE
    trading holidays (group 139: before that a weekday holiday kept both feeds polling all day
    for prices that cannot change). The holiday list is a plain 2026 date set: a date it does
    not know about is treated as a normal trading day, so a stale list errs on the side of
    polling, never of silencing a live feed."""
    if _ALWAYS_ON:
        return True
    ist_now = (now or datetime.now(timezone.utc)).astimezone(IST)
    if ist_now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if is_nse_holiday_ist(ist_now):
        return False
    open_dt = datetime.combine(ist_now.date(), _MARKET_OPEN, tzinfo=IST) - timedelta(minutes=_PRE_OPEN_SLACK_MIN)
    close_dt = datetime.combine(ist_now.date(), _MARKET_CLOSE, tzinfo=IST) + timedelta(minutes=_POST_CLOSE_SLACK_MIN)
    return open_dt <= ist_now <= close_dt


def is_preopen_ist(now: datetime | None = None) -> bool:
    """True on a trading day between the start of the feed window (09:05 with the default slack) and the
    09:15 bell: the pre-open session, when feeds are polled but a symbol AngelOne cannot price has no
    live price anywhere (group244). Never true with MARKET_HOURS_FEED_ALWAYS_ON, on weekends or holidays."""
    if _ALWAYS_ON:
        return False
    ist_now = (now or datetime.now(timezone.utc)).astimezone(IST)
    if ist_now.weekday() >= 5 or is_nse_holiday_ist(ist_now):
        return False
    open_dt = datetime.combine(ist_now.date(), _MARKET_OPEN, tzinfo=IST)
    return open_dt - timedelta(minutes=_PRE_OPEN_SLACK_MIN) <= ist_now < open_dt


def seconds_until_next_window(now: datetime | None = None) -> float:
    """How long an idling feed loop should sleep before checking again.
    Short poll (60s) — cheap, and means the feed reliably picks back up
    within a minute of the window opening rather than needing an exact
    wake time computed."""
    return 60.0
