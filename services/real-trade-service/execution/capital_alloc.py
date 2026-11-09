"""execution/capital_alloc.py - Real Trade's side of the three-way capital split (round 242; the rule and the files are in capital_board.py, the same file in all three services).

Real Trade is the HUB: it owns the Dhan balance, so once per equity sync it (1) publishes its own figures (net P&L after charges over the window, closed trades, capital, open exposure),
(2) lets capital_board.refresh() read all three services' figures and write capital_alloc.json (percentages decided once per IST day or when the active set changes; rupee amounts every time),
(3) gives the rest of this service the percentage it may use: effective_share_pct(). In shadow mode (default) nothing about sizing changes. Every function here never raises."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

import capital_board
import config

logger = logging.getLogger("real-trade-capital-alloc")
SERVICE = "real_trade"


def _mode() -> str:
    return capital_board.norm_mode(getattr(config, "CAPITAL_ALLOC_MODE", "off"))


def effective_share_pct(fixed: Optional[float] = None) -> float:
    """The percentage of the shared account this service may use now: the board's figure in live mode, otherwise `fixed` (default config.CAPITAL_SHARE_PCT)."""
    fixed = float(config.CAPITAL_SHARE_PCT if fixed is None else fixed)
    try:
        pct, _why = capital_board.effective_share(config.CAPITAL_ALLOC_DIR, SERVICE, _mode(), fixed, config.CAPITAL_ALLOC_MAX_AGE_S)
        return float(pct)
    except Exception:  # noqa: BLE001
        return fixed


def extra_peer_exposure() -> float:
    """F&O's open premium value (the third service). Added to the peer exposure ONLY in live mode, so shadow changes nothing."""
    try:
        if _mode() != "live":
            return 0.0
        return capital_board.exposure_of(config.CAPITAL_ALLOC_DIR, "fno", config.CAPITAL_LEND_PEER_MAX_AGE_S)
    except Exception:  # noqa: BLE001
        return 0.0


def own_stats(db, today: Optional[str] = None, window_days: Optional[int] = None) -> Tuple[float, int]:
    """(net P&L after charges, closed trades) of REAL over the last window_days IST days. (0.0, 0) when unreadable."""
    try:
        from sqlalchemy import func
        from models import TradePnlDaily, TradePosition
        from tz_utils import ist_today_str
        today = today or ist_today_str()
        days = int(window_days or config.CAPITAL_ALLOC_WINDOW_DAYS)
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
        net = db.query(func.coalesce(func.sum(TradePnlDaily.net_realized), 0.0)).filter(TradePnlDaily.mode == "REAL", TradePnlDaily.day >= cutoff).scalar()
        n = db.query(func.count(TradePosition.id)).filter(TradePosition.mode == "REAL", TradePosition.status == "CLOSED",
                                                          TradePosition.closed_at >= datetime.strptime(cutoff, "%Y-%m-%d")).scalar()
        return float(net or 0.0), int(n or 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("capital_alloc: own stats unavailable: %s", e)
        return 0.0, 0


def sync(db, free_cash: float, own_exposure: float, capital_base: float):
    """Publish this service's figures, then (re)write the board. Returns the board document or None. Call once per REAL equity sync."""
    try:
        mode = _mode()
        if mode == "off":
            return None
        from tz_utils import ist_today_str
        today = ist_today_str()
        net, trades = own_stats(db, today)
        capital_board.write_stats(config.CAPITAL_ALLOC_DIR, SERVICE, net_pnl=net, trades=trades, capital_base=capital_base, exposure=own_exposure, active=True, mode=mode,
                                  window_days=config.CAPITAL_ALLOC_WINDOW_DAYS)
        doc = capital_board.refresh(config.CAPITAL_ALLOC_DIR, mode=mode, day=today, free_cash=free_cash, base_pct=config.CAPITAL_ALLOC_BASE_PCT, min_trades=config.CAPITAL_ALLOC_MIN_TRADES,
                                    bonus_mode=config.CAPITAL_ALLOC_BONUS_MODE, max_pct=config.CAPITAL_ALLOC_MAX_PCT, max_step_pct=config.CAPITAL_ALLOC_MAX_STEP_PCT)
        if doc:
            _log_once(doc)
        return doc
    except Exception as e:  # noqa: BLE001
        logger.debug("capital_alloc: sync skipped: %s", e)
        return None


_LAST: dict = {"key": None}


def _log_once(doc: dict) -> None:
    key = (doc.get("day"), tuple(sorted((doc.get("pcts") or {}).items())), tuple(doc.get("active") or ()), doc.get("mode"))
    if key != _LAST["key"]:
        _LAST["key"] = key
        logger.info("capital board (%s): %s active=%s total=Rs%.2f scores=%s trades=%s", doc.get("mode"), doc.get("pcts"), doc.get("active"), float(doc.get("total_equity") or 0.0),
                    doc.get("scores"), doc.get("trades"))


def status(db=None) -> dict:
    """What GET /capital/board shows: the live board plus what this service would use right now."""
    mode = _mode()
    doc = capital_board.read_alloc(config.CAPITAL_ALLOC_DIR, config.CAPITAL_ALLOC_MAX_AGE_S) if mode != "off" else None
    pct, why = capital_board.effective_share(config.CAPITAL_ALLOC_DIR, SERVICE, mode, config.CAPITAL_SHARE_PCT, config.CAPITAL_ALLOC_MAX_AGE_S)
    return {"mode": mode, "this_service": SERVICE, "effective_share_pct": pct, "why": why, "fixed_share_pct": config.CAPITAL_SHARE_PCT, "board": doc}
