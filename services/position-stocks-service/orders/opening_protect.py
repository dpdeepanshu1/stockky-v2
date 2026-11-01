"""orders/opening_protect.py - opening-window protection for the automatic scalp entry (group 392, 2026-10-15).

The first 30 minutes after the bell are the noisiest of the day (wide spreads, auction-driven prints, stops hit by the first swing). Two guards:
  * size_mult(): the automatic entry is sized at OPENING_SIZE_MULT (default 0.5) of the normal quantity from 09:15 until OPENING_PROTECT_UNTIL_IST (09:45);
  * pause_reason(db): once OPENING_PAUSE_STOPOUTS (default 2) positions have been stopped out since the 09:15 bell (status STOP_HIT or STOP_BREACH_EXIT),
    no new automatic entry is taken until the window ends. Counted from the positions table, so it survives a restart.
E285 round 99 adds two more guards to pause_reason(): early_cap_reason (at most OPENING_MAX_POSITIONS, ships 2, open at once in the window) and loss_budget_reason
(realised loss since the bell reaching OPENING_LOSS_BUDGET_FRAC, ships 1/3, of the day's loss cap = MAX_DAILY_LOSS_PCT_OF_POOL x pool). Both fail open; 0 turns each off.
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


_OPEN_STATUSES = ("OPEN", "EXIT_LEGS_REJECTED")


def open_count(db) -> int:
    """Positions open right now. 0 on any error."""
    try:
        from models import ScalpPosition
        return int(db.query(ScalpPosition).filter(ScalpPosition.status.in_(_OPEN_STATUSES)).count())
    except Exception as e:  # noqa: BLE001
        logger.debug("opening_protect: open count failed (no cap): %s", e)
        return 0


def early_cap_reason(db, now: Optional[datetime] = None) -> Optional[str]:
    """E285 round 99: at most OPENING_MAX_POSITIONS (ships 2) positions open at once inside the opening window; a skip reason when that many are open, else None.
    0 = no cap. Never raises."""
    try:
        limit = int(config.OPENING_MAX_POSITIONS or 0)
        if limit <= 0 or not active(now):
            return None
        n = open_count(db)
        if n >= limit:
            return (f"OPENING_PROTECT:MAX_POSITIONS:{n} open (limit {limit} until "
                    f"{parse_hhmm(config.OPENING_PROTECT_UNTIL_IST, 9, 45).strftime('%H:%M')})")
        return None
    except Exception:  # noqa: BLE001
        return None


def realized_since_open(db, now: Optional[datetime] = None) -> float:
    """Net realised rupees of positions closed since today's 09:15 bell (net_pnl, else realized_pnl, else 0 per row). 0.0 on any error."""
    try:
        from models import ScalpPosition
        n = ist_now(now)
        since = n.replace(hour=9, minute=15, second=0, microsecond=0).astimezone(timezone.utc)
        total = 0.0
        for r in db.query(ScalpPosition).filter(ScalpPosition.closed_at.isnot(None), ScalpPosition.closed_at >= since).all():
            v = r.net_pnl if r.net_pnl is not None else r.realized_pnl
            if v is not None and v == v:
                total += float(v)
        return total
    except Exception as e:  # noqa: BLE001
        logger.debug("opening_protect: realised-since-open failed (no budget stop): %s", e)
        return 0.0


def loss_budget_rupees(db) -> float:
    """OPENING_LOSS_BUDGET_FRAC (ships 1/3) of the day's loss cap (MAX_DAILY_LOSS_PCT_OF_POOL of the pool), in rupees. 0.0 = no budget (switch off, pool unknown, error). Never raises."""
    try:
        frac = float(config.OPENING_LOSS_BUDGET_FRAC or 0.0)
        if not (frac > 0.0):
            return 0.0
        from models import ScalpCapitalLedger
        row = db.query(ScalpCapitalLedger).filter_by(mode="REAL").first()          # read only: never creates the row
        pool = float(getattr(row, "total_allocated_capital", 0.0) or 0.0)
        pct = float(getattr(config, "MAX_DAILY_LOSS_PCT_OF_POOL", 0.0) or 0.0)
        if not (pool > 0.0 and pct > 0.0):
            return 0.0
        return min(1.0, frac) * pool * pct / 100.0
    except Exception as e:  # noqa: BLE001
        logger.debug("opening_protect: loss budget failed (no budget stop): %s", e)
        return 0.0


def loss_budget_reason(db, now: Optional[datetime] = None) -> Optional[str]:
    """E285 round 99: inside the opening window, once the realised loss since the bell reaches OPENING_LOSS_BUDGET_FRAC of the day's loss cap, no new automatic entry
    until the window ends. Realised only (open positions are not marked). Never raises."""
    try:
        if not active(now):
            return None
        budget = loss_budget_rupees(db)
        if not (budget > 0.0):
            return None
        pnl = realized_since_open(db, now)
        if -pnl >= budget:
            return (f"OPENING_PROTECT:LOSS_BUDGET:Rs{-pnl:.0f} lost since the open (budget Rs{budget:.0f}) - no new entries until "
                    f"{parse_hhmm(config.OPENING_PROTECT_UNTIL_IST, 9, 45).strftime('%H:%M')}")
        return None
    except Exception:  # noqa: BLE001
        return None


def pause_reason(db, now: Optional[datetime] = None) -> Optional[str]:
    """A skip reason while an opening guard is in force (stop-out pause, E285 round 99 position cap, E285 round 99 loss budget), else None. Never raises."""
    try:
        limit = int(config.OPENING_PAUSE_STOPOUTS or 0)
        if limit > 0 and active(now):
            n = stopouts_since_open(db, now)
            if n >= limit:
                return (f"OPENING_PROTECT:PAUSED:{n} stop-outs since the open (limit {limit}) - no new entries until "
                        f"{parse_hhmm(config.OPENING_PROTECT_UNTIL_IST, 9, 45).strftime('%H:%M')}")
        return early_cap_reason(db, now) or loss_budget_reason(db, now)
    except Exception:  # noqa: BLE001
        return None
