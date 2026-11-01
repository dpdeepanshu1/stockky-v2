"""orders/stale_entry.py - E285 round 100: cancel a long entry that never filled.

The automatic long entry is a marketable LIMIT (dhan_client.long_entry_order). If the price runs away from it the order rests; a fill minutes later at a stale price
is not the trade the gates approved. cancel_if_stale() is called by orders/reconcile.run_exit_reconciliation for each OPEN long whose Super Order row is visible: when the
ENTRY leg is still waiting (PENDING / TRANSIT), nothing has filled, and the position is older than LONG_ENTRY_STALE_S, it cancels the ENTRY leg at Dhan. The existing
dead-entry path in reconcile (parent / ENTRY_LEG CANCELLED, with the fresh order-book check of group 276 / 291) then books the position as a never-filled entry and
gives the capital and the symbol lock back. Only LONG rows, only a wholly unfilled entry (a part fill is left to E184's quantity sync), one cancel per position per
process. LONG_ENTRY_MARKETABLE_LIMIT=false turns it off. Never raises.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import config
from execution import dhan_client

logger = logging.getLogger("position-stocks-stale-entry")

_WAITING = ("PENDING", "TRANSIT")
_QTY_KEYS = ("filledQty", "filled_qty", "tradedQuantity")
_tried: set = set()


def reset() -> None:
    _tried.clear()


def _qty(v) -> int:
    try:
        n = float(v)
        return int(n) if n == n else 0
    except (TypeError, ValueError):
        return 0


def _filled(row: dict) -> int:
    for k in _QTY_KEYS:
        q = _qty(row.get(k))
        if q:
            return q
    for leg in row.get("legDetails") or []:
        if str(leg.get("legName", "")).upper() == "ENTRY_LEG":
            for k in _QTY_KEYS:
                q = _qty(leg.get(k))
                if q:
                    return q
    return 0


def _entry_status(row: dict) -> str:
    for leg in row.get("legDetails") or []:
        if str(leg.get("legName", "")).upper() == "ENTRY_LEG" and leg.get("orderStatus"):
            return str(leg.get("orderStatus")).upper()
    return str(row.get("orderStatus", "")).upper()


def _age_s(pos, now: Optional[datetime]) -> float:
    t = pos.opened_at
    if t is None:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - t).total_seconds()


def is_stale(pos, row: dict, now: Optional[datetime] = None) -> bool:
    """True when this open long's entry is wholly unfilled, still waiting at Dhan, and older than LONG_ENTRY_STALE_S. Never raises."""
    try:
        if not getattr(config, "LONG_ENTRY_MARKETABLE_LIMIT", True):
            return False
        if str(getattr(pos, "side", "LONG") or "LONG").upper() != "LONG" or pos.status != "OPEN" or not pos.dhan_super_order_id:
            return False
        if _filled(row) > 0 or _entry_status(row) not in _WAITING:
            return False
        return _age_s(pos, now) >= float(getattr(config, "LONG_ENTRY_STALE_S", 15.0))
    except Exception:  # noqa: BLE001
        return False


def cancel_if_stale(db, pos, row: dict, now: Optional[datetime] = None) -> bool:
    """Cancel the ENTRY leg of a stale unfilled long entry (once). True when a cancel was sent. Never raises."""
    try:
        if not is_stale(pos, row, now):
            return False
        oid = str(pos.dhan_super_order_id)
        if oid in _tried:
            return False
        _tried.add(oid)
        dhan_client.cancel_super_order(db, order_id=oid, order_leg="ENTRY_LEG")
        logger.warning("stale entry: %s (id=%s) BUY not filled after %.0fs (limit entry) - ENTRY leg cancelled; reconcile books it as never filled",
                       pos.symbol, pos.id, _age_s(pos, now))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("stale entry: cancel for %s failed (%s) - will not retry this run", getattr(pos, "symbol", "?"), e)
        return False
