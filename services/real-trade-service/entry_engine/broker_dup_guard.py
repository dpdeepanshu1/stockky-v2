"""E285 round 165: ask Dhan itself whether a symbol is already held, or has a BUY still working, right before a REAL BUY is sent.

Why: JSW Energy ended the day with 4 shares (Dhan Open P&L) after two BUY 2 orders about 23 minutes apart. The existing no-pyramiding guard
(risk_engine, group297) only knows this service's own database (held positions + PLACED / PARTIAL orders). If the database label is wrong or late
(an order marked EXPIRED that still filled, a fill not booked yet), the guard sees nothing and a second BUY goes out. This check reads Dhan's own
positions and order book, so it does not depend on our labels.

Rules (REAL only, a BUY only):
  * blocked when Dhan shows a net long position in the symbol (net quantity above 0), or
  * blocked when Dhan shows a BUY order for the symbol that is still live (not rejected / cancelled / expired and not fully traded).
  * a symbol that was bought and fully sold today has net quantity 0 and is NOT blocked.
  * Dhan unreadable: allowed with a WARNING by default (same fail-open shape as the shared order budget); ENTRY_BROKER_DUP_CHECK_FAIL_CLOSED=true
    blocks instead.
Switches: ENTRY_BROKER_DUP_CHECK (default true), ENTRY_BROKER_DUP_CHECK_FAIL_CLOSED (default false). Never raises.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger("real-trade-dup-guard")

_LIVE_STATUSES = {"TRANSIT", "PENDING", "OPEN", "PART_TRADED", "PARTIALLY_TRADED", "TRIGGER_PENDING", "AMO_SUBMITTED"}


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def enabled() -> bool:
    return _flag("ENTRY_BROKER_DUP_CHECK", True)


def fail_closed() -> bool:
    return _flag("ENTRY_BROKER_DUP_CHECK_FAIL_CLOSED", False)


def _norm(sym: Any) -> str:
    s = str(sym or "").strip().upper()
    for suf in ("-EQ", "-BE"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _same(row: dict, symbol: str, security_id: Optional[str]) -> bool:
    sid = str(row.get("securityId") or row.get("security_id") or "").strip()
    if security_id and sid and sid == str(security_id).strip():
        return True
    return _norm(row.get("tradingSymbol") or row.get("trading_symbol") or row.get("symbol")) == _norm(symbol)


def find_conflict(positions: list, orders: list, symbol: str, security_id: Optional[str] = None) -> Optional[str]:
    """Pure function. Returns a short reason when the symbol is already held / has a live BUY, else None."""
    for p in positions or []:
        if isinstance(p, dict) and _same(p, symbol, security_id) and _num(p.get("netQty", p.get("net_qty"))) > 0:
            return f"Dhan already shows a net long position of {_num(p.get('netQty', p.get('net_qty'))):g} in {symbol}"
    for o in orders or []:
        if not isinstance(o, dict) or not _same(o, symbol, security_id):
            continue
        if str(o.get("transactionType") or o.get("transaction_type") or "").upper() != "BUY":
            continue
        status = str(o.get("orderStatus") or o.get("order_status") or "").upper()
        if status in _LIVE_STATUSES:
            return f"Dhan has a BUY order for {symbol} still live (status {status}, id {o.get('orderId') or o.get('order_id') or '?'})"
    return None


def _dhan() -> Any:
    from execution import dhan_client
    return dhan_client


def check(db: Any, symbol: str, security_id: Optional[str] = None) -> Tuple[bool, str]:
    """(blocked, reason). Reads Dhan; never raises."""
    if not enabled():
        return False, ""
    try:
        dhan = _dhan()
        positions = dhan.get_positions(db)
        orders = dhan.get_order_list(db)
    except Exception as ex:  # noqa: BLE001
        if fail_closed():
            return True, f"Dhan positions / orders could not be read ({str(ex)[:120]}); fail-closed is on"
        logger.warning("broker dup check for %s skipped: Dhan unreadable (%s); BUY allowed", symbol, str(ex)[:160])
        return False, ""
    try:
        reason = find_conflict(positions, orders, symbol, security_id)
    except Exception:  # noqa: BLE001
        logger.warning("broker dup check for %s failed", symbol, exc_info=True)
        return False, ""
    return (True, reason) if reason else (False, "")
