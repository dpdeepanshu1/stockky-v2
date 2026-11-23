"""orders/midday_size.py - half size for new automatic entries in the midday window (round 675, 23 Nov 2026).

Evidence (30-day Position Stocks hour split, 159 trades): entries started 12:00-13:59 were 38 trades, 1 target hit, 25 stagnation exits, 11 stops, about -163 net
(12:00 hour -88, 13:00 hour -75); entries started in the 09:00 hour hit the target in 12 of 36. The exits themselves are not the problem (the after-exit check on
91 stagnation trades showed holding longer does not help), the late entry is. So the size shrinks; nothing is blocked and nothing is added.

  * size_mult(): MIDDAY_SIZE_MULT (clamped 0.1..1.0, ships 0.5) from MIDDAY_SIZE_FROM_IST (12:00) until MIDDAY_SIZE_UNTIL_IST (14:00, the same time as the ordinary-day
    entry cut-off SCALP_ENTRY_CUTOFF_IST) on a trading day, else 1.0.
  * The caller treats it like the opening-window size: the LOWER of it and a matched setup's own size applies, they do not stack (so a setup already at half size or
    less is unchanged, and a full-size legacy or promoted entry becomes half size).
  * Only the automatic path uses it. A manual BUY was confirmed by a person. Fails open: a bad clock, a bad setting or a holiday means full size.
MIDDAY_SIZE_ENABLED=false turns it off. A full stop of this window is NOT built: decide it only after 100 trades in the window are still negative.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as _time
from typing import Optional

import config
from tz_utils import ist_now, is_nse_holiday, parse_hhmm

logger = logging.getLogger("position-stocks-midday-size")


def active(now: Optional[datetime] = None) -> bool:
    """True on a trading day from MIDDAY_SIZE_FROM_IST until MIDDAY_SIZE_UNTIL_IST while the switch is on. Never raises."""
    try:
        if not getattr(config, "MIDDAY_SIZE_ENABLED", True):
            return False
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(n):
            return False
        start = parse_hhmm(str(getattr(config, "MIDDAY_SIZE_FROM_IST", "12:00")), 12, 0)
        end = parse_hhmm(str(getattr(config, "MIDDAY_SIZE_UNTIL_IST", "14:00")), 14, 0)
        if not (start < end):
            return False          # an empty or reversed window never shrinks anything
        return start <= n.time() < end
    except Exception:  # noqa: BLE001
        return False


def size_mult(now: Optional[datetime] = None) -> float:
    """MIDDAY_SIZE_MULT (clamped 0.1..1.0) while the window is active, else 1.0. Never raises."""
    try:
        if not active(now):
            return 1.0
        m = float(getattr(config, "MIDDAY_SIZE_MULT", 0.5))
        if m != m:
            return 1.0
        return min(1.0, max(0.1, m))
    except Exception:  # noqa: BLE001
        return 1.0
