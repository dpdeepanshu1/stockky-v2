"""orders/scaleout.py - E285 round 164 (plan item 12): MOMENTUM_SCALEOUT, the half-sell exit. Position Stocks only, LONG Super Order trades only.

WHEN: orders/momentum_hook.py reads the five momentum signals. With MOMENTUM_SCALEOUT on, a LONG trade of MOMENTUM_SCALEOUT_MIN_QTY (4) shares or more that reaches the BOOK level (3+ signals, in
profit) is split instead of closed: half is sold at market and the other half stays under a new stop. Off by default: nothing here runs and the trade is closed in full, as before.

THE SEQUENCE (every step was proven by the ONGC half-sell probe on 3 Nov: unprotected window 1.7 s, goal 3 s):
  0  Dhan's positions must show at least the whole quantity long (never sell what is not there); a position that cannot be read closes in full as before
  1  cancel the Super Order's TARGET leg, then its STOP leg (the stop leg stays live after the target cancel, so the position is protected until step 2)
  2  both legs must read cancelled / gone and neither may have FILLED (a filled leg means the position is already exited: nothing is sent, reconcile books it)
  3  MARKET SELL of half (quantity // 2)
  4  AT ONCE a plain SELL stop for the other half (place_intraday_sell_stop: STOP_LOSS_MARKET, retried once as a stop-limit when Dhan asks for it), BEFORE any database write or fill poll, so the
     window between the stop leg and the new stop is only the order round trips
  5  the row is rewritten: Super Order id cleared (kept in flow_json["scaleout"]), plain_stop_order_id set, stop_price = the new stop, stamp written; then the sell's fill price is read and the half is
     booked through overnight_stop.book_delta (quantity, capital, realised P&L, ledger) and rolled into overnight_stop_prior_qty, the same accounting reconcile and the closers already understand

EVERY FAILURE HAS A FALLBACK, and none of them can leave shares without a stop for longer than one guard pass:
  * before the first cancel, or a leg that stays live / cannot be cancelled     -> nothing was sold: the caller closes the whole position the old way (fallback "FULL")
  * a leg already filled, or Dhan shows less than the whole quantity long       -> nothing is sent (fallback "NONE"): reconcile books what Dhan did
  * the SELL is refused                                                         -> nothing was sold: close in full ("FULL"); an unclear SELL error first looks for the order at Dhan
  * the stop cannot be placed (or the price is already under it)                -> the half is booked, a stray stop is cancelled, the rest is sold at market ("HALF_THEN_FULL")
  * the SELL is not confirmed within SCALEOUT_FILL_WAIT_S                       -> booked later by the guard (settle_sell) from the order book; no flat SELL is sent meanwhile
  * the SELL dies unfilled after it was accepted                                -> the stop is cancelled and the whole position is sold at market

AFTER THE SPLIT (run_scaleout_guard, every fast reconcile loop, right after the plain-short guard): the kept half is a "plain long": the broker stop filled -> STOP_HIT booked at its real fill with the
half already booked added; the stop died -> ONE re-place, then sold at market (STOP_BREACH_EXIT); price at or over the target -> the stop is cancelled and read back, then MARKET SELL (TARGET_EXIT); price
under the stop for SCALEOUT_STOP_GRACE_S while the broker stop has not fired -> MARKET SELL (STOP_BREACH_EXIT). Every other exit (EOD, manual, stagnation, momentum, close-all) goes through
eod_squareoff._fire_flat_sell, which calls settle_before_flat_sell and plain_short.release_stop_before_cover first, so a resting stop and a flat SELL can never both exist.
A position is split at most once.

Never raises into the caller's loop. Nothing here is reached unless MOMENTUM_SCALEOUT is on.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

import config
import notifier
from execution import dhan_client as _dhan
from orders import side as _side

logger = logging.getLogger("position-stocks-scaleout")

TAG_SELL = "SCALEOUT_SELL"
TAG_STOP = "SCALEOUT_STOP"
FILLED = {"TRADED", "FILLED", "EXECUTED", "COMPLETE", "COMPLETED"}
DEAD = {"REJECTED", "CANCELLED", "CANCELED", "EXPIRED"}
LIVE_LEG = {"PENDING", "TRANSIT", "OPEN", "TRIGGERED", "CONFIRM", "PART_TRADED", "PARTIALLY_TRADED"}
WINDOW_TARGET_S = 3.0

_stats = {"attempts": 0, "split": 0, "fallback_full": 0, "half_then_full": 0, "aborted": 0, "stop_filled": 0, "stop_replaced": 0, "target_exit": 0,
          "breach_exit": 0, "settled_late": 0, "errors": 0, "max_window_s": 0.0}
_inflight: set = set()
_breach_since: Dict[Any, float] = {}
_next_try: Dict[Any, float] = {}
_replaced: Dict[Any, int] = {}
_alerted: set = set()
_pending_since: Dict[Any, float] = {}


def reset_state() -> None:
    """Test helper."""
    _inflight.clear(); _breach_since.clear(); _next_try.clear(); _replaced.clear(); _alerted.clear(); _pending_since.clear()
    for k in _stats:
        _stats[k] = 0.0 if k == "max_window_s" else 0


def stats() -> Dict[str, Any]:
    return {**_stats, "enabled": enabled()}


def enabled() -> bool:
    return bool(getattr(config, "MOMENTUM_SCALEOUT", False))


# ---------------------------------------------------------------- the stamp (flow_json["scaleout"])
def read_stamp(flow_json: Any) -> Optional[dict]:
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (flow_json if isinstance(flow_json, dict) else None)
        st = (d or {}).get("scaleout")
        return dict(st) if isinstance(st, dict) else None
    except Exception:  # noqa: BLE001
        return None


def write_stamp(flow_json: Any, record: dict) -> Any:
    """flow_json with `record` as its "scaleout" key. Returns `flow_json` unchanged on any problem. Never raises."""
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else {})
        if not isinstance(d, dict):
            d = {}
        d["scaleout"] = record
        return json.dumps(d, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json


def is_remainder(pos: Any) -> bool:
    """True for the kept half of a split position: a long with a plain stop, no Super Order and the scale-out stamp."""
    try:
        return bool(getattr(pos, "plain_stop_order_id", None)) and not getattr(pos, "dhan_super_order_id", None) \
            and not _side.is_short(pos) and read_stamp(getattr(pos, "flow_json", None)) is not None
    except Exception:  # noqa: BLE001
        return False


def eligible(pos: Any, now_hhmm: Optional[str] = None) -> Tuple[bool, str]:
    """(True, "") when this position may be split now. Pure apart from the clock."""
    try:
        if not enabled():
            return False, "off"
        if getattr(pos, "status", None) != "OPEN":
            return False, "not open"
        if _side.is_short(pos):
            return False, "short"
        if getattr(pos, "overnight_converted_to_cnc", False):
            return False, "carried overnight"
        if not getattr(pos, "dhan_super_order_id", None) or getattr(pos, "plain_stop_order_id", None):
            return False, "no Super Order"
        if read_stamp(getattr(pos, "flow_json", None)) is not None:
            return False, "already split"
        if int(getattr(pos, "quantity", 0) or 0) < int(getattr(config, "MOMENTUM_SCALEOUT_MIN_QTY", 4)):
            return False, "too small"
        until = str(getattr(config, "MOMENTUM_SCALEOUT_UNTIL_IST", "14:30") or "14:30")
        if now_hhmm is None:
            from tz_utils import ist_now
            now_hhmm = ist_now().strftime("%H:%M")
        if now_hhmm >= until:
            return False, f"after {until}"
        return True, ""
    except Exception:  # noqa: BLE001
        return False, "error"


# ---------------------------------------------------------------- small readers
def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x > 0 else None


def _status(row: Optional[dict]) -> str:
    return str((row or {}).get("orderStatus") or (row or {}).get("order_status") or (row or {}).get("status") or "").upper()


def _filled_qty(row: Optional[dict]) -> int:
    q = _num((row or {}).get("filledQty") or (row or {}).get("filled_qty") or (row or {}).get("tradedQuantity"))
    return int(q) if q else 0


def _fill_price(row: Optional[dict]) -> Optional[float]:
    return _num((row or {}).get("averageTradedPrice") or (row or {}).get("average_traded_price"))


def _oid(res: Any) -> str:
    return str((res or {}).get("orderId") or (res or {}).get("order_id") or (res or {}).get("id") or "")


def _alert(text: str) -> None:
    try:
        notifier.notify_fire_and_forget(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("scaleout: alert failed (%s)", e)


def _alert_once(pos_id: Any, kind: str, text: str) -> None:
    if (pos_id, kind) in _alerted:
        return
    _alerted.add((pos_id, kind))
    _alert(text)


def _row(db, broker, oid: str) -> Tuple[Optional[dict], bool]:
    """(row, readable). readable False = the book could not be read; row None with readable True = the order is not in the book."""
    try:
        return broker.get_order_row(db, oid), True
    except Exception as e:  # noqa: BLE001
        logger.warning("scaleout: order book read failed for %s (%s)", oid, e)
        return None, False


def _long_qty(db, broker, security_id: str) -> Optional[int]:
    """Net long shares Dhan shows for this security today; None when the positions cannot be read."""
    try:
        got = broker.get_positions(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("scaleout: positions read failed (%s)", e)
        return None
    if not isinstance(got, list):
        return None
    total = 0
    for r in got:
        if isinstance(r, dict) and str(r.get("securityId") or r.get("security_id") or "") == str(security_id):
            try:
                total += int(float(r.get("netQty") if r.get("netQty") is not None else r.get("net_qty") or 0))
            except (TypeError, ValueError):
                continue
    return total


def _super_row(db, broker, oid: str) -> Optional[dict]:
    try:
        return next((r for r in broker.get_super_order_list(db) or [] if str(r.get("orderId") or "") == str(oid)), None)
    except Exception as e:  # noqa: BLE001
        logger.warning("scaleout: Super Order list read failed (%s)", e)
        return None


def _legs(row: Optional[dict]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for lg in (row or {}).get("legDetails") or (row or {}).get("legs") or []:
        if isinstance(lg, dict) and lg.get("legName"):
            out[str(lg.get("legName"))] = _status(lg)
    return out


# ---------------------------------------------------------------- booking the half
def settle_sell(db, pos, broker=_dhan, now_mono: Optional[float] = None) -> str:
    """Book the half-sell once its fill price is known. Returns 'booked' (done now or earlier), 'pending' (the order is not filled yet) or 'dead' (it was rejected / cancelled with no fill).
    Reads the SELL order from the stamp; never raises (an unreadable book is 'pending')."""
    st = read_stamp(getattr(pos, "flow_json", None))
    if not st:
        return "booked"
    if st.get("booked"):
        return "booked"
    sid = str(st.get("sell_order_id") or "")
    half = int(st.get("qty_sold") or 0)
    if not sid or half <= 0:
        return "booked"
    row, ok = _row(db, broker, sid)
    if not ok or row is None:
        return "pending"
    s = _status(row)
    if s in DEAD and _filled_qty(row) <= 0:
        return "dead"
    if s not in FILLED:
        return "pending"
    price = _fill_price(row)
    estimated = False
    if price is None:
        price = _num(st.get("est_price")) or _num(getattr(pos, "entry_price", None))
        estimated = True
    return _book_half(db, pos, st, float(price), half, estimated)


def _book_half(db, pos, st: dict, price: float, half: int, estimated: bool) -> str:
    try:
        from orders import overnight_stop
        if int(pos.quantity or 0) < half:
            logger.error("scaleout: %s (id=%s) holds %s but the half-sell was %s - not booking", pos.symbol, pos.id, pos.quantity, half)
            return "pending"
        overnight_stop.book_delta(db, pos, price, half)
        pos.exit_price = None                                # book_delta set it; the row is still open
        overnight_stop.assign_stop_order(pos, None)          # rolls the per-order counters into overnight_stop_prior_qty; the plain stop id lives in plain_stop_order_id
        st = dict(st)
        st.update(booked=True, sell_price=round(price, 4), price_estimated=bool(estimated), booked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        pos.flow_json = write_stamp(getattr(pos, "flow_json", None), st)
        db.commit()
        _pending_since.pop(pos.id, None)
        logger.info("scaleout: %s (id=%s) half-sell of %d booked at %.2f%s; %d kept", pos.symbol, pos.id, half, price, " (ESTIMATED, check Dhan)" if estimated else "", pos.quantity)
        if estimated:
            _alert_once(pos.id, "est", f"⚠️ <b>Half-sell price estimated</b> - {pos.symbol}: Dhan showed no average price for the SELL of {half}. Booked at ₹{price:.2f}; check the trade book.")
        return "booked"
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("scaleout.py: rollback ignored %s", _e)
        logger.error("scaleout: booking the half-sell of %s (id=%s) failed (%s)", getattr(pos, "symbol", "?"), getattr(pos, "id", "?"), e, exc_info=True)
        return "pending"


def settle_before_flat_sell(db, pos, broker=_dhan) -> None:
    """Called by eod_squareoff._fire_flat_sell for a split position BEFORE it sizes a flat SELL: an unbooked half-sell is booked first (so pos.quantity is what is really left);
    one that cannot be confirmed blocks the SELL (RuntimeError, the caller retries later) because a full-size SELL on top of a half-sell would sell shares twice."""
    st = read_stamp(getattr(pos, "flow_json", None))
    if not st or st.get("booked"):
        return
    r = settle_sell(db, pos, broker)
    if r == "pending":
        raise RuntimeError(f"{pos.symbol} (id={pos.id}) flat SELL NOT sent: the half-sell {st.get('sell_order_id')} is not confirmed yet (booking it first, retry shortly)")
    if r == "dead":
        raise RuntimeError(f"{pos.symbol} (id={pos.id}) flat SELL NOT sent: the half-sell {st.get('sell_order_id')} was rejected; the guard re-sizes the position first")


# ---------------------------------------------------------------- the split
def _compute_stop(ltp: float, old_stop: Optional[float], decision: dict, broker) -> Optional[float]:
    """The kept half's stop: behind the price by the stock's own range (the signal's trail gap) or SCALEOUT_STOP_GAP_PCT, never below the trade's current stop, always under the price. None = no valid stop."""
    try:
        gap = _num((decision or {}).get("trail_gap_pct")) or float(getattr(config, "SCALEOUT_STOP_GAP_PCT", 1.0))
        gap = min(3.0, max(0.3, gap))
        tick = broker.tick_size_for_price(ltp)
        cand = broker.round_stop_to_tick(ltp * (1.0 - gap / 100.0))
        stop = max(float(cand), float(old_stop)) if old_stop else float(cand)
        stop = broker.round_stop_to_tick(stop)
        if stop <= 0 or stop > ltp - tick + 1e-9:
            return None
        return float(stop)
    except Exception:  # noqa: BLE001
        return None


def _find_sell(db, broker, security_id: str, qty: int, tag: str) -> Tuple[Optional[dict], bool]:
    """A SELL of `qty` with our tag that exists at Dhan (a placement that errored on our side may have gone through). (row, readable)."""
    try:
        rows = broker.get_order_list(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("scaleout: order list read failed (%s)", e)
        return None, False
    for r in rows or []:
        if not isinstance(r, dict) or str(r.get("securityId") or r.get("security_id") or "") != str(security_id):
            continue
        if str(r.get("transactionType") or r.get("transaction_type") or "").upper() != "SELL":
            continue
        if str(r.get("tag") or r.get("correlationId") or "") != tag or _status(r) in DEAD:
            continue
        try:
            if int(float(r.get("quantity") or 0)) == int(qty):
                return r, True
        except (TypeError, ValueError):
            continue
    return None, True


def _cancel_stray_stops(db, broker, security_id: str, qty: int) -> None:
    """Cancel a resting SELL stop of ours that a failed placement may have left behind, before the rest is sold at market (a resting stop plus a market SELL could sell the shares twice)."""
    row, readable = _find_sell(db, broker, security_id, qty, TAG_STOP)
    if row is None:
        if not readable:
            logger.warning("scaleout: could not look for a stray stop on %s (order book unreadable)", security_id)
        return
    try:
        broker.cancel_cnc_stop_loss_order(db, order_id=_oid(row))
    except Exception as e:  # noqa: BLE001
        logger.warning("scaleout: cancel of stray stop %s failed (%s)", _oid(row), e)


def run_half_sell(db, pos, *, ltp: float, decision: Optional[dict] = None, broker=_dhan, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """Split one eligible position. Returns {"ok", "stage", "fallback", "qty_sold", "qty_kept", "window_s", "reason"}.
    fallback: "FULL" = nothing was sold, the caller closes the whole position the old way; "NONE" = nothing more to do (done, or nothing was sent); "HALF_THEN_FULL" = the half was sold and booked
    and the rest was sold at market here (the caller does nothing). Never raises."""
    res: Dict[str, Any] = {"ok": False, "stage": "start", "fallback": "FULL", "qty_sold": 0, "qty_kept": int(getattr(pos, "quantity", 0) or 0), "window_s": None, "reason": ""}
    pid = getattr(pos, "id", None)
    if pid in _inflight:
        res.update(stage="inflight", fallback="NONE", reason="a split of this position is already running")
        return res
    _inflight.add(pid)
    _stats["attempts"] += 1
    try:
        return _run(db, pos, float(ltp), decision or {}, broker, sleep, clock, res)
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        logger.error("scaleout: %s (id=%s) unexpected error at stage %s: %s", getattr(pos, "symbol", "?"), pid, res.get("stage"), e, exc_info=True)
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("scaleout.py: rollback ignored %s", _e)
        res["reason"] = f"unexpected error: {str(e)[:160]}"
        # an error before the first cancel leaves everything as it was: close in full. After a cancel or a SELL the state is unclear: do nothing more, shout (the guard / EOD sweep own the row).
        if res.get("stage") in ("start", "positions", "cancel_target"):
            res["fallback"] = "FULL"
        else:
            res["fallback"] = "NONE"
            _alert(f"🚨 <b>Scale-out error</b> - {getattr(pos, 'symbol', '?')} stage {res.get('stage')}: {str(e)[:200]}\nCheck Dhan: the position may have no stop.")
        return res
    finally:
        _inflight.discard(pid)


def _run(db, pos, ltp: float, decision: dict, broker, sleep, clock, res: Dict[str, Any]) -> Dict[str, Any]:  # noqa: C901 - one straight line of steps, each with its own fallback
    sym = pos.symbol
    qty = int(pos.quantity or 0)
    half = qty // 2
    keep = qty - half
    sec = str(pos.dhan_security_id)
    sup = str(pos.dhan_super_order_id)
    res.update(qty_kept=qty)
    if half < 1 or keep < 1 or ltp <= 0:
        res.update(stage="size", reason="nothing to split", fallback="FULL")
        return res

    # 0. Dhan must show the whole quantity long
    res["stage"] = "positions"
    net = _long_qty(db, broker, sec)
    if net is None:
        res.update(reason="Dhan positions unreadable", fallback="FULL")
        _stats["fallback_full"] += 1
        return res
    if net < qty:
        res.update(stage="positions", fallback="NONE", reason=f"Dhan shows {net} long, the row says {qty}: nothing sent, reconcile settles it")
        _stats["aborted"] += 1
        return res

    # 1. cancel the target leg, then the stop leg
    res["stage"] = "cancel_target"
    try:
        broker.cancel_super_order(db, order_id=sup, order_leg="TARGET_LEG")
    except Exception as e:  # noqa: BLE001 - it may already be gone: the read-back below decides
        logger.info("scaleout: %s target leg cancel not done: %s", sym, e)
    row = _super_row(db, broker, sup)
    legs = _legs(row)
    if any(v in FILLED for k, v in legs.items() if k in ("TARGET_LEG", "STOP_LOSS_LEG")):
        res.update(stage="leg_filled", fallback="NONE", reason=f"an exit leg already filled ({legs}): nothing sent")
        _stats["aborted"] += 1
        return res
    if legs.get("TARGET_LEG") in LIVE_LEG:
        res.update(stage="cancel_target", reason=f"the target leg is still live ({legs})", fallback="FULL")
        _stats["fallback_full"] += 1
        return res
    t_unprotected = None
    res["stage"] = "cancel_stop"
    if legs.get("STOP_LOSS_LEG") in LIVE_LEG or not legs:
        try:
            broker.cancel_super_order(db, order_id=sup, order_leg="STOP_LOSS_LEG")
        except Exception as e:  # noqa: BLE001
            logger.info("scaleout: %s stop leg cancel not done: %s", sym, e)
        t_unprotected = clock()
        row = _super_row(db, broker, sup)
        legs = _legs(row)
    else:
        t_unprotected = clock()                    # the stop leg died with the target leg: the window started now
    if any(v in FILLED for k, v in legs.items() if k in ("TARGET_LEG", "STOP_LOSS_LEG")):
        res.update(stage="leg_filled", fallback="NONE", reason=f"an exit leg filled during the cancel ({legs}): nothing sent")
        _stats["aborted"] += 1
        return res
    if any(v in LIVE_LEG for k, v in legs.items() if k in ("TARGET_LEG", "STOP_LOSS_LEG")):
        res.update(stage="cancel_stop", reason=f"an exit leg is still live ({legs})", fallback="FULL")
        _stats["fallback_full"] += 1
        return res

    # 2. the half-sell, at market
    res["stage"] = "sell"
    sell_id = ""
    try:
        sold = broker.place_order(db, is_armed=True, security_id=sec, exchange_segment=config.SCALP_EXCHANGE_SEGMENT, transaction_type="SELL", quantity=half,
                                  order_type="MARKET", price=0.0, product_type=config.SCALP_PRODUCT_TYPE, tag=TAG_SELL)
        sell_id = _oid(sold)
        if not sell_id:
            raise RuntimeError(f"Dhan answered the SELL without an order id ({str(sold)[:100]})")
    except Exception as e:  # noqa: BLE001
        unclear = False
        try:
            unclear = bool(broker.is_ambiguous_placement_error(e))
        except Exception:  # noqa: BLE001
            unclear = False
        found, readable = (None, True)
        if unclear or not sell_id:
            found, readable = _find_sell(db, broker, sec, half, TAG_SELL)
        if found is not None:
            sell_id = _oid(found)
            logger.warning("scaleout: %s SELL errored (%s) but Dhan has order %s - carrying on with it", sym, str(e)[:100], sell_id)
        elif not readable:
            res.update(stage="sell", fallback="NONE", reason=f"the SELL errored ({str(e)[:100]}) and the order book is unreadable")
            _alert(f"🚨 <b>Scale-out: SELL unclear</b> - {sym}\nThe half-sell errored ({str(e)[:160]}) and Dhan's order book could not be read. The Super Order legs are cancelled: check Dhan NOW and close or protect {sym} by hand.")
            _stats["errors"] += 1
            return res
        else:
            res.update(stage="sell", fallback="FULL", reason=f"the SELL was refused: {str(e)[:160]}")
            _stats["fallback_full"] += 1
            return res

    # 3. the stop for the kept half, AT ONCE (no database write, no poll before it)
    res["stage"] = "stop"
    new_stop = _compute_stop(ltp, getattr(pos, "stop_price", None), decision, broker)
    stop_id, stop_err, stop_res = "", "", {}
    if new_stop is None:
        stop_err = "no valid stop under the price"
    else:
        try:
            stop_res = broker.place_intraday_sell_stop(db, is_armed=True, security_id=sec, exchange_segment=config.SCALP_EXCHANGE_SEGMENT, quantity=keep, trigger_price=new_stop, tag=TAG_STOP) or {}
            stop_id = _oid(stop_res)
            if not stop_id:
                stop_err = "Dhan gave no stop order id"
        except Exception as e:  # noqa: BLE001
            stop_err = str(e)[:200]
    window = None if t_unprotected is None else round(clock() - t_unprotected, 2)
    res["window_s"] = window
    if window is not None:
        _stats["max_window_s"] = max(float(_stats["max_window_s"]), float(window))
        if window > WINDOW_TARGET_S:
            logger.warning("scaleout: %s unprotected window %.1fs is over the %.0fs goal", sym, window, WINDOW_TARGET_S)

    st = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "qty_sold": half, "qty_kept": keep, "sell_order_id": sell_id, "est_price": round(ltp, 4),
          "stop_order_id": stop_id or None, "new_stop": new_stop, "old_stop": getattr(pos, "stop_price", None), "super_order_id": sup, "window_s": window,
          "signals": (decision or {}).get("signals"), "reason": str((decision or {}).get("reason") or "")[:160], "booked": False,
          "stop_type": (stop_res or {}).get("order_type_sent")}

    if stop_err:
        # the stop could not be placed: book the half, make sure no stray stop rests, sell the rest at market
        res.update(stage="stop_failed", reason=f"stop not placed ({stop_err})")
        logger.error("scaleout: %s stop for the kept %d not placed (%s): booking the half and selling the rest", sym, keep, stop_err)
        _cancel_stray_stops(db, broker, sec, keep)
        pos.dhan_super_order_id = None
        pos.flow_json = write_stamp(getattr(pos, "flow_json", None), st)
        db.commit()
        outcome = _wait_and_book(db, pos, st, broker, sleep, clock)
        if outcome != "booked":
            res.update(fallback="NONE", reason=res["reason"] + f"; the half-sell is {outcome}, the rest was NOT sold: the guard finishes it")
            _alert(f"🚨 <b>Scale-out: no stop</b> - {sym}\nThe stop failed ({stop_err}) and the half-sell is {outcome}. The guard will sell the rest at market once the half is booked. Check Dhan.")
            _stats["errors"] += 1
            return res
        res.update(qty_sold=half, qty_kept=int(pos.quantity or 0), fallback="HALF_THEN_FULL")
        _stats["half_then_full"] += 1
        _full_close(db, pos, "MOMENTUM_EXIT", decision)
        _alert(f"⚠️ <b>Half-sell, then full exit</b> - {sym}\nSold {half}; the stop for the other {keep} could not be placed ({stop_err}), so the rest was sold at market.")
        return res

    # 4. the row: Super Order cleared, plain stop recorded, stamp written
    res["stage"] = "record"
    old_stop = pos.stop_price
    pos.dhan_super_order_id = None
    pos.plain_stop_order_id = stop_id
    pos.stop_price = new_stop
    pos.stop_moved_to_breakeven = True
    pos.flow_json = write_stamp(getattr(pos, "flow_json", None), st)
    db.commit()

    # 5. the sell's fill price, then the booking of the half
    res["stage"] = "book"
    outcome = _wait_and_book(db, pos, st, broker, sleep, clock)
    res.update(ok=True, stage="done", fallback="NONE", qty_sold=half, qty_kept=int(pos.quantity or 0) if outcome == "booked" else keep,
               reason=f"half sold, stop {new_stop:.2f} (was {old_stop}), window {window}s, booking {outcome}")
    if outcome == "dead":
        res.update(ok=False, stage="sell_dead", reason="the half-sell was rejected after it was accepted: the stop is cancelled and the whole position is sold at market")
        _undo_dead_sell(db, pos, broker)
        return res
    _stats["split"] += 1
    try:
        from orders import momentum_hook
        pos.flow_json = momentum_hook.stamp_momentum(getattr(pos, "flow_json", None), {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": "SCALEOUT", "ltp": round(ltp, 2), "sold": half, "kept": keep, "new_stop": new_stop,
            "window_s": window, "signals": (decision or {}).get("signals"), "reason": str((decision or {}).get("reason") or "")[:160]})
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.debug("scaleout: momentum stamp skipped (%s)", e)
    logger.warning("SCALEOUT: %s (id=%s) sold %d, kept %d under stop %.2f (was %s), unprotected window %ss, booking %s - %s", sym, pos.id, half, keep, new_stop, old_stop, window, outcome, (decision or {}).get("reason"))
    _alert(f"✂️ <b>Half-sell</b> - {sym}\nSold {half} at market, kept {keep} under a new stop ₹{new_stop:.2f} (was {old_stop}).\nUnprotected window {window}s. {str((decision or {}).get('reason') or '')[:120]}")
    return res


def _wait_and_book(db, pos, st: dict, broker, sleep, clock) -> str:
    """Poll the SELL until it fills (SCALEOUT_FILL_WAIT_S), then book it. 'booked' | 'pending' | 'dead'."""
    wait = float(getattr(config, "SCALEOUT_FILL_WAIT_S", 6.0))
    end = clock() + wait
    while True:
        r = settle_sell(db, pos, broker)
        if r in ("booked", "dead"):
            return r
        if clock() >= end:
            _pending_since.setdefault(pos.id, time.monotonic())
            return "pending"
        sleep(0.5)


def _full_close(db, pos, reason: str, decision: Optional[dict]) -> None:
    from orders import eod_squareoff
    try:
        eod_squareoff._stamp_exit_rule(pos, "momentum")    # noqa: SLF001 - same record as the momentum full exit
        eod_squareoff.close_position_now(db, pos, exit_reason=reason)
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("scaleout.py: rollback ignored %s", _e)
        logger.error("scaleout: full close of %s (id=%s) failed (%s): the guard retries", pos.symbol, pos.id, e)
        _alert(f"🚨 <b>Scale-out: rest not sold</b> - {pos.symbol} x{pos.quantity}: {str(e)[:200]}\nThe guard retries; check Dhan.")


def _undo_dead_sell(db, pos, broker) -> None:
    """The half-sell was accepted but died unfilled: the position still holds the whole quantity, the stop covers only half. Cancel the stop and sell everything."""
    try:
        if pos.plain_stop_order_id:
            broker.cancel_cnc_stop_loss_order(db, order_id=pos.plain_stop_order_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("scaleout: cancel of the stop after a dead half-sell failed (%s)", e)
    st = read_stamp(pos.flow_json) or {}
    st["booked"] = True                                  # nothing was sold: no booking will come
    st["dead"] = True
    pos.flow_json = write_stamp(pos.flow_json, st)
    db.commit()
    _stats["half_then_full"] += 1
    _full_close(db, pos, "MOMENTUM_EXIT", None)


# ---------------------------------------------------------------- the guard for split positions
def _split_longs(db) -> list:
    from models import ScalpPosition
    rows = (db.query(ScalpPosition).filter(ScalpPosition.status == "OPEN", ScalpPosition.dhan_super_order_id.is_(None)).all())
    return [p for p in rows if not getattr(p, "overnight_converted_to_cnc", False) and not _side.is_short(p) and read_stamp(getattr(p, "flow_json", None)) is not None]


def _last_tick(symbol: str):
    try:
        from orders import plain_short
        return plain_short._last_tick(symbol)      # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None, None


def _book_stop_fill(db, pos, row: dict) -> None:
    """The broker stop of the kept half filled: STOP_HIT at its real fill, with the half already booked added."""
    from capital import ledger, shared_symbol_lock
    from orders import reconcile
    exit_price = _fill_price(row) or (_num(pos.stop_price) or 0.0)
    confirmed = _fill_price(row) is not None
    try:
        reconcile.note_stop_hit(pos, exit_price, confirmed)
    except Exception as e:  # noqa: BLE001
        logger.debug("scaleout: note_stop_hit skipped (%s)", e)
    prior_pnl = float(pos.realized_pnl or 0.0)
    prior_qty = int(pos.overnight_stop_prior_qty or 0) + int(pos.overnight_stop_filled_qty_so_far or 0)
    sell_pnl = _side.pnl(pos, pos.entry_price, exit_price, pos.quantity)
    total = prior_pnl + sell_pnl
    basis = pos.entry_price * (int(pos.quantity or 0) + prior_qty)
    pct = (total / basis * 100.0) if basis else 0.0
    pos.status, pos.exit_price, pos.realized_pnl, pos.realized_pnl_pct = "STOP_HIT", exit_price, total, pct
    pos.dhan_exit_order_id = str(pos.plain_stop_order_id or "") or None
    pos.error_message = None if confirmed else "STOP_HIT booked at the stop price: Dhan showed no average traded price (check the trade book)"
    pos.closed_at = datetime.now(timezone.utc)
    db.commit()
    ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=sell_pnl)
    shared_symbol_lock.release(db, pos.symbol)
    _stats["stop_filled"] += 1
    logger.info("scaleout: %s (id=%s) kept half STOP_HIT @ %.2f, total P&L %.2f", pos.symbol, pos.id, exit_price, total)
    _alert(f"🔴 <b>STOP_HIT</b> - {pos.symbol} x{pos.quantity} (kept half)\nEntry ₹{pos.entry_price:.2f} → exit ₹{exit_price:.2f}\nTotal P&L with the half-sell ₹{total:,.2f} ({pct:.2f}%)")


def _sell_now(db, pos, reason: str, now: float) -> bool:
    from orders import eod_squareoff
    try:
        eod_squareoff.close_position_now(db, pos, exit_reason=reason)
        return True
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("scaleout.py: rollback ignored %s", _e)
        _next_try[pos.id] = now + float(getattr(config, "STOP_WATCHDOG_RETRY_S", 30.0))
        logger.warning("scaleout: %s (id=%s) %s sell not placed (%s) - retry later", pos.symbol, pos.id, reason, e)
        return False


def run_scaleout_guard(db, now_mono: Optional[float] = None, broker=_dhan) -> int:
    """One pass over every split position. Returns how many were closed or resolved. Never raises. Does nothing when no position carries the stamp (so it is harmless with MOMENTUM_SCALEOUT off)."""
    now = time.monotonic() if now_mono is None else now_mono
    done = 0
    try:
        positions = _split_longs(db)
    except Exception as e:  # noqa: BLE001
        logger.error("scaleout guard: query failed (%s)", e)
        return 0
    live_ids = {p.id for p in positions}
    for d in (_breach_since, _next_try, _replaced, _pending_since):
        for k in [k for k in d if k not in live_ids]:
            d.pop(k, None)
    max_age = float(getattr(config, "STOP_WATCHDOG_MAX_TICK_AGE_S", 15.0))
    for pos in positions:
        try:
            if now < _next_try.get(pos.id, 0.0) or pos.id in _inflight:
                continue
            # 0. an unbooked half-sell first: nothing else is sized until it is known
            st = read_stamp(pos.flow_json) or {}
            if not st.get("booked"):
                r = settle_sell(db, pos, broker)
                if r == "pending":
                    first = _pending_since.setdefault(pos.id, now)
                    if now - first > 60.0:
                        _alert_once(pos.id, "pending", f"🚨 <b>Half-sell not confirmed</b> - {pos.symbol}: order {st.get('sell_order_id')} is still not filled after 60 s. Check Dhan.")
                    continue
                if r == "dead":
                    _alert_once(pos.id, "dead", f"🚨 <b>Half-sell rejected</b> - {pos.symbol}: order {st.get('sell_order_id')} did not fill. Cancelling the stop and selling the whole position at market.")
                    _undo_dead_sell(db, pos, broker)
                    done += 1
                    continue
            # 1. the broker stop
            sid = pos.plain_stop_order_id
            if sid:
                row, ok = _row(db, broker, sid)
                s = _status(row) if (ok and row is not None) else ""
                if s in FILLED:
                    _book_stop_fill(db, pos, row)
                    done += 1
                    continue
                f = _filled_qty(row)
                if ok and row is not None and 1 <= f < int(pos.quantity) and s not in FILLED:
                    if s in DEAD:
                        _alert_once(pos.id, "partial", f"🚨 <b>Kept-half stop filled in part</b> - {pos.symbol}: {f} of {pos.quantity}, then the order {s}.\nNot booked automatically: check Dhan.")
                    continue                                    # a live part-filled stop completes by itself
                if s in DEAD:
                    if _replaced.get(pos.id, 0) < 1:
                        _replaced[pos.id] = 1
                        try:
                            ns = broker.place_intraday_sell_stop(db, is_armed=True, security_id=pos.dhan_security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                                                                 quantity=int(pos.quantity), trigger_price=float(pos.stop_price), tag=TAG_STOP)
                            pos.plain_stop_order_id = _oid(ns) or None
                            db.commit()
                            _stats["stop_replaced"] += 1
                            _alert(f"⚠️ <b>Kept-half stop {s}</b> - {pos.symbol} x{pos.quantity}\nA new stop was placed at ₹{pos.stop_price:.2f}.")
                            continue
                        except Exception as e:  # noqa: BLE001
                            db.rollback()
                            logger.warning("scaleout: %s stop re-place failed (%s)", pos.symbol, e)
                    _alert_once(pos.id, "lost", f"🚨 <b>Kept-half stop lost</b> - {pos.symbol} x{pos.quantity}\nThe broker stop is {s} and could not be re-placed. Selling at market.")
                    pos.plain_stop_order_id = None
                    db.commit()
                    if _sell_now(db, pos, "STOP_BREACH_EXIT", now):
                        _stats["breach_exit"] += 1
                        done += 1
                    continue
            else:
                _alert_once(pos.id, "nostop", f"🚨 <b>Kept half with no stop</b> - {pos.symbol} x{pos.quantity}\nNo broker stop is on record. Selling at market (STOP_BREACH_EXIT).")
                if _sell_now(db, pos, "STOP_BREACH_EXIT", now):
                    _stats["breach_exit"] += 1
                    done += 1
                continue
            # 2. price rules (a fresh tick only)
            ltp, age = _last_tick(pos.symbol)
            if ltp is None or age is None or age > max_age:
                _breach_since.pop(pos.id, None)
                continue
            if pos.target_price and ltp >= float(pos.target_price) - 1e-9:
                if _sell_now(db, pos, "TARGET_EXIT", now):
                    _stats["target_exit"] += 1
                    done += 1
                continue
            from orders import stop_watchdog
            if not stop_watchdog.is_breached(ltp, pos.stop_price, short=False):
                _breach_since.pop(pos.id, None)
                continue
            first = _breach_since.setdefault(pos.id, now)
            if now - first < float(getattr(config, "SCALEOUT_STOP_GRACE_S", 15.0)):
                continue
            logger.critical("scaleout: %s (id=%s) price %.2f is under its stop %.2f for %.0fs and the broker stop has not fired - selling at market", pos.symbol, pos.id, ltp, pos.stop_price, now - first)
            if _sell_now(db, pos, "STOP_BREACH_EXIT", now):
                _stats["breach_exit"] += 1
                done += 1
                _breach_since.pop(pos.id, None)
                _alert(f"🚨 <b>Kept-half stop did not fire</b> - {pos.symbol} x{pos.quantity}\nPrice ₹{ltp:.2f} sat under the stop ₹{pos.stop_price:.2f}. Sold at market (STOP_BREACH_EXIT).")
        except Exception as e:  # noqa: BLE001
            _stats["errors"] += 1
            try:
                db.rollback()
            except Exception as _e:  # noqa: BLE001
                logger.debug("scaleout.py: rollback ignored %s", _e)
            logger.error("scaleout guard: %s (id=%s) unexpected error: %s", getattr(pos, "symbol", "?"), getattr(pos, "id", "?"), e, exc_info=True)
    return done
