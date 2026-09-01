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

# A few minutes of slack on each side so a feed doesn't stop/start right
# at the bell — lets it warm up just before open and finish flushing just
# after close. Configurable without a code change.
_PRE_OPEN_SLACK_MIN = int(os.getenv("MARKET_HOURS_PRE_OPEN_SLACK_MIN", "10"))
_POST_CLOSE_SLACK_MIN = int(os.getenv("MARKET_HOURS_POST_CLOSE_SLACK_MIN", "5"))

# Escape hatch: force the feeds to poll 24/7 anyway (e.g. local dev/testing
# outside market hours). Off by default.
_ALWAYS_ON = os.getenv("MARKET_HOURS_FEED_ALWAYS_ON", "false").strip().lower() in (
    "1", "true", "yes", "on",
)


def is_feed_window_ist(now: datetime | None = None) -> bool:
    """True during Mon-Fri, roughly 09:05-15:35 IST (open/close +/- slack).
    Deliberately does NOT know about exchange holidays — same tradeoff as
    real-trade-service's is_market_open_ist(): a holiday just means the
    feed idles for nothing that day rather than crashing anything. Good
    enough for gating a background polling/streaming thread."""
    if _ALWAYS_ON:
        return True
    ist_now = (now or datetime.now(timezone.utc)).astimezone(IST)
    if ist_now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    open_dt = datetime.combine(ist_now.date(), _MARKET_OPEN, tzinfo=IST) - timedelta(minutes=_PRE_OPEN_SLACK_MIN)
    close_dt = datetime.combine(ist_now.date(), _MARKET_CLOSE, tzinfo=IST) + timedelta(minutes=_POST_CLOSE_SLACK_MIN)
    return open_dt <= ist_now <= close_dt


def seconds_until_next_window(now: datetime | None = None) -> float:
    """How long an idling feed loop should sleep before checking again.
    Short poll (60s) — cheap, and means the feed reliably picks back up
    within a minute of the window opening rather than needing an exact
    wake time computed."""
    return 60.0
