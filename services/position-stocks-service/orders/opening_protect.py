"""orders/opening_protect.py - opening-window protection for the automatic scalp entry (group 392, 2026-10-15).

The first 30 minutes after the bell are the noisiest of the day (wide spreads, auction-driven prints, stops hit by the first swing). Two guards:
  * size_mult(): the automatic entry is sized at OPENING_SIZE_MULT (default 0.5) of the normal quantity from 09:15 until OPENING_PROTECT_UNTIL_IST (09:45);
  * pause_reason(db): once OPENING_PAUSE_STOPOUTS (default 2) positions have been stopped out since the 09:15 bell (status STOP_HIT or STOP_BREACH_EXIT),
    no new automatic entry is taken until the window ends. Counted from the positions table, so it survives a restart.
Only the automatic path uses these; a manual BUY was confirmed by a person. Fails open (a DB error or bad clock means no pause / full size).
OPENING_PROTECT_ENABLED=0 turns both off.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as _time, timezone
from typing import Optional

import config
from tz_utils import IST, ist_now, is_nse_holiday, parse_hhmm

logger = logging.getLogger("position-stocks-opening-protect")

_STOP_STATUSES = ("STOP_HIT", "STOP_BREACH_EXIT")


def active(now: Optional[datetime] = None) -> bool:
    """True on a trading day from 09:15 until OPENING_PROTECT_UNTIL_IST while the protection is enabled. Never raises."""
    try:
        if not config.OPENING_PROTECT_ENABLED:
            return False
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(n):
            return False
        return _time(9, 15) <= n.time() < parse_hhmm(config.OPENING_PROTECT_UNTIL_IST, 9, 45)
    except Exception:  # noqa: BLE001
        return False


def size_mult(now: Optional[datetime] = None) -> float:
    """OPENING_SIZE_MULT (clamped 0.1..1.0) while the window is active, else 1.0. Never raises."""
    try:
        if not active(now):
            return 1.0
        return min(1.0, max(0.1, float(config.OPENING_SIZE_MULT)))
    except Exception:  # noqa: BLE001
        return 1.0


def stopouts_since_open(db, now: Optional[datetime] = None) -> int:
    """Positions stopped out since today's 09:15 bell. 0 on any error."""
    try:
        from models import ScalpPosition
        n = ist_now(now)
        since = n.replace(hour=9, minute=15, second=0, microsecond=0).astimezone(timezone.utc)
        return int(db.query(ScalpPosition)
                   .filter(ScalpPosition.status.in_(_STOP_STATUSES), ScalpPosition.closed_at >= since)
                   .count())
    except Exception as e:  # noqa: BLE001
        logger.debug("opening_protect: stop-out count failed (no pause): %s", e)
        return 0


def pause_reason(db, now: Optional[datetime] = None) -> Optional[str]:
    """A skip reason while the opening pause is in force, else None. Never raises."""
    try:
        limit = int(config.OPENING_PAUSE_STOPOUTS or 0)
        if limit <= 0 or not active(now):
            return None
        n = stopouts_since_open(db, now)
        if n >= limit:
            return (f"OPENING_PROTECT:PAUSED:{n} stop-outs since the open (limit {limit}) - no new entries until "
                    f"{parse_hhmm(config.OPENING_PROTECT_UNTIL_IST, 9, 45).strftime('%H:%M')}")
        return None
    except Exception:  # noqa: BLE001
        return None
