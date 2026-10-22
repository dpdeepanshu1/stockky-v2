"""orders/entry_cap.py - real daily cap on NEW automatic entries (E39, 2026-10-23).

Position Stocks only had DAILY_ORDER_BUDGET (300 = practically no cap) and the open-positions cap, so nothing limited over-trading across a day.
Real Trade has RISK_MAX_ENTRIES_PER_DAY (6). This is the same rule here: once MAX_ENTRIES_PER_DAY positions were opened today (IST),
no new automatic entry until the next session. 0 = off.
Counted from scalp_positions (opened_at since today's IST midnight, rows with status ERROR = the order never filled, are not entries),
so a restart does not reset it. Automatic path only. Fails open: any DB / clock / config problem = no cap.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import config
from tz_utils import ist_now

logger = logging.getLogger("position-stocks-entry-cap")


def limit() -> int:
    try:
        return max(0, int(config.MAX_ENTRIES_PER_DAY or 0))
    except Exception:  # noqa: BLE001
        return 0


def reason_from(n: int) -> Optional[str]:
    """Pure: the skip reason when `n` entries are already opened today, else None."""
    cap = limit()
    if cap > 0 and n >= cap:
        return f"ENTRY_CAP:{n} entries already opened today (cap {cap}) - no new entries until the next session"
    return None


def entries_today(db, now: Optional[datetime] = None) -> int:
    """Positions opened since today's IST midnight, not counting ERROR rows. 0 on any error."""
    try:
        from models import ScalpPosition
        since = ist_now(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        return int(db.query(ScalpPosition).filter(ScalpPosition.opened_at >= since, ScalpPosition.status != "ERROR").count())
    except Exception as e:  # noqa: BLE001
        logger.debug("entry_cap: count failed (no cap): %s", e)
        return 0


def entry_reason(db, now: Optional[datetime] = None) -> Optional[str]:
    """One call for the entry path. Never raises."""
    try:
        if limit() <= 0:
            return None
        return reason_from(entries_today(db, now))
    except Exception:  # noqa: BLE001
        return None
