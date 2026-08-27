"""
exit_engine/exit.py — continuous evaluation of every open position, per
the plan's "never a single fixed target" exit design:

  1. Stop hit           -> FULL_EXIT (capital protection always wins)
  2. Target hit (first) -> PARTIAL_EXIT (lock in some profit), then trail
                            the stop on the remainder instead of a second
                            fixed target
  3. Trailing stop       -> ATR-based ratchet, only ever tightens
  4. Time stop           -> FULL_EXIT if the position hasn't moved
                            favorably within MAX_HOLD_DAYS (capital
                            shouldn't sit dead in a non-performing name)
  5. Otherwise           -> HOLD

Every evaluation — including HOLD — writes a TradeExitDecision row (per
the plan's audit principle: "every consequential action gets logged").
REAL-mode exits ARE wired to Dhan (Phase 3, execution/dhan_client.py via
_send_real_sell below) — stop/target/time-stop all send a MARKET SELL.
A sent-not-yet-confirmed exit is tracked via the TradeOrder row itself
(_has_pending_real_sell), not by removing the position from evaluation,
so a partial exit's remainder still gets trailed/evaluated every cycle.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

import models
from audit.logger import log_action
from execution import dhan_client
from market_feed.feed import get_quotes
from notifier import notify_sync
from portfolio.portfolio import close_position, open_positions, refresh_unrealized, record_real_exit_sent
from tz_utils import as_aware

logger = logging.getLogger("real-trade-exit")

PARTIAL_EXIT_FRACTION = 0.5   # lock in half the position at first target
TRAIL_ATR_MULTIPLIER = 1.5    # same multiplier buy_sniper.py/entry_engine use for the initial stop
MAX_HOLD_DAYS = 10            # hard time-stop — capital shouldn't sit dead


def _write_exit_decision(db: Session, position: models.TradePosition, action: str, reasoning: str, ltp: float) -> None:
    db.add(models.TradeExitDecision(
        position_id=position.id, action=action, reasoning=reasoning, ltp_at_decision=ltp,
    ))


def _has_pending_real_sell(db: Session, symbol: str) -> bool:
    """True if a REAL SELL for this symbol is already sent and awaiting
    broker confirmation — the guard against sending a second SELL for the
    same shares before reconcile_real_orders() has confirmed the first
    one (see record_real_exit_sent's full=False docstring)."""
    return (
        db.query(models.TradeOrder)
        .filter_by(mode="REAL", symbol=symbol, side="SELL", status="PLACED")
        .first()
        is not None
    )


def _send_real_sell(
    db: Session, position: models.TradePosition, qty: int, reason: str, full: bool = True,
    execution_source: str = "AUTO", confirmed_by: str | None = None,
) -> bool:
    """Places a MARKET SELL at Dhan for `qty` shares of an open REAL
    position. MARKET, not LIMIT — an exit's whole purpose is capital
    protection or locking in profit; a limit sell that never fills would
    defeat that. Returns True and marks the position PENDING_EXIT only if
    Dhan actually accepted the order; on any failure the position is left
    untouched so exit_engine tries again next cycle rather than silently
    giving up on a stop that needs to fire.

    execution_source/confirmed_by: passed through by manual_engine.py when
    a human clicked SELL (source="MANUAL", confirmed_by=admin username);
    every other caller (exit_engine's own stop/target/time-stop cycle,
    main.py's existing "Close" button) leaves these at the AUTO default —
    the point is only to distinguish a human-initiated sell from the
    automatic exit cycle in the audit trail, not to change behavior."""
    try:
        security_id = dhan_client.get_security_id(db, position.symbol)
        result = dhan_client.place_order(
            db, is_armed=True,  # exits are always allowed — see dhan_client.cancel_order's same policy
            security_id=security_id,
            exchange_segment=dhan_client.NSE_EQ_SEGMENT,
            transaction_type="SELL",
            quantity=qty,
            order_type="MARKET",
            price=0,
        )
        dhan_order_id = str(result.get("orderId") or result.get("order_id") or "")
        if not dhan_order_id:
            raise RuntimeError(f"Dhan accepted the SELL but returned no order id: {result}")

        order = models.TradeOrder(
            mode="REAL", symbol=position.symbol, side="SELL", order_type="MARKET",
            qty=qty, status="PLACED", dhan_order_id=dhan_order_id,
            execution_source=execution_source,
            confirmed_by=confirmed_by,
            confirmed_at=datetime.now(timezone.utc) if confirmed_by else None,
            exit_reason=reason,
        )
        db.add(order)
        db.flush()
        db.add(models.TradeOrderEvent(order_id=order.id, event_type="PLACED",
                                       detail=f"{reason}: MARKET SELL {qty} sent to Dhan"))
        record_real_exit_sent(db, position, dhan_order_id, qty, reason, full=full)
        notify_sync(f"📤 *SELL sent* — {position.symbol} x{qty} ({reason})\nAwaiting broker fill confirmation.")
        return True
    except Exception as e:  # noqa: BLE001 — must never crash the whole exit cycle
        logger.error("REAL exit SELL failed for %s (%s): %s", position.symbol, reason, e)
        notify_sync(f"⚠️ *SELL rejected by Dhan* — {position.symbol} x{qty} ({reason})\n{str(e)[:300]}")
        return False


async def evaluate_mode(db: Session, mode: str) -> dict:
    """One evaluation cycle for every open position in `mode`. Returns a
    tally for the caller/logs."""
    positions = open_positions(db, mode)
    if not positions:
        return {"evaluated": 0, "held": 0, "trailed": 0, "partial_exits": 0, "full_exits": 0, "time_stops": 0}

    symbols = list({p.symbol for p in positions})
    ticks = await get_quotes(symbols)

    # Mark-to-market first — even positions we don't act on this cycle
    # should show a current unrealized P&L, not a stale one.
    if mode == "DEMO":
        refresh_unrealized(db, mode, ticks)

    held = trailed = partial_exits = full_exits = time_stops = 0
    now = datetime.now(timezone.utc)

    for idx, position in enumerate(positions):
        try:
            import pipeline_status as pstat
            pstat.set_symbol_progress(mode, position.symbol, idx, len(positions))
        except Exception:
            pass
        tick = ticks.get(position.symbol)
        if tick is None:
            _write_exit_decision(db, position, "HOLD", "No current price available this cycle.", 0.0)
            held += 1
            continue
        ltp = tick.price

        # REAL only: a SELL already sent to Dhan for this symbol is still
        # awaiting broker confirmation — don't re-evaluate stop/target/
        # time-stop against it again until reconcile_real_orders() has
        # confirmed or rejected that order. DEMO has no such in-flight
        # state (close_position is synchronous), so this never applies to it.
        if mode == "REAL" and _has_pending_real_sell(db, position.symbol):
            _write_exit_decision(db, position, "HOLD", "Exit already sent to Dhan, awaiting confirmation.", ltp)
            held += 1
            continue

        # 1. Stop hit — capital protection always wins, checked first.
        if position.current_stop is not None and ltp <= position.current_stop:
            reasoning = f"Stop {position.current_stop} hit at LTP {ltp}."
            _write_exit_decision(db, position, "FULL_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, position.qty_open, "stop_hit")
                full_exits += 1
            else:
                if _send_real_sell(db, position, position.qty_open, "stop_hit"):
                    full_exits += 1
                else:
                    held += 1  # placement failed — still open, retry next cycle
            continue

        # 2. First target hit — partial exit, not a full close.
        if position.current_target is not None and ltp >= position.current_target and position.status == "OPEN":
            qty_to_close = max(1, int(position.qty_open * PARTIAL_EXIT_FRACTION))
            reasoning = f"Target {position.current_target} hit at LTP {ltp} — locking in {qty_to_close} shares."
            _write_exit_decision(db, position, "PARTIAL_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, qty_to_close, "target_hit_partial")
                # Move the stop up to breakeven on the remainder — a
                # partial exit should de-risk the rest, not leave it
                # exposed to the original stop distance.
                if position.qty_open > 0:
                    position.current_stop = max(position.current_stop or 0, position.avg_entry_price)
                    db.commit()
                partial_exits += 1
            else:
                if _send_real_sell(db, position, qty_to_close, "target_hit_partial", full=False):
                    partial_exits += 1
                else:
                    held += 1
            continue

        # 3. Time stop — capital shouldn't sit dead in a non-performing name.
        held_days = (now - as_aware(position.opened_at)).days
        if held_days >= MAX_HOLD_DAYS and ltp <= position.avg_entry_price * 1.01:
            reasoning = f"Held {held_days}d with no meaningful favorable move (LTP {ltp} vs entry {position.avg_entry_price})."
            _write_exit_decision(db, position, "EMERGENCY_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, position.qty_open, "time_stop")
                time_stops += 1
            else:
                if _send_real_sell(db, position, position.qty_open, "time_stop"):
                    time_stops += 1
                else:
                    held += 1
            continue

        # 4. Trailing stop — ATR-based, only ever tightens (never loosens).
        if tick.atr and ltp > position.avg_entry_price:
            atr_pct = tick.atr / ltp * 100.0
            trail_candidate = round(ltp * (1 - (atr_pct * TRAIL_ATR_MULTIPLIER) / 100.0), 2)
            if position.current_stop is None or trail_candidate > position.current_stop:
                old_stop = position.current_stop
                position.current_stop = trail_candidate
                db.add(models.TradePositionEvent(
                    position_id=position.id, event_type="STOP_TRAILED",
                    detail=f"{old_stop} -> {trail_candidate} (LTP {ltp})",
                ))
                db.commit()
                _write_exit_decision(db, position, "TRAIL_STOP", f"Stop trailed to {trail_candidate}.", ltp)
                trailed += 1
                continue

        # 5. Otherwise — hold.
        _write_exit_decision(db, position, "HOLD", f"No exit condition met at LTP {ltp}.", ltp)
        held += 1

    db.commit()
    tally = {
        "evaluated": len(positions), "held": held, "trailed": trailed,
        "partial_exits": partial_exits, "full_exits": full_exits, "time_stops": time_stops,
    }
    log_action(db, actor="system", action="EXIT_CYCLE", mode=mode, detail=str(tally))
    return tally
