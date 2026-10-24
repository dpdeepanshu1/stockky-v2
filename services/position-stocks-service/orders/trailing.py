"""orders/trailing.py - ratchet a scalp's STOP_LOSS_LEG up behind the peak price (group 217, 2026-10-07).

WHY: a scalp's Super Order bracket was fixed (target about 1.4-3.5%, stop 0.8-2.0%) and the one-time
breakeven move is a DB toggle that is OFF by default. A trade that ran +1.5% and reversed gave the gain back
and often ended at the stop, which is why average wins were small next to average losses.

WHAT: for every OPEN position that has a Super Order, once its peak price is TRAIL_ACTIVATE_PCT above entry
the stop is moved to  peak * (1 - distance)  where
    distance = max(TRAIL_MIN_DISTANCE_PCT, adaptive_stop_pct * TRAIL_DISTANCE_STOP_FRACTION).
Rules, all enforced in compute_trailing_stop():
  * the stop only ever goes UP (and by at least TRAIL_MIN_STEP_PCT of entry, so Dhan is not spammed);
  * never below entry + BREAKEVEN_STOP_BUFFER_TICKS ticks once active (a trailed trade cannot end as a loss);
  * always strictly below the live price (Dhan rejects a BUY's stop at/above the market) - when the price
    has already fallen through the trail level the stop is put one tick under the market, i.e. exit now;
  * a stop at/above the target is not set (the target leg owns that exit);
  * the target leg is never touched.
Runs from main.py's fast-reconcile loop after excursion tracking (which keeps max_price_seen current).
Fails open per position: a missing tick or a Dhan rejection logs and moves on; a rejected modify backs that
position off for TRAIL_RETRY_BACKOFF_S. Sets stop_moved_to_breakeven=True on success so orders/breakeven.py
can never later LOWER a trailed stop back to entry. Off with TRAILING_STOP_ENABLED=0. Never places a new order.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

import config
from execution import dhan_client
from models import ScalpPosition
from orders import side as side_mod

logger = logging.getLogger("position-stocks-trailing")

# position id -> monotonic time before which no new modify is attempted (in-process only; lost on restart)
_next_attempt: dict = {}
_notified: set = set()


def reset_state() -> None:
    """Test helper: forget throttles and first-activation notices."""
    _next_attempt.clear()
    _notified.clear()


def compute_trailing_stop(
    *,
    entry: float,
    peak: float,
    ltp: float,
    current_stop: float,
    target: Optional[float],
    stop_pct: float,
    side: str = "LONG",
) -> Optional[float]:
    """The stop price to move to, or None when the stop should stay where it is. Pure function.
    side="SHORT" (short selling step 4) mirrors it: `peak` is then the LOWEST price seen (the trough), the stop trails ABOVE it and only ever
    goes DOWN, never above entry - buffer once active, always strictly above the live price, and never at/below the target."""
    try:
        if str(side or "LONG").strip().upper() == "SHORT":
            return _compute_trailing_stop_short(entry=entry, trough=peak, ltp=ltp, current_stop=current_stop, target=target, stop_pct=stop_pct)
        if not entry or entry <= 0 or not ltp or ltp <= 0 or not peak or peak <= entry:
            return None
        if (peak - entry) / entry * 100.0 < config.TRAIL_ACTIVATE_PCT:
            return None
        dist_pct = max(
            config.TRAIL_MIN_DISTANCE_PCT,
            float(stop_pct or 0.0) * config.TRAIL_DISTANCE_STOP_FRACTION,
        )
        cand = peak * (1.0 - dist_pct / 100.0)
        floor = entry + dhan_client.tick_size_for_price(entry) * config.BREAKEVEN_STOP_BUFFER_TICKS
        cand = dhan_client.round_to_tick(max(cand, floor))
        if cand >= ltp:
            cand = dhan_client.round_to_tick(ltp - dhan_client.tick_size_for_price(ltp))
        if cand <= 0:
            return None
        if target and cand >= target:
            return None
        min_step = max(
            dhan_client.tick_size_for_price(ltp),
            entry * config.TRAIL_MIN_STEP_PCT / 100.0,
        )
        if cand < (current_stop or 0.0) + min_step:
            return None
        return cand
    except Exception as e:  # never let a maths/config problem reach the loop
        logger.debug("trailing: compute failed: %s", e)
        return None


def _compute_trailing_stop_short(*, entry, trough, ltp, current_stop, target, stop_pct) -> Optional[float]:
    try:
        if not entry or entry <= 0 or not ltp or ltp <= 0 or not trough or trough <= 0 or trough >= entry:
            return None
        if (entry - trough) / entry * 100.0 < config.TRAIL_ACTIVATE_PCT:
            return None
        dist_pct = max(config.TRAIL_MIN_DISTANCE_PCT, float(stop_pct or 0.0) * config.TRAIL_DISTANCE_STOP_FRACTION)
        cand = trough * (1.0 + dist_pct / 100.0)
        ceiling = entry - dhan_client.tick_size_for_price(entry) * config.BREAKEVEN_STOP_BUFFER_TICKS   # a trailed short cannot end as a loss
        cand = dhan_client.round_to_tick(min(cand, ceiling))
        if cand <= ltp:                               # price already rose through the trail level: stop one tick over the market (cover now)
            cand = dhan_client.round_to_tick(ltp + dhan_client.tick_size_for_price(ltp))
        if cand <= 0:
            return None
        if target and cand <= target:
            return None
        min_step = max(dhan_client.tick_size_for_price(ltp), entry * config.TRAIL_MIN_STEP_PCT / 100.0)
        if current_stop and cand > current_stop - min_step:   # the stop only ever moves DOWN, by at least one step
            return None
        return cand
    except Exception as e:  # never let a maths/config problem reach the loop
        logger.debug("trailing: short compute failed: %s", e)
        return None


def run_trailing_stop(db: Session) -> int:
    """Returns the number of stops raised this pass."""
    if not config.TRAILING_STOP_ENABLED or not config.USE_SUPER_ORDER:
        return 0
    positions = (
        db.query(ScalpPosition)
        .filter(ScalpPosition.status == "OPEN")
        .filter(ScalpPosition.dhan_super_order_id.isnot(None))
        .all()
    )
    moved = 0
    for pos in positions:
        try:
            if pos.overnight_converted_to_cnc:
                continue  # carried CNC positions have their own protective stop (overnight_stop.py)
            _short = side_mod.is_short(pos)
            now = time.monotonic()
            if now < _next_attempt.get(pos.id, 0.0):
                continue
            try:
                from feed import ws_client
                buf = ws_client.get_tick_buffer(pos.symbol)
                ltp = buf[-1][1] if buf else None
            except Exception:
                ltp = None
            if ltp is None or ltp <= 0:
                continue
            if _short:
                peak = min(float(pos.min_price_seen or ltp), float(ltp))      # a short's best price is the LOWEST seen
            else:
                peak = max(float(pos.max_price_seen or 0.0), float(ltp))
            new_stop = compute_trailing_stop(
                entry=pos.entry_price, peak=peak, ltp=ltp, current_stop=pos.stop_price,
                target=pos.target_price, stop_pct=pos.adaptive_stop_pct, side="SHORT" if _short else "LONG",
            )
            if new_stop is None:
                continue
            _next_attempt[pos.id] = now + config.TRAIL_MIN_INTERVAL_S
            try:
                dhan_client.modify_super_order(
                    db, order_id=pos.dhan_super_order_id, order_leg="STOP_LOSS_LEG",
                    stop_loss_price=new_stop,
                )
            except Exception as e:
                _next_attempt[pos.id] = now + max(config.TRAIL_RETRY_BACKOFF_S, config.TRAIL_MIN_INTERVAL_S)
                logger.warning(
                    "TRAILING_STOP: %s (id=%d) modify to ₹%.2f rejected (%s) - leg unchanged, retry in %.0fs",
                    pos.symbol, pos.id, new_stop, e, config.TRAIL_RETRY_BACKOFF_S,
                )
                continue
            # group340: read the leg back; an accepted-but-unapplied modify must not be saved as a moved stop
            if dhan_client.verify_stop_leg(db, order_id=pos.dhan_super_order_id, expected_price=new_stop) is False:
                _next_attempt[pos.id] = now + max(config.TRAIL_RETRY_BACKOFF_S, config.TRAIL_MIN_INTERVAL_S)
                logger.warning("TRAILING_STOP: %s (id=%d) modify to ₹%.2f NOT confirmed by Dhan's order book - stored stop left at ₹%.2f",
                               pos.symbol, pos.id, new_stop, pos.stop_price)
                try:
                    import notifier
                    notifier.notify_fire_and_forget(
                        f"⚠️ <b>Stop move not confirmed</b> - {pos.symbol} x{pos.quantity}\n"
                        f"Wanted ₹{new_stop:.2f}, Dhan's leg does not show it. Stored stop stays ₹{pos.stop_price:.2f}.")
                except Exception as ne:  # noqa: BLE001
                    logger.warning("TRAILING_STOP: %s verify alert failed: %s", pos.symbol, ne)
                continue
            old_stop = pos.stop_price
            pos.stop_price = new_stop
            pos.stop_moved_to_breakeven = True
            db.commit()
            moved += 1
            gain_pct = side_mod.pnl_pct(pos, pos.entry_price, peak)
            locked_pct = side_mod.pnl_pct(pos, pos.entry_price, new_stop)
            logger.info(
                "TRAILING_STOP: %s (id=%d) stop ₹%.2f -> ₹%.2f (peak ₹%.2f = +%.2f%%, locks %+.2f%%, ltp ₹%.2f)",
                pos.symbol, pos.id, old_stop, new_stop, peak, gain_pct, locked_pct, ltp,
            )
            if pos.id not in _notified:
                _notified.add(pos.id)
                try:
                    import notifier
                    notifier.notify_fire_and_forget(
                        f"📈 <b>Trailing stop on</b> - {pos.symbol} x{pos.quantity}\n"
                        f"Stop ₹{old_stop:.2f} → ₹{new_stop:.2f} (entry ₹{pos.entry_price:.2f}, peak +{gain_pct:.2f}%)\n"
                        f"Locks {locked_pct:+.2f}% and keeps following the peak."
                    )
                except Exception as ne:
                    logger.warning("TRAILING_STOP: %s moved but notify failed: %s", pos.symbol, ne)
        except Exception as e:
            db.rollback()
            logger.error("TRAILING_STOP: %s (id=%s) unexpected error: %s", pos.symbol, pos.id, e, exc_info=True)
    return moved
