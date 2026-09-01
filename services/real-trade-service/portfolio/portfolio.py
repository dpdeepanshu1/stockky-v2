"""
portfolio/portfolio.py — DEMO-mode fill simulation + account/position
bookkeeping. This is the "not a fake/simple simulator" piece from the
original plan: it uses the SAME entry rules (bounded limit price,
time-boxed validity — config.ENTRY_ZONE_UPPER_PCT/ENTRY_VALIDITY_MINUTES)
that a REAL order would use, and prices fills off the real market_feed
quote, not a synthetic random walk.

Simplifications this phase is explicit about (a real broker adds more
nuance than this):
  * Fill assumption: a DEMO limit order fills at min(limit_price, ltp) the
    moment ltp is at or below the limit — i.e. no partial fills, no queue
    position modeling, no slippage beyond "you don't get a better price
    than your own limit". This is a conservative simplification (real
    fills are sometimes worse due to slippage on illiquid names) — it will
    NOT make DEMO mode look better than REAL mode would perform, which is
    the direction that matters for trusting the rehearsal.
  * No market-order fallback — matches config.ENTRY_NO_CHASE (decision 1):
    an unfilled DEMO entry expires exactly like a real one would.

REAL mode never calls this module for fills — a REAL fill only ever comes
from Dhan's own order/trade webhook or reconciliation (Phase 3). This
module's `record_real_fill()` exists only so REAL positions can be tracked
in the SAME shape once that's wired, without changing this schema again.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import config
import models
from audit.logger import log_action
from market_feed.feed import Tick

logger = logging.getLogger("real-trade-portfolio")


def get_account(db: Session, mode: str) -> models.TradeAccount:
    row = db.query(models.TradeAccount).filter_by(mode=mode).first()
    if row is None:
        raise RuntimeError(f"No trade_accounts row for mode={mode} — schema not seeded.")
    return row


def open_positions(db: Session, mode: str) -> list[models.TradePosition]:
    """Both OPEN and PARTIALLY_CLOSED count as 'live' here — a position
    that's had a partial exit still has shares that need trailing-stop,
    time-stop, and eventual full-exit evaluation. Only CLOSED positions
    are excluded. (A query that only matched status='OPEN' would silently
    stop evaluating a position's remaining shares the moment its first
    partial exit fired — exactly the kind of orphaned-state bug this
    session has been hunting elsewhere in the codebase.)"""
    return (
        db.query(models.TradePosition)
        .filter(models.TradePosition.mode == mode, models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED")))
        .all()
    )


def try_fill_entry(db: Session, order: models.TradeOrder, tick: Tick, stop_price: float, target_price: float) -> bool:
    """DEMO-only. Returns True and records a fill + opens/adds-to a
    position if the current tick would fill this pending limit BUY order;
    False (order left PENDING) otherwise. Expiry (valid_until) is checked
    by the caller (entry_engine), not here — this function only ever
    answers "would this order fill right now".

    stop_price/target_price come from the TradeDecision that produced this
    order (TradeOrder itself has no stop/target columns — only the
    decision does) and are written onto the position at OPEN time so
    exit_engine has something to trail/check from the very first cycle,
    not just after its own first evaluation."""
    if order.mode != "DEMO":
        raise RuntimeError("try_fill_entry is DEMO-only — REAL fills come from Dhan, never simulated.")
    if order.side != "BUY" or order.status != "PLACED":
        return False
    if order.limit_price is None or tick.price > order.limit_price:
        return False  # price hasn't come down into the entry zone yet

    fill_price = min(order.limit_price, tick.price)
    now = datetime.now(timezone.utc)

    order.status = "FILLED"
    order.updated_at = now
    db.add(models.TradeOrderEvent(order_id=order.id, event_type="FILLED",
                                   detail=f"Simulated DEMO fill @ {fill_price}"))
    db.add(models.TradeFill(order_id=order.id, qty=order.qty, price=fill_price, filled_at=now))

    position = db.query(models.TradePosition).filter_by(mode="DEMO", symbol=order.symbol, status="OPEN").first()
    if position is None:
        position = models.TradePosition(
            mode="DEMO", symbol=order.symbol, status="OPEN",
            qty_open=order.qty, avg_entry_price=fill_price, opened_at=now,
            current_stop=stop_price, current_target=target_price,
            # 2026-09-01 fix: fixed at open so exit_engine's gap-down check
            # has a stable reference distance, not one that drifts as
            # current_stop trails.
            initial_stop_distance=abs(fill_price - stop_price),
        )
        db.add(position)
        db.flush()
        db.add(models.TradePositionEvent(position_id=position.id, event_type="OPENED",
                                          detail=f"{order.qty} @ {fill_price}, stop {stop_price}, target {target_price}"))
    else:
        # Pyramiding case — only reachable if risk_engine's check #6 was
        # configured to allow it; recompute a volume-weighted average.
        # Stop/target are intentionally left at the ORIGINAL position's
        # values here rather than overwritten by the new add's numbers —
        # exit_engine's trailing logic owns tightening the stop from here,
        # not a fresh entry signal on an already-open name.
        total_cost = position.avg_entry_price * position.qty_open + fill_price * order.qty
        position.qty_open += order.qty
        position.avg_entry_price = round(total_cost / position.qty_open, 4)
        db.add(models.TradePositionEvent(position_id=position.id, event_type="ADDED",
                                          detail=f"+{order.qty} @ {fill_price}"))

    account = get_account(db, "DEMO")
    account.cash_available -= fill_price * order.qty
    account.updated_at = now

    db.commit()
    log_action(db, actor="system", action="ORDER_FILLED", mode="DEMO",
               detail=f"{order.symbol} BUY {order.qty} @ {fill_price}")
    return True


def close_position(
    db: Session, position: models.TradePosition, tick: Tick, qty_to_close: int, reason: str,
) -> float:
    """DEMO-only full or partial exit at the current tick price. Returns
    the realized P&L booked by this close. Updates the account's
    cash/equity/realized_pnl_today in the same transaction so a reader can
    never observe a half-updated state."""
    if position.mode != "DEMO":
        raise RuntimeError("close_position is DEMO-only in this phase.")
    qty_to_close = min(qty_to_close, position.qty_open)
    if qty_to_close <= 0:
        return 0.0

    now = datetime.now(timezone.utc)
    exit_price = tick.price
    pnl = round((exit_price - position.avg_entry_price) * qty_to_close, 2)

    position.qty_open -= qty_to_close
    position.realized_pnl += pnl
    if position.qty_open <= 0:
        position.status = "CLOSED"
        position.closed_at = now
    else:
        position.status = "PARTIALLY_CLOSED"

    db.add(models.TradePositionEvent(
        position_id=position.id,
        event_type="CLOSED" if position.status == "CLOSED" else "PARTIAL_EXIT",
        detail=f"{reason}: {qty_to_close} @ {exit_price} (pnl {pnl:+.2f})",
    ))
    # Flush before re-querying open positions below — the session is
    # autoflush=False (see db.py), so without this the equity recompute
    # could still see this position's PRE-update status and double-count
    # (or drop) it depending on flush timing. Cheap: one extra round trip,
    # correctness-critical: equity must reflect the close that just
    # happened, not a stale read of it.
    db.flush()

    account = get_account(db, "DEMO")
    proceeds = exit_price * qty_to_close
    account.cash_available += proceeds
    account.realized_pnl_today += pnl
    account.realized_pnl_total += pnl
    account.current_equity = account.cash_available + _open_positions_market_value(db, "DEMO")
    account.updated_at = now

    db.commit()
    log_action(db, actor="system", action="POSITION_CLOSED" if position.status == "CLOSED" else "POSITION_PARTIAL_EXIT",
               mode="DEMO", detail=f"{position.symbol} {reason} qty={qty_to_close} pnl={pnl:+.2f}")
    return pnl


def record_real_order_sent(db: Session, order: models.TradeOrder, dhan_order_id: str) -> None:
    """REAL-only. Marks a TradeOrder as sent to the broker. Does NOT open a
    position or touch account cash — a REAL fill is never assumed just
    because the order was accepted; only reconcile_real_orders() (which
    reads Dhan's own order/trade state) is allowed to do that."""
    order.status = "PLACED"
    order.dhan_order_id = dhan_order_id
    order.updated_at = datetime.now(timezone.utc)
    db.add(models.TradeOrderEvent(order_id=order.id, event_type="PLACED",
                                   detail=f"Sent to Dhan, broker order_id={dhan_order_id}"))
    db.commit()


def record_real_fill(db: Session, order: models.TradeOrder, fill_price: float, filled_qty: int,
                      stop_price: float, target_price: float, is_partial: bool = False) -> None:
    """REAL-only equivalent of try_fill_entry's position-opening half, but
    driven by a CONFIRMED fill from Dhan (reconcile_real_orders), never by
    a simulated price check. Mirrors try_fill_entry's pyramiding/average
    logic so both modes produce the same TradePosition shape.

    `filled_qty` here is always the NEW/incremental qty to book this call
    (reconcile_real_orders is responsible for diffing against
    order.filled_qty_so_far before calling this — see that module's
    docstring) — never the order's cumulative broker-reported qty, or a
    PART_TRADED order would get the same shares added to the position
    twice.

    `is_partial=True` (order still PART_TRADED at the broker — more fills
    may still come) leaves order.status as "PARTIAL" instead of "FILLED",
    so reconcile's next-cycle query (status in PLACED/PARTIAL) keeps
    checking this order for the rest of the fill. Caller still owns
    order.filled_qty_so_far — this function only ever books the position
    side of a fill, same division of responsibility record_real_exit_fill
    already uses for exits."""
    now = datetime.now(timezone.utc)
    order.status = "PARTIAL" if is_partial else "FILLED"
    order.updated_at = now
    event_type = "PARTIAL_FILL" if is_partial else "FILLED"
    detail_verb = "Broker-confirmed partial fill" if is_partial else "Broker-confirmed fill"
    db.add(models.TradeOrderEvent(order_id=order.id, event_type=event_type,
                                   detail=f"{detail_verb} @ {fill_price} x{filled_qty}"))
    db.add(models.TradeFill(order_id=order.id, qty=filled_qty, price=fill_price, filled_at=now))

    position = db.query(models.TradePosition).filter_by(mode="REAL", symbol=order.symbol, status="OPEN").first()
    if position is None:
        position = models.TradePosition(
            mode="REAL", symbol=order.symbol, status="OPEN",
            qty_open=filled_qty, avg_entry_price=fill_price, opened_at=now,
            current_stop=stop_price, current_target=target_price,
            # 2026-09-01 fix: same fixed-at-open distance as the DEMO path.
            initial_stop_distance=abs(fill_price - stop_price),
        )
        db.add(position)
        db.flush()
        db.add(models.TradePositionEvent(position_id=position.id, event_type="OPENED",
                                          detail=f"{filled_qty} @ {fill_price}, stop {stop_price}, target {target_price} (broker-confirmed)"))
    else:
        total_cost = position.avg_entry_price * position.qty_open + fill_price * filled_qty
        position.qty_open += filled_qty
        position.avg_entry_price = round(total_cost / position.qty_open, 4)
        db.add(models.TradePositionEvent(position_id=position.id, event_type="ADDED",
                                          detail=f"+{filled_qty} @ {fill_price} (broker-confirmed)"))

    account = get_account(db, "REAL")
    account.cash_available -= fill_price * filled_qty
    account.updated_at = now
    db.commit()
    log_action(db, actor="system", action="ORDER_FILLED", mode="REAL",
               detail=f"{order.symbol} BUY {filled_qty} @ {fill_price} (broker-confirmed)")


def record_real_exit_sent(db: Session, position: models.TradePosition, dhan_order_id: str,
                           qty: int, reason: str, full: bool = True) -> None:
    """REAL-only. A SELL was sent to Dhan for this position but is not yet
    confirmed filled. `full=True` (stop hit / time stop — the whole open
    qty) marks the position PENDING_EXIT so exit_engine stops evaluating
    it until reconcile_real_orders() confirms the fill. `full=False`
    (a partial target exit) leaves the position's status untouched — the
    remainder is still a live, OPEN position that still needs stop
    trailing and further exit evaluation every cycle; only the specific
    in-flight SELL is tracked, via the TradeOrder row itself, so exit_engine's
    own duplicate-send guard (_has_pending_real_sell) is what prevents a
    second SELL before this one confirms — not the position status."""
    if full:
        position.status = "PENDING_EXIT"
    db.add(models.TradePositionEvent(
        position_id=position.id, event_type="EXIT_SENT",
        detail=f"{reason}: SELL {qty} sent to Dhan, broker order_id={dhan_order_id}",
    ))
    db.commit()


def record_real_exit_fill(db: Session, position: models.TradePosition, exit_price: float,
                           qty_closed: int, reason: str) -> float:
    """REAL-only. Confirmed by reconcile_real_orders() against Dhan's own
    trade book — never called speculatively. Books realized P&L exactly
    like close_position() does for DEMO, so both modes report P&L the
    same way.

    BUG FIX (2026-08-27): a still-open remainder used to be left at status
    "OPEN" (not "PARTIALLY_CLOSED", unlike close_position()'s DEMO
    equivalent). exit_engine's target-hit check guards against firing a
    second partial exit at the same target with `position.status ==
    "OPEN"` — so a REAL position that stayed at "OPEN" after its first
    partial exit could get partial-exited AGAIN next cycle if price was
    still above target, instead of moving on to trailing-stop management
    of the remainder like the module docstring describes. Now matches
    DEMO: status becomes "PARTIALLY_CLOSED", and (for a target-hit partial
    specifically) the stop is moved to breakeven on the remainder, exactly
    like close_position()'s DEMO caller does inline."""
    now = datetime.now(timezone.utc)
    qty_closed = min(qty_closed, position.qty_open)
    pnl = round((exit_price - position.avg_entry_price) * qty_closed, 2)

    position.qty_open -= qty_closed
    position.realized_pnl += pnl
    if position.qty_open <= 0:
        position.status = "CLOSED"
        position.closed_at = now
    else:
        position.status = "PARTIALLY_CLOSED"
        if reason == "target_hit_partial":
            # De-risk the remainder the same way DEMO does — move the stop
            # up to breakeven rather than leaving it at the original,
            # wider stop distance now that some profit is locked in.
            position.current_stop = max(position.current_stop or 0, position.avg_entry_price)

    db.add(models.TradePositionEvent(
        position_id=position.id,
        event_type="CLOSED" if position.status == "CLOSED" else "PARTIAL_EXIT",
        detail=f"{reason}: {qty_closed} @ {exit_price} (pnl {pnl:+.2f}, broker-confirmed)",
    ))
    db.flush()

    account = get_account(db, "REAL")
    account.cash_available += exit_price * qty_closed
    account.realized_pnl_today += pnl
    account.realized_pnl_total += pnl
    account.current_equity = account.cash_available + _open_positions_market_value(db, "REAL")
    account.updated_at = now
    db.commit()
    log_action(db, actor="system",
               action="POSITION_CLOSED" if position.status == "CLOSED" else "POSITION_PARTIAL_EXIT",
               mode="REAL", detail=f"{position.symbol} {reason} qty={qty_closed} pnl={pnl:+.2f} (broker-confirmed)")
    return pnl


def _open_positions_market_value(db: Session, mode: str) -> float:
    """Best-effort mark-to-market using each position's last known price
    (avg_entry_price as a floor when no fresher tick has been recorded
    this cycle — refresh_unrealized() below is what keeps this current)."""
    total = 0.0
    for p in open_positions(db, mode):
        total += p.avg_entry_price * p.qty_open
    return total


def refresh_unrealized(db: Session, mode: str, ticks: dict[str, Tick]) -> None:
    """Called once per evaluation cycle with the latest ticks for every
    open-position symbol — updates each position's unrealized_pnl and the
    account's current_equity to reflect live prices, not just entry
    prices. Cheap and idempotent; safe to call even with a partial tick
    dict (positions with no fresh tick keep their last-known valuation)."""
    positions = open_positions(db, mode)
    market_value = 0.0
    for p in positions:
        tick = ticks.get(p.symbol)
        last_price = tick.price if tick else p.avg_entry_price
        p.unrealized_pnl = round((last_price - p.avg_entry_price) * p.qty_open, 2)
        market_value += last_price * p.qty_open

    account = get_account(db, mode)
    account.current_equity = round(account.cash_available + market_value, 2)
    account.updated_at = datetime.now(timezone.utc)
    db.commit()
