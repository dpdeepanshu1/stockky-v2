"""portfolio/open_pnl.py - E368: the unrealized P&L of the open positions of one mode, from fresh quotes only, for the daily loss limit in risk_engine/engine.py.
Only ever used to BLOCK a new BUY: nothing is sold, the kill switch is not touched. Fail-open: a stale or missing quote, a bad row, a slow or failing quote call all count as 0."""
from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import datetime, timezone

logger = logging.getLogger("real-trade-open-pnl")


def _max_age_s() -> float:
    try:
        return float((os.getenv("DAILY_LOSS_UNREALIZED_MAX_TICK_AGE_S") or "").strip() or 30.0)
    except ValueError:
        return 30.0


def _fresh_price(tick, now: datetime, max_age: float):
    """The tick's price when it is finite, positive, not a served last-good fallback and not older than max_age seconds; else None."""
    try:
        if str(getattr(tick, "source", "") or "").startswith(("stale_last_good", "stale_cooldown")):
            return None
        px = float(getattr(tick, "price", None))
        if not (math.isfinite(px) and px > 0):
            return None
        as_of = getattr(tick, "as_of", None)
        if as_of is None or not getattr(tick, "age_known", True):
            return None
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        return px if (now - as_of).total_seconds() <= max_age else None
    except Exception:  # noqa: BLE001
        return None


def unrealized_from(positions, ticks, now: datetime, max_age: float) -> float:
    """Sum of (price - avg entry) x open qty over positions that have a fresh tick. Pure."""
    total = 0.0
    for p in positions or []:
        try:
            entry, qty = float(p.avg_entry_price), float(p.qty_open or 0)
            if not (math.isfinite(entry) and entry > 0 and qty > 0):
                continue
            px = _fresh_price(ticks.get(p.symbol), now, max_age)
            if px is not None:
                total += (px - entry) * qty
        except Exception:  # noqa: BLE001
            continue
    return float(total) if math.isfinite(total) else 0.0


async def open_unrealized_pnl(db, mode: str, quotes_fn=None, timeout_s: float = 4.0) -> float:
    """Unrealized P&L of the open positions of `mode` (0.0 on any problem). Never raises."""
    try:
        if quotes_fn is None:
            from market_feed.feed import get_quotes as quotes_fn
        from portfolio.portfolio import held_exposure_positions
        positions = list(held_exposure_positions(db, mode))
        if not positions:
            return 0.0
        ticks = await asyncio.wait_for(quotes_fn(sorted({p.symbol for p in positions}), priority=True), timeout=timeout_s)
        return unrealized_from(positions, ticks or {}, datetime.now(timezone.utc), _max_age_s())
    except Exception as e:  # noqa: BLE001
        logger.warning("open pnl: could not be computed for %s (%s) - counted as 0", mode, e)
        return 0.0
