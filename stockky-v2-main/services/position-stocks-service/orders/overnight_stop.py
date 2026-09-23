"""
orders/overnight_stop.py — single home for overnight protective-stop accounting.

WHY THIS MODULE EXISTS (session 72, open-issue #1 — overnight-stop partial fills)
A carried-overnight CNC position is protected by a plain STOP_LOSS_MARKET SELL
(ScalpPosition.overnight_stop_order_id). Unlike a Super Order bracket, a plain
order can PART_TRADE, expire (DAY validity) part-way, be cancelled by us before
a flat SELL, or be re-armed the next morning for the remaining quantity. The
first partial-fill version of this logic lived inside reconcile.py and had five
gaps this module closes:

  1. Re-arming a stop assigned a NEW order id but never reset the "already
     booked" counter, so the new order's cumulative filled qty was compared
     against the OLD order's booked qty (negative/short deltas, position never
     closed correctly). -> assign_stop_order() resets the per-order counters and
     rolls them into overnight_stop_prior_qty.
  2. CANCELLED/EXPIRED orders that had already partially filled were skipped
     entirely, and Dhan's order book only holds the current day, so those fills
     were never booked. -> settle_stop_row() books fills on dead orders too, and
     settle_from_trades() recovers them from Dhan's trade history.
  3. Each delta was priced at the order's CUMULATIVE average fill price, not the
     average of the newly-filled chunk. -> delta_fill_price() uses cumulative
     notional (overnight_stop_filled_notional_so_far).
  4. _fire_flat_sell() cancelled the stop and sold the FULL pos.quantity without
     first booking a partial that had already sold shares (oversell). ->
     settle_before_flat_sell() runs before AND after the cancel.
  5. main.py's morning recheck looked for "PARTIALLY_TRADED" but Dhan's real
     status is "PART_TRADED". -> one shared status vocabulary here.

Everything here is synchronous (callers run it via asyncio.to_thread).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

import notifier
from capital import ledger, shared_symbol_lock
from execution import dhan_client
from models import ScalpPosition
from tz_utils import ist_now

logger = logging.getLogger("position-stocks-overnight-stop")

# Dhan v2 orderStatus vocabulary (plain orders). "PART_TRADED" is Dhan's real
# partial status; the other two are defensive alternate spellings.
PARTIAL_STATUSES = frozenset({"PART_TRADED", "PARTIALLY_TRADED", "PARTIALLY_FILLED"})
FILLED_STATUSES = frozenset({"TRADED", "FILLED", "EXECUTED", "COMPLETE"})
DEAD_STATUSES = frozenset({"CANCELLED", "EXPIRED", "REJECTED"})
LIVE_STATUSES = frozenset({"PENDING", "TRANSIT", "OPEN"})
# Trigger hit / modification acknowledged but not yet terminal — transient.
IN_FLIGHT_STATUSES = frozenset({"TRIGGERED", "CONFIRM"})


# ── Row parsing ──────────────────────────────────────────────────────────────
def row_status(row: dict) -> str:
    return str(row.get("orderStatus") or row.get("order_status") or row.get("status") or "").upper()


def row_avg_price(row: dict) -> Optional[float]:
    raw = row.get("averageTradedPrice") or row.get("average_traded_price")
    try:
        v = float(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return v if v and v > 0 else None


def row_cum_qty(row: dict) -> Optional[int]:
    """Cumulative filled quantity Dhan reports for the order to date (NOT a
    per-poll increment)."""
    raw = (row.get("filledQty") if row.get("filledQty") is not None else
           row.get("filled_qty") if row.get("filled_qty") is not None else
           row.get("tradedQuantity") if row.get("tradedQuantity") is not None else
           row.get("traded_quantity"))
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    total = row.get("quantity") if row.get("quantity") is not None else row.get("qty")
    remaining = row.get("remainingQuantity") if row.get("remainingQuantity") is not None else row.get("remaining_quantity")
    if total is not None and remaining is not None:
        try:
            return int(total) - int(remaining)
        except (TypeError, ValueError):
            return None
    return None


# ── Accounting primitives ────────────────────────────────────────────────────
def delta_fill_price(pos: ScalpPosition, cum_avg_price: float, cum_qty: int) -> float:
    """Average price of ONLY the newly-filled chunk, from cumulative notional:
    (cum_avg*cum_qty - notional_already_booked) / delta_qty. Falls back to the
    cumulative average when history is unavailable/implausible (legacy rows
    booked before the notional column existed)."""
    booked_qty = int(pos.overnight_stop_filled_qty_so_far or 0)
    delta_qty = cum_qty - booked_qty
    if delta_qty <= 0:
        return cum_avg_price
    prev_notional = float(pos.overnight_stop_filled_notional_so_far or 0.0)
    if booked_qty > 0 and prev_notional <= 0:
        prev_notional = booked_qty * cum_avg_price  # legacy row: assume same price
    price = (cum_avg_price * cum_qty - prev_notional) / delta_qty
    if not (price > 0) or price > cum_avg_price * 3 or price < cum_avg_price / 3:
        return cum_avg_price
    return price


def book_delta(db: Session, pos: ScalpPosition, fill_price: float, delta_qty: int) -> tuple[float, float]:
    """Books DELTA_QTY newly-filled shares against quantity/capital_risked/
    realized_pnl and the ledger. Capital is released proportionally
    (capital_risked / quantity, evaluated BEFORE the delta is applied).
    Returns (realized_pnl_delta, capital_released)."""
    per_share_capital = (pos.capital_risked / pos.quantity) if pos.quantity else 0.0
    capital_released = per_share_capital * delta_qty
    realized_pnl_delta = (fill_price - pos.entry_price) * delta_qty

    pos.quantity = max(0, pos.quantity - delta_qty)
    pos.capital_risked = max(0.0, pos.capital_risked - capital_released)
    pos.realized_pnl = (pos.realized_pnl or 0.0) + realized_pnl_delta
    pos.overnight_stop_filled_qty_so_far = (pos.overnight_stop_filled_qty_so_far or 0) + delta_qty
    pos.overnight_stop_filled_notional_so_far = (pos.overnight_stop_filled_notional_so_far or 0.0) + fill_price * delta_qty
    pos.exit_price = fill_price
    db.commit()

    ledger.release_capital(db, position_value=capital_released, realized_pnl=realized_pnl_delta)
    return realized_pnl_delta, capital_released


def assign_stop_order(pos: ScalpPosition, new_order_id: Optional[str]) -> None:
    """The ONLY way overnight_stop_order_id should change once a stop exists.
    Rolls the finished order's booked qty into overnight_stop_prior_qty and
    zeroes the per-order counters so the next order's cumulative figures are
    compared against ITS OWN booked amount, not the previous order's."""
    pos.overnight_stop_prior_qty = int(pos.overnight_stop_prior_qty or 0) + int(pos.overnight_stop_filled_qty_so_far or 0)
    pos.overnight_stop_filled_qty_so_far = 0
    pos.overnight_stop_filled_notional_so_far = 0.0
    pos.overnight_stop_order_id = new_order_id


def _close_out(db: Session, pos: ScalpPosition, last_price: float, staged: bool) -> None:
    total_qty = int(pos.overnight_stop_prior_qty or 0) + int(pos.overnight_stop_filled_qty_so_far or 0)
    basis = pos.entry_price * total_qty
    pct = ((pos.realized_pnl or 0.0) / basis * 100.0) if basis else 0.0
    pos.status = "STOP_HIT"
    pos.realized_pnl_pct = pct
    pos.dhan_exit_order_id = pos.overnight_stop_order_id or pos.dhan_exit_order_id
    pos.overnight_stop_order_id = None
    pos.error_message = None          # session72: never leave a stale failure/sentinel on a closed row
    pos.closed_at = datetime.now(timezone.utc)
    db.commit()
    shared_symbol_lock.release(db, pos.symbol)
    logger.info("overnight stop: %s (id=%d) TRIGGERED (final) @ ~₹%.2f — total P&L ₹%.2f (%.2f%%)",
                pos.symbol, pos.id, last_price, pos.realized_pnl or 0.0, pct)
    notifier.notify_sync(
        f"🔴 <b>STOP_HIT (overnight)</b> — {pos.symbol} (final exit price ~₹{last_price:.2f})\n"
        f"Entry ₹{pos.entry_price:.2f}\n"
        f"Total P&L ₹{pos.realized_pnl or 0.0:,.2f} ({pct:.2f}%)\n"
        f"Protective stop triggered pre-market/overnight."
        + (" (in stages — see earlier partial-fill alert(s).)" if staged else "")
    )


def _settle(db: Session, pos: ScalpPosition, *, kind: str, status: str,
            price: Optional[float], cum_qty: Optional[int]) -> dict:
    """kind: 'partial' | 'complete' | 'dead' | 'history'. Idempotent — the
    booked counters make repeated calls with the same cumulative data no-ops."""
    result = {"status": status, "booked_qty": 0, "closed": False, "residual": False}
    if cum_qty is None or cum_qty <= 0:
        return result
    if price is None:
        logger.warning("overnight stop: %s (id=%d) order %s shows %s cum_qty=%s but no fill price — leaving for next pass.",
                       pos.symbol, pos.id, pos.overnight_stop_order_id, status, cum_qty)
        return result

    already = int(pos.overnight_stop_filled_qty_so_far or 0)
    delta = cum_qty - already
    if delta < 0:
        logger.warning("overnight stop: %s (id=%d) order %s cumulative filled %d < %d already booked — not booking a negative delta.",
                       pos.symbol, pos.id, pos.overnight_stop_order_id, cum_qty, already)
        return result

    if delta > 0:
        dprice = delta_fill_price(pos, price, cum_qty)
        pnl_delta, _cap = book_delta(db, pos, dprice, delta)
        result["booked_qty"] = delta
        if pos.quantity > 0:
            logger.info("overnight stop: %s (id=%d) PARTIAL fill %d @ ₹%.2f (cum %d) [%s], %d sh remain.",
                        pos.symbol, pos.id, delta, dprice, cum_qty, status, pos.quantity)
            notifier.notify_sync(
                f"🟠 <b>STOP partial fill (overnight)</b> — {pos.symbol}: {delta} sh @ ₹{dprice:.2f} "
                f"(P&L ₹{pnl_delta:,.2f}). {pos.quantity} sh still open."
            )

    if pos.quantity <= 0:
        _close_out(db, pos, price, staged=already > 0 or int(pos.overnight_stop_prior_qty or 0) > 0)
        result["closed"] = True
    elif kind == "complete":
        # Broker says the order is fully TRADED but shares remain in our books:
        # never mark the position closed with shares outstanding.
        result["residual"] = True
        msg = (f"🚨 <b>OVERNIGHT STOP FILLED BUT {pos.quantity} SH REMAIN</b> — {pos.symbol} (id={pos.id}): "
               f"order {pos.overnight_stop_order_id} is {status} (cum {cum_qty}) yet the books still hold "
               f"{pos.quantity}. Position left OPEN with NO stop — verify holdings in the Dhan app.")
        logger.critical(msg)
        notifier.notify_critical(msg)
        assign_stop_order(pos, None)
        db.commit()
    return result


def settle_stop_row(db: Session, pos: ScalpPosition, row: dict) -> dict:
    status = row_status(row)
    if status in FILLED_STATUSES:
        kind = "complete"
    elif status in PARTIAL_STATUSES:
        kind = "partial"
    elif status in DEAD_STATUSES:
        kind = "dead"      # CANCELLED/EXPIRED can still carry a partial fill
    else:
        return {"status": status, "booked_qty": 0, "closed": False, "residual": False}
    return _settle(db, pos, kind=kind, status=status, price=row_avg_price(row), cum_qty=row_cum_qty(row))


def _trade_qty(t: dict) -> int:
    for k in ("tradedQuantity", "traded_quantity", "quantity"):
        if t.get(k) is not None:
            try:
                return int(t[k])
            except (TypeError, ValueError):
                pass
    return 0


def _trade_price(t: dict) -> float:
    for k in ("tradedPrice", "traded_price", "price"):
        if t.get(k) is not None:
            try:
                return float(t[k])
            except (TypeError, ValueError):
                pass
    return 0.0


def _trade_order_id(t: dict) -> str:
    return str(t.get("orderId") or t.get("order_id") or "")


def aggregate_trades(trades: list, order_id: str) -> tuple[int, Optional[float]]:
    """(total_qty, weighted_avg_price) of every trade row for ORDER_ID."""
    q = 0
    notional = 0.0
    for t in trades:
        if _trade_order_id(t) != str(order_id):
            continue
        tq, tp = _trade_qty(t), _trade_price(t)
        if tq > 0 and tp > 0:
            q += tq
            notional += tq * tp
    return q, (notional / q if q else None)


def settle_from_trades(db: Session, pos: ScalpPosition, days_back: int = 6) -> dict:
    """Recovery path for a stop order that has aged out of Dhan's (today-only)
    order book: book whatever Dhan's trade history says it filled."""
    order_id = pos.overnight_stop_order_id
    empty = {"status": "HISTORY", "booked_qty": 0, "closed": False, "residual": False}
    if not order_id:
        return empty
    today = ist_now().date()
    try:
        trades = dhan_client.get_trade_history(
            db, (today - timedelta(days=days_back)).isoformat(), today.isoformat(),
        )
    except Exception as e:
        logger.warning("overnight stop: %s (id=%d) trade-history lookup failed: %s", pos.symbol, pos.id, e)
        return empty
    qty, avg = aggregate_trades(trades, order_id)
    if not qty:
        return empty
    return _settle(db, pos, kind="history", status="HISTORY", price=avg, cum_qty=qty)


def settle_before_flat_sell(db: Session, pos: ScalpPosition) -> dict:
    """Book any not-yet-booked fill of the resting stop. Call BEFORE cancelling
    the stop / sizing a flat SELL (so the SELL is for what is really left) and
    again AFTER the cancel (catches a fill that landed in between)."""
    none = {"status": None, "booked_qty": 0, "closed": False, "residual": False}
    if not (pos.overnight_converted_to_cnc and pos.overnight_stop_order_id):
        return none
    try:
        rows = dhan_client.get_order_list(db)
    except Exception as e:
        logger.warning("overnight stop: %s (id=%d) order-list fetch failed before flat SELL: %s", pos.symbol, pos.id, e)
        return none
    row = next((r for r in rows
                if str(r.get("orderId") or r.get("order_id") or "") == str(pos.overnight_stop_order_id)), None)
    if row is not None:
        return settle_stop_row(db, pos, row)
    return settle_from_trades(db, pos)


# ── Re-arm + morning recheck ─────────────────────────────────────────────────
def rearm(db: Session, pos: ScalpPosition, stop_pct: float, *, reason: str) -> Optional[str]:
    from orders.eod_squareoff import _place_overnight_stop  # lazy: avoids an import cycle
    if (pos.quantity or 0) <= 0:
        return None
    old_id = pos.overnight_stop_order_id
    new_id = _place_overnight_stop(db, pos, stop_pct)
    if new_id:
        assign_stop_order(pos, new_id)
        db.commit()
        notifier.notify_critical(
            f"🔄 <b>Overnight stop RE-ARMED</b> — {pos.symbol} (id={pos.id}) x{pos.quantity}: {reason}; "
            f"old stop {old_id!r} → new order {new_id} at ₹{round(pos.entry_price * (1 - stop_pct / 100.0), 2):.2f}."
        )
    else:
        notifier.notify_critical(
            f"🚨 <b>OVERNIGHT STOP RE-ARM FAILED</b> — {pos.symbol} (id={pos.id}) x{pos.quantity}: {reason}. "
            f"Stop {old_id!r} is not protecting this position. Manage it MANUALLY before market open."
        )
    return new_id


def morning_recheck(db: Session, stop_pct: float) -> dict:
    """Once per day pre-market: verify every carried position's stop, book any
    fills first, then re-arm (for the REMAINING quantity) if the stop is gone."""
    summary = {"checked": 0, "live": 0, "rearmed": 0, "rearm_failed": 0, "closed": 0, "settled_qty": 0}
    positions = db.query(ScalpPosition).filter_by(status="OPEN", overnight_converted_to_cnc=True).all()
    if not positions:
        return summary
    try:
        rows = dhan_client.get_order_list(db)
    except Exception as e:
        logger.error("overnight stop recheck: get_order_list failed: %s", e, exc_info=True)
        return summary
    order_map = {str(r.get("orderId") or r.get("order_id") or ""): r for r in rows if r.get("orderId") or r.get("order_id")}

    for pos in positions:
        try:
            if not pos.overnight_stop_order_id:
                continue  # no stop ever placed (OVERNIGHT_STOP_LOSS_PCT=0 / pre-option-3 row)
            summary["checked"] += 1
            row = order_map.get(str(pos.overnight_stop_order_id))
            if row is None:
                logger.warning("overnight stop recheck: %s (id=%d) stop %s not in today's order book — checking trade history before re-arm.",
                               pos.symbol, pos.id, pos.overnight_stop_order_id)
                res = settle_from_trades(db, pos)
                summary["settled_qty"] += res["booked_qty"]
                if pos.status != "OPEN" or (pos.quantity or 0) <= 0:
                    summary["closed"] += 1
                    continue
                if not res.get("residual") and stop_pct > 0:
                    summary["rearmed" if rearm(db, pos, stop_pct, reason="previous stop missing from Dhan order book") else "rearm_failed"] += 1
                continue

            status = row_status(row)
            if status in LIVE_STATUSES:
                summary["live"] += 1
                logger.info("overnight stop recheck: %s (id=%d) stop %s confirmed live (%s)", pos.symbol, pos.id, pos.overnight_stop_order_id, status)
                continue
            if status in IN_FLIGHT_STATUSES:
                logger.info("overnight stop recheck: %s (id=%d) stop %s is %s — in flight, reconcile will settle it.", pos.symbol, pos.id, pos.overnight_stop_order_id, status)
                continue

            res = settle_stop_row(db, pos, row)
            summary["settled_qty"] += res["booked_qty"]
            if res["closed"] or pos.status != "OPEN":
                summary["closed"] += 1
                continue
            if status in DEAD_STATUSES and stop_pct > 0 and not res.get("residual"):
                summary["rearmed" if rearm(db, pos, stop_pct, reason=f"previous stop was {status}") else "rearm_failed"] += 1
            elif status not in DEAD_STATUSES | FILLED_STATUSES | PARTIAL_STATUSES:
                logger.warning("overnight stop recheck: %s (id=%d) unrecognized status %r — not acting.", pos.symbol, pos.id, status)
        except Exception as e:
            logger.error("overnight stop recheck failed for %s (id=%s): %s", pos.symbol, pos.id, e, exc_info=True)
    return summary
