"""entry_engine/stopout_guard.py - whole-day stop-out protection for automatic entries (group 471, 2026-10-20).

WHY: the 20 Oct review: 24 stop-hit exits cost -1340 gross (17 filled beyond the stop) and 5 gap-down exits another -618. Group 394
already pauses entries after 2 stop-outs, but only until 09:45. Stop-outs cluster (weak tape, thin stocks) and later entries keep paying
the same overshoot, so the same idea now covers the WHOLE session:
  * day pause   : once STOPOUT_DAY_PAUSE_N (default 3) LOSING stop-outs have closed today, no new automatic entry is evaluated until tomorrow
                  (candidates stay queued, like opening_protect). 0 = off.
  * symbol block: a symbol that was stopped out today (losing) is not re-bought today (STOPOUT_SYMBOL_BLOCK, default on).
Counted from trade_position_events + trade_positions, so a restart does not reset it. Automatic path only (manual_engine is never touched).
Fails open: any DB / clock / config problem = no pause, no block. STOPOUT_GUARD_ENABLED=false turns everything off.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Optional, Set, Tuple

import config
import models
from entry_engine.opening_protect import STOP_REASONS
from tz_utils import as_aware, ist_now

logger = logging.getLogger("stopout-guard")

_warned: Set[str] = set()


def _warn_once(what: str, err: Exception) -> None:
    """E148 (F-16): these two checks stay fail-open (an error must never block an entry), but a broken setting is no longer silent:
    one warning per kind per process, so the log shows the guard is off without flooding it once per candidate."""
    if what not in _warned:
        _warned.add(what)
        logger.warning("stopout_guard: %s check failed (%s: %s) - the guard is OFF until this is fixed (check STOPOUT_DAY_PAUSE_N / STOPOUT_SYMBOL_BLOCK)",
                       what, type(err).__name__, err)
_LOOKBACK_ROWS = 400


def covers(mode: str) -> bool:
    try:
        return bool(config.STOPOUT_GUARD_ENABLED) and str(mode).upper() in config.STOPOUT_GUARD_MODES
    except Exception:  # noqa: BLE001
        return False


def tally(rows: Iterable[Tuple], since: datetime) -> Tuple[int, Set[str]]:
    """rows of (detail, occurred_at, realized_pnl, symbol) -> (losing stop-out count, symbols) closed at/after `since`. Pure, never raises."""
    n, syms = 0, set()
    try:
        for detail, occurred_at, pnl, symbol in rows:
            when = as_aware(occurred_at)
            if when is None or when < since:
                continue
            head = str(detail or "").split(":", 1)[0].strip()
            if head in STOP_REASONS and float(pnl or 0.0) < 0.0:
                n += 1
                if symbol:
                    syms.add(str(symbol).strip().upper())
    except Exception:  # noqa: BLE001
        return 0, set()
    return n, syms


def stopouts_today(db, mode: str, now: Optional[datetime] = None) -> Tuple[int, Set[str]]:
    """(losing stop-outs closed since today's IST midnight, their symbols). (0, set()) on any error."""
    try:
        since = ist_now(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        rows = (db.query(models.TradePositionEvent.detail, models.TradePositionEvent.occurred_at,
                         models.TradePosition.realized_pnl, models.TradePosition.symbol)
                .join(models.TradePosition, models.TradePosition.id == models.TradePositionEvent.position_id)
                .filter(models.TradePosition.mode == mode, models.TradePositionEvent.event_type == "CLOSED")
                .order_by(models.TradePositionEvent.occurred_at.desc())
                .limit(_LOOKBACK_ROWS).all())
        return tally(rows, since)
    except Exception as e:  # noqa: BLE001
        logger.debug("stopout_guard: count failed (no pause): %s", e)
        return 0, set()


def pause_reason_from(n: int) -> Optional[str]:
    try:
        limit = int(config.STOPOUT_DAY_PAUSE_N or 0)
        if limit > 0 and n >= limit:
            return f"STOPOUT_GUARD:PAUSED:{n} losing stop-outs today (limit {limit}) - no new entries until the next session"
    except Exception as e:  # noqa: BLE001
        _warn_once("day-pause", e)
    return None


def symbol_reason(symbol: str, stopped: Set[str]) -> Optional[str]:
    try:
        if config.STOPOUT_SYMBOL_BLOCK and str(symbol or "").strip().upper() in stopped:
            return f"STOPOUT_GUARD:SYMBOL:{symbol} was stopped out today - not re-bought the same session"
    except Exception as e:  # noqa: BLE001
        _warn_once("symbol-block", e)
    return None
