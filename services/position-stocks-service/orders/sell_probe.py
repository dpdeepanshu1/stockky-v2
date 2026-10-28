"""orders/sell_probe.py - T6, the Dhan SELL probe (E108, 24 Oct 2026). Position Stocks only.

WHAT: one deliberate, supervised round trip with the real broker, so the live short has been tried once before it is switched on.
  1. place ONE 1-share INTRADAY SELL Super Order (MARKET entry, stop above, target below) on a liquid stock
  2. read the order back from Dhan and write down what it says: did the entry fill, are the stop and target legs there, at which prices, with which side
  3. cancel the legs, cover with a 1-share MARKET BUY, read the cover back
  4. record Dhan's answers and a verdict: `SELL PROBE: PASS | FAIL - reason | SKIPPED - reason`
A PASS is what `SHORT_LIVE_REQUIRES_PROBE` (ships true) looks for before a live short may be placed (orders/short_live.py).

SAFETY (every rail fails closed; nothing here runs unless the caller passes the exact confirm phrase):
  * quantity is the constant PROBE_QTY = 1. It is not a parameter.
  * only during market hours, between PROBE_FROM_IST and PROBE_UNTIL_IST (09:30 to 14:30): not in the opening noise, and well before the 14:55 short cover.
  * the service must be armed and the kill switch not tripped; the symbol must not be held by either service and not have an open position here; one probe at a time.
  * the BUY cover is sent ONLY when Dhan's own order book says the SELL entry filled. A BUY with no short under it would open a LONG, so when the state cannot
    be read nothing is bought and the failure is shouted (Telegram) instead. The stop leg stays at the broker until it has been cancelled and read back.
  * if the cover cannot be completed, the short is adopted as a normal SHORT position row (stop and target as sent), so the 14:55 cover, reconcile and the stop
    watchdog manage it like any other short. It is never left without an owner.
  * the symbol lock and the order budget are claimed before the first order and handed back at the end.

Nothing in this file touches orders/entry.py (the long entry) or orders/short_live.py's order path. Stdlib + the service's own modules.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import config

logger = logging.getLogger("position-stocks-sell-probe")

PROBE_QTY = 1                                    # constant on purpose: not a switch, not a parameter
CONFIRM_PHRASE = "PLACE_ONE_REAL_SELL_ORDER"
PROBE_FROM_IST = "09:30"
PROBE_UNTIL_IST = "14:30"
DEFAULT_SYMBOLS = ("ITC", "NTPC", "ONGC", "SBIN", "TATASTEEL")       # liquid and cheap: the smallest margin for one share
STOP_PCT = 1.5                                   # stop sits this far above the sell price ...
TARGET_PCT = 3.0                                 # ... and the target this far below. The probe covers at once; the legs only have to exist and read back right.
TAG_ENTRY = "SELLPROBE"
TAG_COVER = "SELLPROBE_COVER"
_FILLED = {"TRADED", "FILLED", "EXECUTED", "COMPLETE"}
_DEAD = {"REJECTED", "CANCELLED", "CANCELED", "EXPIRED"}

_lock = threading.Lock()                         # one probe at a time
_mem: Dict[str, Any] = {}


# ---------------------------------------------------------------- the last result (file + memory, same pattern as approve_first)
def _path() -> str:
    return os.path.join((os.environ.get("CONTROL_DIR") or "").strip() or "/data/control", "sell_probe_ps.json")


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
        logger.error("sell probe: saved result unreadable (%s: %s); using the in-memory copy", type(e).__name__, e)
    return dict(_mem)


def _history_path() -> str:
    return os.path.join((os.environ.get("CONTROL_DIR") or "").strip() or "/data/control", "sell_probe_history_ps.json")


def history() -> List[Dict[str, Any]]:
    """E243: every probe that reached Dhan, oldest first (compact rows, at most 50). Feeds the SELL orders panel on the Positions tab. Never raises."""
    try:
        with open(_history_path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except Exception:  # noqa: BLE001
        return []


def _remember(res: Dict[str, Any]) -> None:
    row = {k: res.get(k) for k in ("at", "finished_at", "symbol", "qty", "verdict", "reason", "order_id", "cover_order_id", "entry_order_type", "entry_price_sent",
                                   "ref_price", "entry_fill", "cover_fill", "security_id", "short_seen", "stop_price", "target_price")}
    try:
        rows = history()
        rows.append(row)
        p = _history_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".sh", dir=os.path.dirname(p))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows[-50:], fh, default=str)
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        logger.error("sell probe: history not persisted (%s: %s)", type(e).__name__, e)


def _save(res: Dict[str, Any]) -> None:
    _mem.clear()
    _mem.update(res)
    _remember(res)
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".sp", dir=os.path.dirname(p))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(res, fh, default=str)
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        logger.error("sell probe: result not persisted (%s: %s); kept in memory", type(e).__name__, e)


def passed() -> bool:
    """True when the last probe on record is a PASS. Unreadable or missing = False (a rail, so it fails closed)."""
    try:
        return str(last_result().get("verdict") or "").upper() == "PASS"
    except Exception:  # noqa: BLE001
        return False


def summary() -> Dict[str, Any]:
    """Small public view for GET /short/shadow: no raw broker payloads."""
    r = last_result()
    if not r:
        return {"ran": False, "verdict": "NOT_RUN", "line": "SELL PROBE: NOT_RUN - T6 has not been run yet", "passed": False,
                "required_for_live": bool(getattr(config, "SHORT_LIVE_REQUIRES_PROBE", True))}
    return {"ran": True, "verdict": r.get("verdict"), "line": r.get("line"), "at": r.get("at"), "symbol": r.get("symbol"), "passed": passed(),
            "rules": r.get("rules") or [], "required_for_live": bool(getattr(config, "SHORT_LIVE_REQUIRES_PROBE", True))}


# ---------------------------------------------------------------- reading Dhan's rows
def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _status(d: Optional[dict]) -> str:
    return str((d or {}).get("orderStatus") or (d or {}).get("order_status") or "").upper()


def _leg(row: Optional[dict], name: str) -> Optional[dict]:
    for leg in (row or {}).get("legDetails") or []:
        if isinstance(leg, dict) and str(leg.get("legName", "")).upper() == name:
            return leg
    return None


def _leg_price(leg: Optional[dict], keys: tuple) -> Optional[float]:
    for k in keys:
        v = _num((leg or {}).get(k))
        if v:
            return v
    return None


def _row_for(rows: List[dict], oid: str) -> Optional[dict]:
    return next((r for r in rows or [] if isinstance(r, dict) and str(r.get("orderId") or r.get("order_id") or "") == str(oid)), None)


def _entry_filled(row: Optional[dict]) -> Optional[float]:
    """Entry fill price when Dhan's row shows the SELL entry filled, else None. Same reading the reconcile uses for every Super Order."""
    if not row:
        return None
    from orders import reconcile
    return reconcile._entry_fill_from_row(row)


def _compact(row: Optional[dict]) -> Dict[str, Any]:
    """What we keep of a raw Super Order row: the fields that answer the T6 questions, nothing personal."""
    if not isinstance(row, dict):
        return {}
    keep = ("orderId", "orderStatus", "transactionType", "productType", "orderType", "quantity", "filledQty", "averageTradedPrice", "price", "triggerPrice",
            "targetPrice", "stopLossPrice", "trailingJump", "omsErrorCode", "omsErrorDescription", "remainingQuantity")
    out = {k: row.get(k) for k in keep if k in row}
    out["legs"] = [{k: lg.get(k) for k in ("legName", "orderStatus", "transactionType", "price", "triggerPrice", "averageTradedPrice", "quantity", "filledQty",
                                           "omsErrorCode", "omsErrorDescription") if k in lg} for lg in (row.get("legDetails") or []) if isinstance(lg, dict)]
    return out


# ---------------------------------------------------------------- Dhan's positions: the ground truth for "is a short really open"
# E242 (28 Oct 2026): the first real run said "entry never filled ... nothing to cover" and sent no BUY, yet Dhan showed an open ITC short with every leg cancelled.
# The Super Order list is one view of the fill; the position is the fact. The probe now reads the position before (baseline) and after the order, and a BUY
# is sent only while the position really is short beyond the baseline (never a blind BUY, never a BUY on a flat book).
def _pos_rows(positions: Any, security_id: str) -> List[dict]:
    return [r for r in positions or [] if isinstance(r, dict) and str(r.get("securityId") or r.get("security_id") or "") == str(security_id)]


def _net_qty(positions: Any, security_id: str) -> int:
    """Signed net quantity Dhan shows for this security today (negative = short). 0 when there is no row."""
    total = 0
    for r in _pos_rows(positions, security_id):
        try:
            total += int(float(r.get("netQty") if r.get("netQty") is not None else r.get("net_qty") or 0))
        except (TypeError, ValueError):
            continue
    return total


def _open_rows(positions: Any, security_id: str) -> List[dict]:
    """Rows for this security with a non-zero net quantity (a +1 delivery row and a -1 intraday row net to 0 but are not flat)."""
    out = []
    for r in _pos_rows(positions, security_id):
        try:
            if int(float(r.get("netQty") if r.get("netQty") is not None else r.get("net_qty") or 0)) != 0:
                out.append(r)
        except (TypeError, ValueError):
            out.append(r)
    return out


def _sell_avg(positions: Any, security_id: str) -> Optional[float]:
    for r in _pos_rows(positions, security_id):
        v = _num(r.get("sellAvg") or r.get("sell_avg"))
        if v:
            return v
    return None


def _read_positions(db, broker) -> Optional[list]:
    """Dhan's positions list, or None when it cannot be read (the caller then refuses to act on a guess)."""
    try:
        got = broker.get_positions(db)
        return got if isinstance(got, list) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("sell probe: positions read failed (%s: %s)", type(e).__name__, str(e)[:120])
        return None


def _short_beyond(db, broker, security_id: str, base: int) -> Optional[int]:
    """Shares short beyond the baseline right now (0 = no probe short open), or None when Dhan's positions cannot be read."""
    pos = _read_positions(db, broker)
    if pos is None:
        return None
    return max(0, base - _net_qty(pos, security_id))


def _leg_entry_done(row: Optional[dict]) -> bool:
    """Dhan documents no filled-quantity field on legDetails, only totalQuatity (sic) / remainingQuantity / triggeredQuantity. An ENTRY_LEG with a total and nothing
    remaining, and not dead, is filled. Used only as probe evidence next to the position, never to price a fill."""
    leg = _leg(row, "ENTRY_LEG")
    if not leg or _status(leg) in _DEAD:
        return False
    total = _num(leg.get("totalQuantity") if leg.get("totalQuantity") is not None else leg.get("totalQuatity"))
    try:
        remaining = float(leg.get("remainingQuantity"))
    except (TypeError, ValueError):
        return False
    return bool(total) and remaining == 0


def _order_book_fill(db, broker, security_id: str, oid: str) -> Optional[float]:
    """Average price of a TRADED SELL at Dhan for this security placed by this probe (order id or our correlation tag), from the plain order book. None if absent."""
    try:
        for r in broker.get_order_list(db) or []:
            if not isinstance(r, dict) or str(r.get("securityId") or r.get("security_id") or "") != str(security_id):
                continue
            if str(r.get("transactionType") or "").upper() != "SELL" or _status(r) not in _FILLED:
                continue
            if str(r.get("orderId") or "") == str(oid) or str(r.get("correlationId") or "") == TAG_ENTRY:
                v = _num(r.get("averageTradedPrice") or r.get("average_traded_price"))
                if v:
                    return v
    except Exception as e:  # noqa: BLE001
        logger.warning("sell probe: order book read for the entry fill failed (%s)", e)
    return None


# ---------------------------------------------------------------- the probe
class _Run:
    """Collects the steps and the verdict of one probe."""

    def __init__(self, symbol: str) -> None:
        self.res: Dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "symbol": symbol, "qty": PROBE_QTY, "steps": [], "rules": [], "answers": {}}

    def step(self, msg: str) -> None:
        self.res["steps"].append(msg)
        logger.info("sell probe: %s", msg)

    def rule(self, msg: str) -> None:
        self.res["rules"].append(msg)

    def done(self, verdict: str, reason: str) -> Dict[str, Any]:
        self.res["verdict"] = verdict
        self.res["reason"] = reason
        self.res["line"] = f"SELL PROBE: {verdict} - {reason}"
        self.res["finished_at"] = datetime.now(timezone.utc).isoformat()
        return self.res


def _hhmm(s: str):
    from tz_utils import parse_hhmm
    return parse_hhmm(s, 9, 30)


def _skip(symbol: str, reason: str) -> Dict[str, Any]:
    """A refusal before any order: reported, never saved over a real earlier result (a SKIPPED run says nothing about Dhan)."""
    r = _Run(symbol)
    r.step(f"not run: {reason}")
    return r.done("SKIPPED", reason)


def run_probe(db, symbol: Optional[str] = None, confirm: str = "", *,
              broker: Any = None, price_fn: Optional[Callable[[str], Optional[float]]] = None, now: Optional[datetime] = None,
              sleep: Callable[[float], None] = time.sleep, poll_s: float = 20.0, notify: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Run the T6 probe once. Never raises. Returns the result dict (also saved, except for SKIPPED). `broker` / `price_fn` / `sleep` are injectable for tests."""
    sym = (symbol or "").strip().upper()
    if confirm != CONFIRM_PHRASE:
        return _skip(sym or "-", f"confirm phrase missing: this places a REAL 1-share SELL order, so the call must carry confirm={CONFIRM_PHRASE}")
    if not _lock.acquire(blocking=False):
        return _skip(sym or "-", "another probe is already running")
    try:
        return _run(db, sym, broker, price_fn, now, sleep, poll_s, notify)
    except Exception as e:  # noqa: BLE001 - the last-resort net: say so loudly, never raise into the route
        logger.error("sell probe: unexpected error (%s: %s)", type(e).__name__, e, exc_info=True)
        r = _Run(sym or "-")
        res = r.done("FAIL", f"unexpected error {type(e).__name__}: {str(e)[:200]} - CHECK DHAN NOW for an open 1-share short or a resting Super Order")
        _save(res)
        _tell(notify, f"🚨 <b>SELL probe crashed</b> ({type(e).__name__}). Check Dhan for an open 1-share short or a resting Super Order and clear it by hand.")
        return res
    finally:
        _lock.release()


def _tell(notify: Optional[Callable[[str], None]], text: str) -> None:
    try:
        if notify is not None:
            notify(text)
        else:
            import notifier
            notifier.notify_fire_and_forget(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("sell probe: alert failed (%s)", e)


def _run(db, sym, broker, price_fn, now, sleep, poll_s, notify) -> Dict[str, Any]:  # noqa: C901 - one straight line of steps, each with its own refusal
    from capital import shared_order_budget, shared_symbol_lock
    from models import ScalpGateState, ScalpPosition
    from orders import approve_first, short_entry
    from tz_utils import ist_now, is_market_open_ist

    if broker is None:
        from execution import dhan_client as broker
    price_fn = price_fn or short_entry._live_price

    # ---- 1. refusals before anything is claimed or sent
    gate = db.query(ScalpGateState).first()
    if gate is None or not getattr(gate, "is_armed", False):
        return _skip(sym or "-", "the service is not armed")
    if gate.daily_loss_kill_switch_tripped:
        return _skip(sym or "-", "the daily-loss kill switch is tripped")
    if not config.USE_SUPER_ORDER:
        return _skip(sym or "-", "USE_SUPER_ORDER is off: the probe tests the Super Order and a short must have a broker stop leg")
    if approve_first.active():
        return _skip(sym or "-", "an approve-first round is open")
    t = ist_now(now)
    if not is_market_open_ist(now) or not (_hhmm(PROBE_FROM_IST) <= t.time() <= _hhmm(PROBE_UNTIL_IST)):
        return _skip(sym or "-", f"outside the probe window ({PROBE_FROM_IST}-{PROBE_UNTIL_IST} IST on a trading day)")

    candidates = [sym] if sym else list(DEFAULT_SYMBOLS)
    live, security_id, base, why_not = None, None, 0, []
    for c in candidates:
        px = price_fn(c)
        if not (px and px > 0):
            continue
        # E242: the symbol must be FLAT at Dhan (any row, any product) so that what the probe leaves behind can only be its own share, and so an old short is never
        # mistaken for the probe's fill. An unreadable positions list refuses the probe: it would have no way to tell.
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
            return _skip(c, "Dhan's positions could not be read, so the probe cannot tell its own fill from a position that was already there")
        held = _net_qty(pos0, str(sid))
        if held != 0 or _open_rows(pos0, str(sid)):
            why_not.append(f"{c} already has a position at Dhan (net {held:+d}) that Stockky did not open: close it in Dhan first")
            continue
        sym, live, security_id, base = c, float(px), str(sid), held
        break
    if live is None:
        return _skip(candidates[0], "; ".join(why_not) if why_not else f"no live price for {', '.join(candidates)} (the tick feed has nothing yet)")
    if db.query(ScalpPosition).filter(ScalpPosition.symbol == sym, ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED"))).first():
        return _skip(sym, f"{sym} already has an open position here")

    run = _Run(sym)
    if not shared_symbol_lock.try_claim(db, sym):
        return _skip(sym, f"{sym} is held by the other service")
    claimed = True
    try:
        if not shared_order_budget.check_and_reserve(db):
            return _skip(sym, "the shared daily order budget is used up")

        # ---- 2. levels, side-aware and tick-clamped by the same function the live short uses
        tick = broker.round_to_tick
        t_px, s_px, notes = broker.clamp_super_levels("SELL", live, tick(live * (1 - TARGET_PCT / 100.0)), tick(live * (1 + STOP_PCT / 100.0)))
        # E243: the SAME entry the live short sends (a marketable limit under the last price, or MARKET when that switch is off). The first real run sent MARKET and Dhan's
        # row read back as LIMIT at the last price, which rested unfilled.
        e_type, e_px = broker.short_entry_order(live, float(t_px), float(s_px))
        run.res.update(ref_price=live, target_price=t_px, stop_price=s_px, security_id=str(security_id), entry_order_type=e_type, entry_price_sent=e_px)
        run.step(f"SELL {PROBE_QTY} {sym} ref {live:.2f}: entry {e_type} at {float(e_px):.2f}, target {t_px:.2f} (below), stop {s_px:.2f} (above)" + (f" [{'; '.join(notes)}]" if notes else ""))

        # ---- 3. the SELL Super Order. A refusal here is itself the answer T6 exists to get.
        try:
            placed = broker.place_super_order(
                db, is_armed=True, security_id=security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT, transaction_type="SELL", quantity=PROBE_QTY,
                order_type=e_type, price=e_px, target_price=float(t_px), stop_loss_price=float(s_px), trailing_jump=0.0, product_type="INTRADAY", tag=TAG_ENTRY)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            found = None
            if broker.is_ambiguous_placement_error(e):
                # a timeout / dropped connection does not prove Dhan never got it: look for a young matching SELL before calling it failed
                try:
                    row0 = broker.find_recent_buy_order(db, security_id, PROBE_QTY, super_order=True, transaction_type="SELL")
                    found = str((row0 or {}).get("orderId") or (row0 or {}).get("order_id") or (row0 or {}).get("id") or "") or None
                except Exception as le:  # noqa: BLE001
                    logger.warning("sell probe: lookup after an unclear SELL failed (%s)", le)
            if found:
                placed = {"orderId": found, "recovered_after": msg[:120]}
                run.step(f"the SELL errored ({msg[:80]}) but Dhan has order {found}: carrying on with it")
            else:
                run.res["answers"]["place_refused"] = msg[:600]
                if broker.is_ambiguous_placement_error(e):
                    run.rule(f"The SELL placement errored on our side ({msg[:200]}) and no matching order was found at Dhan.")
                    res = run.done("FAIL", f"the SELL placement errored on our side ({msg[:160]}) - it may exist at Dhan: CHECK DHAN for an open 1-share {sym} short")
                    _tell(notify, f"🚨 <b>SELL probe: placement unclear</b> - {sym}\n{msg[:300]}\nCheck Dhan for a 1-share short or a resting Super Order and clear it by hand.")
                else:
                    run.rule(f"Dhan REFUSED a 1-share INTRADAY SELL Super Order with a {e_type} entry: {msg[:300]}")
                    res = run.done("FAIL", f"Dhan refused the SELL Super Order: {msg[:240]}")
                _save(res)
                return res
        oid = str((placed or {}).get("orderId") or (placed or {}).get("id") or "")
        run.res["answers"]["place"] = {k: (placed or {}).get(k) for k in ("orderId", "orderStatus", "status", "message") if (placed or {}).get(k) is not None}
        if not oid:
            res = run.done("FAIL", f"Dhan answered the SELL without an order id ({str(placed)[:160]}) - CHECK DHAN for an open 1-share {sym} short")
            _save(res)
            _tell(notify, f"🚨 <b>SELL probe: no order id</b> - {sym}. Check Dhan for a 1-share short and clear it by hand.")
            return res
        run.res["order_id"] = oid
        run.step(f"Dhan accepted the SELL Super Order, id {oid}")
        try:
            from tz_utils import ist_today_str
            today = ist_today_str(now)
            if gate.orders_placed_today_date != today:
                gate.orders_placed_today, gate.orders_placed_today_date = 0, today
            gate.orders_placed_today = int(gate.orders_placed_today or 0) + 1
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            logger.warning("sell probe: order-budget count failed (%s)", e)

        # ---- 4. read it back: wait for the entry to fill and the legs to appear
        row, fill, seen_short = _poll_row(db, broker, oid, poll_s, sleep, security_id, base)
        run.res["answers"]["row_after_fill"] = _compact(row)
        problems: List[str] = []
        if fill is None and seen_short:
            # the short is open at Dhan but the Super Order list does not show the fill: the probe cannot price it from the row, so it prices it from the
            # position / order book, covers it below, and reports the mismatch, because the live short's reconcile reads the same list
            pos_now = _read_positions(db, broker) or []
            fill = _sell_avg(pos_now, security_id) or _order_book_fill(db, broker, security_id, oid) or live
            run.res["entry_fill"] = fill
            run.res["short_seen"] = True
            run.step(f"Dhan's POSITIONS show the short open (net {_net_qty(pos_now, security_id):+d}) while the Super Order list said {_status(row) or 'nothing'}: treating the entry as filled at about {fill:.2f}")
            problems.append(f"the Super Order list did not show the entry as filled (status {_status(row) or 'unreadable'}, entry leg {_status(_leg(row, 'ENTRY_LEG')) or 'missing'}) "
                            f"although Dhan's positions show the short filled: the list's fill fields are not what reconcile reads, see the raw row")
        elif fill is None:
            problems.append(f"the SELL entry did not show as filled within {poll_s:.0f}s (status {_status(row) or 'unreadable'}) and Dhan's positions show no short")
        else:
            run.step(f"entry filled at {fill:.2f} (ref {live:.2f}; slippage {live - fill:+.2f} per share in our favour when positive)")
            run.res["entry_fill"] = fill
            run.rule(f"Dhan accepted a {e_type}-entry INTRADAY SELL Super Order and filled it (1 share).")
        stop_leg, tgt_leg = _leg(row, "STOP_LOSS_LEG"), _leg(row, "TARGET_LEG")
        seen_stop = broker.read_stop_leg_price(row) if row else None
        seen_tgt = _leg_price(tgt_leg, ("price", "targetPrice", "triggerPrice"))
        tol = 2 * broker.tick_size_for_price(live)
        if stop_leg is None and seen_stop is None:
            problems.append("no STOP_LOSS_LEG on the row: a short would have no broker stop")
        elif seen_stop is None:
            problems.append("STOP_LOSS_LEG shows no readable price")
        elif abs(seen_stop - s_px) > tol + 1e-9:
            problems.append(f"STOP_LOSS_LEG shows {seen_stop:.2f}, sent {s_px:.2f} (more than 2 ticks apart)")
        else:
            run.rule(f"The stop leg rests at the broker and reads back {seen_stop:.2f} (sent {s_px:.2f}), above the sell price as a short needs.")
        if tgt_leg is None:
            problems.append("no TARGET_LEG on the row")
        elif seen_tgt is None:
            problems.append("TARGET_LEG shows no readable price")
        elif abs(seen_tgt - t_px) > tol + 1e-9:
            problems.append(f"TARGET_LEG shows {seen_tgt:.2f}, sent {t_px:.2f} (more than 2 ticks apart)")
        else:
            run.rule(f"The target leg reads back {seen_tgt:.2f} (sent {t_px:.2f}), below the sell price.")
        for nm, lg in (("STOP_LOSS_LEG", stop_leg), ("TARGET_LEG", tgt_leg)):
            ttype = str((lg or {}).get("transactionType") or "").upper()
            if ttype:
                run.rule(f"{nm} transactionType reads {ttype}" + (" (a cover, as expected)" if ttype == "BUY" else " - NOT a BUY: check before trusting the legs"))
                if ttype != "BUY":
                    problems.append(f"{nm} transactionType is {ttype}, a short's exit legs must be BUY")
            if _status(lg) in _DEAD:
                problems.append(f"{nm} is {_status(lg)} right after the entry")
        if row is None:
            problems.append("the Super Order is not in Dhan's list")

        # ---- 5. cancel the legs and cover, whatever the checks said (a probe must never leave a short behind)
        covered, cover_note = _cancel_and_cover(db, broker, run, oid, security_id, sym, fill, sleep, poll_s, base)
        fill = run.res.get("entry_fill") or fill                   # the cover step may have found a late fill (row or position)
        try:
            shared_order_budget.record_order_unconditional(db)
        except Exception as e:  # noqa: BLE001
            logger.warning("sell probe: cover order not counted (%s)", e)

        if run.res.get("flat_despite_fill"):
            problems.append("the Super Order list showed the entry filled but Dhan's positions never showed a short: the two disagree, nothing was bought")

        # ---- 6. a cover we could not finish is adopted as a normal short so nothing is left without an owner
        if (fill is not None or run.res.get("short_seen")) and not covered:
            adopted = _adopt(db, run, sym, security_id, float(fill or live), s_px, t_px, oid)
            problems.append(f"COVER NOT CONFIRMED ({cover_note}); {'adopted as a normal SHORT row, the 14:55 cover and the stop watchdog own it now' if adopted else 'could NOT be adopted: cover it by hand in Dhan'}")
            _tell(notify, f"🚨 <b>SELL probe: cover not confirmed</b> - {sym} 1 share\n{cover_note}\n"
                          f"{'It is now a normal short position here (14:55 cover, stop at the broker).' if adopted else 'Cover it by hand in Dhan NOW.'}")
        elif fill is None and covered is False and cover_note:
            problems.append(cover_note)

        if problems:
            res = run.done("FAIL", "; ".join(problems)[:700])
        else:
            pnl = (fill - float(run.res.get("cover_fill") or fill)) * PROBE_QTY
            run.rule(f"The cover (1-share MARKET BUY) filled at {run.res.get('cover_fill')}; the round trip came to {pnl:+.2f} rupees before charges.")
            res = run.done("PASS", f"clean round trip on {sym}: entry {fill:.2f}, stop and target legs read back, covered at {run.res.get('cover_fill')}")
        _save(res)
        _tell(notify, f"{'✅' if res['verdict'] == 'PASS' else '⚠️'} <b>{res['line']}</b>\n" + "\n".join(f"• {x}" for x in res["rules"][:6]))
        return res
    finally:
        if claimed:
            try:
                shared_symbol_lock.release(db, sym)
            except Exception as e:  # noqa: BLE001
                logger.error("sell probe: symbol lock release for %s failed (%s)", sym, e)


def _poll_row(db, broker, oid: str, poll_s: float, sleep, security_id: str = "", base: int = 0):
    """Poll Dhan until the entry shows filled and the legs are listed, or poll_s runs out. Returns (row or None, fill or None, short_seen).
    fill comes only from the Super Order row (the reading reconcile uses). short_seen is True when Dhan's POSITIONS show a short beyond the baseline, which is
    the fact the row can lag behind (E242). The wait ends early on a row fill with both legs listed; with a short seen but no row fill it waits out poll_s
    so the row gets every chance to show it (the mismatch is reported)."""
    waited, row, fill, seen = 0.0, None, None, False
    while True:
        try:
            row = _row_for(broker.get_super_order_list(db), oid) or row
        except Exception as e:  # noqa: BLE001
            logger.warning("sell probe: Super Order list read failed (%s)", e)
        fill = _entry_filled(row)
        if fill is not None and _leg(row, "STOP_LOSS_LEG") is not None and _leg(row, "TARGET_LEG") is not None:
            return row, fill, True
        if security_id and (not seen or fill is None) and int(waited) % 2 == 0:
            short = _short_beyond(db, broker, security_id, base)
            seen = seen or bool(short)
        if waited >= poll_s:
            return row, fill, seen
        sleep(1.0)
        waited += 1.0


def _cancel_and_cover(db, broker, run: _Run, oid: str, security_id: str, sym: str, fill: Optional[float], sleep, poll_s: float, base: int = 0):
    """Cancel every leg, then settle what is REALLY open from Dhan's positions, and only if a short beyond the baseline exists BUY 1 share and read the cover back.
    Returns (covered, note). covered is True when no short remains: either nothing ever filled, or the cover is confirmed.
    E242: the BUY decision no longer rests on the Super Order list alone. The first real run read "never filled" from the list while the position was short."""
    answers = run.res["answers"].setdefault("cancel", {})
    for leg in ("ENTRY_LEG", "TARGET_LEG", "STOP_LOSS_LEG"):
        try:
            answers[leg] = str(broker.cancel_super_order(db, order_id=oid, order_leg=leg))[:200]
        except Exception as e:  # noqa: BLE001
            answers[leg] = f"not cancelled: {str(e)[:160]}"
    run.step("cancel answers: " + "; ".join(f"{k}={v}" for k, v in answers.items()))
    sleep(float(getattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0.5) or 0.0))
    try:
        row = _row_for(broker.get_super_order_list(db), oid)
    except Exception as e:  # noqa: BLE001
        return False, f"could not read the order back after the cancels ({str(e)[:120]}), so no cover was sent (a BUY blind could open a long)"
    run.res["answers"]["row_after_cancel"] = _compact(row)
    filled_now = _entry_filled(row)

    # what is really open: the position, read after the cancels. A fill can land between the last poll and the cancel, and a position can lag a second or two.
    short = _short_beyond(db, broker, security_id, base)
    for _ in range(2):
        if short != 0 or fill is not None or filled_now is not None:
            break                                    # a short is seen, unreadable, or the row already says filled: no need to wait for the position
        sleep(1.0)                                   # nothing seen anywhere: give a lagging position one more look before calling it flat
        short = _short_beyond(db, broker, security_id, base)
    run.res["answers"]["short_after_cancel"] = short
    if short is None and fill is None and filled_now is None:
        return False, "Dhan's positions could not be read after the cancel and the order list shows no fill, so no cover was sent (a BUY blind could open a long): CHECK DHAN for a 1-share short"
    if short is not None and short > 0 and fill is None and filled_now is None:
        pos_now = _read_positions(db, broker) or []
        late = _sell_avg(pos_now, security_id) or _order_book_fill(db, broker, security_id, oid) or run.res.get("ref_price")
        run.res["short_seen"] = True
        run.res["entry_fill"] = late
        fill = late
        run.step(f"Dhan's positions show a short of {short} that the order list did not show as filled: covering it (entry about {late})")
    if fill is None and filled_now is None and not short:
        run.step("the entry never filled, Dhan's positions are flat and the order is cancelled: nothing to cover")
        live_legs = [lg.get("legName") for lg in (row or {}).get("legDetails") or [] if isinstance(lg, dict) and _status(lg) not in _DEAD | _FILLED]
        return (not live_legs), ("" if not live_legs else f"legs still live after the cancel: {live_legs}")
    if fill is None and filled_now is not None:
        run.res["entry_fill"] = filled_now
        fill = filled_now
        run.step(f"the entry filled late, at {filled_now:.2f}: covering it")
    live_legs = [lg.get("legName") for lg in (row or {}).get("legDetails") or [] if isinstance(lg, dict) and str(lg.get("legName", "")).upper() != "ENTRY_LEG"
                 and _status(lg) not in _DEAD | _FILLED]
    if live_legs:
        return False, f"exit legs still live after the cancel ({live_legs}): a BUY now could double-cover into a long, so none was sent"
    for leg in (row or {}).get("legDetails") or []:
        if isinstance(leg, dict) and str(leg.get("legName", "")).upper() != "ENTRY_LEG" and _status(leg) in _FILLED:
            run.step(f"{leg.get('legName')} filled by itself before the cancel: the short is already flat")
            run.res["cover_fill"] = _leg_price(leg, ("averageTradedPrice", "price", "triggerPrice"))
            return True, ""
    if short is not None and short == 0:
        # the row says filled but the position is flat: an exit already closed it (or it never was open). A BUY now would open a LONG.
        run.step("the order list shows a fill but Dhan's positions are flat: nothing to cover (a BUY now would open a long)")
        run.res["flat_despite_fill"] = True
        return True, ""
    cover_qty = PROBE_QTY if short is None else max(1, min(int(short), PROBE_QTY))       # never more than the probe's own share
    # the cover itself
    last = ""
    cover_id = ""
    for attempt in range(1, 4):
        try:
            res = broker.place_order(db, is_armed=True, security_id=security_id, exchange_segment=config.SCALP_EXCHANGE_SEGMENT, transaction_type="BUY",
                                     quantity=cover_qty, order_type="MARKET", price=0.0, product_type="INTRADAY", tag=TAG_COVER)
            cover_id = str((res or {}).get("orderId") or (res or {}).get("order_id") or "")
            if cover_id:
                break
            last = f"cover answered without an order id ({str(res)[:120]})"
        except Exception as e:  # noqa: BLE001
            last = f"cover attempt {attempt} failed: {str(e)[:160]}"
            if broker.is_ambiguous_placement_error(e):
                break                               # may exist at Dhan: do not send a second BUY blind
        sleep(1.0)
    if not cover_id:
        return False, last or "cover not placed"
    run.res["cover_order_id"] = cover_id
    run.step(f"cover BUY placed, id {cover_id}")
    waited = 0.0
    while True:
        try:
            for r in broker.get_order_list(db) or []:
                if isinstance(r, dict) and str(r.get("orderId") or r.get("order_id") or "") == cover_id:
                    st = _status(r)
                    if st in _FILLED:
                        run.res["cover_fill"] = _num(r.get("averageTradedPrice") or r.get("average_traded_price")) or run.res.get("ref_price")
                        run.res["answers"]["cover"] = {"orderStatus": st, "averageTradedPrice": r.get("averageTradedPrice")}
                        left = _short_beyond(db, broker, security_id, base)
                        if left:
                            return False, f"the cover BUY filled but Dhan's positions still show a short of {left}"
                        return True, ""
                    if st in _DEAD:
                        return False, f"the cover BUY ended {st}"
        except Exception as e:  # noqa: BLE001
            logger.warning("sell probe: order list read failed (%s)", e)
        if waited >= poll_s:
            return False, f"the cover BUY {cover_id} was not confirmed filled within {poll_s:.0f}s"
        sleep(1.0)
        waited += 1.0


def _adopt(db, run: _Run, sym: str, security_id: str, fill: float, stop: float, target: float, oid: str) -> bool:
    """The cover could not be confirmed: record the short as an ordinary open SHORT row so the existing exit side owns it. Never raises."""
    try:
        from models import ScalpPosition
        pos = ScalpPosition(
            symbol=sym, dhan_security_id=str(security_id), window_source="PROBE", status="OPEN", side="SHORT", entry_price=float(fill), signal_price=float(fill),
            quantity=PROBE_QTY, target_price=float(target), stop_price=float(stop), adaptive_target_pct=TARGET_PCT, adaptive_stop_pct=STOP_PCT,
            dhan_super_order_id=oid, dhan_entry_order_id=oid, capital_risked=float(fill) * PROBE_QTY, is_first_live_order=False, opened_at=datetime.now(timezone.utc))
        db.add(pos)
        db.commit()
        run.step(f"adopted the uncovered probe short as position row {pos.id}")
        return True
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception as _e:  # noqa: BLE001
            logger.debug("sell_probe.py: rollback ignored %s", _e)
        logger.critical("sell probe: could not adopt the uncovered short %s (%s)", sym, e)
        return False
