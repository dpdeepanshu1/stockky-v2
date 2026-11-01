"""execution/lock_sweep.py - release stale shared symbol locks (task 4, 31 Oct 2026).

WHY: 11 symbols were locked by real-trade-service, some since 18 Sep, with no open row in trade_positions or the swing ledger and a published open
market value of 0.0. A lock nothing backs blocks position-stocks on that symbol for nothing.

A lock is released ONLY when all of these hold:
  * it is held by real-trade-service with mode REAL, and is older than LOCK_SWEEP_MIN_AGE_MIN (an order may be mid-placement);
  * no open TradePosition (status OPEN / PARTIALLY_CLOSED, or qty_open > 0) and no open swing row (PLANNED / OPEN, LIVE kind) names the symbol;
  * no live BUY order (PENDING / PLACED / PARTIAL) names the symbol;
  * neither Dhan holdings nor Dhan positions show the symbol.
If ANY input cannot be read, nothing is released. Locks held by position-stocks-service are never touched.

Switch: config.LOCK_SWEEP (Calibration page), read at call time. Throttled by maybe_sweep() to one run per LOCK_SWEEP_INTERVAL_MIN.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

logger = logging.getLogger("real-trade-lock-sweep")

SVC = "real-trade-service"
_last_run = {"t": None}


def reset() -> None:
    _last_run["t"] = None


def _norm(sym) -> str:
    s = str(sym or "").strip().upper()
    return s[:-3] if s.endswith("-EQ") else s


def _cfg(name: str, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:  # noqa: BLE001
        return default


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _broker_symbols(holdings: Iterable, positions: Iterable) -> set:
    """Symbols the broker shows at all (any row naming the symbol with a non-zero quantity). Raises on an unreadable row so the sweep releases nothing."""
    out = set()
    for h in holdings or []:
        q = float(h.get("totalQty") or h.get("availableQty") or 0)
        if q != 0:
            out.add(_norm(h.get("tradingSymbol")))
    for p in positions or []:
        if float(p.get("netQty") or 0) != 0:
            out.add(_norm(p.get("tradingSymbol")))
    out.discard("")
    return out


def _backed_in_db(db) -> set:
    """Symbols backed by an open position, an open LIVE swing row or a live BUY order (any mode: stricter is safer). Raises on a DB error."""
    import models
    out = set()
    for r in db.query(models.TradePosition).filter(
            (models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED"))) | (models.TradePosition.qty_open > 0)).all():
        out.add(_norm(r.symbol))
    for r in db.query(models.SwingTrade).filter(models.SwingTrade.kind == "LIVE", models.SwingTrade.status.in_(("PLANNED", "OPEN"))).all():
        out.add(_norm(r.symbol))
    for r in db.query(models.TradeOrder).filter(models.TradeOrder.side == "BUY", models.TradeOrder.status.in_(("PENDING", "PLACED", "PARTIAL"))).all():
        out.add(_norm(r.symbol))
    return out


def sweep(db, holdings, positions, now: Optional[datetime] = None) -> list:
    """One pass. Returns the symbols released. Never raises; releases nothing when any input is unreadable."""
    try:
        import models
        from execution import shared_symbol_lock as ssl
        now = _aware(now) or datetime.now(timezone.utc)
        min_age = timedelta(minutes=float(_cfg("LOCK_SWEEP_MIN_AGE_MIN", 15)))
        locks = db.query(models.SharedSymbolLock).filter_by(held_by_service=SVC).all()
        candidates = []
        for lk in locks:
            if (lk.held_by_mode or "REAL") != "REAL":
                continue
            claimed = _aware(lk.claimed_at)
            if claimed is not None and now - claimed < min_age:
                continue
            candidates.append(_norm(lk.symbol))
        if not candidates:
            return []
        backed = _backed_in_db(db) | _broker_symbols(holdings, positions)
        released = []
        for sym in candidates:
            if sym in backed:
                continue
            ssl.release(db, sym)
            released.append(sym)
            logger.warning("lock sweep: released stale lock on %s (no position, swing row, order or broker holding backs it)", sym)
        if released:
            try:
                from notifier import notify_fire_and_forget
                notify_fire_and_forget(f"{SVC}: lock sweep released {len(released)} stale symbol lock(s): {', '.join(sorted(released))}")
            except Exception:  # noqa: BLE001
                pass
        return released
    except Exception as e:  # noqa: BLE001
        logger.warning("lock sweep: skipped, an input was unreadable (%s)", str(e)[:200] or type(e).__name__)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


def maybe_sweep(db, mode: str, now_mono: Optional[float] = None) -> list:
    """Cycle hook: REAL only, switch on, at most one sweep per LOCK_SWEEP_INTERVAL_MIN. Never raises."""
    try:
        if mode != "REAL" or not bool(_cfg("LOCK_SWEEP", True)):
            return []
        every = float(_cfg("LOCK_SWEEP_INTERVAL_MIN", 10)) * 60.0
        t = time.monotonic() if now_mono is None else now_mono
        if _last_run["t"] is not None and t - _last_run["t"] < every:
            return []
        _last_run["t"] = t
        from execution import dhan_client
        holdings = dhan_client.get_holdings(db) or []
        positions = dhan_client.get_positions(db) or []
        return sweep(db, holdings, positions)
    except Exception as e:  # noqa: BLE001 - an unreadable broker read releases nothing
        logger.warning("lock sweep: broker read failed, nothing released (%s)", str(e)[:200] or type(e).__name__)
        return []
