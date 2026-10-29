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

  * eDIS backoff: an eDIS / TPIN refusal cannot be fixed by retrying (the TPIN is typed by a human on CDSL's page), so that symbol
    is not sent to Dhan again for BROKER_STOP_EDIS_BACKOFF_S seconds, then tried once more (so it self-heals after the user
    completes 'Verify Holdings'). Without this the same two doomed orders were re-sent every sync pass.
  * SL-M learning: Dhan rejects STOP_LOSS_MARKET on equity with 'Trigger Price should be greater than Price'. Once the STOP_LOSS
    retry gets past that validation, later placements in this process go straight to STOP_LOSS (one Dhan call instead of two).

NOT verified against a live Dhan account. Try it with ONE small position first and read /orders and Dhan's order book.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

import config
import models
import notifier
from execution import dhan_client
from tz_utils import as_aware, ist_today_str

logger = logging.getLogger("real-trade-broker-stop")

BROKER_STOP_REASON = "broker_stop"
_last_sync: dict = {}          # mode -> monotonic time of the last pass
_cancel_fails: dict = {}       # symbol -> consecutive cancel failures
_warned: dict = {}             # symbol -> monotonic time of the last placement warning
_edis_until: dict = {}         # symbol -> clock time before which no placement is attempted (eDIS / TPIN refusal backoff)
_slm_rejected: bool = False    # True once Dhan is known to reject STOP_LOSS_MARKET here (go straight to STOP_LOSS)


def enabled() -> bool:
    return bool(getattr(config, "BROKER_STOP_ENABLED", False))


def swing_only() -> bool:
    """E132: SWING_BROKER_STOP_ENABLED while BROKER_STOP_ENABLED is off = a resting stop for swing positions ONLY."""
    return bool(getattr(config, "SWING_BROKER_STOP_ENABLED", False)) and not enabled()


def boot_safety_message(enabled: bool, store_persistent: Optional[bool]) -> Optional[str]:
    """A2: text for the boot-time Telegram alert, or None when there is nothing to say. Pure (no I/O) so it can be tested.
    `enabled` is the running BROKER_STOP_ENABLED; `store_persistent` is whether switch overrides are saved to disk (None = unknown).
    A non-persistent store matters because a switch changed on the calibration page would silently revert on the next restart."""
    lines = []
    if not enabled:
        lines.append("BROKER_STOP_ENABLED is OFF: REAL positions have no resting broker stop, only this service's software exit loop. "
                     "If the container dies or loses network, nothing stops a loss. Set it back to true (calibration page or .env).")
    if store_persistent is False:
        lines.append("Switch overrides are NOT saved to disk (the control volume is missing or read-only): any switch changed on the "
                     "calibration page will revert to .env after a restart.")
    if not lines:
        return None
    return "\u26a0\ufe0f *Safety check at boot - real-trade*\n" + "\n".join(lines)


def reset_state() -> None:
    global _slm_rejected
    _last_sync.clear(); _cancel_fails.clear(); _warned.clear(); _edis_until.clear()
    _slm_rejected = False


def _edis_backoff_s() -> float:
    try:
        return max(0.0, float(getattr(config, "BROKER_STOP_EDIS_BACKOFF_S", 900) or 0))
    except Exception:  # noqa: BLE001
        return 900.0


def edis_backoff_left(symbol: str, now: Optional[float] = None) -> float:
    """Seconds left before a placement may be retried for `symbol` after an eDIS / TPIN refusal (0 = free to try)."""
    until = _edis_until.get(symbol)
    if until is None:
        return 0.0
    left = until - (time.monotonic() if now is None else now)
    if left <= 0:
        _edis_until.pop(symbol, None)
        return 0.0
    return left


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
        if not getattr(config, "BROKER_STOP_INTRADAY", False):   # group479: opt-in resting stop for a same-day MIS position
            return "intraday"
        return None
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
        trig = dhan_client.round_stop_to_tick(float(stop) * (1.0 - float(config.BROKER_STOP_BUFFER_PCT) / 100.0))
        if not trig or trig <= 0 or not ltp or float(ltp) <= 0 or trig >= float(ltp):
            return None
        return trig
    except Exception:  # noqa: BLE001
        return None


def _warn_once(symbol: str, msg: str, *args) -> None:
    """Log a placement failure at most once per BROKER_STOP_ALERT_COOLDOWN_S per symbol and, on that same cadence, send a
    Telegram alert: a held position with no resting broker stop is protected by the software stop alone (group384)."""
    cd = float(getattr(config, "BROKER_STOP_ALERT_COOLDOWN_S", 900) or 0)
    now = time.monotonic()
    if cd > 0 and now - _warned.get(symbol, -1e12) < cd:
        return
    _warned[symbol] = now
    logger.warning(msg, *args)
    try:
        notifier.notify_fire_and_forget("\u26a0\ufe0f " + (msg % args if args else msg) + " - the position has only its software stop.")
    except Exception:  # noqa: BLE001
        logger.debug("broker stop: alert failed", exc_info=True)


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


def _is_tick_size_error(msg: str) -> bool:
    """Exchange rejection EXCH:16283 'The order price is not multiple of the tick size'. Retrying the same price can never succeed
    (group 412: a Railtel stop was re-sent 86 times), so the caller backs the symbol off instead of re-sending every pass."""
    m = (msg or "").lower()
    return "16283" in m or ("tick" in m and "multiple" in m)


def _is_trigger_price_error(msg: str) -> bool:
    """Dhan's 'Trigger Price should be greater than Price' rejection (a SELL stop whose limit price is not below its trigger)."""
    m = (msg or "").lower()
    return "trigger price" in m and "greater than price" in m


def limit_price_below(trigger: float, pct: float = 0.5) -> float:
    """Tick-rounded limit price STRICTLY below `trigger` (Dhan needs trigger > price on a SELL stop-limit). `pct` is the gap
    in percent; at least one tick is always kept so a low-priced stock can never round back up onto the trigger."""
    tick = dhan_client.stop_tick_for_price(trigger) or 0.05
    px = dhan_client.round_stop_to_tick(trigger * (1.0 - pct / 100.0))
    if not px or px >= trigger:
        px = dhan_client.round_stop_to_tick(trigger - tick)
    return px


def _not_in_demat_holdings(db: Session, symbol: str) -> bool:
    """E256: True only when Dhan's demat holdings were read successfully and do NOT list `symbol` (with a quantity).
    "Insufficient Holding Quantity" is what Dhan also says for a same-day CNC delivery buy that has not settled into demat yet (T+1,
    still under Dhan *positions*): a TPIN cannot fix that, so the alert must not send the user to Verify Holdings. Any read error
    returns False (keeps the original eDIS wording)."""
    try:
        want = str(symbol or "").strip().upper()
        for row in dhan_client.get_holdings(db) or []:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("tradingSymbol") or row.get("trading_symbol") or row.get("symbol") or "").strip().upper()
            qty = row.get("totalQty", row.get("total_qty", row.get("quantity")))
            try:
                qty = float(qty) if qty is not None else 1.0
            except (TypeError, ValueError):
                qty = 1.0
            if sym == want and qty > 0:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _send_stop(db: Session, security_id: str, position, trigger: float, otype: str):
    price = limit_price_below(trigger, 0.5) if otype == "STOP_LOSS" else 0.0
    return dhan_client.place_order(
        db, is_armed=True, security_id=security_id, exchange_segment=dhan_client.NSE_EQ_SEGMENT,
        transaction_type="SELL", quantity=int(position.qty_open), order_type=otype, price=price,
        product_type=("INTRADAY" if getattr(position, "entry_product_type", None) in ("INTRADAY", "MIS") else "CNC"), tag="BSTOP", trigger_price=trigger,
    )


def _place(db: Session, position, trigger: float, now: Optional[float] = None) -> bool:
    global _slm_rejected
    try:
        security_id = dhan_client.get_security_id(db, position.symbol)
        otype = config.BROKER_STOP_ORDER_TYPE
        if otype == "STOP_LOSS_MARKET" and _slm_rejected:
            otype = "STOP_LOSS"          # learned earlier in this process: Dhan rejects SL-M here
        try:
            res = _send_stop(db, security_id, position, trigger, otype)
        except Exception as first:  # noqa: BLE001
            # Dhan rejected the stop for its trigger/price relation (seen live: BLEL, BAJAJFINSV, AVADHSUGAR, ABDL). Retry ONCE as a
            # stop-limit whose limit price is strictly below the trigger; any other error is not retried. A STOP_LOSS that was
            # already a stop-limit is not re-sent unchanged.
            if otype == "STOP_LOSS" or not _is_trigger_price_error(str(first)):
                raise
            logger.info("broker stop for %s: Dhan rejected %s (%s) - retrying as STOP_LOSS with a limit below the trigger",
                        position.symbol, otype, str(first)[:120])
            otype = "STOP_LOSS"
            try:
                res = _send_stop(db, security_id, position, trigger, otype)
            except Exception as second:  # noqa: BLE001
                if not _is_trigger_price_error(str(second)):
                    _slm_rejected = True  # the stop-limit got past trigger validation (e.g. eDIS refusal): SL-M is the broken one
                raise
            _slm_rejected = True
        oid = str((res or {}).get("orderId") or (res or {}).get("order_id") or "")
        if not oid:
            raise RuntimeError(f"Dhan accepted the stop but returned no order id: {res}")
        db.add(models.TradeOrder(
            mode="REAL", symbol=position.symbol, side="SELL", order_type=otype, qty=int(position.qty_open),
            status="PLACED", dhan_order_id=oid, limit_price=trigger, execution_source="AUTO",
            exit_reason=BROKER_STOP_REASON,
        ))
        db.commit()
        _edis_until.pop(position.symbol, None)
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
            backoff = _edis_backoff_s()
            if backoff > 0:
                _edis_until[position.symbol] = (time.monotonic() if now is None else now) + backoff
            retry = "every %d min" % (max(1, int(backoff // 60))) if backoff > 0 else "on every sync pass"
            if _not_in_demat_holdings(db, position.symbol):
                _warn_once(position.symbol, "broker stop for %s deferred: this is a same-day delivery (CNC) buy that has not settled "
                           "into your demat holdings yet (T+1), so Dhan cannot take a sell stop on it (%s). No TPIN is needed; "
                           "placement is retried automatically %s and should succeed once it settles",
                           position.symbol, msg[:160], retry)
            else:
                _warn_once(position.symbol, "broker stop for %s refused: CDSL eDIS / TPIN authorisation is needed before Dhan "
                           "accepts a sell order on this holding (%s). Open the Dhan app -> Verify Holdings and enter the TPIN; "
                           "placement is retried automatically %s", position.symbol, msg[:160], retry)
        elif _is_tick_size_error(msg):
            # group 412: a deterministic exchange rejection - the same trigger would be refused again. Back off (same window as
            # eDIS) instead of sending a doomed order every sync pass; the price is re-derived (and re-rounded) on the next try.
            backoff = _edis_backoff_s()
            if backoff > 0:
                _edis_until[position.symbol] = (time.monotonic() if now is None else now) + backoff
            _warn_once(position.symbol, "broker stop for %s rejected for tick size (trigger %.2f): %s - not retried for %d s",
                       position.symbol, trigger, msg[:160], int(backoff))
        else:
            _warn_once(position.symbol, "broker stop for %s could not be placed: %s", position.symbol, msg[:200])
        return False


async def sync_stops(db: Session, mode: str, positions, ticks: dict, now_mono: Optional[float] = None) -> dict:
    """One pass: make sure every eligible held position has a resting stop at the right trigger. Throttled to once per
    BROKER_STOP_SYNC_SECONDS. Never raises."""
    tally = {"checked": 0, "placed": 0, "replaced": 0, "kept": 0, "skipped": 0, "failed": 0, "backoff": 0}
    try:
        if mode != "REAL" or not (enabled() or swing_only()):
            return tally
        now = time.monotonic() if now_mono is None else now_mono
        if now - _last_sync.get("REAL", -1e12) < float(config.BROKER_STOP_SYNC_SECONDS):
            return tally
        _last_sync["REAL"] = now
        today = ist_today_str()
        held = {p.symbol for p in positions}
        for sym in [x for x in _edis_until if x not in held]:
            _edis_until.pop(sym, None)       # position gone: forget its backoff
        for pos in positions:
            tally["checked"] += 1
            if swing_only() and (getattr(pos, "entry_decision_label", None) or "").upper() != "SWING_MOMENTUM":
                tally["skipped"] += 1
                continue
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
                if _place(db, pos, max(trig, cur) if same_qty else trig, now):
                    tally["replaced"] += 1
                else:
                    tally["failed"] += 1
                continue
            if edis_backoff_left(pos.symbol, now) > 0:
                tally["failed"] += 1         # still no resting stop (software stop only) - but Dhan is not called again yet
                tally["backoff"] += 1
                continue
            if _place(db, pos, trig, now):
                tally["placed"] += 1
            else:
                tally["failed"] += 1
    except Exception as e:  # noqa: BLE001
        logger.error("broker stop sync failed: %s", e)
    return tally
