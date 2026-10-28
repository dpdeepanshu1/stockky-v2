"""orders/sell_orders.py - E243: one read-only view of every SELL order Stockky has sent to Dhan today (Position Stocks only).

WHY: a SELL Super Order that never fills (the first real SELL probe, NTPC, 28 Oct 2026) never becomes a position, so the Positions tab showed nothing and the
operator could not see that an order had been sent at all. This view lists, in one place and with a SELL tag:
  * the SELL probe runs (persisted by orders/sell_probe.py), newest first, with the entry type / price sent, the fill and the verdict;
  * the live shorts (position rows with side SHORT), open and recent;
  * the matching rows of Dhan's own Super Order book (order status, filled quantity, entry order type and price, leg statuses), for the order ids Stockky placed.
Read-only: it places, cancels and changes nothing. Never raises into the route (each source fails on its own and says so under `errors`).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("position-stocks-sell-orders")

_FILLED = {"TRADED", "FILLED", "EXECUTED", "COMPLETE"}
_DEAD = {"REJECTED", "CANCELLED", "CANCELED", "EXPIRED"}


def _s(v: Any) -> str:
    return str(v if v is not None else "").strip()


def _status(d: Optional[dict]) -> str:
    return _s((d or {}).get("orderStatus") or (d or {}).get("order_status")).upper()


def _iso(dt: Any) -> Optional[str]:
    try:
        from tz_utils import iso_utc
        return iso_utc(dt)
    except Exception:  # noqa: BLE001
        return dt.isoformat() if dt is not None else None


def state_of(row: Optional[dict]) -> str:
    """One word for a Dhan SELL Super Order row: FILLED (entry done), PARTIAL, WAITING (resting, nothing filled), CANCELLED, REJECTED or UNKNOWN."""
    if not isinstance(row, dict):
        return "UNKNOWN"
    st = _status(row)
    try:
        filled = float(row.get("filledQty") or row.get("filled_qty") or 0)
        qty = float(row.get("quantity") or 0)
    except (TypeError, ValueError):
        filled, qty = 0.0, 0.0
    if st == "REJECTED":
        return "REJECTED"
    if filled > 0 and qty > 0:
        return "FILLED" if filled >= qty else "PARTIAL"
    if st in _FILLED:
        return "FILLED"
    if st in _DEAD:
        return "CANCELLED"
    if st in {"PENDING", "TRANSIT", "OPEN", "TRIGGERED", "PART_TRADED"}:
        return "WAITING"
    return "UNKNOWN"


def dhan_row(row: dict) -> Dict[str, Any]:
    """The few fields of a Dhan Super Order row the panel shows."""
    legs = {}
    for lg in row.get("legDetails") or row.get("legs") or []:
        if isinstance(lg, dict):
            legs[_s(lg.get("legName")).upper()] = {"status": _status(lg), "price": lg.get("price") or lg.get("triggerPrice")}
    return {"order_id": _s(row.get("orderId") or row.get("order_id")), "symbol": _s(row.get("tradingSymbol") or row.get("trading_symbol")) or None,
            "security_id": _s(row.get("securityId") or row.get("security_id")) or None, "transaction_type": _s(row.get("transactionType")).upper(),
            "order_type": _s(row.get("orderType")).upper() or None, "status": _status(row), "state": state_of(row), "quantity": row.get("quantity"),
            "filled_qty": row.get("filledQty"), "price": row.get("price"), "average_price": row.get("averageTradedPrice"), "legs": legs,
            "created_at": row.get("createTime") or row.get("createdAt") or None}


def build(db, *, super_orders: Optional[List[dict]], super_error: Optional[str], probe_history: List[dict], short_rows: List[Any]) -> Dict[str, Any]:
    """Assemble the view. `short_rows` are ScalpPosition rows with side SHORT (any status); `super_orders` is Dhan's live Super Order list (None = unreadable)."""
    errors: List[str] = []
    if super_error:
        errors.append(f"Dhan's order book could not be read: {super_error[:160]}")

    probe_ids = {_s(r.get("order_id")) for r in probe_history if _s(r.get("order_id"))}
    short_ids = {_s(getattr(r, "dhan_super_order_id", None)) for r in short_rows if _s(getattr(r, "dhan_super_order_id", None))}
    ours = probe_ids | short_ids
    broker_rows = {}
    for o in super_orders or []:
        if isinstance(o, dict):
            oid = _s(o.get("orderId") or o.get("order_id"))
            if oid in ours and _s(o.get("transactionType")).upper() == "SELL":
                broker_rows[oid] = dhan_row(o)

    probes = []
    for r in reversed(probe_history[-20:]):
        oid = _s(r.get("order_id"))
        probes.append({"kind": "PROBE", "symbol": r.get("symbol"), "at": r.get("at"), "order_id": oid or None, "verdict": r.get("verdict"), "reason": r.get("reason"),
                       "entry_order_type": r.get("entry_order_type"), "entry_price_sent": r.get("entry_price_sent"), "ref_price": r.get("ref_price"),
                       "entry_fill": r.get("entry_fill"), "cover_fill": r.get("cover_fill"), "qty": r.get("qty"), "broker": broker_rows.get(oid)})

    shorts = []
    for r in short_rows:
        oid = _s(getattr(r, "dhan_super_order_id", None))
        shorts.append({"kind": "SHORT", "id": getattr(r, "id", None), "symbol": getattr(r, "symbol", None), "status": getattr(r, "status", None),
                       "quantity": getattr(r, "quantity", None), "entry_price": getattr(r, "entry_price", None), "stop_price": getattr(r, "stop_price", None),
                       "target_price": getattr(r, "target_price", None), "exit_price": getattr(r, "exit_price", None), "order_id": oid or None,
                       "plain": bool(getattr(r, "plain_stop_order_id", None)),
                       "opened_at": _iso(getattr(r, "opened_at", None)), "broker": broker_rows.get(oid)})

    waiting = [b for b in broker_rows.values() if b["state"] == "WAITING"]
    return {"probes": probes, "shorts": shorts, "broker_rows": list(broker_rows.values()), "errors": errors,
            "summary": {"probe_runs": len(probe_history), "shorts_open": sum(1 for s in shorts if s["status"] in ("OPEN", "EXIT_LEGS_REJECTED")),
                        "sell_orders_at_dhan": len(broker_rows), "sell_orders_waiting": len(waiting)}}
