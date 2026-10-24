"""orders/plain_short.py - E114: the plain-order short (open item 1 of the plan). Position Stocks only, shorts only.

WHEN: orders/short_live.py asks for a SELL Super Order first. Only when Dhan REFUSES it (or USE_SUPER_ORDER is off) and SHORT_PLAIN_FALLBACK is on, the short is opened
here instead, as a plain INTRADAY MARKET SELL. A refusal that a plain SELL would meet too (restricted stock, circuit limit, intraday cut-off, no funds) and an error that
may have created the Super Order anyway (timeout) never reach this module.

THE RULE: a short never exists without a stop. After the SELL shows as filled in Dhan's order book, a BUY STOP_LOSS_MARKET order at the stop price (the broker stop) is
placed and read back. If it cannot be placed, the short is covered at once with a MARKET BUY and the DAY BREAKER trips: no further plain short is opened today. If even the
cover cannot be placed the short is adopted as a position row without a broker stop (Telegram: critical) and the guard below covers it by hand.

STATE UNKNOWN = NOTHING BOUGHT: when the order book cannot say whether the SELL filled, no stop and no BUY is sent (a BUY with no short under it would open a LONG). The
SELL is put on a WATCH LIST instead (watch_orphan): every guard pass reads it again; once it is known to have filled it is covered with a MARKET BUY, once it is known to be dead
the capital and the symbol lock are released. The watch list lives in this process only: after a restart Dhan has to be checked by hand (the Telegram alert says so).

AFTER ENTRY (run_plain_short_guard, called from the fast reconcile loop right after the stop watchdog), for every OPEN SHORT that has no Super Order:
  * the broker stop filled          -> booked STOP_HIT at its real fill, capital and symbol lock released
  * the broker stop died (rejected / cancelled / expired) -> ONE re-place, then cover by hand (STOP_BREACH_EXIT)
  * no stop on record               -> cover by hand (STOP_BREACH_EXIT), critical alert once
  * price at or under the target    -> TARGET_EXIT: the stop is cancelled (and read back), then MARKET BUY. The stop is never trailed: a plain short keeps its first stop
  * price over the stop by STOP_WATCHDOG_MIN_BREACH_TICKS for SHORT_PLAIN_STOP_GRACE_S and the broker stop has not fired -> cover by hand (STOP_BREACH_EXIT)
  * a cover BUY that already exists at Dhan is joined (adopted), never doubled
  * the 14:55 cover, the 15:00 sweep and manual exits go through orders/eod_squareoff._fire_flat_sell, which calls release_stop_before_cover() first: the resting stop is
    cancelled and read back, a stop that already filled raises PositionAlreadyFlat (the cover BUY is never sent on top of it) and a stop that filled in PART blocks the BUY.
The guard keeps running whatever SHORT_PLAIN_FALLBACK says (the switch only gates NEW plain shorts). Every function never raises into the caller's loop except where documented.
Known limit: a stop that filled in part and then died is alerted (critical), not auto-booked; Dhan has to be checked by hand.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
import notifier
from execution import dhan_client as broker

logger = logging.getLogger("position-stocks-plain-short")

FILLED = {"TRADED", "FILLED", "EXECUTED", "COMPLETE", "COMPLETED"}
DEAD = {"REJECTED", "CANCELLED", "CANCELED", "EXPIRED"}
TAG_ENTRY = "SCALP"
TAG_STOP = "SCALPSTOP"
TAG_COVER = "SCALPCOVER"
_COVER_TAGS = ("", TAG_COVER, "EOD_SQUAREOFF")
ORPHAN_ABSENT_S = 120.0                   # a watched SELL missing from a readable order book this long does not exist
ORPHAN_COVER_TRIES = 5

_breach_since: Dict[int, float] = {}      # position id -> monotonic time the price was first seen past the stop
_next_try: Dict[int, float] = {}          # position id -> monotonic time before which no new cover is attempted
_replaced: Dict[int, int] = {}            # position id -> how many times a dead stop was re-placed
_alerted: set = set()                     # (position id, kind) alerts already sent once
_orphans: Dict[str, Dict[str, Any]] = {}  # entry order id -> {sym, security_id, qty, value, since, cancelled, tries}
_tripped_day: List[Optional[str]] = [None]   # IST date on which a stop could not be placed: no new plain short that day
_stats: Dict[str, int] = {"opened": 0, "stop_failed_covered": 0, "unprotected": 0, "unknown_state": 0, "stop_filled": 0, "target_exit": 0, "breach_cover": 0,
                          "stop_replaced": 0, "orphan_covered": 0, "orphan_released": 0, "cover_adopted": 0}


def reset_state() -> None:
    """Test helper."""
    _breach_since.clear(); _next_try.clear(); _replaced.clear(); _alerted.clear(); _orphans.clear()
    _tripped_day[0] = None
    for k in _stats:
        _stats[k] = 0


def stats() -> Dict[str, Any]:
    return {**_stats, "orphans_watched": len(_orphans), "breaker_day": _tripped_day[0]}


def _today() -> str:
    from tz_utils import ist_today_str
    return ist_today_str()


def enabled() -> bool:
    return bool(getattr(config, "SHORT_PLAIN_FALLBACK", True))


def breaker_tripped() -> bool:
    return _tripped_day[0] is not None and _tripped_day[0] == _today()


def entry_allowed() -> bool:
    """A NEW plain short may be opened: the switch is on and no stop failed to place earlier today."""
    return enabled() and not breaker_tripped()


# ---------------------------------------------------------------- small readers
def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x > 0 else None


def _status(row: Optional[dict]) -> str:
    return str((row or {}).get("orderStatus") or (row or {}).get("order_status") or (row or {}).get("status") or "").upper()


def _raw_filled(row: Optional[dict]) -> int:
    q = _num((row or {}).get("filledQty") or (row or {}).get("filled_qty") or (row or {}).get("tradedQuantity"))
    return int(q) if q else 0


def _filled_qty(row: Optional[dict], planned: int) -> int:
    """Shares the order has filled: filledQty when Dhan shows it, else everything for a filled status, else 0."""
    q = _raw_filled(row)
    if q:
        return q
    return int(planned) if _status(row) in FILLED else 0


def _partly(row: Optional[dict], qty: int) -> bool:
    """True when the order filled some shares but not all of them and is not reported as filled."""
    f = _raw_filled(row)
    return 1 <= f < int(qty) and _status(row) not in FILLED


def _fill_price(row: Optional[dict]) -> Optional[float]:
    return _num((row or {}).get("averageTradedPrice") or (row or {}).get("average_traded_price"))


def _row(db, oid: str) -> Tuple[Optional[dict], bool]:
    """(row, readable). readable False = the book could not be read at all; row None with readable True = the order is not in the book."""
    try:
        return broker.get_order_row(db, oid), True
    except Exception as e:  # noqa: BLE001
        logger.warning("plain short: order book read failed for %s (%s)", oid, e)
        return None, False


def refusal_allows_fallback(error_msg: str, ambiguous: bool) -> bool:
    """True when a refused SELL Super Order may be retried as a plain SELL. Never for an ambiguous error (the Super Order may exist), nor for a refusal a plain
    SELL would meet as well: restricted stock, circuit limit, intraday cut-off, insufficient funds; nor while the switch is off or the day breaker is tripped. Pure."""
    if not entry_allowed() or ambiguous:
        return False
    low = error_msg or ""
    return not (broker.is_security_intraday_restricted_error(low) or broker.is_circuit_limit_error(low)
                or broker.is_intraday_cutoff_error(low) or broker.is_insufficient_funds_error(low))


def _alert(text: str) -> None:
    try:
        notifier.notify_fire_and_forget(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("plain short: alert failed (%s)", e)


def _alert_once(pos_id: Any, kind: str, text: str) -> None:
    if (pos_id, kind) in _alerted:
        return
    _alerted.add((pos_id, kind))
    _alert(text)


def _round_levels(fill: float, stop_pct: float, target_pct: float, stop_fallback: float, target_fallback: float) -> Tuple[float, float]:
    """Stop (above) and target (below) re-anchored on the real fill, tick-rounded; the planned ones when the result would sit on the wrong side of the fill."""
    try:
        stop = broker.round_stop_to_tick(round(fill * (1 + stop_pct / 100.0), 2))
        target = broker.round_to_tick(round(fill * (1 - target_pct / 100.0), 2))
    except Exception:  # noqa: BLE001
        return float(stop_fallback), float(target_fallback)
    if not (stop > fill):
        stop = float(stop_fallback)
    if not (0 < target < fill):
        target = float(target_fallback)
    return float(stop), float(target)


# ---------------------------------------------------------------- the orphan watch list
def watch_orphan(entry_order_id: Optional[str], sym: str, security_id: str, qty: int, value: float) -> None:
    """Put a SELL whose state is unknown on the watch list. The caller keeps the capital and the symbol lock held; this module releases them when the SELL resolves."""
    if not entry_order_id:
        return
    _orphans[str(entry_order_id)] = {"sym": sym, "security_id": str(security_id), "qty": int(qty), "value": float(value or 0.0),
                                     "since": time.monotonic(), "cancelled": False, "tries": 0, "next": 0.0}


def _release_claims(db, o: Dict[str, Any]) -> None:
    try:
        from capital import ledger, shared_symbol_lock
        ledger.release_capital(db, position_value=o["value"], realized_pnl=0.0)
        shared_symbol_lock.release(db, o["sym"])
    except Exception as e:  # noqa: BLE001
        logger.error("plain short: releasing the capital / symbol lock of %s failed (%s)", o.get("sym"), e)


def run_orphan_check(db, now_mono: Optional[float] = None) -> int:
    """One pass over the watched SELLs. Returns how many were resolved. Never raises."""
    now = time.monotonic() if now_mono is None else now_mono
    done = 0
    for oid, o in list(_orphans.items()):
        try:
            if now < o.get("next", 0.0):
                continue
            row, ok = _row(db, oid)
            if not ok:
                continue
            st, fq, sym = _status(row), _filled_qty(row, o["qty"]), o["sym"]
            age = now - o["since"]
            if fq >= 1:
                if st not in FILLED | DEAD and not o["cancelled"]:      # part filled and still live: stop the rest first, read again next pass
                    o["cancelled"] = True
                    try:
                        broker.cancel_cnc_stop_loss_order(db, order_id=oid)
                    except Exception as e:  # noqa: BLE001
                        logger.info("plain short: orphan %s cancel of the rest not done: %s", oid, e)
                    o["next"] = now + 1.0
                    continue
                try:
                    c = broker.place_order(db, is_armed=True, security_id=o["security_id"], exchange_segment=config.SCALP_EXCHANGE_SEGMENT, transaction_type="BUY",
                                           quantity=int(fq), order_type="MARKET", price=0.0, product_type="INTRADAY", tag=TAG_COVER)
                    cid = str((c or {}).get("orderId") or (c or {}).get("order_id") or "")
                except Exception as e:  # noqa: BLE001
                    o["tries"] += 1
                    if broker.is_ambiguous_placement_error(e) or o["tries"] >= ORPHAN_COVER_TRIES:
                        _orphans.pop(oid, None)                          # may exist at Dhan (or we gave up): no second BUY blind
                        _alert(f"🚨 <b>Watched SELL filled, cover NOT confirmed</b> - {sym} x{fq}\nSELL {oid} filled but the cover BUY failed ({str(e)[:160]}). Check Dhan and cover by hand.")
                        done += 1
                    else:
                        o["next"] = now + 10.0
                    continue
                _orphans.pop(oid, None)
                _release_claims(db, o)
                _stats["orphan_covered"] += 1
                done += 1
                _alert(f"⚠️ <b>Unconfirmed short covered</b> - {sym} x{fq}\nThe plain SELL {oid} turned out to be filled and had no stop. Covered at market (BUY {cid}). No short remains; check Dhan for the round-trip result.")
                continue
            if st in DEAD or (row is None and age >= ORPHAN_ABSENT_S):
                _orphans.pop(oid, None)
                _release_claims(db, o)
                _stats["orphan_released"] += 1
                done += 1
                logger.warning("plain short: watched SELL %s (%s) resolved with no fill (%s): capital and lock released", oid, sym, st or "not in the book")
                continue
            if age >= float(config.SHORT_PLAIN_FILL_WAIT_S) and not o["cancelled"]:   # still pending with no fill: stop it, read the final state next pass
                o["cancelled"] = True
                try:
                    broker.cancel_cnc_stop_loss_order(db, order_id=oid)
                except Exception as e:  # noqa: BLE001
                    logger.info("plain short: watched SELL %s cancel not done: %s", oid, e)
                o["next"] = now + 1.0
        except Exception as e:  # noqa: BLE001
            logger.error("plain short: orphan check for %s failed (%s)", oid, e, exc_info=True)
    return done


# ---------------------------------------------------------------- entry
def open_plain_short(db, *, is_armed: bool, security_id: str, sym: str, qty: int, live: float, stop_price: float, target_price: float, stop_pct: float, target_pct: float,
                     sleep: Callable[[float], None] = time.sleep, poll_every_s: float = 0.5) -> Dict[str, Any]:
    """Plain MARKET SELL, wait for the fill, then the BUY stop-loss-market. Returns
    {ok, reason, entry_order_id, stop_order_id, fill_price, qty, stop_price, target_price, protected, covered, critical}.
    ok=True: a short exists and has an owner (protected=False only when the stop AND the cover both failed). ok=False: no short exists (covered=True when one existed
    for a moment) - except critical=True, where the state is unknown (entry_order_id is set when the SELL is known to exist: the caller puts it on the watch list)."""
    res: Dict[str, Any] = {"ok": False, "reason": "", "entry_order_id": None, "stop_order_id": None, "fill_price": None, "qty": 0, "stop_price": float(stop_price),
                           "target_price": float(target_price), "protected": False, "covered": False, "critical": False}
    # 1. the SELL
    oid = ""
    try:
        r = broker.place_order(db, is_armed=is_armed, security_id=security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT, transaction_type="SELL",
                               quantity=int(qty), order_type="MARKET", price=0.0, product_type="INTRADAY", tag=TAG_ENTRY)
        oid = str((r or {}).get("orderId") or (r or {}).get("order_id") or "")
        if not oid:
            res["critical"] = True
            res["reason"] = f"Dhan answered the plain SELL for {sym} without an order id: check Dhan for a {sym} short"
            _stats["unknown_state"] += 1
            _alert(f"🚨 <b>Plain short state unknown</b> - {sym} x{qty}\n{res['reason']}")
            return res
    except Exception as e:  # noqa: BLE001
        if broker.is_ambiguous_placement_error(e):
            try:
                found = broker.find_recent_buy_order(db, security_id, qty, super_order=False, transaction_type="SELL")
            except Exception as e2:  # noqa: BLE001
                found = None
                logger.warning("plain short: %s SELL errored (%s) and the lookup failed too (%s)", sym, e, e2)
            oid = str((found or {}).get("orderId") or (found or {}).get("order_id") or "")
            if not oid:
                res["critical"] = True
                res["reason"] = f"the plain SELL errored ({str(e)[:160]}) and Dhan's book could not confirm it either way: check Dhan for a {sym} short"
                _stats["unknown_state"] += 1
                _alert(f"🚨 <b>Plain short state unknown</b> - {sym} x{qty}\n{res['reason']}")
                return res
        else:
            res["reason"] = f"the plain SELL was refused: {str(e)[:300]}"
            return res
    res["entry_order_id"] = oid

    # 2. wait for the fill
    waited, row, readable, filled = 0.0, None, False, 0
    while True:
        row, ok_read = _row(db, oid)
        readable = readable or ok_read
        filled = _filled_qty(row, qty)
        if row is not None and (_status(row) in FILLED or _status(row) in DEAD):
            break
        if waited >= float(config.SHORT_PLAIN_FILL_WAIT_S):
            break
        sleep(poll_every_s)
        waited += poll_every_s
    if row is None or _status(row) not in FILLED | DEAD:        # still pending, part filled, or unreadable: stop the rest, then read the final state
        try:
            broker.cancel_cnc_stop_loss_order(db, order_id=oid)
        except Exception as e:  # noqa: BLE001
            logger.warning("plain short: %s could not cancel the unfinished SELL %s (%s)", sym, oid, e)
        sleep(0.5)
        row, ok_read = _row(db, oid)
        readable = readable or ok_read
        filled = max(filled, _filled_qty(row, qty))
    if filled < 1:
        if readable and row is not None and _status(row) in DEAD:
            res["reason"] = f"the plain SELL ended {_status(row)} with no fill: no short opened"
            return res
        res["critical"] = True
        res["reason"] = (f"the plain SELL {oid} for {sym} could not be read back ({_status(row) or 'unreadable'}), so nothing was bought and no stop was placed: "
                         f"check Dhan for a {sym} short")
        _stats["unknown_state"] += 1
        _alert(f"🚨 <b>Plain short state unknown</b> - {sym} x{qty}\n{res['reason']}\nThe SELL is on the watch list: it is covered if it turns out filled.")
        return res
    if row is not None and _status(row) not in FILLED | DEAD:
        _alert(f"⚠️ <b>Plain short partly filled</b> - {sym}: {filled} of {qty} shares, order {oid} still {_status(row)}\nThe stop covers the {filled} filled shares; check Dhan for the rest.")
    price = _fill_price(row)
    res["fill_price"], res["qty"] = price, int(filled)
    if price:
        res["stop_price"], res["target_price"] = _round_levels(price, stop_pct, target_pct, stop_price, target_price)

    # 3. the broker stop (BUY stop-loss-market)
    try:
        s = broker.place_intraday_buy_stop(db, is_armed=True, security_id=security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                                           quantity=int(filled), trigger_price=float(res["stop_price"]), tag=TAG_STOP)
        res["stop_order_id"] = str((s or {}).get("orderId") or "") or None
        res.update(ok=True, protected=bool(res["stop_order_id"]), reason="")
        if not res["protected"]:
            raise RuntimeError("stop answered without an order id")
        _stats["opened"] += 1
        return res
    except Exception as e:  # noqa: BLE001
        stop_err = str(e)[:300]
        res.update(ok=False, protected=False)
    # 4. no stop: the day breaker trips and the short is covered at once
    _tripped_day[0] = _today()
    cover_id, last = "", ""
    for attempt in range(1, 4):
        try:
            c = broker.place_order(db, is_armed=True, security_id=security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT, transaction_type="BUY",
                                   quantity=int(filled), order_type="MARKET", price=0.0, product_type="INTRADAY", tag=TAG_COVER)
            cover_id = str((c or {}).get("orderId") or (c or {}).get("order_id") or "")
            if cover_id:
                break
            last = "cover answered without an order id"
        except Exception as e:  # noqa: BLE001
            last = f"cover attempt {attempt} failed: {str(e)[:160]}"
            if broker.is_ambiguous_placement_error(e):
                break                                           # may exist at Dhan: no second BUY blind
        sleep(1.0)
    if cover_id:
        res.update(covered=True, reason=f"the broker stop could not be placed ({stop_err}); the short was covered at once (BUY {cover_id})")
        _stats["stop_failed_covered"] += 1
        _alert(f"⚠️ <b>Plain short covered at once</b> - {sym} x{filled}\nNo broker stop could be placed: {stop_err}\nCover BUY {cover_id}. No short remains and no more plain shorts today; "
               f"the small round-trip loss is not in the trade list, check Dhan.")
        return res
    res.update(ok=True, protected=False, critical=True,
               reason=f"the broker stop could not be placed ({stop_err}) AND the cover failed ({last}): the short is adopted without a broker stop")
    _stats["unprotected"] += 1
    _alert(f"🚨 <b>SHORT WITHOUT A STOP</b> - {sym} x{filled}\nNo broker stop ({stop_err}) and the cover failed ({last}).\nStockky owns it and covers by hand on the next guard pass "
           f"(every few seconds), on a breach or at 14:55. Check Dhan now.")
    return res


# ---------------------------------------------------------------- before any cover BUY (14:55, 15:00 sweep, manual, watchdog)
def release_stop_before_cover(db, pos, sleep: Callable[[float], None] = time.sleep) -> Tuple[str, str]:
    """Cancel the resting broker stop of a plain short and read it back. Returns (state, why):
    'none'      the position has no broker stop (nothing to do),
    'cancelled' the stop is gone (cancelled now, or it was already dead): the cover BUY may go,
    'filled'    the stop already filled: the short is flat, NO BUY may be sent (the guard books the STOP_HIT),
    'error'     the stop filled in part, or could not be cancelled or read back: NO BUY may be sent now (a full-size BUY could double-cover into a long); retry later.
    Sets pos.plain_stop_order_id to None on 'cancelled' (the caller commits)."""
    sid = getattr(pos, "plain_stop_order_id", None)
    if not sid:
        return "none", ""
    qty = int(getattr(pos, "quantity", 0) or 0)

    def judge(row: Optional[dict], ok: bool) -> Optional[Tuple[str, str]]:
        if ok and row is not None and _status(row) in FILLED:
            return "filled", f"the broker stop {sid} already filled"
        if ok and row is not None and _partly(row, qty):
            return "error", f"the broker stop {sid} filled in part ({_raw_filled(row)} of {qty}): no cover was sent, check Dhan"
        if ok and row is not None and _status(row) in DEAD:
            pos.plain_stop_order_id = None
            return "cancelled", ""
        return None

    row, ok = _row(db, sid)
    v = judge(row, ok)
    if v:
        return v
    try:
        broker.cancel_cnc_stop_loss_order(db, order_id=sid)
    except Exception as e:  # noqa: BLE001 - may have filled in between: the read-back below decides
        logger.info("plain short: cancel of stop %s (%s) not done: %s", sid, getattr(pos, "symbol", "?"), e)
    sleep(float(getattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0.5) or 0.0))
    row, ok = _row(db, sid)
    v = judge(row, ok)
    if v:
        return ("filled", f"the broker stop {sid} filled while it was being cancelled") if v[0] == "filled" else v
    return "error", f"the broker stop {sid} could not be cancelled and read back ({_status(row) or 'unreadable'}): no cover was sent"


# ---------------------------------------------------------------- the guard
def _plain_shorts(db) -> List[Any]:
    from models import ScalpPosition
    rows = (db.query(ScalpPosition).filter(ScalpPosition.status == "OPEN", ScalpPosition.dhan_super_order_id.is_(None),
                                           ScalpPosition.dhan_entry_order_id.isnot(None)).all())
    return [p for p in rows if str(getattr(p, "side", "") or "").strip().upper() == "SHORT" and not getattr(p, "overnight_converted_to_cnc", False)]


def _last_tick(symbol: str):
    try:
        from orders import stop_watchdog
        return stop_watchdog._last_tick(symbol)
    except Exception:  # noqa: BLE001
        return None, None


def _book_stop_fill(db, pos, row: dict) -> None:
    """The broker stop filled: book STOP_HIT at the real fill (same bookkeeping as reconcile's bracket exit)."""
    from capital import ledger, shared_symbol_lock
    from orders import reconcile, side as _side
    stop = _num(pos.stop_price) or 0.0
    exit_price = _fill_price(row) or stop
    confirmed = _fill_price(row) is not None
    try:
        reconcile.note_stop_hit(pos, exit_price, confirmed)
    except Exception as e:  # noqa: BLE001
        logger.debug("plain short: note_stop_hit skipped (%s)", e)
    pnl = _side.pnl(pos, pos.entry_price, exit_price, pos.quantity)
    pct = _side.pnl_pct(pos, pos.entry_price, exit_price) if pos.entry_price else 0.0
    pos.status, pos.exit_price, pos.realized_pnl, pos.realized_pnl_pct = "STOP_HIT", exit_price, pnl, pct
    pos.dhan_exit_order_id = str(pos.plain_stop_order_id or "") or None
    pos.error_message = None if confirmed else "STOP_HIT booked at the stop price: Dhan showed no average traded price (check the trade book)"
    pos.closed_at = datetime.now(timezone.utc)
    db.commit()
    ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=pnl)
    shared_symbol_lock.release(db, pos.symbol)
    _stats["stop_filled"] += 1
    logger.info("plain short: %s (id=%d) STOP_HIT @ %.2f P&L %.2f", pos.symbol, pos.id, exit_price, pnl)
    _alert(f"🔴 <b>STOP_HIT</b> - {pos.symbol} x{pos.quantity} (short, plain order)\nEntry ₹{pos.entry_price:.2f} → Cover ₹{exit_price:.2f}\nP&L ₹{pnl:,.2f} ({pct:.2f}%)")


def _find_cover(db, pos) -> Tuple[Optional[dict], bool]:
    """(a live-or-filled BUY at Dhan for this security and quantity that a cover of ours would have created, readable). Never raises."""
    try:
        rows = broker.get_order_list(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("plain short: cover lookup for %s failed (%s)", getattr(pos, "symbol", "?"), e)
        return None, False
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("transactionType") or r.get("transaction_type") or "").upper() != "BUY":
            continue
        if str(r.get("securityId") or r.get("security_id") or "") != str(pos.dhan_security_id):
            continue
        if str(r.get("orderType") or r.get("order_type") or "").upper() in ("STOP_LOSS_MARKET", "SL-M", "SLM", "STOP_LOSS"):
            continue                                            # the stop itself is not a cover
        if _status(r) in DEAD:
            continue
        try:
            if int(float(r.get("quantity") or 0)) != int(pos.quantity):
                continue
        except (TypeError, ValueError):
            continue
        if str(r.get("tag") or r.get("correlationId") or "") not in _COVER_TAGS:
            continue
        return r, True
    return None, True


def _adopt_cover(db, pos, row: dict, reason: str) -> None:
    """A cover BUY already exists at Dhan: record the position as closed-pending-reconcile on it (same bookkeeping as eod_squareoff.close_position_now), never send a second."""
    from capital import ledger, shared_symbol_lock
    oid = str(row.get("orderId") or row.get("order_id") or "")
    pos.dhan_exit_order_id = oid or None
    pos.plain_stop_order_id = None
    pos.status = reason
    pos.closed_at = datetime.now(timezone.utc)
    pos.exit_price = pos.entry_price
    pos.realized_pnl, pos.realized_pnl_pct = 0.0, 0.0
    pos.error_message = f"{reason}_PENDING_RECONCILE: adopted the cover BUY {oid} found at Dhan; exit_price=entry_price placeholder until reconcile fills in the real price."
    db.commit()
    ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=0.0)
    shared_symbol_lock.release(db, pos.symbol)
    _stats["cover_adopted"] += 1
    _alert(f"⚠️ <b>Short cover found at Dhan</b> - {pos.symbol} x{pos.quantity}\nBUY {oid} already exists: adopted as {reason}, no second BUY sent.")


def _cover_now(db, pos, reason: str, label: str, now: float) -> bool:
    from orders import eod_squareoff
    row, readable = _find_cover(db, pos)
    if not readable:
        _next_try[pos.id] = now + 5.0
        return False                                            # cannot tell whether a cover already exists: no BUY blind, try again soon
    if row is not None:
        try:
            _adopt_cover(db, pos, row, reason)
            return True
        except Exception as e:  # noqa: BLE001
            try:
                db.rollback()
            except Exception as _e:  # noqa: BLE001
                logger.debug("plain_short.py: rollback ignored %s", _e)
            logger.error("plain short: adopting the cover of %s failed (%s)", pos.symbol, e)
            return False
    try:
        eod_squareoff.close_position_now(db, pos, exit_reason=reason)
        return True
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("plain_short.py: rollback ignored %s", _e)
        _next_try[pos.id] = now + float(getattr(config, "STOP_WATCHDOG_RETRY_S", 30.0))
        logger.warning("plain short: %s (id=%s) %s cover not placed (%s) - retry later", pos.symbol, pos.id, label, e)
        return False


def run_plain_short_guard(db, now_mono: Optional[float] = None) -> int:
    """One pass: the watched SELLs first, then every open plain-order short. Returns how many were closed, covered or resolved this pass. Never raises.
    Runs whatever SHORT_PLAIN_FALLBACK says: the switch gates new plain shorts only."""
    now = time.monotonic() if now_mono is None else now_mono
    done = 0
    try:
        done += run_orphan_check(db, now)
    except Exception as e:  # noqa: BLE001
        logger.error("plain short guard: orphan check failed (%s)", e)
    try:
        positions = _plain_shorts(db)
    except Exception as e:  # noqa: BLE001
        logger.error("plain short guard: query failed (%s)", e)
        return done
    live_ids = {p.id for p in positions}
    for d in (_breach_since, _next_try, _replaced):
        for k in [k for k in d if k not in live_ids]:
            d.pop(k, None)
    max_age = float(getattr(config, "STOP_WATCHDOG_MAX_TICK_AGE_S", 15.0))
    for pos in positions:
        try:
            if now < _next_try.get(pos.id, 0.0):
                continue
            # 1. the broker stop
            if pos.plain_stop_order_id:
                row, ok = _row(db, pos.plain_stop_order_id)
                st = _status(row) if (ok and row is not None) else ""
                if st in FILLED:
                    _book_stop_fill(db, pos, row)
                    done += 1
                    continue
                if ok and row is not None and _partly(row, pos.quantity):
                    if st in DEAD:
                        _alert_once(pos.id, "partial", f"🚨 <b>Short stop filled in part</b> - {pos.symbol}: {_raw_filled(row)} of {pos.quantity} shares, then the order {st}.\nNot booked automatically: check Dhan and cover the rest by hand.")
                    continue                                    # a live part-filled stop completes by itself; never cover on top of it
                if st in DEAD:
                    cov, readable = _find_cover(db, pos)
                    if not readable or cov is not None:
                        if cov is not None:
                            _adopt_cover(db, pos, cov, "STOP_BREACH_EXIT")
                            done += 1
                        continue
                    if _replaced.get(pos.id, 0) < 1:
                        _replaced[pos.id] = 1
                        try:
                            s = broker.place_intraday_buy_stop(db, is_armed=True, security_id=pos.dhan_security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                                                               quantity=int(pos.quantity), trigger_price=float(pos.stop_price), tag=TAG_STOP)
                            pos.plain_stop_order_id = str((s or {}).get("orderId") or "") or None
                            db.commit()
                            _stats["stop_replaced"] += 1
                            _alert(f"⚠️ <b>Short stop {st}</b> - {pos.symbol} x{pos.quantity}\nThe broker stop was {st}; a new one was placed at ₹{pos.stop_price:.2f}.")
                            continue
                        except Exception as e:  # noqa: BLE001
                            db.rollback()
                            logger.warning("plain short: %s stop re-place failed (%s)", pos.symbol, e)
                    _alert_once(pos.id, "lost", f"🚨 <b>Short stop lost</b> - {pos.symbol} x{pos.quantity}\nThe broker stop is {st} and could not be re-placed. Covering at market.")
                    pos.plain_stop_order_id = None
                    db.commit()
                    if _cover_now(db, pos, "STOP_BREACH_EXIT", "lost-stop", now):
                        _stats["breach_cover"] += 1
                        done += 1
                    continue
            else:
                # no stop on record (the stop and the cover both failed at entry, or it was cleared): cover by hand
                _alert_once(pos.id, "nostop", f"🚨 <b>Short with no stop</b> - {pos.symbol} x{pos.quantity}\nNo broker stop is on record. Covering at market (STOP_BREACH_EXIT).")
                if _cover_now(db, pos, "STOP_BREACH_EXIT", "no-stop", now):
                    _stats["breach_cover"] += 1
                    done += 1
                continue
            # 2. price rules (a fresh tick only)
            ltp, age = _last_tick(pos.symbol)
            if ltp is None or age is None or age > max_age:
                _breach_since.pop(pos.id, None)
                continue
            if pos.target_price and ltp <= float(pos.target_price) + 1e-9:
                if _cover_now(db, pos, "TARGET_EXIT", "target", now):
                    _stats["target_exit"] += 1
                    done += 1
                continue
            from orders import stop_watchdog
            if not stop_watchdog.is_breached(ltp, pos.stop_price, short=True):
                _breach_since.pop(pos.id, None)
                continue
            first = _breach_since.setdefault(pos.id, now)
            if now - first < float(config.SHORT_PLAIN_STOP_GRACE_S):
                continue
            logger.critical("plain short: %s (id=%d) price %.2f is past its stop %.2f for %.0fs and the broker stop has not fired - covering at market", pos.symbol, pos.id, ltp, pos.stop_price, now - first)
            if _cover_now(db, pos, "STOP_BREACH_EXIT", "breach", now):
                _stats["breach_cover"] += 1
                done += 1
                _breach_since.pop(pos.id, None)
                _alert(f"🚨 <b>Short stop did not fire</b> - {pos.symbol} x{pos.quantity}\nPrice ₹{ltp:.2f} sat over the stop ₹{pos.stop_price:.2f}. Covered at market (STOP_BREACH_EXIT).")
        except Exception as e:  # noqa: BLE001
            try:
                db.rollback()
            except Exception as _e:  # noqa: BLE001
                logger.debug("plain_short.py: rollback ignored %s", _e)
            logger.error("plain short guard: %s (id=%s) unexpected error: %s", getattr(pos, "symbol", "?"), getattr(pos, "id", "?"), e, exc_info=True)
    return done
