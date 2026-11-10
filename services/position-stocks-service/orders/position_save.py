"""orders/position_save.py - E332: save the position row of an order that is ALREADY LIVE at the broker, with a bounded retry.

WHY: in attempt_entry, attempt_manual_entry and short_live the order call has already succeeded when the local ScalpPosition row is written. A
single failed commit (Oracle blip, lost connection, lock timeout) left a live position at Dhan with no row: nothing managed its stop or its
square-off. The short path alerted but never retried; the two long paths had no handling at all (the exception escaped with the symbol claim and
the capital still held, the circuit breaker saw a crash, and the position was unmanaged).

WHAT: save_new_position() rolls back, waits a short backoff and writes the SAME row again, up to ENTRY_POSITION_SAVE_RETRIES times. It reports
True/False; it never raises. alert_unrecorded() is the one place that tells the operator when every try failed (capital and the symbol claim stay
held by the caller - the order is live, so freeing either would invite a second BUY/SELL of the same name).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

import config
import notifier

logger = logging.getLogger(__name__)


def _tries() -> int:
    try:
        return max(1, int(getattr(config, "ENTRY_POSITION_SAVE_RETRIES", 3)))
    except Exception:  # noqa: BLE001
        return 3


def _backoff() -> float:
    try:
        v = float(getattr(config, "ENTRY_POSITION_SAVE_BACKOFF_S", 0.3))
        return v if v == v and 0.0 <= v <= 5.0 else 0.3          # NaN / negative / absurd -> default
    except Exception:  # noqa: BLE001
        return 0.3


def save_new_position(db: Any, pos: Any, *, sleep: Optional[Callable[[float], None]] = None) -> bool:
    """add + commit `pos`, retrying after a rollback. True when the row is committed; False when every try failed. A failing refresh after a
    good commit is NOT a failed save (the row is there; its attributes simply reload on next access)."""
    pause = sleep or time.sleep
    n = _tries()
    last: Optional[BaseException] = None
    for i in range(n):
        try:
            db.add(pos)
            db.commit()
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("position save: try %d/%d failed (%s)", i + 1, n, e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                logger.warning("position save: rollback after failed try %d also failed", i + 1, exc_info=True)
            if i + 1 < n:
                try:
                    pause(_backoff() * (i + 1))
                except Exception as pe:  # noqa: BLE001
                    logger.debug("position save: backoff sleep failed (%s)", pe)
            continue
        try:
            db.refresh(pos)
        except Exception as e:  # noqa: BLE001
            logger.warning("position save: committed but refresh failed (%s) - row is saved", e)
        if i:
            logger.warning("position save: succeeded on try %d/%d", i + 1, n)
        return True
    logger.error("position save: all %d tries failed (last error: %s)", n, last)
    return False


def alert_unrecorded(kind: str, symbol: str, qty: Any, order_id: Any, detail: str = "") -> None:
    """The order is live but no row exists. Loud log + Telegram; never raises."""
    logger.critical("position-stocks %s: order %s for %s x%s WAS PLACED but the position row could not be saved after retries (%s). Capital and the "
                    "symbol claim are kept. Check Dhan and close it by hand.", kind, order_id, symbol, qty, detail)
    try:
        notifier.notify_fire_and_forget(
            f"🚨 <b>{kind} placed but NOT recorded</b> - {symbol} x{qty}\nOrder {order_id}. "
            "The position row could not be saved after retries, so nothing manages its stop or square-off. Check Dhan now and close it by hand.")
    except Exception as ne:  # noqa: BLE001
        logger.warning("position save: alert failed (%s)", ne)
