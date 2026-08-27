"""execution/reconcile.py — the only place a REAL fill becomes "real" in
this service's own DB.

Placing an order (entry_engine) or sending a SELL (exit_engine) only ever
proves Dhan ACCEPTED the request — never that it filled. This module is
what actually asks Dhan "did it fill", via the read-only, non-arming-gated
dhan_client.get_order_list(), and only then calls
portfolio.record_real_fill / record_real_exit_fill. Runs on the same
manual Run Cycle trigger as everything else in this phase (see main.py's
module note on why there's no background scheduler yet) — call it once
per cycle for REAL, same as check_pending_fills is for DEMO.

Defensive key lookup below: the dhanhq SDK response mirrors Dhan's raw v2
JSON keys, but this hasn't been run against a live sandbox in this session
(see the SDK-version caveat in dhan_client.py's docstring) — prefer
several plausible key names over a hard KeyError, and treat "can't tell"
as "leave it PLACED/PENDING_EXIT for next cycle", never as a fill.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

import models
from execution import dhan_client
from notifier import notify_async
from portfolio.portfolio import record_real_fill, record_real_exit_fill

logger = logging.getLogger("real-trade-reconcile")

_FILLED_STATUSES = {"TRADED", "COMPLETE", "FILLED", "EXECUTED"}
_DEAD_STATUSES = {"REJECTED", "CANCELLED", "CANCELED"}


def _get(row: dict, *keys, default=None):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return default


async def reconcile_real_orders(db: Session) -> dict:
    """One pass over every REAL order still PLACED and every REAL position
    PENDING_EXIT. Returns a tally for the cycle summary."""
    tally = {"checked": 0, "entries_filled": 0, "exits_confirmed": 0, "dead_orders": 0, "errors": 0}

    pending_orders = db.query(models.TradeOrder).filter_by(mode="REAL", status="PLACED").all()
    if not pending_orders:
        return tally

    try:
        broker_orders = dhan_client.get_order_list(db)
    except Exception as e:  # noqa: BLE001 — a reconcile failure must never crash the cycle
        logger.warning("reconcile: could not fetch Dhan order list: %s", e)
        tally["errors"] += 1
        return tally

    by_id: dict[str, dict] = {}
    for row in broker_orders or []:
        oid = str(_get(row, "orderId", "order_id", default=""))
        if oid:
            by_id[oid] = row

    for order in pending_orders:
        tally["checked"] += 1
        if not order.dhan_order_id:
            continue
        broker_row = by_id.get(str(order.dhan_order_id))
        if broker_row is None:
            continue  # not visible yet — check again next cycle, never assume

        status = str(_get(broker_row, "orderStatus", "order_status", default="")).upper()
        if status in _DEAD_STATUSES:
            order.status = "REJECTED" if status == "REJECTED" else "CANCELLED"
            db.add(models.TradeOrderEvent(order_id=order.id, event_type=order.status,
                                           detail=f"Broker reported {status}"))
            db.commit()
            tally["dead_orders"] += 1
            continue
        if status not in _FILLED_STATUSES:
            continue  # still pending at the broker

        fill_price = _get(broker_row, "averageTradedPrice", "average_traded_price", "tradedPrice", default=None)
        fill_qty = _get(broker_row, "tradedQuantity", "traded_quantity", default=None)
        if fill_price is None or fill_qty is None:
            logger.warning("reconcile: order %s reports %s but no fill price/qty — leaving PLACED.",
                            order.dhan_order_id, status)
            continue

        decision = db.query(models.TradeDecision).filter_by(id=order.decision_id).first()
        stop_price = decision.proposed_stop if decision else float(fill_price) * 0.97
        target_price = decision.proposed_target if decision else float(fill_price) * 1.03

        if order.side == "BUY":
            record_real_fill(db, order, float(fill_price), int(fill_qty), stop_price, target_price)
            tally["entries_filled"] += 1
            await notify_async(
                f"✅ *BUY filled* — {order.symbol}\n"
                f"{int(fill_qty)} shares @ ₹{float(fill_price):.2f} "
                f"(₹{float(fill_price) * int(fill_qty):,.2f})\n"
                f"Stop ₹{stop_price:.2f} · Target ₹{target_price:.2f}"
            )
        else:
            position = db.query(models.TradePosition).filter_by(
                mode="REAL", symbol=order.symbol, status="PENDING_EXIT"
            ).first()
            if position is not None:
                reason = _get(broker_row, "remarks", default="exit")
                pnl = record_real_exit_fill(db, position, float(fill_price), int(fill_qty), str(reason) or "exit")
                tally["exits_confirmed"] += 1
                pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                await notify_async(
                    f"{pnl_emoji} *SELL filled* — {order.symbol}\n"
                    f"{int(fill_qty)} shares @ ₹{float(fill_price):.2f}\n"
                    f"P&L: ₹{pnl:+,.2f}"
                )
            order.status = "FILLED"
            db.commit()

    return tally
