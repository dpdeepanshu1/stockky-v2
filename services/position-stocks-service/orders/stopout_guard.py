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
        logger.debug("stopout_guard: pause_reason_from failed (no pause)", exc_info=True)
    return None


def symbol_reason(symbol: str, stopped: Set[str]) -> Optional[str]:
    try:
        if config.STOPOUT_SYMBOL_BLOCK and str(symbol or "").strip().upper() in stopped:
            return f"STOPOUT_GUARD:SYMBOL:{symbol} was stopped out today - not re-bought the same session"
    except Exception:  # noqa: BLE001
        logger.debug("stopout_guard: symbol_reason failed (no block)", exc_info=True)
    return None


def _aware(dt):
    if dt is None:
        return None
    return dt if getattr(dt, "tzinfo", None) else dt.replace(tzinfo=timezone.utc)


def reclaim_decision(rows, ltp: float, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """E251: may this symbol be bought ONE more time after today's single losing stop-out? `rows` are today's trades of the symbol, newest first, as dicts
    (status, closed_at, realized_pnl, entry_price, target_price). All must hold: switch on; the symbol has no more trades today than
    REENTRY_RECLAIM_MAX_PER_SYMBOL (the pass is one-time); the last one was a losing stop-out; it closed between REENTRY_RECLAIM_MIN_WAIT_S and
    REENTRY_RECLAIM_MAX_AGE_MIN ago; price is back above the old entry (+REENTRY_RECLAIM_ABOVE_ENTRY_PCT) but not past REENTRY_RECLAIM_MAX_TARGET_FRAC
    of the way to the old target (that would be chasing). (ok, reason). Pure, never raises."""
    try:
        if not getattr(config, "REENTRY_RECLAIM_ENABLED", False):
            return False, "reclaim off"
        max_n = int(getattr(config, "REENTRY_RECLAIM_MAX_PER_SYMBOL", 0) or 0)
        rows = list(rows or ())
        if max_n <= 0 or not rows or len(rows) > max_n:
            return False, "no reclaim pass left"
        last = rows[0]
        if str(last.get("status") or "").strip().upper() not in STOP_STATUSES or float(last.get("realized_pnl") or 0.0) >= 0.0:
            return False, "last trade was not a losing stop-out"
        closed = _aware(last.get("closed_at"))
        entry, target = float(last.get("entry_price") or 0.0), float(last.get("target_price") or 0.0)
        if closed is None or entry <= 0 or target <= entry or ltp <= 0:
            return False, "last trade has no usable levels"
        t = _aware(now) or datetime.now(timezone.utc)
        age_s = (t - closed).total_seconds()
        if age_s < float(getattr(config, "REENTRY_RECLAIM_MIN_WAIT_S", 120.0)):
            return False, f"stopped out {age_s:.0f}s ago - too soon"
        if age_s > float(getattr(config, "REENTRY_RECLAIM_MAX_AGE_MIN", 120.0)) * 60.0:
            return False, "stop-out too old"
        lo = entry * (1.0 + float(getattr(config, "REENTRY_RECLAIM_ABOVE_ENTRY_PCT", 0.1)) / 100.0)
        hi = entry + float(getattr(config, "REENTRY_RECLAIM_MAX_TARGET_FRAC", 0.5)) * (target - entry)
        if ltp < lo:
            return False, f"price {ltp:.2f} has not reclaimed the old entry {entry:.2f} (needs >= {lo:.2f})"
        if ltp > hi:
            return False, f"price {ltp:.2f} is past {hi:.2f} (half way to the old target) - chasing"
        return True, f"reclaimed the old entry {entry:.2f} ({ltp:.2f}), {age_s / 60:.1f} min after the stop-out, room left to the old target {target:.2f}"
    except Exception:  # noqa: BLE001
        return False, "reclaim check failed"


def reclaim_allowed(db, symbol: str, ltp: float, now: Optional[datetime] = None) -> Optional[dict]:
    """The reclaim verdict for the entry path ({"why": ...}) or None. Reads today's trades of the symbol from scalp_positions. Fails closed (None). Never raises."""
    try:
        if not enabled() or not getattr(config, "REENTRY_RECLAIM_ENABLED", False) or int(getattr(config, "REENTRY_RECLAIM_MAX_PER_SYMBOL", 0) or 0) <= 0:
            return None
        from models import ScalpPosition
        since = ist_now(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        sym = str(symbol or "").strip().upper()
        recs = (db.query(ScalpPosition).filter(ScalpPosition.symbol == sym, ScalpPosition.opened_at >= since)
                .order_by(ScalpPosition.opened_at.desc()).limit(10).all())
        rows = [{"status": r.status, "closed_at": r.closed_at, "realized_pnl": r.realized_pnl, "entry_price": r.entry_price, "target_price": r.target_price}
                for r in recs]
        ok, why = reclaim_decision(rows, float(ltp), now)
        return {"why": why} if ok else None
    except Exception as e:  # noqa: BLE001
        logger.debug("stopout_guard: reclaim check failed for %s (no reclaim): %s", symbol, e)
        return None


def entry_reason(db, symbol: str, now: Optional[datetime] = None, allow_symbol: bool = False) -> Optional[str]:
    """One call for the entry path: the day-pause reason, else the symbol-block reason, else None. allow_symbol (E251 reclaim) lifts only the symbol
    block - the whole-day pause still wins. Never raises."""
    try:
        if not enabled():
            return None
        n, stopped = stopouts_today(db, now)
        return pause_reason_from(n) or (None if allow_symbol else symbol_reason(symbol, stopped))
    except Exception:  # noqa: BLE001
        return None


def status(db, now: Optional[datetime] = None) -> dict:
    """Read-only snapshot for /review-fixes/status style endpoints."""
    n, stopped = stopouts_today(db, now)
    return {"enabled": enabled(), "day_pause_n": int(getattr(config, "STOPOUT_DAY_PAUSE_N", 0) or 0),
            "symbol_block": bool(getattr(config, "STOPOUT_SYMBOL_BLOCK", False)),
            "losing_stopouts_today": n, "symbols": sorted(stopped), "paused": pause_reason_from(n) is not None}
