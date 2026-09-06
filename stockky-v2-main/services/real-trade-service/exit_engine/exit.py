"""
exit_engine/exit.py

MARKET INTELLIGENCE APPLIED TO EXIT LOGIC (28-Aug-2026):
═════════════════════════════════════════════════════════
Nifty in correction (−7% in 6m). FIIs net short 1,97,792 contracts.
Midcap/Smallcap outperforming — DII buying provides a floor at 24,000.

What this means for exits:
  1. PROTECT PROFITS FASTER: in a choppy/weak market, open gains evaporate
     quickly. Partial exit raised to 60% (was 50%) at first target — lock
     in more when you have it. The remaining 40% still rides the trail.

  2. AGE-AWARE TRAILING STOP: trail ATR multiplier tightens as trade ages.
     Day 0–3: 2.0×ATR (let the trade breathe, avoid noise-stop).
     Day 4–7: 1.5×ATR (original — standard phase).
     Day 8+:  1.0×ATR (very tight — protect accumulated profit).
     In a choppy market, a trade that hasn't hit target by day 8 is likely
     churning. Tighten the trail and be ready to exit.

  3. BREAKEVEN STOP: once unrealized gain ≥ 1×ATR, automatically move
     stop to entry price. This creates a "free ride" — if the trade
     reverses from here, we exit at breakeven, not a loss. Critical in a
     choppy market where moves can reverse sharply.

  4. SHORTER TIME-STOP: 10 days (unchanged from original). But now there's
     an EARLY WARNING at day 6: if still below entry, log a HOLD decision
     with a note. At day 10, if not profitable, exit — capital shouldn't
     sit dead when midcap/smallcap opportunities are turning over faster.

  5. GAP-DOWN EMERGENCY EXIT: if unrealized loss exceeds 1.5× the original
     stop distance (gap-through scenario), exit IMMEDIATELY regardless of
     current_stop level. In a weak market, gap-downs are common and a stop
     that was "breached but not hit" needs catching.

  6. TARGET NULLIFIED AFTER PARTIAL: after taking the first partial exit,
     current_target is set to None so the remainder is trailed indefinitely
     rather than re-triggering at a stale target price.

All REAL-mode Dhan placement, IP guard, audit trail unchanged from original.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import models
from audit.logger import log_action
from execution import dhan_client
from market_feed.feed import get_quotes
from notifier import notify_sync
from portfolio.portfolio import (
    close_position, open_positions, refresh_unrealized, record_real_exit_sent,
)
from tz_utils import as_aware

# §6 — corporate-action clamp for ATR trailing stop inputs
try:
    from return_sanity import clamp_for_atr as _clamp_for_atr
except ImportError:
    def _clamp_for_atr(x):
        return None if x is None or abs(x) > 30.0 else x

logger = logging.getLogger("real-trade-exit")

# ── Exit constants — market-intelligence tuned ────────────────────────────────
# Lock in 60% at first target (was 50%) — in choppy market, don't let
# profits turn into losses. The 40% remainder rides an ever-tightening trail.
PARTIAL_EXIT_FRACTION = float(os.getenv("EXIT_PARTIAL_FRACTION", "0.60"))

# Age → ATR multiplier mapping for trailing stop.
# Younger positions need more room; older ones should be protecting profit.
# Format: list of (max_days_inclusive, atr_multiplier).
TRAIL_ATR_SCHEDULE = [
    (3,  2.0),   # day 0–3:  2×ATR — let the trade breathe through noise
    (7,  1.5),   # day 4–7:  1.5×ATR — standard, same as original default
    (99, 1.0),   # day 8+:   1×ATR — tight, protect accumulated gains
]

# Breakeven stop: move stop to entry once unrealized gain >= this many ATRs.
BREAKEVEN_ATR_TRIGGER = float(os.getenv("EXIT_BREAKEVEN_ATR_TRIGGER", "1.0"))

# Emergency exit: fire if unrealized loss > this × original stop distance.
# Catches gap-down scenarios where price breaks through the stop level.
EMERGENCY_LOSS_MULT = float(os.getenv("EXIT_EMERGENCY_LOSS_MULT", "1.5"))

# Time-stop: max days to hold a non-performing position.
MAX_HOLD_DAYS = int(os.getenv("EXIT_MAX_HOLD_DAYS", "10"))
# Day at which we log an early warning (no exit yet, just visibility).
EARLY_WARN_DAYS = int(os.getenv("EXIT_EARLY_WARN_DAYS", "6"))


def _trail_atr_mult(held_days: int, schedule=None) -> float:
    """Return the ATR multiplier for trailing stop based on how long
    the position has been held. Tightens over time to protect profits.
    Accepts an optional schedule override (per-catalyst-horizon profile);
    falls back to the module-level TRAIL_ATR_SCHEDULE."""
    s = schedule if schedule is not None else TRAIL_ATR_SCHEDULE
    for max_days, mult in s:
        if held_days <= max_days:
            return mult
    return s[-1][1]


# ── Short-Term Trading Upgrade (2026-09-02): per-position exit profile ────────
# Positions opened from a WatchlistEntry carry watchlist_entry_id, which lets
# us look up the catalyst's horizon_class and apply a tighter or looser exit
# profile. Manual trades (watchlist_entry_id=None) fall back to the existing
# module-level constants above — NO behavior change for them.

def _load_profile(db: Session, position) -> dict:
    """
    Return the exit profile dict for this position.
    Keys match exit.py's module constants: trail_atr_schedule,
    breakeven_atr_trigger, max_hold_days, early_warn_days, partial_exit_fraction.
    """
    from watchlist_engine.decay import exit_profile_for

    if getattr(position, "watchlist_entry_id", None) is None:
        # Manual or pre-upgrade position — use existing global defaults.
        return {
            "trail_atr_schedule":    TRAIL_ATR_SCHEDULE,
            "breakeven_atr_trigger": BREAKEVEN_ATR_TRIGGER,
            "max_hold_days":         MAX_HOLD_DAYS,
            "early_warn_days":       EARLY_WARN_DAYS,
            "partial_exit_fraction": PARTIAL_EXIT_FRACTION,
            "horizon_class":         None,
        }
    try:
        row = db.query(models.WatchlistEntry).get(position.watchlist_entry_id)
        horizon_class = row.horizon_class if row else None
    except Exception:
        horizon_class = None
    profile = exit_profile_for(horizon_class)
    return {**profile, "horizon_class": horizon_class}


def _write_exit_decision(
    db: Session,
    position: models.TradePosition,
    action: str,
    reasoning: str,
    ltp: float,
) -> None:
    """Write an audit exit decision row. Every evaluation — including HOLD —
    gets logged per the plan's audit principle."""
    db.add(models.TradeExitDecision(
        position_id=position.id,
        action=action,
        reasoning=reasoning,
        ltp_at_decision=ltp,
    ))


def _has_pending_real_sell(db: Session, symbol: str) -> bool:
    """True if a REAL SELL for this symbol is already placed and awaiting
    broker confirmation — prevents double-selling before reconcile runs.
    Includes "PARTIAL" (Dhan PART_TRADED — reconcile.py) alongside
    "PLACED": a SELL that's only partially filled is still awaiting the
    rest of its own fill at the broker, so it must keep blocking a second
    SELL the exact same way an unfilled one does — otherwise this system
    could send another market SELL for the same remaining qty while
    Dhan's own order is still working."""
    return (
        db.query(models.TradeOrder)
        .filter(
            models.TradeOrder.mode == "REAL",
            models.TradeOrder.symbol == symbol,
            models.TradeOrder.side == "SELL",
            models.TradeOrder.status.in_(("PLACED", "PARTIAL")),
        )
        .first()
        is not None
    )


def _send_real_sell(
    db: Session,
    position: models.TradePosition,
    qty: int,
    reason: str,
    full: bool = True,
    execution_source: str = "AUTO",
    confirmed_by: Optional[str] = None,
) -> bool:
    """Place a MARKET SELL at Dhan for `qty` shares of an open REAL position.
    MARKET (not LIMIT) — an exit's purpose is capital protection; a limit
    sell that never fills defeats that.

    Returns True only if Dhan accepted and returned an order id.
    On failure the position is left untouched so exit_engine retries next cycle.
    execution_source/confirmed_by: set by manual_engine.py for human-initiated
    sells; left at AUTO defaults for all automatic exit logic."""
    try:
        security_id = dhan_client.get_security_id(db, position.symbol)
        result = dhan_client.place_order(
            db, is_armed=True,   # exits always allowed — never blocked by armed state
            security_id=security_id,
            exchange_segment=dhan_client.NSE_EQ_SEGMENT,
            transaction_type="SELL",
            quantity=qty,
            order_type="MARKET",
            price=0,
        )
        dhan_order_id = str(result.get("orderId") or result.get("order_id") or "")
        if not dhan_order_id:
            raise RuntimeError(
                f"Dhan accepted the SELL but returned no order id: {result}"
            )

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
        db.add(models.TradeOrderEvent(
            order_id=order.id, event_type="PLACED",
            detail=f"{reason}: MARKET SELL {qty} sent to Dhan",
        ))
        record_real_exit_sent(db, position, dhan_order_id, qty, reason, full=full)
        # ENRICHMENT (2026-09-02): previously just symbol + qty + reason, no
        # price at all. Now includes entry price, current stop/target, and
        # last-mark unrealized P&L so the Telegram alert is actionable on
        # its own without needing to open the dashboard.
        _stop_txt = f"₹{position.current_stop:.2f}" if position.current_stop is not None else "—"
        _target_txt = f"₹{position.current_target:.2f}" if position.current_target is not None else "—"
        notify_sync(
            f"📤 *SELL sent* — {position.symbol} ×{qty} ({reason})\n"
            f"Entry: ₹{position.avg_entry_price:.2f} | Stop: {_stop_txt} | Target: {_target_txt}\n"
            f"Unrealized P&L: ₹{position.unrealized_pnl:,.2f}\n"
            "Awaiting broker fill confirmation."
        )
        return True

    except Exception as e:
        logger.error("REAL exit SELL failed for %s (%s): %s", position.symbol, reason, e)
        if dhan_client.is_invalid_ip_error(str(e)):
            from auth.dhan_credentials import disarm_on_invalid_ip
            just_disarmed = disarm_on_invalid_ip(db, "REAL", str(e))
            notify_sync(
                (
                    f"🚨 *EXIT BLOCKED — IP not whitelisted* — "
                    f"{position.symbol} ×{qty} ({reason})\n"
                    "Dhan rejected this SELL. Position still open and exposed. "
                    "REAL auto-paused. Check GET /dhan/network-check."
                ) if just_disarmed else (
                    f"⚠️ *EXIT still blocked (IP)* — "
                    f"{position.symbol} ×{qty} ({reason}) — position remains open."
                )
            )
        else:
            notify_sync(
                f"⚠️ *SELL rejected by Dhan* — {position.symbol} ×{qty} ({reason})\n"
                f"{str(e)[:300]}"
            )
        return False


async def evaluate_mode(db: Session, mode: str) -> dict:
    """One evaluation cycle for every open position in `mode`.
    Checks in order: emergency_gap, stop_hit, target_hit, time_stop,
    breakeven_stop, trail_stop, hold.
    Returns a tally dict for logs and dashboard."""
    positions = open_positions(db, mode)
    if not positions:
        return {
            "evaluated": 0, "held": 0, "trailed": 0,
            "partial_exits": 0, "full_exits": 0,
            "time_stops": 0, "emergency_exits": 0,
        }

    symbols = list({p.symbol for p in positions})
    ticks   = await get_quotes(symbols)

    # Mark-to-market all DEMO positions even on cycles where we don't act —
    # the dashboard should always show current unrealized P&L.
    if mode == "DEMO":
        refresh_unrealized(db, mode, ticks)

    held = trailed = partial_exits = full_exits = time_stops = emergency_exits = 0
    now  = datetime.now(timezone.utc)

    for idx, position in enumerate(positions):
        try:
            import pipeline_status as pstat
            pstat.set_symbol_progress(mode, position.symbol, idx, len(positions))
        except Exception:
            pass

        tick = ticks.get(position.symbol)
        if tick is None:
            _write_exit_decision(
                db, position, "HOLD",
                "No current price available this cycle — skipping evaluation.", 0.0,
            )
            held += 1
            continue

        ltp = tick.price

        # REAL: if a SELL is already in-flight, don't re-evaluate until
        # reconcile confirms or rejects it. Prevents double-selling.
        if mode == "REAL" and _has_pending_real_sell(db, position.symbol):
            _write_exit_decision(
                db, position, "HOLD",
                "Exit already sent to Dhan — awaiting fill confirmation.", ltp,
            )
            held += 1
            continue

        held_days = (now - as_aware(position.opened_at)).days

        # Short-Term Trading Upgrade (2026-09-02): load per-position exit
        # profile. For watchlist-sourced positions this uses the catalyst's
        # horizon_class; for manual/pre-upgrade positions it returns the
        # existing global constants — zero behavior change for those.
        _prof = _load_profile(db, position)
        _trail_schedule  = _prof["trail_atr_schedule"]
        _be_trigger      = _prof["breakeven_atr_trigger"]
        _max_hold        = _prof["max_hold_days"]
        _early_warn      = _prof["early_warn_days"]
        _partial_frac    = _prof["partial_exit_fraction"]
        _horizon         = _prof["horizon_class"]  # for audit trail

        # ── 0. Emergency gap-down exit ────────────────────────────────────────
        # In a weak market (Aug-2026), gap-downs are common. If unrealized loss
        # exceeds EMERGENCY_LOSS_MULT × original stop distance, the stop has
        # been gapped through — exit immediately regardless of current_stop level.
        #
        # 2026-09-01 fix: use position.initial_stop_distance (fixed once at
        # OPEN time) instead of re-deriving from current_stop every cycle.
        # current_stop moves via breakeven/ATR-trail, so the old approach
        # drifted: once trail tightens near LTP the threshold shrinks toward
        # zero (relabels an ordinary stop-hit as "EMERGENCY" — harmless but
        # confusing in the audit log), and once breakeven pushes current_stop
        # above entry the threshold GROWS (delays the emergency catch exactly
        # when there's the most unrealized profit at stake — the opposite of
        # the intent). Rows opened before this migration have no stored
        # value, so they fall back to the previous approximation.
        original_risk = position.initial_stop_distance
        if original_risk is None:
            original_risk = abs(
                position.avg_entry_price - (position.current_stop or position.avg_entry_price)
            )
        unrealized_loss_per_share = position.avg_entry_price - ltp
        if (
            original_risk > 0
            and unrealized_loss_per_share > EMERGENCY_LOSS_MULT * original_risk
        ):
            reasoning = (
                f"EMERGENCY: price ₹{ltp:.2f} gapped {unrealized_loss_per_share:.2f} "
                f"below entry ₹{position.avg_entry_price:.2f} "
                f"({EMERGENCY_LOSS_MULT}× original stop distance ₹{original_risk:.2f}). "
                "Gap-down scenario — closing immediately to prevent further damage."
            )
            _write_exit_decision(db, position, "EMERGENCY_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, position.qty_open, "emergency_gap_down")
                emergency_exits += 1
            else:
                if _send_real_sell(db, position, position.qty_open, "emergency_gap_down"):
                    emergency_exits += 1
                else:
                    held += 1
            continue

        # ── 1. Stop hit — capital protection always checked first ─────────────
        if position.current_stop is not None and ltp <= position.current_stop:
            reasoning = (
                f"Stop ₹{position.current_stop:.2f} hit at LTP ₹{ltp:.2f}. "
                f"Closing full position ({position.qty_open} shares)."
            )
            _write_exit_decision(db, position, "FULL_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, position.qty_open, "stop_hit")
                full_exits += 1
            else:
                if _send_real_sell(db, position, position.qty_open, "stop_hit"):
                    full_exits += 1
                else:
                    held += 1
            continue

        # ── 2. First target hit — partial exit (60%) ──────────────────────────
        if (
            position.current_target is not None
            and ltp >= position.current_target
            and position.status == "OPEN"
        ):
            qty_to_close = max(1, int(position.qty_open * _partial_frac))
            pct_locked   = qty_to_close / position.qty_open * 100
            reasoning = (
                f"Target ₹{position.current_target:.2f} hit at LTP ₹{ltp:.2f}. "
                f"Locking in {qty_to_close} shares ({pct_locked:.0f}% of position). "
                f"Remainder trailed — stop moved to breakeven ₹{position.avg_entry_price:.2f}."
            )
            _write_exit_decision(db, position, "PARTIAL_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, qty_to_close, "target_hit_partial")
                if position.qty_open > 0:
                    # Raise stop to breakeven on the remainder so the rest
                    # is now a "free trade" — worst case exits at entry price.
                    position.current_stop   = max(
                        position.current_stop or 0, position.avg_entry_price
                    )
                    # Nullify target — remainder is now trailed, not held to a
                    # stale fixed target that could be hit again for a second
                    # unintended partial exit.
                    position.current_target = None
                    db.add(models.TradePositionEvent(
                        position_id=position.id, event_type="PARTIAL_EXIT_TRAIL",
                        detail=(
                            f"Stop raised to breakeven ₹{position.avg_entry_price:.2f}, "
                            "target nullified — remainder now on ATR trail."
                        ),
                    ))
                    db.commit()
                partial_exits += 1
            else:
                if _send_real_sell(
                    db, position, qty_to_close, "target_hit_partial", full=False
                ):
                    partial_exits += 1
                    # FIX: nullify target + raise stop to breakeven for REAL too.
                    # Without this, the next cycle sees ltp >= target again and
                    # fires another partial sell on the already-reduced position.
                    position.current_stop   = max(
                        position.current_stop or 0, position.avg_entry_price
                    )
                    position.current_target = None
                    db.add(models.TradePositionEvent(
                        position_id=position.id, event_type="PARTIAL_EXIT_TRAIL",
                        detail=(
                            f"REAL partial sent to Dhan. Stop raised to breakeven "
                            f"₹{position.avg_entry_price:.2f}, target nullified — "
                            "remainder now on ATR trail."
                        ),
                    ))
                    db.commit()
                else:
                    held += 1
            continue

        # ── 3. Time stop (with early warning at EARLY_WARN_DAYS) ─────────────
        # In a choppy market, a non-performing position after MAX_HOLD_DAYS
        # is tying up capital that could be in outperforming midcaps/PSU banks.
        if held_days >= _max_hold and ltp <= position.avg_entry_price * 1.01:
            reasoning = (
                f"Time-stop: held {held_days} days with no meaningful favorable move "
                f"(LTP ₹{ltp:.2f} vs entry ₹{position.avg_entry_price:.2f}). "
                f"Capital freed for better-performing setups."
                f" [horizon={_horizon or 'manual'}]"
            )
            # BUG FIX (2026-09-01): this was logging action="EMERGENCY_EXIT" —
            # copy-pasted from the gap-down branch above. A time-stop close is
            # a full position exit, not the gap-through emergency case (that
            # branch, and its own "emergency_exits" tally counter, are above
            # and untouched). Using "EMERGENCY_EXIT" here mislabeled every
            # time-stop close in the audit trail/dashboard decision history —
            # reasoning correctly said "Time-stop:..." but the action field
            # said EMERGENCY_EXIT, and the counters (time_stops vs
            # emergency_exits) already disagreed with what got written to
            # TradeExitDecision.action. "FULL_EXIT" matches models.py's
            # documented action taxonomy and the stop-hit branch below, which
            # uses the same label for the same kind of event (full close).
            _write_exit_decision(db, position, "FULL_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, position.qty_open, "time_stop")
                time_stops += 1
            else:
                if _send_real_sell(db, position, position.qty_open, "time_stop"):
                    time_stops += 1
                else:
                    held += 1
            continue

        # Early warning (no exit — just visibility for the dashboard)
        if held_days == _early_warn and ltp <= position.avg_entry_price:
            _write_exit_decision(
                db, position, "HOLD",
                f"Day {held_days} review: LTP ₹{ltp:.2f} still at/below entry "
                f"₹{position.avg_entry_price:.2f}. "
                f"Time-stop fires in {_max_hold - held_days} more days if no move."
                f" [horizon={_horizon or 'manual'}]",
                ltp,
            )
            held += 1
            continue

        # ── 4. Breakeven stop (once gain ≥ _be_trigger × ATR) ────────────────
        # Creates a free-ride floor: once the trade is meaningfully in profit
        # (defined as 1×ATR gain), we protect that by moving stop to entry.
        # Even if price reverses from here, we exit at breakeven, not a loss.
        if tick.atr and ltp > position.avg_entry_price:
            gain_per_share = ltp - position.avg_entry_price
            if gain_per_share >= _be_trigger * tick.atr:
                be_level = position.avg_entry_price
                if position.current_stop is None or position.current_stop < be_level:
                    old_stop = position.current_stop
                    position.current_stop = be_level
                    db.add(models.TradePositionEvent(
                        position_id=position.id, event_type="BREAKEVEN_STOP",
                        detail=(
                            f"Stop raised to breakeven ₹{be_level:.2f} "
                            f"(was ₹{old_stop}) — gain ₹{gain_per_share:.2f} "
                            f"≥ {_be_trigger}×ATR ₹{tick.atr:.2f}. "
                            f"Trade is now a free ride. [horizon={_horizon or 'manual'}]"
                        ),
                    ))
                    db.commit()
                    _write_exit_decision(
                        db, position, "TRAIL_STOP",
                        f"Breakeven stop set at ₹{be_level:.2f} — "
                        f"gain ₹{gain_per_share:.2f} ≥ {_be_trigger}×ATR. "
                        f"Trade is now risk-free. [horizon={_horizon or 'manual'}]",
                        ltp,
                    )
                    trailed += 1
                    continue

        # ── 5. Age-aware ATR trailing stop ────────────────────────────────────
        # Only trail when price is above entry (never trail a losing position —
        # that would loosen the stop, which is wrong).
        # ATR multiplier tightens as trade ages to protect accumulated profit.
        if tick.atr and ltp > position.avg_entry_price:
            trail_mult   = _trail_atr_mult(held_days, schedule=_trail_schedule)
            raw_atr_pct  = tick.atr / ltp * 100.0
            # §6 — clamp: if today's ATR looks like a corporate-action jump, skip
            # the trail update entirely this cycle rather than using a distorted ATR.
            atr_pct = _clamp_for_atr(raw_atr_pct)
            if atr_pct is None:
                # Corporate-action day — don't trail on bad data, just hold current stop
                _write_exit_decision(
                    db, position, "HOLD",
                    f"ATR clamped (corporate-action suspected, raw {raw_atr_pct:.1f}%) "
                    "— trail skipped this cycle to avoid distorted stop.",
                    ltp,
                )
                held += 1
                continue
            trail_candidate = round(ltp * (1 - (atr_pct * trail_mult) / 100.0), 2)

            # Only ever tighten (ratchet up), never loosen the stop.
            if position.current_stop is None or trail_candidate > position.current_stop:
                old_stop = position.current_stop
                position.current_stop = trail_candidate
                db.add(models.TradePositionEvent(
                    position_id=position.id, event_type="STOP_TRAILED",
                    detail=(
                        f"₹{old_stop} → ₹{trail_candidate} "
                        f"(LTP ₹{ltp}, day {held_days}, {trail_mult}×ATR "
                        f"= {atr_pct * trail_mult:.2f}%)"
                    ),
                ))
                db.commit()
                _write_exit_decision(
                    db, position, "TRAIL_STOP",
                    f"Stop trailed to ₹{trail_candidate:.2f} "
                    f"({trail_mult}×ATR, day {held_days} held).",
                    ltp,
                )
                trailed += 1
                continue

        # ── 6. Hold ───────────────────────────────────────────────────────────
        _write_exit_decision(
            db, position, "HOLD",
            f"No exit condition met at LTP ₹{ltp:.2f}. Monitoring.", ltp,
        )
        held += 1

    db.commit()
    tally = {
        "evaluated":      len(positions),
        "held":           held,
        "trailed":        trailed,
        "partial_exits":  partial_exits,
        "full_exits":     full_exits,
        "time_stops":     time_stops,
        "emergency_exits": emergency_exits,
    }
    log_action(
        db, actor="system", action="EXIT_CYCLE", mode=mode, detail=str(tally)
    )
    return tally
