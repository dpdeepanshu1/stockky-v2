"""entry_engine/opening_protect.py - opening-window protection for automatic entries in real-trade (group 394, 2026-10-16).

Same two guards as position-stocks (group 392, orders/opening_protect.py), adapted to this service's sizing and bookkeeping:
  * size_mult(mode): inside the window (09:15 until OPENING_PROTECT_UNTIL_IST, default 09:45) an automatic entry is cut to OPENING_SIZE_MULT
    (default 0.5) of the quantity the risk sizing produced - nearest share, a 1-share quantity is left alone (scaled_qty);
  * pause_reason(db, mode): once OPENING_PAUSE_STOPOUTS (default 2) LOSING stop-outs have closed since today's 09:15 bell, no new automatic
    entry is evaluated until the window ends. The candidates stay queued (not consumed, not rejected), exactly like the opening guard, so
    they are looked at normally once the pause lifts.

A stop-out here is a position fully CLOSED by one of STOP_REASONS ("stop_hit" the bot's own stop, "broker_stop" the resting Dhan stop, "emergency_gap_down")
with a negative realized P&L. Counted from trade_position_events + trade_positions, so a restart does not reset it. (position-stocks counts
every STOP_HIT / STOP_BREACH_EXIT status; here a trailing stop that closed in profit does not count, because it is not a failed entry.)

Only the automatic path (entry_engine.evaluate_mode) uses these; manual_engine is never touched (a person confirmed that order).
Modes: OPENING_PROTECT_MODES (default REAL,DEMO; set REAL to keep DEMO as an unprotected control). Fails open: a DB error, bad clock or bad
config means full size and no pause. OPENING_PROTECT_ENABLED=false turns both off.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as _time, timezone
from typing import Optional

import config
import models
from tz_utils import as_aware, is_nse_holiday, ist_now, parse_hhmm

logger = logging.getLogger("opening-protect")

STOP_REASONS = ("stop_hit", "broker_stop", "emergency_gap_down")
_LOOKBACK_ROWS = 300          # newest CLOSED events scanned; far more than one opening window can produce


def covers(mode: str) -> bool:
    """True when the protection is enabled and applies to `mode`. Never raises."""
    try:
        return bool(config.OPENING_PROTECT_ENABLED) and str(mode).upper() in config.OPENING_PROTECT_MODES
    except Exception:  # noqa: BLE001
        return False


def active(mode: str, now: Optional[datetime] = None) -> bool:
    """True on a trading day from 09:15 until OPENING_PROTECT_UNTIL_IST for a covered mode. Never raises."""
    try:
        if not covers(mode):
            return False
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(n):
            return False
        return _time(9, 15) <= n.time() < parse_hhmm(config.OPENING_PROTECT_UNTIL_IST, 9, 45)
    except Exception:  # noqa: BLE001
        return False


def size_mult(mode: str, now: Optional[datetime] = None) -> float:
    """OPENING_SIZE_MULT (clamped 0.1..1.0) while the window is active for `mode`, else 1.0. Never raises."""
    try:
        if not active(mode, now):
            return 1.0
        return min(1.0, max(0.1, float(config.OPENING_SIZE_MULT)))
    except Exception:  # noqa: BLE001
        return 1.0


def scaled_qty(qty: int, mult: float) -> int:
    """Nearest share; a quantity of 1 (or a mult of 1.0 or more) is unchanged; never below 1. Same rule as the replay tool."""
    try:
        if mult >= 1.0 or qty <= 1:
            return qty
        return max(1, int(qty * mult + 0.5))
    except Exception:  # noqa: BLE001
        return qty


def stopouts_since_open(db, mode: str, now: Optional[datetime] = None) -> int:
    """Losing stop-outs of `mode` closed since today's 09:15 IST bell. 0 on any error."""
    try:
        since = ist_now(now).replace(hour=9, minute=15, second=0, microsecond=0).astimezone(timezone.utc)
        rows = (db.query(models.TradePositionEvent.detail, models.TradePositionEvent.occurred_at, models.TradePosition.realized_pnl)
                .join(models.TradePosition, models.TradePosition.id == models.TradePositionEvent.position_id)
                .filter(models.TradePosition.mode == mode, models.TradePositionEvent.event_type == "CLOSED")
                .order_by(models.TradePositionEvent.occurred_at.desc())
                .limit(_LOOKBACK_ROWS).all())
        n = 0
        for detail, occurred_at, pnl in rows:
            when = as_aware(occurred_at)
            if when is None or when < since:
                continue
            head = str(detail or "").split(":", 1)[0].strip()
            if head in STOP_REASONS and float(pnl or 0.0) < 0.0:
                n += 1
        return n
    except Exception as e:  # noqa: BLE001
        logger.debug("opening_protect: stop-out count failed (no pause): %s", e)
        return 0


def pause_reason(db, mode: str, now: Optional[datetime] = None) -> Optional[str]:
    """A message while the opening pause is in force for `mode`, else None. Never raises."""
    try:
        limit = int(config.OPENING_PAUSE_STOPOUTS or 0)
        if limit <= 0 or not active(mode, now):
            return None
        n = stopouts_since_open(db, mode, now)
        if n >= limit:
            return (f"OPENING_PROTECT:PAUSED:{n} stop-outs since the open (limit {limit}) - no new entries until "
                    f"{parse_hhmm(config.OPENING_PROTECT_UNTIL_IST, 9, 45).strftime('%H:%M')} IST")
        return None
    except Exception:  # noqa: BLE001
        return None
