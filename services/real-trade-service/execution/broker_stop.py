"""Broker-side stop-loss for held CNC positions (group 322). OFF unless BROKER_STOP_ENABLED=true.

WHY: real-trade stops are software only (polled every ~8 s, then a market SELL). If this service is down, blind (no price)
or the network is cut, a CNC position held up to 10 days has no protection at all. A resting Dhan stop order is the safety net.

DESIGN (kept deliberately small, and subordinate to the software exits):
  * The stop is an ordinary TradeOrder row (side SELL, exit_reason == BROKER_STOP_REASON, limit_price = trigger), so no
    schema change and the existing reconcile loop books a fill like any SELL (it closes the position).
  * The trigger sits BROKER_STOP_BUFFER_PCT % below the software stop, so the software stop normally fires first.
  * Only positions held since an EARLIER day (settled in demat, product CNC) get one: a same-day CNC position cannot be sold
    at all until T+1 (CDSL), and an INTRADAY position is squared off the same day anyway.
  * It is never lowered, and replaced (cancel + place) only when the stop ratchets up by BROKER_STOP_MIN_MOVE_PCT % or the open
    quantity changed.
  * cancel_for_exit() runs inside exit_engine._send_real_sell before ANY software SELL: the stop must be gone (or confirmed
    traded) first. If Dhan says the stop already traded, the software SELL is skipped and reconcile books the fill. If the cancel
    keeps failing for another reason, after BROKER_STOP_CANCEL_MAX_FAILS tries the software SELL goes ahead anyway (a second SELL
    on a CNC holding that is already gone is rejected by Dhan as an oversell, which the exit engine already handles).
  * Everything here is best effort: nothing raises into the exit loop; failures are logged (placement failures at most once per
    BROKER_STOP_ALERT_COOLDOWN_S per symbol, and an eDIS / TPIN rejection is named as such - a CNC stop on holdings needs it).

NOT verified against a live Dhan account. Try it with ONE small position first and read /orders and Dhan's order book.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import config
import models
from execution import dhan_client
from tz_utils import as_aware, ist_today_str

logger = logging.getLogger("real-trade-broker-stop")

BROKER_STOP_REASON = "broker_stop"
_last_sync: dict = {}          # mode -> monotonic time of the last pass
_cancel_fails: dict = {}       # symbol -> consecutive cancel failures
_warned: dict = {}             # symbol -> monotonic time of the last placement warning


def enabled() -> bool:
    return bool(getattr(config, "BROKER_STOP_ENABLED", False))


def reset_state() -> None:
    _last_sync.clear(); _cancel_fails.clear(); _warned.clear()


def open_stop(db: Session, symbol: str) -> Optional[models.TradeOrder]:
    """The resting broker stop row for `symbol`, or None."""
    return (
        db.query(models.TradeOrder)
        .filter(
            models.TradeOrder.mode == "REAL",
            models.TradeOrder.symbol == symbol,
            models.TradeOrder.side == "SELL",
            models.TradeOrder.exit_reason == BROKER_STOP_REASON,
            models.TradeOrder.status.in_(("PLACED", "PARTIAL")),
        )
        .first()
    )


def skip_reason(position, today_ist: Optional[str] = None) -> Optional[str]:
    """Why this position gets no broker stop (None = eligible)."""
    if getattr(position, "status", None) not in ("OPEN", "PARTIALLY_CLOSED"):
        return "status"
    if not (position.qty_open or 0) > 0:
        return "no_qty"
    if not (position.current_stop or 0) > 0:
        return "no_stop"
    if position.entry_product_type in ("INTRADAY", "MIS"):
        return "intraday"
    if not position.broker_imported:
        today = today_ist or ist_today_str()
        try:
            opened = ist_today_str(as_aware(position.opened_at))
        except Exception:  # noqa: BLE001
            return "opened_at_unreadable"
        if opened == today:
            return "same_day_cdsl"
    return None


def trigger_for(stop: float, ltp: Optional[float]) -> Optional[float]:
    """Tick-rounded trigger = software stop less the buffer; None unless it is below the live price (a sell stop at or above
    the market would trigger at once / be rejected - the software emergency exit owns that case)."""
    try:
        trig = dhan_client.round_to_tick(float(stop) * (1.0 - float(config.BROKER_STOP_BUFFER_PCT) / 100.0))
        if not trig or trig <= 0 or not ltp or float(ltp) <= 0 or trig >= float(ltp):
            return None
        return trig
    except Exception:  # noqa: BLE001
        return None


def _warn_once(symbol: str, msg: str, *args) -> None:
    cd = float(getattr(config, "BROKER_STOP_ALERT_COOLDOWN_S", 900) or 0)
    now = time.monotonic()
    if cd > 0 and now - _warned.get(symbol, -1e12) < cd:
        return
    _warned[symbol] = now
    logger.warning(msg, *args)


def _broker_status(db: Session, order_id: str) -> Optional[str]:
    try:
        for r in dhan_client.get_order_list(db):
            if str(r.get("orderId") or r.get("order_id") or "") == str(order_id):
                return str(r.get("orderStatus") or r.get("order_status") or "").upper() or None
    except Exception as e:  # noqa: BLE001
        logger.debug("broker stop: order list read failed: %s", e)
    return None


def cancel_for_exit(db: Session, position) -> bool:
    """Called right before a software SELL. True = go ahead (no stop, or it is cancelled / dead). False = do NOT sell this
    cycle (the stop may have traded; reconcile will book it, or the cancel is being retried). Never raises."""
    try:
        stop = open_stop(db, position.symbol)
        if stop is None:
            return True
        try:
            dhan_client.cancel_order(db, is_armed=True, dhan_order_id=stop.dhan_order_id)
        except Exception as e:  # noqa: BLE001
            st = _broker_status(db, stop.dhan_order_id)
            if st in ("TRADED", "PART_TRADED", "PARTIALLY_TRADED"):
                logger.warning("broker stop %s for %s already traded at Dhan - software SELL skipped, reconcile books it",
                               stop.dhan_order_id, position.symbol)
                return False
            if st in ("CANCELLED", "CANCELED", "REJECTED", "EXPIRED"):
                stop.status = "CANCELLED" if st != "REJECTED" else "REJECTED"
                db.commit()
                _cancel_fails.pop(position.symbol, None)
                return True
            n = _cancel_fails.get(position.symbol, 0) + 1
            _cancel_fails[position.symbol] = n
            if n >= int(config.BROKER_STOP_CANCEL_MAX_FAILS):
                logger.error("broker stop %s for %s: cancel failed %d times (%s) - selling anyway; a second SELL on a "
                             "holding that is already gone is rejected by Dhan", stop.dhan_order_id, position.symbol, n, e)
                return True
            logger.warning("broker stop %s for %s: cancel failed (%s) - software SELL waits (try %d/%d)",
                           stop.dhan_order_id, position.symbol, e, n, config.BROKER_STOP_CANCEL_MAX_FAILS)
            return False
        stop.status = "CANCELLED"
        db.commit()
        _cancel_fails.pop(position.symbol, None)
        logger.info("broker stop %s for %s cancelled before the software SELL", stop.dhan_order_id, position.symbol)
        return True
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.error("broker stop: cancel_for_exit(%s) failed (%s) - software SELL goes ahead", position.symbol, e)
        return True


def _place(db: Session, position, trigger: float) -> bool:
    try:
        security_id = dhan_client.get_security_id(db, position.symbol)
        otype = config.BROKER_STOP_ORDER_TYPE
        price = dhan_client.round_to_tick(trigger * 0.995) if otype == "STOP_LOSS" else 0.0
        res = dhan_client.place_order(
            db, is_armed=True, security_id=security_id, exchange_segment=dhan_client.NSE_EQ_SEGMENT,
            transaction_type="SELL", quantity=int(position.qty_open), order_type=otype, price=price,
            product_type="CNC", tag="BSTOP", trigger_price=trigger,
        )
        oid = str((res or {}).get("orderId") or (res or {}).get("order_id") or "")
        if not oid:
            raise RuntimeError(f"Dhan accepted the stop but returned no order id: {res}")
        db.add(models.TradeOrder(
            mode="REAL", symbol=position.symbol, side="SELL", order_type=otype, qty=int(position.qty_open),
            status="PLACED", dhan_order_id=oid, limit_price=trigger, execution_source="AUTO",
            exit_reason=BROKER_STOP_REASON,
        ))
        db.commit()
        logger.info("broker stop placed: %s x%s trigger %.2f (software stop %.2f) order %s",
                    position.symbol, position.qty_open, trigger, position.current_stop, oid)
        return True
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        msg = str(e)
        if dhan_client.is_cdsl_edis_error(msg):
            _warn_once(position.symbol, "broker stop for %s refused: CDSL eDIS / TPIN authorisation is needed before Dhan "
                       "accepts a sell order on this holding (%s)", position.symbol, msg[:160])
        else:
            _warn_once(position.symbol, "broker stop for %s could not be placed: %s", position.symbol, msg[:200])
        return False


async def sync_stops(db: Session, mode: str, positions, ticks: dict, now_mono: Optional[float] = None) -> dict:
    """One pass: make sure every eligible held position has a resting stop at the right trigger. Throttled to once per
    BROKER_STOP_SYNC_SECONDS. Never raises."""
    tally = {"checked": 0, "placed": 0, "replaced": 0, "kept": 0, "skipped": 0, "failed": 0}
    try:
        if mode != "REAL" or not enabled():
            return tally
        now = time.monotonic() if now_mono is None else now_mono
        if now - _last_sync.get("REAL", -1e12) < float(config.BROKER_STOP_SYNC_SECONDS):
            return tally
        _last_sync["REAL"] = now
        today = ist_today_str()
        for pos in positions:
            tally["checked"] += 1
            if skip_reason(pos, today) is not None:
                tally["skipped"] += 1
                continue
            tick = ticks.get(pos.symbol)
            trig = trigger_for(pos.current_stop, getattr(tick, "price", None))
            if trig is None:
                tally["skipped"] += 1
                continue
            existing = open_stop(db, pos.symbol)
            if existing is not None:
                cur = float(existing.limit_price or 0)
                same_qty = int(existing.qty or 0) == int(pos.qty_open)
                moved_up = cur > 0 and trig > cur * (1.0 + float(config.BROKER_STOP_MIN_MOVE_PCT) / 100.0)
                if same_qty and not moved_up:
                    tally["kept"] += 1
                    continue
                if same_qty and cur > 0 and trig < cur:
                    tally["kept"] += 1          # never lower a stop
                    continue
                if not cancel_for_exit(db, pos):
                    tally["failed"] += 1
                    continue
                if _place(db, pos, max(trig, cur) if same_qty else trig):
                    tally["replaced"] += 1
                else:
                    tally["failed"] += 1
                continue
            if _place(db, pos, trig):
                tally["placed"] += 1
            else:
                tally["failed"] += 1
    except Exception as e:  # noqa: BLE001
        logger.error("broker stop sync failed: %s", e)
    return tally
