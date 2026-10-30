"""swing/probe.py - E132 (plan step S4): does a resting Dhan stop on a CNC holding survive the night? PLACES REAL ORDERS (1 share). Never automatic.

E261 (30 Oct 2026): the first run (a normal STOP_LOSS, validity DAY) FAILed the next morning - correctly: Dhan drops DAY orders at the close and its order book
only lists today's orders, so that order type can never rest overnight. The probe now defaults to kind=forever (Dhan's Forever Order / good-till-triggered,
POST /v2/forever/orders, read back from GET /v2/forever/all); kind=day keeps the old stop as a control.

Two phases, started by hand (POST /swing/probe?phase=open|check&yes=true, or the runner job dhan_swing_probe):
  open   market hours, armed REAL: BUY 1 share (CNC, limit a little above the price), wait for the fill, place a STOP_LOSS sell 4 % below with a limit under its trigger,
         read it back. Saves {symbol, buy order id, stop order id, ...} in trade_swing_runs (kind probe).
  check  the NEXT trading morning (09:20-14:30): read the order book. PASS only when the stop is still PENDING / TRIGGER_PENDING. Then the stop is cancelled and the share is sold
         at market (selling a CNC holding needs the CDSL TPIN that morning: a refusal is reported and the share is left for you to sell; the result does not hide it).
Verdict: PASS | FAIL - <reason> | NOT_RUN - <reason>. The lane's SWING_BROKER_STOP_ENABLED stays your decision either way.
The shared order budget and symbol lock are used like any order; a symbol that this service or position-stocks holds is refused.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("real-trade-swing-probe")
CANDIDATES = ("ITC", "NTPC", "ONGC", "SBIN", "TATASTEEL")
LIVE_STOP_STATES = ("PENDING", "TRIGGER_PENDING", "OPEN", "TRANSIT", "PART_TRADED")
FOREVER_LIVE_STATES = ("PENDING", "CONFIRM", "TRANSIT")      # Dhan's Forever Order (GTT) statuses that mean "still waiting for its trigger"
KINDS = ("forever", "day")


def _status(row: Dict[str, Any]) -> str:
    return str(row.get("orderStatus") or row.get("order_status") or "").upper()


def _oid(row: Dict[str, Any]) -> str:
    return str(row.get("orderId") or row.get("order_id") or "")


def judge_check(stop_row: Optional[Dict[str, Any]], kind: str = "day") -> Dict[str, Any]:
    """Pure: the verdict for the morning-after row of the stop order (None = Dhan no longer lists it). kind "forever" reads Dhan's Forever Order list,
    "day" the normal order book (a DAY order is expected to be gone or EXPIRED by the next morning)."""
    forever = kind == "forever"
    where = "Dhan's forever-order list" if forever else "Dhan's order book"
    if stop_row is None:
        why = "" if forever else " (a DAY order is dropped at the close, so this order type cannot rest overnight)"
        return {"verdict": "FAIL", "reason": f"the stop order is no longer in {where}: it did not survive the night{why}"}
    st = _status(stop_row)
    if st in (FOREVER_LIVE_STATES if forever else LIVE_STOP_STATES):
        return {"verdict": "PASS", "reason": f"the stop order is still live at Dhan ({st})"}
    return {"verdict": "FAIL", "reason": f"the stop order is {st or 'unreadable'} the next morning, not live"}


def _save(repo, day: str, ok: bool, summary: str, doc: Dict[str, Any]) -> None:
    repo.add_run("probe", day, "REAL", ok, summary, json.dumps(doc, default=str))


def last_probe(repo) -> Optional[Dict[str, Any]]:
    r = repo.latest_run("probe", "REAL", ok_only=False)
    if not r:
        return None
    try:
        return {"day": r["day"], "ok": r["ok"], "summary": r["summary"], **json.loads(r.get("detail") or "{}")}
    except ValueError:
        return {"day": r["day"], "ok": r["ok"], "summary": r["summary"]}


def waiting_for_check(prev: Optional[Dict[str, Any]]) -> bool:
    """True only for an open phase that placed a stop and has not been checked yet. A FAILED open (no stop id) is finished: it must not block a new open
    phase, and there is nothing for the check phase to read."""
    return bool(prev and prev.get("phase") == "open" and prev.get("stop_id") and not prev.get("checked"))


def open_phase(db: Any, repo: Any, armed: bool, symbol: str = "", kind: str = "forever") -> Dict[str, Any]:
    import config
    from execution import dhan_client, shared_symbol_lock
    from tz_utils import ist_today_str, is_market_open_ist, ist_time_at_or_after, parse_hhmm
    steps: List[str] = []

    def out(v: str, reason: str, **kw: Any) -> Dict[str, Any]:
        return {"verdict": v, "reason": reason, "steps": steps, "line": f"SWING PROBE (open): {v} - {reason}", **kw}
    if not armed:
        return out("NOT_RUN", "REAL is not armed")
    if not is_market_open_ist() or not ist_time_at_or_after(parse_hhmm("09:30", 9, 30)) or ist_time_at_or_after(parse_hhmm("14:30", 14, 30)):
        return out("NOT_RUN", "open phase runs 09:30-14:30 IST on a trading day")
    prev = last_probe(repo)
    if waiting_for_check(prev):
        return out("NOT_RUN", "an earlier open phase is waiting for its check phase")
    sym = (symbol or "").upper().strip()
    syms = [sym] if sym else list(CANDIDATES)
    kind = (kind or "forever").lower()
    if kind not in KINDS:
        return out("NOT_RUN", f"kind must be one of {', '.join(KINDS)}")
    return _open_live(db, repo, syms, steps, out, ist_today_str(), dhan_client, shared_symbol_lock, config, kind)


def _open_live(db, repo, syms, steps, out, today, dhan_client, shared_symbol_lock, config, kind="forever"):
    import models
    held = {p.symbol for p in db.query(models.TradePosition).filter(models.TradePosition.mode == "REAL", models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED"))).all()}
    sym = next((s for s in syms if s not in held), None)
    if sym is None:
        return out("NOT_RUN", "every candidate symbol is already held")
    price = None
    try:
        import httpx
        from market_feed.feed import MARKET_DATA_URL
        r = httpx.get(f"{MARKET_DATA_URL}/quote/{sym}", timeout=8.0)
        j = r.json() if r.status_code == 200 else {}
        price = float(j.get("price") or j.get("ltp") or 0) or None
    except Exception as e:  # noqa: BLE001
        steps.append(f"price read failed: {e}")
    if not price:
        return out("NOT_RUN", f"no price for {sym}")
    if not shared_symbol_lock.try_claim(db, sym, mode="REAL"):
        return out("NOT_RUN", f"{sym} is held by position-stocks-service")
    sid = dhan_client.get_security_id(db, sym)
    limit = dhan_client.round_to_tick(price * 1.003)
    filled = False
    try:
        buy = dhan_client.place_order(db, is_armed=True, security_id=sid, exchange_segment=dhan_client.NSE_EQ_SEGMENT, transaction_type="BUY", quantity=1,
                                      order_type="LIMIT", price=limit, product_type="CNC", tag="SWPROBE")
        buy_id = str(buy.get("orderId") or buy.get("order_id") or "")
        steps.append(f"BUY 1 {sym} CNC limit {limit} -> order {buy_id}")
        for _ in range(20):
            time.sleep(3)
            row = next((x for x in dhan_client.get_order_list(db) if _oid(x) == buy_id), None)
            if row and _status(row) == "TRADED":
                filled = True
                break
            if row and _status(row) in ("REJECTED", "CANCELLED", "EXPIRED"):
                break
        if not filled:
            try:
                dhan_client.cancel_order(db, is_armed=True, dhan_order_id=buy_id)
            except Exception:  # noqa: BLE001
                pass
            shared_symbol_lock.release(db, sym)
            _save(repo, today, False, f"open: BUY not filled for {sym}", {"phase": "open", "symbol": sym, "verdict": "FAIL"})
            return out("FAIL", "the 1-share BUY did not fill in 60 s (cancel sent)")
        trig = dhan_client.round_stop_to_tick(limit * 0.96)
        lim = dhan_client.round_stop_to_tick(trig * 0.995)
        if kind == "forever":
            stop = dhan_client.place_forever_order(db, is_armed=True, security_id=sid, exchange_segment=dhan_client.NSE_EQ_SEGMENT, transaction_type="SELL", quantity=1,
                                                   price=lim, trigger_price=trig, product_type="CNC", order_type="LIMIT", tag="SWPROBE")
            label = "SELL forever order"
        else:
            stop = dhan_client.place_order(db, is_armed=True, security_id=sid, exchange_segment=dhan_client.NSE_EQ_SEGMENT, transaction_type="SELL", quantity=1,
                                           order_type="STOP_LOSS", price=lim, product_type="CNC", tag="SWPROBE", trigger_price=trig)
            label = "SELL stop"
        stop_id = str(stop.get("orderId") or stop.get("order_id") or "")
        steps.append(f"{label} 1 {sym} trigger {trig} limit {lim} -> order {stop_id}")
        rows = dhan_client.get_forever_orders(db) if kind == "forever" else dhan_client.get_order_list(db)
        row = next((x for x in rows if _oid(x) == stop_id), None)
        st = _status(row) if row else "NOT LISTED"
        steps.append(f"stop read back ({kind}): {st}")
        live_now = FOREVER_LIVE_STATES if kind == "forever" else LIVE_STOP_STATES
        doc = {"phase": "open", "kind": kind, "symbol": sym, "buy_id": buy_id, "stop_id": stop_id, "trigger": trig, "stop_status": st, "checked": False,
               "verdict": "PASS" if st in live_now else "FAIL"}
        _save(repo, today, doc["verdict"] == "PASS", f"open: {sym} stop {st}", doc)
        if doc["verdict"] != "PASS":
            return out("FAIL", f"Dhan did not leave the stop live ({st}); the share is held: sell it in the Dhan app", **doc)
        return out("PASS", f"1 {sym} bought and its stop is live; run the check phase next trading morning", **doc)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        edis = dhan_client.is_cdsl_edis_error(msg)
        try:
            shared_symbol_lock.release(db, sym)          # the claim above must never outlive a failed probe (it would block position-stocks on this symbol)
        except Exception:  # noqa: BLE001
            logger.warning("swing probe: could not release the symbol lock for %s", sym)
        held = (f" 1 {sym} WAS bought and is still held: sell it in the Dhan app." if filled else "")
        _save(repo, today, False, f"open failed: {msg[:120]}", {"phase": "open", "symbol": sym, "verdict": "FAIL", "edis": edis, "held_share": filled})
        return out("FAIL", ("CDSL eDIS / TPIN needed before Dhan accepts the stop: " if edis else "Dhan refused: ") + msg[:160] + held, held_share=filled)


def check_phase(db: Any, repo: Any, armed: bool) -> Dict[str, Any]:
    from execution import dhan_client, shared_symbol_lock
    from tz_utils import ist_today_str, is_market_open_ist, ist_time_at_or_after, parse_hhmm
    steps: List[str] = []

    def out(v: str, reason: str, **kw: Any) -> Dict[str, Any]:
        return {"verdict": v, "reason": reason, "steps": steps, "line": f"SWING PROBE (check): {v} - {reason}", **kw}
    prev = last_probe(repo)
    if not waiting_for_check(prev):
        return out("NOT_RUN", "no open phase is waiting for a check")
    today = ist_today_str()
    if prev["day"] >= today:
        return out("NOT_RUN", "the check must run on a LATER day than the open phase (that is the point: does the stop survive the night)")
    if not armed or not is_market_open_ist() or not ist_time_at_or_after(parse_hhmm("09:20", 9, 20)) or ist_time_at_or_after(parse_hhmm("14:30", 14, 30)):
        return out("NOT_RUN", "needs REAL armed, 09:20-14:30 IST on a trading day")
    kind = str(prev.get("kind") or "day")             # a probe saved before E261 placed a DAY stop
    rows = dhan_client.get_forever_orders(db) if kind == "forever" else dhan_client.get_order_list(db)
    stop_row = next((x for x in rows if _oid(x) == str(prev["stop_id"])), None)
    j = judge_check(stop_row, kind)
    live_now = FOREVER_LIVE_STATES if kind == "forever" else LIVE_STOP_STATES
    steps.append(f"stop {prev['stop_id']}: {j['reason']}")
    sym = prev["symbol"]
    try:
        if stop_row is not None and _status(stop_row) in live_now:
            if kind == "forever":
                dhan_client.cancel_forever_order(db, dhan_order_id=str(prev["stop_id"]))
            else:
                dhan_client.cancel_order(db, is_armed=True, dhan_order_id=str(prev["stop_id"]))
            steps.append("stop cancelled")
        sid = dhan_client.get_security_id(db, sym)
        sell = dhan_client.place_order(db, is_armed=True, security_id=sid, exchange_segment=dhan_client.NSE_EQ_SEGMENT, transaction_type="SELL", quantity=1,
                                       order_type="MARKET", price=0, product_type="CNC", tag="SWPROBE")
        steps.append(f"SELL 1 {sym} market -> order {str(sell.get('orderId') or sell.get('order_id') or '')}")
        shared_symbol_lock.release(db, sym)
    except Exception as e:  # noqa: BLE001
        edis = dhan_client.is_cdsl_edis_error(str(e))
        steps.append(("CDSL eDIS / TPIN needed to sell the share: Verify Holdings in the Dhan app, then sell 1 " if edis else "cleanup failed, sell 1 ") + f"{sym} by hand ({str(e)[:100]})")
        try:
            shared_symbol_lock.release(db, sym)          # the probe's claim must not outlive the probe (it would block position-stocks on this symbol)
        except Exception:  # noqa: BLE001
            logger.warning("swing probe: could not release the symbol lock for %s", sym)
    doc = {**prev, "checked": True, "check_day": today, "verdict": j["verdict"], "check_reason": j["reason"], "check_steps": list(steps)}
    _save(repo, today, j["verdict"] == "PASS", f"check: {j['verdict']}", {**doc, "phase": "open"})
    return out(j["verdict"], j["reason"])
