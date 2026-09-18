"""
orders/breakeven.py — breakeven stop management.

THE BUG THIS FIXES (this session — "breakeven stop is dead code"):
orders/adaptive.py::compute() has, since the 2026-09-15 calibration,
always computed and returned `breakeven_trigger_pct` (the % of target
gain at which the stop should be moved to entry — see that module's
docstring, step 4). Its own docstring says outright: "At 40% of target
the position is far enough in profit that moving the stop to entry is
the right call." But nothing in the codebase ever saved that number
anywhere, and nothing ever checked it or moved a stop-loss order. It was
computed and thrown away on every single entry. A position that ran 40%+
of the way to target and then reversed gave back the entire unrealized
gain (or turned into a full stop-out loss) instead of locking in
breakeven, exactly the opposite of what the docstring already claimed
this service did.

WHAT THIS MODULE ACTUALLY DOES NOW:
Same shape as orders/eod_squareoff.py::run_stagnation_exit — a DB-backed
runtime toggle (ScalpGateState.breakeven_stop_enabled, OFF by default),
called from main.py's fast-reconcile loop on its own short interval, so
it can react well before the next full 10s screening cycle.

For every OPEN position with a recorded breakeven_trigger_pct (see
models.py's comment — positions opened before this column existed simply
have none and are skipped, fail-open) that hasn't already had its stop
moved:
  1. Read live LTP from this service's own tick buffer (ws_client) —
     same source orders/eod_squareoff.py's stagnation check already uses.
  2. unrealized_gain_pct = (ltp - entry_price) / entry_price * 100
  3. If unrealized_gain_pct >= pos.breakeven_trigger_pct: modify the
     Super Order's STOP_LOSS_LEG to entry_price (tick-rounded, with the
     same "must stay strictly below current price" clamp
     orders/adaptive.py's compute() already applies at entry time — Dhan
     rejects a BUY's stop-loss leg at or above the current market).
  4. Mark stop_moved_to_breakeven=True so this is only ever attempted
     once per position — a modify call is not re-sent on every tick once
     it has already succeeded.

Deliberately does NOT touch positions opened via the plain-MARKET-order
fallback (config.USE_SUPER_ORDER=False, or any position with no
dhan_super_order_id) — there is no bracket order to modify in that case;
those positions' stops are managed entirely by orders/entry.py's own
logic (nothing to move here). Also does not touch EXIT_LEGS_REJECTED
positions — a position in that state has no working STOP_LOSS_LEG left
to modify (Dhan already rejected both exit legs); that's
eod_squareoff.py's job, not this one's.

Fails open on every per-position error (mirrors run_stagnation_exit) —
a live-price lookup failure, a Dhan modify rejection, or any other
exception for one symbol logs and moves on to the next position rather
than aborting the whole pass.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

import config
from execution import dhan_client
from models import ScalpGateState, ScalpPosition

logger = logging.getLogger("position-stocks-breakeven")


def _get_gate_state(db: Session) -> ScalpGateState:
    row = db.query(ScalpGateState).filter_by(mode="REAL").first()
    if row is None:
        row = ScalpGateState(mode="REAL")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def run_breakeven_stop(db: Session) -> int:
    """Returns the number of positions whose stop was moved to breakeven
    this pass. See module docstring for the full mechanism."""
    gate = _get_gate_state(db)
    if not gate.breakeven_stop_enabled:
        return 0

    if not config.USE_SUPER_ORDER:
        # No bracket order exists to modify in the plain-order fallback —
        # nothing this module can do. Checked once here rather than per
        # position purely to skip the query below when it can never match.
        return 0

    open_positions = (
        db.query(ScalpPosition)
        .filter(ScalpPosition.status == "OPEN")
        .filter(ScalpPosition.stop_moved_to_breakeven.is_(False))
        .filter(ScalpPosition.breakeven_trigger_pct.isnot(None))
        .filter(ScalpPosition.dhan_super_order_id.isnot(None))
        .all()
    )
    moved = 0
    for pos in open_positions:
        try:
            if not pos.entry_price or pos.entry_price <= 0:
                continue

            try:
                from feed import ws_client
                buf = ws_client.get_tick_buffer(pos.symbol)
                ltp = buf[-1][1] if buf else None
            except Exception:
                ltp = None
            if ltp is None or ltp <= 0:
                continue  # no live price to judge gain by — try again next tick

            unrealized_gain_pct = (ltp - pos.entry_price) / pos.entry_price * 100.0
            if unrealized_gain_pct < pos.breakeven_trigger_pct:
                continue  # not there yet

            # 2026-09-18 audit fix: moving the stop to EXACTLY entry_price
            # is not a true breakeven once brokerage/slippage on the exit
            # SELL is accounted for — a position that round-trips back to
            # entry after this stop moves still realizes a small net loss.
            # Add a small buffer (config.BREAKEVEN_STOP_BUFFER_TICKS ticks,
            # sized off entry_price's own NSE price band) above entry so a
            # worst-case round-trip exit is closer to flat instead of a
            # guaranteed small loss. Set BREAKEVEN_STOP_BUFFER_TICKS=0 to
            # restore the original exact-entry behaviour.
            entry_tick = dhan_client.tick_size_for_price(pos.entry_price)
            buffered_entry = pos.entry_price + (entry_tick * config.BREAKEVEN_STOP_BUFFER_TICKS)
            new_stop = dhan_client.round_to_tick(buffered_entry)

            # Same clamp orders/adaptive.py's compute() applies at entry
            # time (see that module's Step 6): Dhan rejects a BUY's
            # STOP_LOSS_LEG at or above the current market price. Since
            # unrealized_gain_pct >= breakeven_trigger_pct > 0 here,
            # entry_price (and thus the small buffer above it) is normally
            # still strictly below ltp — this only guards the rare case
            # where the buffer or tick-rounding pushes the two back
            # together on a very low-priced / low-trigger stock.
            if new_stop >= ltp:
                band = dhan_client.tick_size_for_price(ltp)
                new_stop = dhan_client.round_to_tick(ltp - band)

            dhan_client.modify_super_order(
                db,
                order_id=pos.dhan_super_order_id,
                order_leg="STOP_LOSS_LEG",
                stop_loss_price=new_stop,
            )

            old_stop = pos.stop_price
            pos.stop_price = new_stop
            pos.stop_moved_to_breakeven = True
            db.commit()
            moved += 1

            logger.info(
                "BREAKEVEN_STOP: %s (id=%d) moved stop ₹%.2f → ₹%.2f "
                "(unrealized gain %.2f%% >= trigger %.2f%%, ltp=₹%.2f entry=₹%.2f)",
                pos.symbol, pos.id, old_stop, new_stop,
                unrealized_gain_pct, pos.breakeven_trigger_pct, ltp, pos.entry_price,
            )
            try:
                import notifier
                notifier.notify_sync(
                    f"🔒 <b>Breakeven stop</b> — {pos.symbol} x{pos.quantity}\n"
                    f"Stop moved ₹{old_stop:.2f} → ₹{new_stop:.2f} (entry ₹{pos.entry_price:.2f})\n"
                    f"Unrealized gain {unrealized_gain_pct:.2f}% at trigger {pos.breakeven_trigger_pct:.2f}%"
                )
            except Exception as notify_e:
                logger.warning(
                    "BREAKEVEN_STOP: %s (id=%d) moved but notify failed: %s",
                    pos.symbol, pos.id, notify_e,
                )
        except Exception as e:
            db.rollback()
            logger.error(
                "BREAKEVEN_STOP: %s (id=%d) unexpected error: %s",
                pos.symbol, pos.id, e, exc_info=True,
            )

    return moved
