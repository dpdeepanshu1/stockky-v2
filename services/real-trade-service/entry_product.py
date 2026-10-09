"""
entry_product.py — group 264 (2026-10-09): pick CNC vs INTRADAY (MIS) for an automated BUY, and the same-day re-entry guard.

Why: the charges dashboard showed Rs 1,987 of charges against a Rs 1,157 price loss, 68 % of it flat DP fees (Rs 14.75 per scrip
sold from demat per day) and 29 % STT (CNC pays 0.1 % on BOTH legs, MIS 0.025 % on the sell only). Every automated entry was
bought CNC even when the position would be squared off the same day (EOD square-off flattens everything that is not a
selected overnight hold), so each same-day round trip paid double STT + stamp at the delivery rate - and every exit of a
carried CNC holding paid DP.

group 266: the shipped default is now "cnc" - Dhan prices a CNC buy sold the same day at intraday rates anyway (contract note
07-Oct-2026), so MIS routing saves nothing on STT / brokerage / stamp. Set ENTRY_PRODUCT_MODE=auto to route.

Rule (config.ENTRY_PRODUCT_MODE = "auto"; "cnc", the default since group 266, keeps the always-CNC behaviour):
  * label may be held overnight (config.OVERNIGHT_HOLD_ELIGIBLE_LABELS and the overnight-hold switch is on) -> CNC
  * at/after config.ENTRY_MIS_LAST_TIME_IST (too close to Dhan's intraday cut-off / our EOD square-off)     -> CNC
  * symbol is on the learned intraday-restricted list (T2T / ASM / GSM)                                       -> CNC
  * otherwise                                                                                                 -> INTRADAY
Exits already follow TradePosition.entry_product_type (exit_engine/exit.py), so an INTRADAY-bought position is sold INTRADAY.
Pure helpers; they never raise - any failure falls back to the previous behaviour (CNC / no block).
"""
from __future__ import annotations

import logging
from datetime import datetime, time as _time
from typing import Optional

import config
import models
from tz_utils import ist_now, ist_today_str, as_aware

logger = logging.getLogger("real-trade-entry-product")


def _parse_hhmm(raw: str, default: _time) -> _time:
    try:
        hh, mm = str(raw).strip().split(":")[:2]
        return _time(int(hh), int(mm))
    except Exception:
        return default


def may_hold_overnight(db, mode: str, decision_label: Optional[str]) -> bool:
    """True when this entry's label is eligible for the selective overnight hold AND that feature is switched on."""
    label = (decision_label or "").upper()
    if label not in config.OVERNIGHT_HOLD_ELIGIBLE_LABELS:
        return False
    try:
        from execution.auto_pilot import _overnight_hold_enabled
        return bool(_overnight_hold_enabled(db, mode))
    except Exception:
        return bool(config.OVERNIGHT_HOLD_ENABLED)


def choose_entry_product(db, mode: str, symbol: str, decision_label: Optional[str],
                         now: Optional[datetime] = None) -> tuple[str, str]:
    """Returns (product_type, reason). product_type is "CNC" or "INTRADAY". Never raises."""
    try:
        if (config.ENTRY_PRODUCT_MODE or "auto").lower() != "auto":
            return "CNC", "ENTRY_PRODUCT_MODE=cnc"
        if may_hold_overnight(db, mode, decision_label):
            return "CNC", "overnight-hold eligible"
        cutoff = _parse_hhmm(config.ENTRY_MIS_LAST_TIME_IST, _time(14, 45))
        if ist_now(now).time() >= cutoff:
            return "CNC", f"after {cutoff.strftime('%H:%M')} IST"
        from intraday_eligibility import is_restricted
        if is_restricted(db, symbol):
            return "CNC", "intraday-restricted symbol"
        return "INTRADAY", "same-day exit -> MIS (no DP, single-leg STT)"
    except Exception:
        logger.warning("choose_entry_product failed for %s - falling back to CNC", symbol, exc_info=True)
        return "CNC", "fallback"


def closed_today_symbols(db, mode: str, now: Optional[datetime] = None) -> set:
    """Symbols whose position was fully CLOSED today (IST) - used by the same-day re-entry guard. Never raises."""
    try:
        today = ist_today_str(now)
        rows = (db.query(models.TradePosition.symbol, models.TradePosition.closed_at)
                .filter(models.TradePosition.mode == mode, models.TradePosition.status == "CLOSED",
                        models.TradePosition.closed_at.isnot(None)).order_by(models.TradePosition.closed_at.desc())
                .limit(300).all())
        return {sym for sym, closed_at in rows if closed_at is not None and ist_today_str(as_aware(closed_at)) == today}
    except Exception:
        logger.warning("closed_today_symbols failed - re-entry guard skipped", exc_info=True)
        return set()
