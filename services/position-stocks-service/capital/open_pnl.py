"""capital/open_pnl.py - E367: the unrealized P&L of this pool's OPEN positions, from fresh ticks only. Used by ledger.reserve_capital() so the daily loss limit also counts a loss
that is still open: a day with realized -1% and open positions another -3% under water is a -4% day, and the bot must not keep buying into it. It only ever BLOCKS a new entry
(never closes anything, never trips the persistent kill switch). Fail-open: any error, a stale tick or a missing price counts as 0 for that position."""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

import config

logger = logging.getLogger("position-stocks-open-pnl")


def _tick(symbol: str):
    try:
        from orders import stop_watchdog
        return stop_watchdog._last_tick(symbol)
    except Exception:  # noqa: BLE001
        return None, None


def open_unrealized_pnl(db, max_tick_age_s: Optional[float] = None) -> float:
    """Sum of (price - entry) x qty (reversed for a short) over OPEN positions with a fresh finite tick. 0.0 on any problem. Never raises."""
    try:
        from models import ScalpPosition
        from orders import side as _side
        age_cap = float(max_tick_age_s if max_tick_age_s is not None else getattr(config, "DAILY_LOSS_UNREALIZED_MAX_TICK_AGE_S", 30.0))
        total = 0.0
        for p in db.query(ScalpPosition).filter(ScalpPosition.status == "OPEN").all():
            try:
                entry, qty = float(p.entry_price), int(p.quantity or 0)
                if not (math.isfinite(entry) and entry > 0 and qty > 0):
                    continue
                ltp, age = _tick(p.symbol)
                if ltp is None or age is None or age > age_cap:
                    continue
                v = _side.pnl(p, entry, ltp, qty)
                if math.isfinite(v):
                    total += v
            except Exception as e:  # noqa: BLE001
                logger.debug("open pnl: %s skipped (%s)", getattr(p, "symbol", "?"), e)
        return float(total)
    except Exception as e:  # noqa: BLE001
        logger.warning("open pnl: could not be computed (%s) - counted as 0", e)
        return 0.0


def day_loss_pct_with_open(db, realized_today: float, pool: float) -> float:
    """Loss % of the pool if today's realized P&L and the open P&L are added. Only a NET loss counts (0.0 otherwise). Pure apart from the position read."""
    if not pool or pool <= 0:
        return 0.0
    total = float(realized_today or 0.0) + open_unrealized_pnl(db)
    return abs(min(total, 0.0)) / float(pool) * 100.0
