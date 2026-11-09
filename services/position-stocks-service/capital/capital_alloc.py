"""capital/capital_alloc.py - Position Stocks' side of the three-way capital split (round 242; the rule and the files are in capital_board.py, the same file in all three services).

Position Stocks does NOT decide the split (Real Trade does, it owns the Dhan balance). It (1) publishes its own figures to the board folder (net P&L after charges over the window, closed
trades, capital, open exposure) at the same heartbeat as its exposure row, (2) asks effective_share_pct() which percentage of the account this pool may use. Shadow mode (default) changes no
sizing. Every function here never raises."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Tuple

import capital_board
import config

logger = logging.getLogger("position-stocks-capital-alloc")
SERVICE = "position_stocks"


def _mode() -> str:
    return capital_board.norm_mode(getattr(config, "CAPITAL_ALLOC_MODE", "off"))


def effective_share_pct() -> float:
    """% of the shared account this pool may use now: the board's figure in live mode, otherwise the fixed SCALP_POOL_CAPITAL_SHARE_PCT (read at call time)."""
    fixed = float(config.SCALP_POOL_CAPITAL_SHARE_PCT)
    try:
        pct, _why = capital_board.effective_share(config.CAPITAL_ALLOC_DIR, SERVICE, _mode(), fixed, config.CAPITAL_ALLOC_MAX_AGE_S)
        return float(pct)
    except Exception:  # noqa: BLE001
        return fixed


def extra_peer_exposure() -> float:
    """F&O's open premium value (the third service). Counted as peer exposure ONLY in live mode, so shadow changes nothing."""
    try:
        if _mode() != "live":
            return 0.0
        return capital_board.exposure_of(config.CAPITAL_ALLOC_DIR, "fno", config.CAPITAL_ALLOC_PEER_MAX_AGE_S)
    except Exception:  # noqa: BLE001
        return 0.0


def own_stats(db, today: str = "", window_days: int = 0) -> Tuple[float, int]:
    """(net P&L after charges, closed trades) over the last window_days IST days, from the never-purged charges ledger. (0.0, 0) when unreadable."""
    try:
        from sqlalchemy import func
        from models import ScalpChargesLedger
        from tz_utils import ist_today_str
        today = today or ist_today_str()
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=int(window_days or config.CAPITAL_ALLOC_WINDOW_DAYS))).strftime("%Y-%m-%d")
        q = db.query(func.coalesce(func.sum(ScalpChargesLedger.net_pnl), 0.0), func.count(ScalpChargesLedger.position_id)).filter(ScalpChargesLedger.day >= cutoff)
        net, n = q.one()
        return float(net or 0.0), int(n or 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("capital_alloc: own stats unavailable: %s", e)
        return 0.0, 0


def publish(db, own_exposure: float) -> None:
    """Write this pool's figures to the board folder. Call at the exposure heartbeat."""
    try:
        if _mode() == "off":
            return
        from models import ScalpCapitalLedger
        row = db.query(ScalpCapitalLedger).filter_by(mode="REAL").first()
        base = float(row.total_allocated_capital or 0.0) if row is not None else 0.0
        net, trades = own_stats(db)
        capital_board.write_stats(config.CAPITAL_ALLOC_DIR, SERVICE, net_pnl=net, trades=trades, capital_base=base, exposure=own_exposure, active=True, mode=_mode(),
                                  window_days=config.CAPITAL_ALLOC_WINDOW_DAYS)
    except Exception as e:  # noqa: BLE001
        logger.debug("capital_alloc: publish skipped: %s", e)


def status() -> dict:
    mode = _mode()
    doc = capital_board.read_alloc(config.CAPITAL_ALLOC_DIR, config.CAPITAL_ALLOC_MAX_AGE_S) if mode != "off" else None
    pct, why = capital_board.effective_share(config.CAPITAL_ALLOC_DIR, SERVICE, mode, config.SCALP_POOL_CAPITAL_SHARE_PCT, config.CAPITAL_ALLOC_MAX_AGE_S)
    return {"mode": mode, "this_service": SERVICE, "effective_share_pct": pct, "why": why, "fixed_share_pct": config.SCALP_POOL_CAPITAL_SHARE_PCT, "board": doc}
