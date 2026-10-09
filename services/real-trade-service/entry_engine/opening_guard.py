"""entry_engine/opening_guard.py - no automatic entries in the first minutes after the open (group 220, 2026-10-07).

WHY: review item 6. Nothing stopped an automatic entry at 09:15-09:30, and the September trade file's five entries
opened 09:19-09:24 IST all lost (3 trading days, -Rs 274 together). Small and old, so a precaution rather than a proven
fix; see docs/GROUP220_OPENING_ENTRY_GUARD.md for what the data does and does not show.

WHAT: while the market is open and the IST clock is before OPENING_ENTRY_NOT_BEFORE_IST (default 09:15 since group 268, was 09:30),
reason(mode) returns a message and entry_engine.evaluate_mode() returns early WITHOUT touching any candidate: they stay
queued (consumed=False), nothing is rejected or logged as a decision, and they are evaluated normally once the guard lifts.
enter_at_open_time(mode) moves the ENTER_AT_OPEN schedule to the guard time when that is later, so the feature's single
daily run is not spent inside the guard.

Pure functions plus a log throttle; never raises into the caller (any error means "no guard"). Off with
OPENING_ENTRY_GUARD_ENABLED=false.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, time as _time
from typing import Optional

import config
from tz_utils import ist_now, is_market_open_ist, parse_hhmm

logger = logging.getLogger("opening-entry-guard")

_LOG_EVERY_S = 300.0
_last_logged: dict = {}


def reset_state() -> None:
    """Test helper: forget the log throttle."""
    _last_logged.clear()


def covers(mode: str) -> bool:
    try:
        return bool(config.OPENING_ENTRY_GUARD_ENABLED) and str(mode).upper() in config.OPENING_ENTRY_GUARD_MODES
    except Exception:
        return False


def not_before() -> _time:
    return parse_hhmm(config.OPENING_ENTRY_NOT_BEFORE_IST, 9, 15)


def reason(mode: str, now: Optional[datetime] = None) -> Optional[str]:
    """A human-readable reason when automatic entries must wait, else None."""
    try:
        if not covers(mode):
            return None
        if not is_market_open_ist(now):
            return None                      # pre-open / after close / holiday: this guard is only about the opening minutes
        cutoff = not_before()
        clock = ist_now(now).time()
        if clock >= cutoff:
            return None
        return (
            f"Opening guard: no new entries before {cutoff.strftime('%H:%M')} IST "
            f"(now {clock.strftime('%H:%M')}) - the first minutes after the open are the most volatile. "
            "Candidates stay queued and are evaluated once the guard lifts."
        )
    except Exception as e:
        logger.debug("opening guard check failed, allowing entry: %s", e)
        return None


def should_log(mode: str) -> bool:
    """True at most once per _LOG_EVERY_S per mode, so a 60 s cycle does not write 15 identical lines."""
    now = time.monotonic()
    if now - _last_logged.get(mode, -1e9) >= _LOG_EVERY_S:
        _last_logged[mode] = now
        return True
    return False


def enter_at_open_time(mode: str) -> _time:
    """When the ENTER_AT_OPEN schedule may fire: its own time, or the guard time if that is later and the guard covers `mode`."""
    base = parse_hhmm(config.ENTER_AT_OPEN_TIME_IST, 9, 20)
    try:
        if covers(mode):
            cutoff = not_before()
            if cutoff > base:
                return cutoff
    except Exception:
        pass
    return base
