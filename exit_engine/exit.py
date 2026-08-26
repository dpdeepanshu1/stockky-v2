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
REAL-mode exits are NOT wired to Dhan yet (Phase 3) — see the TODO in
evaluate_mode(), mirroring entry_engine's same gap.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

import models
from audit.logger import log_action
from market_feed.feed import get_quotes
from portfolio.portfolio import close_position, open_positions, refresh_unrealized
from tz_utils import as_aware

logger = logging.getLogger("real-trade-exit")

PARTIAL_EXIT_FRACTION = 0.5   # lock in half the position at first target
TRAIL_ATR_MULTIPLIER = 1.5    # same multiplier buy_sniper.py/entry_engine use for the initial stop
MAX_HOLD_DAYS = 10            # hard time-stop — capital shouldn't sit dead


def _write_exit_decision(db: Session, position: models.TradePosition, action: str, reasoning: str, ltp: float) -> None:
    db.add(models.TradeExitDecision(
        position_id=position.id, action=action, reasoning=reasoning, ltp_at_decision=ltp,
    ))


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

    for position in positions:
        tick = ticks.get(position.symbol)
        if tick is None:
            _write_exit_decision(db, position, "HOLD", "No current price available this cycle.", 0.0)
            held += 1
            continue
        ltp = tick.price

        # 1. Stop hit — capital protection always wins, checked first.
        if position.current_stop is not None and ltp <= position.current_stop:
            reasoning = f"Stop {position.current_stop} hit at LTP {ltp}."
            _write_exit_decision(db, position, "FULL_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, position.qty_open, "stop_hit")
            else:
                # TODO (Phase 3): execution/dhan_client.place_order(side="SELL", ...)
                # for the REAL position's full qty. Not wired yet.
                logger.warning("REAL exit needed for %s (stop hit) but execution is not wired — Phase 3.", position.symbol)
            full_exits += 1
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
            else:
                logger.warning("REAL partial exit needed for %s but execution is not wired — Phase 3.", position.symbol)
            partial_exits += 1
            continue

        # 3. Time stop — capital shouldn't sit dead in a non-performing name.
        held_days = (now - as_aware(position.opened_at)).days
        if held_days >= MAX_HOLD_DAYS and ltp <= position.avg_entry_price * 1.01:
            reasoning = f"Held {held_days}d with no meaningful favorable move (LTP {ltp} vs entry {position.avg_entry_price})."
            _write_exit_decision(db, position, "EMERGENCY_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, position.qty_open, "time_stop")
            else:
                logger.warning("REAL time-stop exit needed for %s but execution is not wired — Phase 3.", position.symbol)
            time_stops += 1
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
