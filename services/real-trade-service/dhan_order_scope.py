"""Which Dhan orders did THIS service place? (2026-10-08, group 261)

position-stocks-service and real-trade-service share one Dhan account, so GET /dhan/orders (the whole day's
broker order book) lists both services' orders. The Real Auto Trade "Charges" tab built its "Today's Dhan
charges" from that whole list, so the position-stocks MIS legs (e.g. TRIVENI buy + sell, Rs 3.61) were counted
in Real's charges AND in position-stocks' own ledger, and "Net realized P&L today" subtracted them from a P&L
figure that does not contain those trades.

Fix: every order returned by /dhan/orders is tagged ``ours`` = its Dhan orderId is stored on one of OUR
trade_orders rows (dhan_order_id, written when AUTO / MANUAL / EXIT orders are sent). The dashboard sums
charges only over ``ours is not False`` orders. If the lookup fails the tag is left OFF (older behaviour:
nothing is filtered), never guessed.

Pure helpers + one DB reader; never writes.
"""
from __future__ import annotations

from typing import Iterable, Optional


def order_id_of(order: dict) -> str:
    """Dhan's order id as a string ('' when the row has none)."""
    if not isinstance(order, dict):
        return ""
    return str(order.get("orderId") or order.get("order_id") or "").strip()


def known_dhan_order_ids(db, ids: Iterable[str], mode: str = "REAL") -> set:
    """Of ``ids``, those stored as trade_orders.dhan_order_id for ``mode``. Chunked IN lookups."""
    import models  # local import: keeps the pure helpers importable without the DB stack

    wanted = sorted({str(i) for i in ids if i})
    found: set = set()
    for i in range(0, len(wanted), 500):
        chunk = wanted[i:i + 500]
        for (oid,) in (
            db.query(models.TradeOrder.dhan_order_id)
            .filter(models.TradeOrder.mode == mode, models.TradeOrder.dhan_order_id.in_(chunk))
            .all()
        ):
            if oid:
                found.add(str(oid))
    return found


def tag_orders_ours(orders: list, known_ids: Optional[set]) -> list:
    """Return a NEW list of order dicts, each with ``ours`` True/False. ``known_ids=None`` (lookup failed)
    -> the orders are returned untouched, with no ``ours`` key. Non-dict rows pass through unchanged."""
    if known_ids is None:
        return list(orders or [])
    out = []
    for o in orders or []:
        if isinstance(o, dict):
            t = dict(o)
            t["ours"] = order_id_of(o) in known_ids
            out.append(t)
        else:
            out.append(o)
    return out


def tag_dhan_orders(db, orders: list, mode: str = "REAL") -> list:
    """DB-backed wrapper used by GET /dhan/orders. Never raises."""
    try:
        ids = [order_id_of(o) for o in orders or []]
        return tag_orders_ours(orders, known_dhan_order_ids(db, ids, mode))
    except Exception:
        return list(orders or [])
