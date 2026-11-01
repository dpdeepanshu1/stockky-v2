"""orders/half_sell_probe.py - E283 / E285 round 98: the Dhan HALF-SELL probe. Position Stocks only. Run it ONCE, supervised, before `MOMENTUM_SCALEOUT` is built.

WHAT (the protocol, written down here; the separate Markdown note was removed in round 124 because .gitignore keeps only README and CHANGELOG): `modify_super_order` changes only a leg's PRICE, so selling half of a position that sits under a Super Order cannot be one
modify. The planned sequence is: cancel both legs, sell half at market, place a new stop for the rest. Nothing in this repo has proven that on Dhan. This runs it once, at the
smallest size, and writes down what Dhan did:
  T0 BUY 2 shares INTRADAY with a Super Order (target above, stop below), wait for the fill, read the order back
  T1 cancel the TARGET leg                           -> Q1 does the stop leg stay active?
  T2 cancel the STOP_LOSS leg                        -> Q2 what do the parent order and its legs show now?
  T3 plain MARKET SELL of 1 share                    -> Q3 does Dhan accept it while the parent Super Order exists?
  T4 SELL STOP_LOSS_MARKET for the remaining 1 share -> Q4 which stop order type Dhan accepted, and Q5 the seconds between T2 and T4 (the unprotected window)
  T5 read the order book, the Super Order and the positions again -> leftovers, position quantity
  T6 cancel the new stop, MARKET SELL the last share -> Q6 final quantity, orphan orders, round-trip charges (an estimate from our rate card; the contract note is the truth)
  Q7 the order-rate counter (execution/order_rate) before and after: did five-plus order calls in a few seconds stay inside the budget?
Verdict: `HALF-SELL PROBE: PASS | FAIL - reason | SKIPPED - reason`. A FAIL is a result: it says half-sell must be dropped or redesigned.

SAFETY (every rail fails closed; nothing runs unless the caller passes the exact confirm phrase):
  * quantities are constants (BUY 2, SELL 1, SELL 1). They are not parameters. The stock must trade under MAX_PRICE so the whole probe costs under Rs 1,000.
  * only between PROBE_FROM_IST and PROBE_UNTIL_IST (09:30 to 14:30); service armed, kill switch not tripped; the symbol flat at Dhan (every row, every product), not held by either
    service, no open row here; one probe at a time; symbol lock and order budget claimed before the first order and handed back at the end.
  * a SELL is sent ONLY when Dhan's own POSITIONS show a long to sell, and never more than that long (a SELL with no long under it would open a SHORT). When the positions cannot
    be read nothing is sold and the failure is shouted (Telegram).
  * a SELL is never sent while an exit leg of the Super Order is still live (a live stop leg plus a market SELL could sell the same share twice).
  * the probe stops at the first surprise and does NOT retry: it goes to the cleanup, which cancels what is ours, sells what is left (once), and confirms flat from the positions.
  * if the cleanup cannot get flat, what is left is adopted as a normal LONG position row (stop and target as sent), so the stop watchdog, reconcile and the EOD sweep own it.

Nothing in this file touches orders/entry.py or the live exit path. Stdlib + the service's own modules.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from orders.sell_probe import (_DEAD, _FILLED, _buy_avg, _compact, _hhmm, _leg, _net_qty, _open_rows, _pos_rows, _read_positions, _row_for, _sell_avg, _status,
                               _tell)

logger = logging.getLogger("position-stocks-half-sell-probe")

BUY_QTY = 2                                   # constants on purpose: not switches, not parameters
SELL_QTY = 1
KEEP_QTY = BUY_QTY - SELL_QTY
CONFIRM_PHRASE = "PLACE_REAL_HALF_SELL_PROBE"
PROBE_FROM_IST = "09:30"
PROBE_UNTIL_IST = "14:30"
MAX_PRICE = 500.0                             # 2 shares under Rs 1,000
DEFAULT_SYMBOLS = ("ITC", "NTPC", "ONGC", "SBIN", "TATASTEEL")
STOP_PCT = 1.5                                # the Super Order's own stop sits this far below the buy price ...
TARGET_PCT = 3.0                              # ... and its target this far above. The probe cancels both at once; they only have to exist.
NEW_STOP_PCT = 1.0                            # the replacement stop for the kept share sits this far below the last price
WINDOW_TARGET_S = 3.0                         # the protocol's goal for the unprotected window (T2 -> T4)
WINDOW_FAIL_S = 10.0                          # slower than this is a FAIL: the half-sell would leave a position unprotected for too long
TAG_ENTRY = "HALFPROBE"
TAG_SELL = "HALFPROBE_SELL"
TAG_STOP = "HALFPROBE_STOP"
TAG_FINAL = "HALFPROBE_FINAL"
_OPEN_STATES = {"PENDING", "TRANSIT", "OPEN", "TRIGGER_PENDING", "PART_TRADED", "PARTIALLY_TRADED"}

_lock = threading.Lock()                      # one probe at a time
_mem: Dict[str, Any] = {}


# ---------------------------------------------------------------- the last result (file + memory, same pattern as sell_probe)
def _path() -> str:
    return os.path.join((os.environ.get("CONTROL_DIR") or "").strip() or "/data/control", "half_sell_probe_ps.json")


def last_result() -> Dict[str, Any]:
    """The last probe result, or {} when none has run. Never raises."""
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return d
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.error("half-sell probe: saved result unreadable (%s: %s); using the in-memory copy", type(e).__name__, e)
    return dict(_mem)


def _save(res: Dict[str, Any]) -> None:
    _mem.clear()
    _mem.update(res)
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".hs", dir=os.path.dirname(p))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(res, fh, default=str)
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        logger.error("half-sell probe: result not persisted (%s: %s); kept in memory", type(e).__name__, e)


def passed() -> bool:
    """True when the last probe on record is a PASS. Unreadable or missing = False."""
    try:
        return str(last_result().get("verdict") or "").upper() == "PASS"
    except Exception:  # noqa: BLE001
        return False


def summary() -> Dict[str, Any]:
    """Small public view: the verdict line and the answers Q1..Q7, no raw broker payloads."""
    r = last_result()
    if not r:
        return {"ran": False, "verdict": "NOT_RUN", "line": "HALF-SELL PROBE: NOT_RUN - E283 has not been run yet", "passed": False}
    return {"ran": True, "verdict": r.get("verdict"), "line": r.get("line"), "at": r.get("at"), "symbol": r.get("symbol"), "passed": passed(),
            "q": r.get("q") or {}, "rules": r.get("rules") or []}


# ---------------------------------------------------------------- the run record
class _Run:
    """Collects the steps, the timeline, the answers Q1..Q7 and the verdict of one probe."""

    def __init__(self, symbol: str, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.t0 = clock()
        self.res: Dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "symbol": symbol, "buy_qty": BUY_QTY, "sell_qty": SELL_QTY, "steps": [], "rules": [],
                                    "q": {}, "timeline": {}, "answers": {}}

    def step(self, msg: str) -> None:
        self.res["steps"].append(f"+{self.clock() - self.t0:5.1f}s {msg}")
        logger.info("half-sell probe: %s", msg)

    def rule(self, msg: str) -> None:
        self.res["rules"].append(msg)

    def mark(self, name: str) -> float:
        t = self.clock()
        self.res["timeline"][name] = round(t - self.t0, 2)
        return t

    def done(self, verdict: str, reason: str) -> Dict[str, Any]:
        self.res["verdict"] = verdict
        self.res["reason"] = reason
        self.res["line"] = f"HALF-SELL PROBE: {verdict} - {reason}"
        self.res["finished_at"] = datetime.now(timezone.utc).isoformat()
        return self.res


def _skip(symbol: str, reason: str, clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """A refusal before any order: reported, never saved over a real earlier result (a SKIPPED run says nothing about Dhan)."""
    r = _Run(symbol, clock)
    r.step(f"not run: {reason}")
    return r.done("SKIPPED", reason)


def _live_exit_legs(row: Optional[dict]) -> List[str]:
    """Names of the exit legs (not the entry) of a Super Order row that are neither dead nor filled."""
    return [str(lg.get("legName")) for lg in (row or {}).get("legDetails") or []
            if isinstance(lg, dict) and str(lg.get("legName", "")).upper() != "ENTRY_LEG" and _status(lg) not in _DEAD | _FILLED]


def _long_beyond(positions: Any, security_id: str, base: int) -> int:
    """Shares long beyond the baseline right now (negative = short beyond it)."""
    return _net_qty(positions, security_id) - base


def _open_orders_for(db, broker, security_id: str) -> Optional[List[dict]]:
    """Plain orders of this security that are still live at Dhan, or None when the book cannot be read."""
    try:
        rows = broker.get_order_list(db) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("half-sell probe: order book read failed (%s)", str(e)[:120])
        return None
    return [r for r in rows if isinstance(r, dict) and str(r.get("securityId") or r.get("security_id") or "") == str(security_id) and _status(r) in _OPEN_STATES]


def _ours(r: dict, tracked: set) -> bool:
    return str(r.get("orderId") or r.get("order_id") or "") in tracked or str(r.get("correlationId") or r.get("tag") or "").startswith(TAG_ENTRY)


def _compact_order(r: dict) -> Dict[str, Any]:
    return {k: r.get(k) for k in ("orderId", "orderStatus", "transactionType", "orderType", "productType", "quantity", "filledQty", "price", "triggerPrice", "averageTradedPrice",
                                  "correlationId", "omsErrorDescription") if k in r}


# ---------------------------------------------------------------- the cleanup: nothing is ever left open
def _unwind(db, broker, run: _Run, oid: str, security_id: str, sym: str, base: int, tracked: set, sleep, poll_s: float, entry_ref: float, stop_px: float, target_px: float,
            why: str) -> Tuple[bool, str]:
    """Leave nothing open. Cancel every live order of ours (our plain orders by id/tag, the Super Order's legs), then, only if Dhan's positions show a long beyond the baseline and
    no exit leg is live, SELL exactly that long once at MARKET and confirm flat from the positions. Returns (flat, note). Never raises."""
    try:
        run.step(f"cleanup ({why}): cancelling what is ours, then flattening")
        got = run.res["answers"].setdefault("cleanup", {})
        book = _open_orders_for(db, broker, security_id)
        for r in (book or []):
            if _ours(r, tracked):
                rid = str(r.get("orderId") or r.get("order_id") or "")
                try:
                    got[f"cancel_{rid}"] = str(broker.cancel_order(db, is_armed=True, dhan_order_id=rid))[:160]
                except Exception as e:  # noqa: BLE001
                    got[f"cancel_{rid}"] = f"not cancelled: {str(e)[:140]}"
        try:
            row0 = _row_for(broker.get_super_order_list(db), oid)
        except Exception as e:  # noqa: BLE001
            return False, f"cleanup could not read the Super Order ({str(e)[:100]}): no SELL sent (a live stop leg plus a SELL could sell twice): CHECK DHAN"
        cancelled_any = bool(got)
        for lg in (row0 or {}).get("legDetails") or []:                        # only the legs that are still live: a dead leg needs no call (fewer calls in the unprotected moment)
            if not isinstance(lg, dict) or _status(lg) in _DEAD | _FILLED:
                continue
            name = str(lg.get("legName") or "")
            try:
                got[name] = str(broker.cancel_super_order(db, order_id=oid, order_leg=name))[:160]
            except Exception as e:  # noqa: BLE001
                got[name] = f"not cancelled: {str(e)[:140]}"
            cancelled_any = True
        if cancelled_any:
            sleep(float(getattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0.5) or 0.0))
        try:
            row = _row_for(broker.get_super_order_list(db), oid)
        except Exception as e:  # noqa: BLE001
            return False, f"cleanup could not read the Super Order back ({str(e)[:100]}): no SELL sent (a live stop leg plus a SELL could sell twice): CHECK DHAN"
        live = _live_exit_legs(row)
        if live:
            return False, f"exit legs still live after the cancels ({live}): no SELL sent, the long is still protected by them: CHECK DHAN and close it by hand"
        pos = _read_positions(db, broker)
        if pos is None:
            return False, "Dhan's positions could not be read, so nothing was sold (a blind SELL could open a short): CHECK DHAN"
        long_qty = _long_beyond(pos, security_id, base)
        run.res["answers"]["cleanup_long_seen"] = long_qty
        if long_qty < 0:
            return False, f"Dhan's positions show a SHORT of {-long_qty} {sym} after the probe: nothing was bought; CHECK DHAN and cover it by hand"
        if long_qty == 0:
            run.step("cleanup: Dhan's positions are flat")
            return True, ""
        qty = min(long_qty, BUY_QTY)
        try:
            res = broker.place_order(db, is_armed=True, security_id=security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT, transaction_type="SELL", quantity=qty,
                                     order_type="MARKET", price=0.0, product_type="INTRADAY", tag=TAG_FINAL)
        except Exception as e:  # noqa: BLE001
            return False, f"the cleanup SELL of {qty} was refused or unclear ({str(e)[:140]}): not retried; CHECK DHAN for {qty} {sym}"
        fid = str((res or {}).get("orderId") or (res or {}).get("order_id") or "")
        if fid:
            tracked.add(fid)
        run.step(f"cleanup SELL {qty} MARKET placed, id {fid or '?'}")
        waited = 0.0
        while waited <= max(1.0, poll_s) * 2:
            pos = _read_positions(db, broker)
            if pos is not None and _long_beyond(pos, security_id, base) == 0 and _pos_rows(pos, security_id):
                run.step("cleanup: Dhan's positions are flat")
                return True, ""
            sleep(1.0)
            waited += 1.0
        return False, f"the cleanup SELL {fid} was not confirmed flat within {max(1.0, poll_s) * 2:.0f}s"
    except Exception as e:  # noqa: BLE001
        logger.error("half-sell probe: cleanup crashed (%s: %s)", type(e).__name__, e, exc_info=True)
        return False, f"the cleanup crashed ({type(e).__name__}): CHECK DHAN"


def _adopt_long(db, run: _Run, sym: str, security_id: str, qty: int, fill: float, stop: float, target: float, oid: str) -> bool:
    """The cleanup could not get flat: record what is left as an ordinary open LONG row so the existing exit side owns it. Never raises."""
    try:
        from models import ScalpPosition
        pos = ScalpPosition(
            symbol=sym, dhan_security_id=str(security_id), window_source="PROBE", status="OPEN", side="LONG", entry_price=float(fill), signal_price=float(fill),
            quantity=int(qty), target_price=float(target), stop_price=float(stop), adaptive_target_pct=TARGET_PCT, adaptive_stop_pct=STOP_PCT,
            dhan_super_order_id=oid, dhan_entry_order_id=oid, capital_risked=float(fill) * int(qty), is_first_live_order=False, opened_at=datetime.now(timezone.utc))
        db.add(pos)
        db.commit()
        run.step(f"adopted the leftover {qty} share(s) as position row {pos.id}")
        return True
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("half_sell_probe.py: rollback ignored %s", _e)
        logger.critical("half-sell probe: could not adopt the leftover %s (%s)", sym, e)
        return False


# ---------------------------------------------------------------- the probe
def run_probe(db, symbol: Optional[str] = None, confirm: str = "", *,
              broker: Any = None, price_fn: Optional[Callable[[str], Optional[float]]] = None, now: Optional[datetime] = None,
              sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time, poll_s: float = 20.0,
              notify: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Run the half-sell probe once. Never raises. Returns the result dict (also saved, except for SKIPPED). broker / price_fn / sleep / clock are injectable for tests."""
    sym = (symbol or "").strip().upper()
    if confirm != CONFIRM_PHRASE:
        return _skip(sym or "-", f"confirm phrase missing: this places REAL orders (buy {BUY_QTY}, sell {SELL_QTY}, sell {KEEP_QTY}), so the call must carry confirm={CONFIRM_PHRASE}", clock)
    if not _lock.acquire(blocking=False):
        return _skip(sym or "-", "another probe is already running", clock)
    try:
        return _run(db, sym, broker, price_fn, now, sleep, clock, poll_s, notify)
    except Exception as e:  # noqa: BLE001 - the last-resort net: say so loudly, never raise into the route
        logger.error("half-sell probe: unexpected error (%s: %s)", type(e).__name__, e, exc_info=True)
        res = _Run(sym or "-", clock).done("FAIL", f"unexpected error {type(e).__name__}: {str(e)[:200]} - CHECK DHAN NOW for an open {sym or 'probe'} long, a resting stop or a Super Order")
        _save(res)
        _tell(notify, f"🚨 <b>Half-sell probe crashed</b> ({type(e).__name__}). Check Dhan for an open long, a resting stop order or a Super Order and clear it by hand.")
        return res
    finally:
        _lock.release()


def _run(db, sym, broker, price_fn, now, sleep, clock, poll_s, notify) -> Dict[str, Any]:  # noqa: C901 - one straight line of steps, each with its own refusal
    from capital import shared_order_budget, shared_symbol_lock
    from execution import order_rate
    from models import ScalpGateState, ScalpPosition
    from orders import approve_first, charges_ledger, short_entry
    from tz_utils import ist_now, is_market_open_ist

    if broker is None:
        from execution import dhan_client as broker
    price_fn = price_fn or short_entry._live_price

    # ---- 1. refusals before anything is claimed or sent
    gate = db.query(ScalpGateState).first()
    if gate is None or not getattr(gate, "is_armed", False):
        return _skip(sym or "-", "the service is not armed", clock)
    if gate.daily_loss_kill_switch_tripped:
        return _skip(sym or "-", "the daily-loss kill switch is tripped", clock)
    if not config.USE_SUPER_ORDER:
        return _skip(sym or "-", "USE_SUPER_ORDER is off: the probe tests a position that sits under a Super Order", clock)
    if approve_first.active():
        return _skip(sym or "-", "an approve-first round is open", clock)
    t = ist_now(now)
    if not is_market_open_ist(now) or not (_hhmm(PROBE_FROM_IST) <= t.time() <= _hhmm(PROBE_UNTIL_IST)):
        return _skip(sym or "-", f"outside the probe window ({PROBE_FROM_IST}-{PROBE_UNTIL_IST} IST on a trading day)", clock)

    candidates = [sym] if sym else list(DEFAULT_SYMBOLS)
    live, security_id, base, why_not = None, None, 0, []
    for c in candidates:
        px = price_fn(c)
        if not (px and px > 0):
            continue
        if px > MAX_PRICE:
            why_not.append(f"{c} trades at {px:.2f}, above the {MAX_PRICE:.0f} cap that keeps the probe under Rs 1,000")
            continue
        try:
            sid = broker.get_security_id(db, c)
        except Exception as e:  # noqa: BLE001
            why_not.append(f"no Dhan security id for {c} ({e})")
            continue
        if not sid:
            why_not.append(f"no Dhan security id for {c}")
            continue
        pos0 = _read_positions(db, broker)
        if pos0 is None:
            return _skip(c, "Dhan's positions could not be read, so the probe cannot tell its own shares from a position that was already there", clock)
        held = _net_qty(pos0, str(sid))
        if held != 0 or _open_rows(pos0, str(sid)):
            why_not.append(f"{c} already has a position at Dhan (net {held:+d}) that Stockky did not open: close it in Dhan first")
            continue
        sym, live, security_id, base = c, float(px), str(sid), held
        break
    if live is None:
        return _skip(candidates[0], "; ".join(why_not) if why_not else f"no live price for {', '.join(candidates)} (the tick feed has nothing yet)", clock)
    if db.query(ScalpPosition).filter(ScalpPosition.symbol == sym, ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED"))).first():
        return _skip(sym, f"{sym} already has an open position here", clock)

    run = _Run(sym, clock)
    if not shared_symbol_lock.try_claim(db, sym):
        return _skip(sym, f"{sym} is held by the other service", clock)
    claimed = True
    tracked: set = set()
    try:
        if not shared_order_budget.check_and_reserve(db):
            return _skip(sym, "the shared daily order budget is used up", clock)
        rate0 = order_rate.snapshot()

        # ---- 2. T0: BUY 2 with a Super Order (legs below / above), the same call shape the bot's long entry uses
        tick = broker.round_to_tick
        t_px, s_px, notes = broker.clamp_super_levels("BUY", live, tick(live * (1 + TARGET_PCT / 100.0)), tick(live * (1 - STOP_PCT / 100.0)))
        run.res.update(ref_price=live, target_price=t_px, stop_price=s_px, security_id=str(security_id))
        run.step(f"BUY {BUY_QTY} {sym} ref {live:.2f}: MARKET entry, target {t_px:.2f} (above), stop {s_px:.2f} (below)" + (f" [{'; '.join(notes)}]" if notes else ""))
        try:
            placed = broker.place_super_order(db, is_armed=True, security_id=security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT, transaction_type="BUY",
                                              quantity=BUY_QTY, order_type="MARKET", price=live, target_price=float(t_px), stop_loss_price=float(s_px), trailing_jump=0.0,
                                              product_type="INTRADAY", tag=TAG_ENTRY)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            found = None
            if broker.is_ambiguous_placement_error(e):
                try:
                    row0 = broker.find_recent_buy_order(db, security_id, BUY_QTY, super_order=True, transaction_type="BUY")
                    found = str((row0 or {}).get("orderId") or (row0 or {}).get("order_id") or (row0 or {}).get("id") or "") or None
                except Exception as le:  # noqa: BLE001
                    logger.warning("half-sell probe: lookup after an unclear BUY failed (%s)", le)
            if not found:
                run.res["answers"]["place_refused"] = msg[:600]
                if broker.is_ambiguous_placement_error(e):
                    run.rule(f"The BUY placement errored on our side ({msg[:200]}) and no matching order was found at Dhan.")
                    res = run.done("FAIL", f"the BUY placement errored on our side ({msg[:160]}) - it may exist at Dhan: CHECK DHAN for {sym} shares or a resting Super Order")
                    _tell(notify, f"🚨 <b>Half-sell probe: placement unclear</b> - {sym}\n{msg[:300]}\nCheck Dhan for shares or a resting Super Order and clear it by hand.")
                else:
                    run.rule(f"Dhan REFUSED a {BUY_QTY}-share INTRADAY BUY Super Order: {msg[:300]}")
                    res = run.done("FAIL", f"Dhan refused the BUY Super Order: {msg[:240]}")
                _save(res)
                return res
            placed = {"orderId": found, "recovered_after": msg[:120]}
            run.step(f"the BUY errored ({msg[:80]}) but Dhan has order {found}: carrying on with it")
        oid = str((placed or {}).get("orderId") or (placed or {}).get("id") or "")
        if not oid:
            res = run.done("FAIL", f"Dhan answered the BUY without an order id ({str(placed)[:160]}) - CHECK DHAN for {sym} shares")
            _save(res)
            _tell(notify, f"🚨 <b>Half-sell probe: no order id</b> - {sym}. Check Dhan for shares and clear them by hand.")
            return res
        tracked.add(oid)
        run.res["order_id"] = oid
        run.mark("t0_buy_placed")
        run.step(f"Dhan accepted the BUY Super Order, id {oid}")
        try:
            from tz_utils import ist_today_str
            today = ist_today_str(now)
            if gate.orders_placed_today_date != today:
                gate.orders_placed_today, gate.orders_placed_today_date = 0, today
            gate.orders_placed_today = int(gate.orders_placed_today or 0) + 1
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.warning("half-sell probe: order-budget count failed (%s)", e)

        problems: List[str] = []
        q = run.res["q"]

        def bail(reason: str) -> Dict[str, Any]:
            """Stop at the first surprise: no retry, clean up, record, shout."""
            flat, note = _unwind(db, broker, run, oid, security_id, sym, base, tracked, sleep, poll_s, live, float(s_px), float(t_px), reason)
            problems.append(reason)
            if not flat:
                pos_left = _read_positions(db, broker)
                left = max(0, _long_beyond(pos_left, security_id, base)) if pos_left is not None else BUY_QTY
                adopted = bool(left) and _adopt_long(db, run, sym, security_id, left, float(run.res.get("entry_fill") or live), float(s_px), float(t_px), oid)
                problems.append(f"NOT FLAT ({note}); " + ("adopted as a normal LONG row: the stop watchdog and the EOD sweep own it now" if adopted else "close it by hand in Dhan NOW"))
                _tell(notify, f"🚨 <b>Half-sell probe: not flat</b> - {sym}\n{note}\n" + ("It is now a normal long position here." if adopted else "Close it by hand in Dhan NOW."))
            res = run.done("FAIL", "; ".join(problems)[:700])
            _record_rate(run, order_rate, rate0)
            _save(res)
            _tell(notify, f"⚠️ <b>{res['line']}</b>\n" + "\n".join(f"• {x}" for x in res["rules"][:6]))
            return res

        # ---- 3. wait for the fill of both shares and the legs
        row, filled = None, False
        waited = 0.0
        while True:
            try:
                row = _row_for(broker.get_super_order_list(db), oid) or row
            except Exception as e:  # noqa: BLE001
                logger.warning("half-sell probe: Super Order list read failed (%s)", e)
            pos = _read_positions(db, broker)
            if pos is not None and _long_beyond(pos, security_id, base) >= BUY_QTY and _leg(row, "STOP_LOSS_LEG") is not None and _leg(row, "TARGET_LEG") is not None:
                filled = True
                break
            if waited >= poll_s:
                break
            sleep(1.0)
            waited += 1.0
        run.res["answers"]["t0_row"] = _compact(row)
        if not filled:
            pos = _read_positions(db, broker)
            run.rule(f"The BUY Super Order did not show {BUY_QTY} shares filled with both legs listed within {poll_s:.0f}s (row status {_status(row) or 'unreadable'}).")
            return bail(f"the BUY did not fill {BUY_QTY} shares with both legs within {poll_s:.0f}s (status {_status(row) or 'unreadable'}, long seen "
                        f"{_long_beyond(pos, security_id, base) if pos is not None else 'unreadable'})")
        fill = _buy_avg(_read_positions(db, broker) or [], security_id) or live
        run.res["entry_fill"] = fill
        run.mark("t0_filled")
        run.step(f"T0: {BUY_QTY} shares filled at about {fill:.2f}; legs: " + ", ".join(f"{lg.get('legName')}={_status(lg)}" for lg in (row or {}).get("legDetails") or [] if isinstance(lg, dict)))
        run.rule(f"Dhan accepted a {BUY_QTY}-share INTRADAY BUY Super Order and filled it; the stop and target legs were listed.")
        q["Q0_legs_at_t0"] = {str(lg.get("legName")): _status(lg) for lg in (row or {}).get("legDetails") or [] if isinstance(lg, dict)}

        # ---- 4. T1: cancel the TARGET leg only. Q1: does the stop leg stay active?
        try:
            run.res["answers"]["t1_cancel_target"] = str(broker.cancel_super_order(db, order_id=oid, order_leg="TARGET_LEG"))[:200]
        except Exception as e:  # noqa: BLE001
            run.res["answers"]["t1_cancel_target"] = f"not cancelled: {str(e)[:160]}"
            run.rule(f"Dhan refused to cancel the TARGET leg alone: {str(e)[:200]}")
            return bail(f"cancelling the TARGET leg failed ({str(e)[:140]})")
        t1 = run.mark("t1_target_cancelled")
        sleep(float(getattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0.5) or 0.0))
        try:
            row1 = _row_for(broker.get_super_order_list(db), oid)
        except Exception as e:  # noqa: BLE001
            return bail(f"could not read the Super Order after the TARGET cancel ({str(e)[:120]})")
        run.res["answers"]["t1_row"] = _compact(row1)
        stop_after = _status(_leg(row1, "STOP_LOSS_LEG"))
        q["Q1_stop_leg_after_target_cancel"] = stop_after or "not observed"
        q["Q1_stop_still_active"] = bool(stop_after) and stop_after not in _DEAD | _FILLED
        run.step(f"T1: TARGET leg cancelled; the stop leg reads {stop_after or 'not observed'} (Q1)")
        run.rule(f"Q1: after the target leg is cancelled the stop leg is {stop_after or 'not observed'}"
                 + (" - it stays active, so the order 'cancel target, sell half, then modify the stop' leaves no unprotected window." if q["Q1_stop_still_active"] else "."))

        # ---- 5. T2: cancel the STOP_LOSS leg. Q2: the parent's status and quantity. The unprotected window starts here (or at T1, when the stop died with the target).
        if q["Q1_stop_still_active"]:
            try:
                run.res["answers"]["t2_cancel_stop"] = str(broker.cancel_super_order(db, order_id=oid, order_leg="STOP_LOSS_LEG"))[:200]
            except Exception as e:  # noqa: BLE001
                run.res["answers"]["t2_cancel_stop"] = f"not cancelled: {str(e)[:160]}"
                run.rule(f"Dhan refused to cancel the STOP_LOSS leg: {str(e)[:200]}")
                return bail(f"cancelling the STOP_LOSS leg failed ({str(e)[:140]})")
            t2 = run.mark("t2_stop_cancelled")
            sleep(float(getattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0.5) or 0.0))
            try:
                row2 = _row_for(broker.get_super_order_list(db), oid)
            except Exception as e:  # noqa: BLE001
                return bail(f"could not read the Super Order after the STOP cancel ({str(e)[:120]})")
        else:
            t2, row2 = t1, row1                                   # the stop leg went with the target: no second cancel exists, and the window started at T1
            run.res["answers"]["t2_cancel_stop"] = "not needed: the stop leg was already gone after the target cancel"
            run.rule("The stop leg died together with the target leg, so the position was unprotected from the target cancel (T1), not from a separate stop cancel.")
        run.res["answers"]["t2_row"] = _compact(row2)
        q["Q2_parent_after_both_cancels"] = {"orderStatus": (row2 or {}).get("orderStatus"), "quantity": (row2 or {}).get("quantity"), "filledQty": (row2 or {}).get("filledQty"),
                                             "legs": {str(lg.get("legName")): _status(lg) for lg in (row2 or {}).get("legDetails") or [] if isinstance(lg, dict)}}
        run.step(f"T2: STOP leg cancelled; parent order reads status {(row2 or {}).get('orderStatus')}, quantity {(row2 or {}).get('quantity')} (Q2)")
        run.rule(f"Q2: with both legs cancelled the parent Super Order reads {(row2 or {}).get('orderStatus')} with quantity {(row2 or {}).get('quantity')}.")
        live_legs = _live_exit_legs(row2)
        if live_legs:
            run.rule(f"An exit leg is still live after both cancels ({live_legs}), so no SELL was sent: a live stop leg plus a market SELL could sell the same share twice.")
            return bail(f"exit legs still live after both cancels ({live_legs}): no SELL sent")

        # ---- 6. T3: plain MARKET SELL of 1. Q3: accepted while the parent still exists?
        try:
            sold = broker.place_order(db, is_armed=True, security_id=security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT, transaction_type="SELL", quantity=SELL_QTY,
                                      order_type="MARKET", price=0.0, product_type="INTRADAY", tag=TAG_SELL)
            sell_id = str((sold or {}).get("orderId") or (sold or {}).get("order_id") or "")
            if not sell_id:
                raise RuntimeError(f"Dhan answered the SELL without an order id ({str(sold)[:120]})")
        except Exception as e:  # noqa: BLE001
            q["Q3_sell_accepted"] = False
            q["Q3_answer"] = str(e)[:300]
            run.rule(f"Q3: Dhan did NOT accept a separate 1-share MARKET SELL while the cancelled-leg parent exists: {str(e)[:260]}")
            return bail(f"the {SELL_QTY}-share MARKET SELL was refused or unclear ({str(e)[:160]})")
        tracked.add(sell_id)
        t3 = run.mark("t3_sell_placed")
        q["Q3_sell_accepted"] = True
        q["Q3_answer"] = f"order {sell_id} accepted"
        run.step(f"T3: MARKET SELL {SELL_QTY} accepted, id {sell_id} ({t3 - t2:.1f}s after the stop was cancelled) (Q3)")
        run.rule(f"Q3: a separate {SELL_QTY}-share INTRADAY MARKET SELL was accepted while the Super Order's legs were cancelled.")

        # ---- 7. T4: the stop for the kept share, at once (the SELL's fill is confirmed AFTER the stop is in, so the window is as short as the code can make it)
        now_px = price_fn(sym) or live
        trig = broker.round_stop_to_tick(float(min(now_px, live)) * (1 - NEW_STOP_PCT / 100.0)) if hasattr(broker, "round_stop_to_tick") else broker.round_to_tick(float(min(now_px, live)) * (1 - NEW_STOP_PCT / 100.0))
        try:
            stop_res = broker.place_intraday_sell_stop(db, is_armed=True, security_id=security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT, quantity=KEEP_QTY,
                                                       trigger_price=trig, tag=TAG_STOP)
            stop_id = str((stop_res or {}).get("orderId") or "")
        except Exception as e:  # noqa: BLE001
            q["Q4_stop_accepted"] = False
            q["Q4_answer"] = str(e)[:300]
            run.rule(f"Q4: Dhan did NOT accept a SELL STOP_LOSS_MARKET for the kept share: {str(e)[:260]}")
            return bail(f"the SELL stop for the kept share failed ({str(e)[:160]})")
        tracked.add(stop_id)
        t4 = run.mark("t4_stop_accepted")
        window = t4 - t2
        row_stop = None
        try:
            row_stop = broker.get_order_row(db, stop_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("half-sell probe: stop row read failed (%s)", e)
        q["Q4_stop_accepted"] = True
        q["Q4_order_type"] = str((row_stop or {}).get("orderType") or "STOP_LOSS_MARKET")
        q["Q4_trigger_price"] = (row_stop or {}).get("triggerPrice") or trig
        q["Q4_status"] = _status(row_stop)
        q["Q5_unprotected_window_s"] = round(window, 2)
        run.res["answers"]["t4_stop_row"] = _compact_order(row_stop or {})
        run.step(f"T4: SELL stop accepted, id {stop_id}, type {q['Q4_order_type']}, trigger {q['Q4_trigger_price']} (Q4); the unprotected window T2 -> T4 was {window:.1f}s (Q5)")
        run.rule(f"Q4: Dhan accepted a {q['Q4_order_type']} SELL order, INTRADAY, price 0, trigger {q['Q4_trigger_price']}, for the kept share.")
        run.rule(f"Q5: the unprotected window (stop cancelled -> new stop accepted) was {window:.1f}s, target under {WINDOW_TARGET_S:.0f}s.")
        if window > WINDOW_FAIL_S:
            problems.append(f"the unprotected window was {window:.1f}s, above the {WINDOW_FAIL_S:.0f}s limit")
        elif window > WINDOW_TARGET_S:
            run.rule(f"The window was above the {WINDOW_TARGET_S:.0f}s target: look at the round-trip times in the log before building the scale-out.")

        # ---- 8. confirm the SELL really filled (positions), then T5: read everything again
        waited = 0.0
        pos5 = _read_positions(db, broker)
        while pos5 is not None and _long_beyond(pos5, security_id, base) != KEEP_QTY and waited < poll_s:
            sleep(1.0)
            waited += 1.0
            pos5 = _read_positions(db, broker)
        long5 = _long_beyond(pos5, security_id, base) if pos5 is not None else None
        try:
            row5 = _row_for(broker.get_super_order_list(db), oid)
        except Exception as e:  # noqa: BLE001
            row5 = None
            logger.warning("half-sell probe: Super Order read at T5 failed (%s)", e)
        book5 = _open_orders_for(db, broker, security_id)
        run.mark("t5_read")
        q["Q5_position_qty_after_sell"] = long5
        q["Q5_open_orders"] = [_compact_order(r) for r in (book5 or [])]
        q["Q5_super_legs"] = {str(lg.get("legName")): _status(lg) for lg in (row5 or {}).get("legDetails") or [] if isinstance(lg, dict)}
        run.step(f"T5: position shows {long5} share(s) beyond the baseline (expected {KEEP_QTY}); open orders: {[r.get('orderId') for r in (book5 or [])]}; Super Order legs: {q['Q5_super_legs']}")
        if long5 != KEEP_QTY:
            problems.append(f"after the SELL Dhan's positions show {long5} share(s) long, expected {KEEP_QTY}")
        if book5 is None:
            problems.append("the order book could not be read at T5")
        else:
            stray = [r for r in book5 if str(r.get("orderId") or "") != stop_id]
            if stray:
                problems.append(f"orphan live order(s) at T5 besides our stop: {[str(r.get('orderId')) for r in stray]}")
        if _live_exit_legs(row5):
            problems.append(f"Super Order exit legs live at T5: {_live_exit_legs(row5)}")

        # ---- 9. T6: cancel the new stop, sell the last share, confirm flat
        if long5 != KEEP_QTY or book5 is None:
            return bail("; ".join(problems) or "T5 was not clean")
        try:
            run.res["answers"]["t6_cancel_stop"] = str(broker.cancel_order(db, is_armed=True, dhan_order_id=stop_id))[:200]
        except Exception as e:  # noqa: BLE001
            run.res["answers"]["t6_cancel_stop"] = f"not cancelled: {str(e)[:160]}"
            return bail(f"cancelling the new stop failed ({str(e)[:140]})")
        sleep(float(getattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0.5) or 0.0))
        try:
            stop_after_cancel = broker.get_order_row(db, stop_id)
        except Exception as e:  # noqa: BLE001
            return bail(f"could not read the stop back after cancelling it ({str(e)[:120]}): not selling the last share blind")
        if stop_after_cancel is not None and _status(stop_after_cancel) not in _DEAD:
            return bail(f"the new stop still reads {_status(stop_after_cancel)} after the cancel: the last share was not sold (a live stop plus a SELL could sell twice)")
        flat, note = _unwind(db, broker, run, oid, security_id, sym, base, tracked, sleep, poll_s, live, float(s_px), float(t_px), "T6 final sell")
        run.mark("t6_flat" if flat else "t6_not_flat")
        if not flat:
            return bail(f"the final SELL did not confirm flat ({note})")

        # ---- 10. Q6 / Q7 and the verdict
        pos_end = _read_positions(db, broker) or []
        buy_avg, sell_avg = _buy_avg(pos_end, security_id), _sell_avg(pos_end, security_id)
        book_end = _open_orders_for(db, broker, security_id)
        try:
            row_end = _row_for(broker.get_super_order_list(db), oid)
        except Exception:  # noqa: BLE001
            row_end = None
        q["Q6_final_net_qty"] = _net_qty(pos_end, security_id) - base
        q["Q6_orphan_orders"] = [_compact_order(r) for r in (book_end or []) if _ours(r, tracked)]
        q["Q6_orphan_legs"] = _live_exit_legs(row_end)
        if buy_avg and sell_avg:
            ch = charges_ledger.charges_for_values(buy_avg * BUY_QTY, sell_avg * BUY_QTY)
            q["Q6_gross_pnl_rs"] = round((sell_avg - buy_avg) * BUY_QTY, 2)
            q["Q6_charges_estimate_rs"] = round(ch["total"], 2)
            q["Q6_net_pnl_estimate_rs"] = round(q["Q6_gross_pnl_rs"] - ch["total"], 2)
        else:
            q["Q6_gross_pnl_rs"] = q["Q6_charges_estimate_rs"] = "not observed (Dhan's positions carried no buyAvg / sellAvg)"
        run.step(f"T6: final net {q['Q6_final_net_qty']:+d}, orphan orders {len(q['Q6_orphan_orders'])}, orphan legs {q['Q6_orphan_legs']}, gross {q['Q6_gross_pnl_rs']}, charges about {q['Q6_charges_estimate_rs']} (Q6)")
        if q["Q6_final_net_qty"] != 0:
            problems.append(f"the final position is {q['Q6_final_net_qty']:+d}, expected 0")
        if q["Q6_orphan_orders"]:
            problems.append(f"orphan order(s) left: {[r.get('orderId') for r in q['Q6_orphan_orders']]}")
        if q["Q6_orphan_legs"]:
            problems.append(f"orphan Super Order legs left: {q['Q6_orphan_legs']}")
        _record_rate(run, order_rate, rate0)
        try:
            shared_order_budget.record_order_unconditional(db)          # the extra order calls of the probe count against the shared daily budget
        except Exception as e:  # noqa: BLE001
            logger.warning("half-sell probe: extra orders not counted (%s)", e)
        if problems:
            res = run.done("FAIL", "; ".join(problems)[:700])
        else:
            run.rule(f"Q6: flat at the end, nothing left open; round trip about {q['Q6_gross_pnl_rs']} gross, charges about {q['Q6_charges_estimate_rs']} (an estimate from our rate card).")
            res = run.done("PASS", f"cancel both legs, sell {SELL_QTY} of {BUY_QTY}, new stop in {window:.1f}s, flat at the end on {sym}")
        _save(res)
        _tell(notify, f"{'✅' if res['verdict'] == 'PASS' else '⚠️'} <b>{res['line']}</b>\n" + "\n".join(f"• {x}" for x in res["rules"][:7]))
        return res
    finally:
        if claimed:
            try:
                shared_symbol_lock.release(db, sym)
            except Exception as e:  # noqa: BLE001
                logger.error("half-sell probe: symbol lock release for %s failed (%s)", sym, e)


def _record_rate(run: _Run, order_rate, before: Dict[str, Any]) -> None:
    """Q7: the order-rate counter before and after. Compact on purpose (calls, waits, peak in the one-second window)."""
    try:
        after = order_rate.snapshot()
        keys = ("calls", "waits", "waited_s", "max_wait_s", "forced", "peak_in_window", "account_waits")
        run.res["q"]["Q7_order_rate"] = {"before": {k: before.get(k) for k in keys}, "after": {k: after.get(k) for k in keys}, "budget_per_s": after.get("budget_per_s"),
                                         "calls_during_probe": int(after.get("calls") or 0) - int(before.get("calls") or 0),
                                         "waits_during_probe": int(after.get("waits") or 0) - int(before.get("waits") or 0)}
        run.rule(f"Q7: the probe made {run.res['q']['Q7_order_rate']['calls_during_probe']} counted order calls and waited on the rate budget "
                 f"{run.res['q']['Q7_order_rate']['waits_during_probe']} time(s) (budget {after.get('budget_per_s')} per second).")
    except Exception as e:  # noqa: BLE001
        logger.debug("half-sell probe: order-rate snapshot skipped (%s)", e)
