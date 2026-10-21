"""orders/stopout_guard.py - whole-day stop-out protection for the automatic scalp entry (group 479, 2026-10-21).

Same idea as real-trade-service entry_engine/stopout_guard.py (group 471). The 21 Oct review: stop-hit exits were the biggest loss driver
and they cluster (weak tape, thin stocks). The group 392 opening pause only covers 09:15-09:45; this covers the whole session:
  * day pause   : once STOPOUT_DAY_PAUSE_N (default 3) LOSING stop-outs (status STOP_HIT / STOP_BREACH_EXIT, realized_pnl < 0) have closed today,
                  no new automatic entry until tomorrow. 0 = off.
  * symbol block: a symbol that was stopped out today (losing) is not re-bought today (STOPOUT_SYMBOL_BLOCK, default on).
Counted from scalp_positions, so a restart does not reset it. Automatic path only (a manual BUY was confirmed by a person).
Fails open: any DB / clock / config problem = no pause, no block. STOPOUT_GUARD_ENABLED=false turns everything off.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Optional, Set, Tuple

import config
from tz_utils import ist_now

logger = logging.getLogger("position-stocks-stopout-guard")

STOP_STATUSES = ("STOP_HIT", "STOP_BREACH_EXIT")
_LOOKBACK_ROWS = 400


def enabled() -> bool:
    try:
        return bool(config.STOPOUT_GUARD_ENABLED)
    except Exception:  # noqa: BLE001
        return False


def tally(rows: Iterable[Tuple], since: Optional[datetime] = None) -> Tuple[int, Set[str]]:
    """rows of (status, closed_at, realized_pnl, symbol) -> (losing stop-out count, symbols) closed at/after `since`. Pure, never raises."""
    n, syms = 0, set()
    try:
        for status, closed_at, pnl, symbol in rows:
            if str(status or "").strip().upper() not in STOP_STATUSES:
                continue
            if since is not None:
                if closed_at is None:
                    continue
                when = closed_at if closed_at.tzinfo else closed_at.replace(tzinfo=timezone.utc)
                if when < since:
                    continue
            if float(pnl if pnl is not None else 0.0) < 0.0:
                n += 1
                if symbol:
                    syms.add(str(symbol).strip().upper())
    except Exception:  # noqa: BLE001
        return 0, set()
    return n, syms


def stopouts_today(db, now: Optional[datetime] = None) -> Tuple[int, Set[str]]:
    """(losing stop-outs closed since today's IST midnight, their symbols). (0, set()) on any error."""
    try:
        from models import ScalpPosition
        since = ist_now(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        rows = (db.query(ScalpPosition.status, ScalpPosition.closed_at, ScalpPosition.realized_pnl, ScalpPosition.symbol)
                .filter(ScalpPosition.status.in_(STOP_STATUSES), ScalpPosition.closed_at >= since)
                .order_by(ScalpPosition.closed_at.desc()).limit(_LOOKBACK_ROWS).all())
        return tally(rows, since)
    except Exception as e:  # noqa: BLE001
        logger.debug("stopout_guard: count failed (no pause): %s", e)
        return 0, set()


def pause_reason_from(n: int) -> Optional[str]:
    try:
        limit = int(config.STOPOUT_DAY_PAUSE_N or 0)
        if limit > 0 and n >= limit:
            return f"STOPOUT_GUARD:PAUSED:{n} losing stop-outs today (limit {limit}) - no new entries until the next session"
    except Exception:  # noqa: BLE001
        pass
    return None


def symbol_reason(symbol: str, stopped: Set[str]) -> Optional[str]:
    try:
        if config.STOPOUT_SYMBOL_BLOCK and str(symbol or "").strip().upper() in stopped:
            return f"STOPOUT_GUARD:SYMBOL:{symbol} was stopped out today - not re-bought the same session"
    except Exception:  # noqa: BLE001
        pass
    return None


def entry_reason(db, symbol: str, now: Optional[datetime] = None) -> Optional[str]:
    """One call for the entry path: the day-pause reason, else the symbol-block reason, else None. Never raises."""
    try:
        if not enabled():
            return None
        n, stopped = stopouts_today(db, now)
        return pause_reason_from(n) or symbol_reason(symbol, stopped)
    except Exception:  # noqa: BLE001
        return None


def status(db, now: Optional[datetime] = None) -> dict:
    """Read-only snapshot for /review-fixes/status style endpoints."""
    n, stopped = stopouts_today(db, now)
    return {"enabled": enabled(), "day_pause_n": int(getattr(config, "STOPOUT_DAY_PAUSE_N", 0) or 0),
            "symbol_block": bool(getattr(config, "STOPOUT_SYMBOL_BLOCK", False)),
            "losing_stopouts_today": n, "symbols": sorted(stopped), "paused": pause_reason_from(n) is not None}
