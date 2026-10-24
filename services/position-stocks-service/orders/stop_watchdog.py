"""orders/stop_watchdog.py - close a position whose own stop did not fire (group 340, 2026-10-12).

WHY: FEDDERSHOL's stop had been trailed to Rs 58.85 and the trade still ended at Rs 58.41. Nothing watched for a price sitting UNDER an
OPEN position's stored stop, so a Super Order stop leg that Dhan rejected, ignored or never applied left the position open until a time
rule happened to fire.

WHAT: for every OPEN, non-CNC position with a Super Order, when the live price (a fresh tick, not older than STOP_WATCHDOG_MAX_TICK_AGE_S)
stays at least STOP_WATCHDOG_MIN_BREACH_TICKS ticks under the stored stop for STOP_WATCHDOG_GRACE_S seconds, the broker stop did not do its
job: close at market through eod_squareoff.close_position_now() (same cancel-wait-sell + PENDING_RECONCILE path as manual and stagnation
exits, with its PositionAlreadyFlat guard against a bracket leg that did fill) and store status STOP_BREACH_EXIT. A price back above the stop
resets the clock; a stale or missing tick neither starts nor continues it. A rejected exit backs that position off for STOP_WATCHDOG_RETRY_S.
Runs from main.py's fast reconcile loop right after the trailing stop. Fails open per position. STOP_WATCHDOG_ENABLED=0 turns it off.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

import config
from execution import dhan_client
from models import ScalpPosition
from orders import side as _side

logger = logging.getLogger("position-stocks-stop-watchdog")

STATUS = "STOP_BREACH_EXIT"
_breach_since: dict = {}     # position id -> monotonic time the price was first seen under the stop
_next_attempt: dict = {}     # position id -> monotonic time before which no new exit is attempted


def reset_state() -> None:
    """Test helper."""
    _breach_since.clear()
    _next_attempt.clear()


def _last_tick(symbol: str):
    """(ltp, age_seconds) of the newest tick, or (None, None)."""
    try:
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        if not buf:
            return None, None
        ts, ltp = buf[-1][0], buf[-1][1]
        return (float(ltp) if ltp and ltp > 0 else None), max(0.0, time.time() - float(ts))
    except Exception:  # noqa: BLE001
        return None, None


def is_breached(ltp: Optional[float], stop: Optional[float], short: bool = False) -> bool:
    """True when `ltp` is at least STOP_WATCHDOG_MIN_BREACH_TICKS ticks under `stop` (a SHORT's stop sits ABOVE
    entry, so for a short: at least that many ticks OVER it). Pure."""
    try:
        if not ltp or not stop or ltp <= 0 or stop <= 0:
            return False
        ticks = max(1, int(config.STOP_WATCHDOG_MIN_BREACH_TICKS))
        if short:
            return ltp >= stop + dhan_client.tick_size_for_price(stop) * ticks - 1e-9
        return ltp <= stop - dhan_client.tick_size_for_price(stop) * ticks + 1e-9
    except Exception:  # noqa: BLE001
        return False


def run_stop_watchdog(db: Session) -> int:
    """Returns the number of positions closed this pass."""
    if not getattr(config, "STOP_WATCHDOG_ENABLED", True) or not config.USE_SUPER_ORDER:
        return 0
    from orders import eod_squareoff
    positions = (
        db.query(ScalpPosition)
        .filter(ScalpPosition.status == "OPEN")
        .filter(ScalpPosition.dhan_super_order_id.isnot(None))
        .all()
    )
    live_ids = {p.id for p in positions}
    for stale in [k for k in _breach_since if k not in live_ids]:
        _breach_since.pop(stale, None)
    closed = 0
    now = time.monotonic()
    max_age = float(getattr(config, "STOP_WATCHDOG_MAX_TICK_AGE_S", 15.0))
    retry_s = float(getattr(config, "STOP_WATCHDOG_RETRY_S", 30.0))
    for pos in positions:
        try:
            if pos.overnight_converted_to_cnc or not pos.stop_price:
                continue
            ltp, age = _last_tick(pos.symbol)
            if ltp is None or age is None or age > max_age:
                _breach_since.pop(pos.id, None)
                continue
            if not is_breached(ltp, pos.stop_price, short=_side.is_short(pos)):
                _breach_since.pop(pos.id, None)
                continue
            first = _breach_since.setdefault(pos.id, now)
            held = now - first
            if held < config.STOP_WATCHDOG_GRACE_S or now < _next_attempt.get(pos.id, 0.0):
                continue
            logger.critical(
                "STOP_WATCHDOG: %s (id=%d) price ₹%.2f has been under its stop ₹%.2f for %.0fs and the position is still open - closing at market",
                pos.symbol, pos.id, ltp, pos.stop_price, held,
            )
            stop_at_breach = pos.stop_price
            try:
                eod_squareoff.close_position_now(db, pos, exit_reason=STATUS)
            except Exception as e:  # noqa: BLE001  (ManualCloseRejected, cooldown, broker error ...)
                db.rollback()
                _next_attempt[pos.id] = now + retry_s
                logger.warning("STOP_WATCHDOG: %s (id=%d) exit not placed (%s) - retry in %.0fs", pos.symbol, pos.id, e, retry_s)
                continue
            closed += 1
            _breach_since.pop(pos.id, None)
            try:
                import notifier
                notifier.notify_fire_and_forget(
                    f"🚨 <b>Stop did not fire</b> - {pos.symbol} x{pos.quantity}\n"
                    f"Price ₹{ltp:.2f} sat under the stop ₹{stop_at_breach:.2f} for {held:.0f}s. Closed at market (STOP_BREACH_EXIT)."
                )
            except Exception as ne:  # noqa: BLE001
                logger.warning("STOP_WATCHDOG: %s closed but alert failed: %s", pos.symbol, ne)
        except Exception as e:  # noqa: BLE001
            try:
                db.rollback()
            except Exception as rb:  # noqa: BLE001
                logger.warning("STOP_WATCHDOG: %s rollback after error also failed: %s", pos.symbol, rb)
            logger.error("STOP_WATCHDOG: %s (id=%s) unexpected error: %s", pos.symbol, pos.id, e, exc_info=True)
    return closed
