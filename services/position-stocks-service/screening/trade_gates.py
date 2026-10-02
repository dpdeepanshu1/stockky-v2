"""screening/trade_gates.py — market filter and loss brake (2026-10-02).

Two pre-entry gates for AUTO scalp entries, both fail-open:

  market_gate_reject()  async  — blocks entries while Nifty is below its day
                                 open by more than the configured threshold.
  loss_brake_reject()   sync   — blocks entries after N consecutive losing
                                 closes today (timed pause) or once today's
                                 realized loss reaches a % of the pool.

Each returns None (allowed) or a short reason string that begins with a stable
prefix (MARKET_WEAK / LOSS_BRAKE_*) so it can be logged and counted.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

import config
from models import ScalpCapitalLedger, ScalpPosition
from tz_utils import as_aware, ist_now

logger = logging.getLogger(__name__)

_NOT_CLOSED = ("OPEN", "EXIT_LEGS_REJECTED", "ERROR")

# {"pct": float|None, "ts": float}
_market_cache: dict = {"pct": None, "ts": 0.0}


def last_nifty_change_pct() -> Optional[float]:
    """Most recently fetched Nifty % change vs day open (None if unknown) —
    exposed so entry logging can record market context for later analysis."""
    return _market_cache["pct"]


async def _fetch_nifty_change_pct() -> Optional[float]:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                config.MARKET_INDICES_URL,
                params={"force_refresh": "false"},
                timeout=config.MARKET_GATE_TIMEOUT_S,
            )
        if r.status_code != 200:
            return None
        pct = (r.json().get("nifty") or {}).get("change_pct")
        return float(pct) if pct is not None else None
    except Exception as e:
        logger.debug("market gate: indices fetch failed (fail-open): %s", e)
        return None


async def market_gate_reject() -> Optional[str]:
    if not config.MARKET_GATE_ENABLED:
        return None
    now = time.time()
    if now - _market_cache["ts"] >= config.MARKET_GATE_CACHE_TTL_S:
        pct = await _fetch_nifty_change_pct()
        # Cache failures too (as None) so a dead gateway is retried every TTL,
        # not on every 10s cycle.
        _market_cache.update({"pct": pct, "ts": now})
    pct = _market_cache["pct"]
    if pct is None:
        return None
    if pct <= config.MARKET_GATE_MIN_NIFTY_CHANGE_PCT:
        return (f"MARKET_WEAK:nifty {pct:+.2f}% vs day open "
                f"<= {config.MARKET_GATE_MIN_NIFTY_CHANGE_PCT:+.2f}%")
    return None


def _ist_day_start_utc(now: Optional[datetime] = None) -> datetime:
    n = ist_now(now)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def closed_today(db: Session, now: Optional[datetime] = None) -> list:
    """Today's (IST) closed positions, newest close first."""
    return (
        db.query(ScalpPosition)
        .filter(ScalpPosition.status.notin_(_NOT_CLOSED))
        .filter(ScalpPosition.closed_at.isnot(None))
        .filter(ScalpPosition.closed_at >= _ist_day_start_utc(now).replace(tzinfo=None))
        .order_by(ScalpPosition.closed_at.desc())
        .all()
    )


def _is_pending_reconcile(r: ScalpPosition) -> bool:
    """True for a flat-SELL exit (EOD sweep / stagnation / manual close) whose
    P&L is still the 0.0 placeholder written before reconcile() fills in the
    real fill price. Same sentinel orders/reconcile.py keys on."""
    return (r.error_message or "").startswith(f"{r.status}_PENDING_RECONCILE")


def loss_brake_reject(db: Session, now: Optional[datetime] = None) -> Optional[str]:
    if not config.LOSS_BRAKE_ENABLED:
        return None
    try:
        rows = closed_today(db, now)
        if not rows:
            return None

        # (a) daily realized-loss cap, % of pool
        day_pnl = sum((r.realized_pnl or 0.0) for r in rows)
        ledger = db.query(ScalpCapitalLedger).first()
        pool = (ledger.total_allocated_capital if ledger else 0.0) or 0.0
        if pool > 0 and day_pnl < 0:
            loss_pct = -day_pnl / pool * 100.0
            if loss_pct >= config.LOSS_BRAKE_DAILY_PCT_OF_POOL:
                return (f"LOSS_BRAKE_DAILY:today's realized loss ₹{-day_pnl:.2f} "
                        f"= {loss_pct:.2f}% of pool >= {config.LOSS_BRAKE_DAILY_PCT_OF_POOL:.2f}%")

        # (b) consecutive losses, timed pause
        # Exits still awaiting their real fill price carry a 0.0 placeholder
        # P&L: they are unknown, not break-even, so they neither extend nor
        # reset the streak (they would otherwise hide 3 real losses behind them).
        resolved = [r for r in rows if not _is_pending_reconcile(r)]
        streak = 0
        for r in resolved:  # newest first
            if (r.realized_pnl or 0.0) < 0:
                streak += 1
            else:
                break
        if streak >= config.LOSS_BRAKE_MAX_CONSECUTIVE_LOSSES:
            last_close = as_aware(resolved[0].closed_at)
            resume = last_close + timedelta(minutes=config.LOSS_BRAKE_COOLDOWN_MINUTES)
            current = now or datetime.now(timezone.utc)
            if current < resume:
                mins = (resume - current).total_seconds() / 60.0
                return (f"LOSS_BRAKE_STREAK:{streak} consecutive losses, "
                        f"paused {mins:.0f}m more")
        return None
    except Exception as e:
        logger.error("loss brake check failed (fail-open): %s", e, exc_info=True)
        return None


def symbol_lost_today(db: Session, symbol: str, now: Optional[datetime] = None) -> Optional[ScalpPosition]:
    """Newest position in `symbol` that closed today at a realized loss, else None."""
    for r in closed_today(db, now):
        if r.symbol == symbol and (r.realized_pnl or 0.0) < 0:
            return r
    return None
