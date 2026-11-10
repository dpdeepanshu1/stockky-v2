"""fno/stages.py - Round 264, slice 2 of the go-live plan: GET /fno/pipeline as eight ordered stages instead of one flat list.

market data > chains > signal > gates > sizing > order > exits > reconcile. Each stage is OK / PROBLEM / idle, says how long ago it last did something, and carries the last decision
that passed through it (the journal already stores every veto, skip, entry, exit and failed order, with its reason). Nothing here changes what the engine does: `build` is pure and
reads the engine's last cycle summary plus today's journal events, so it is tested without a database; `describe` turns one journal event into a sentence. Idle is never green: a
stage with nothing to show says "idle", and a gate that blocked an entry is the gate doing its job (OK), not a fault.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

ORDER = ("market_data", "chains", "signal", "gates", "sizing", "order", "exits", "reconcile")
LABELS = {"market_data": "Market data", "chains": "Chains", "signal": "Signal", "gates": "Gates", "sizing": "Sizing", "order": "Order", "exits": "Exits / square-off", "reconcile": "Reconcile"}
SIZING_STAGES = ("capital", "contract", "sizing", "capital_room")      # the `stage` a skip event carries (engine._enter)
EXIT_SIDES = ("SL", "SELL")                                           # the `side` of an order_failed / the `would` of a real_blocked that belongs to the exit path
STALE_CYCLE_S = 90.0
RECONCILE_MAX_AGE_S = 1200.0                                          # round 306: the same limit as fno/readiness.py (a test keeps them equal): an older reconcile says nothing about now
EVENT_LIMIT = 500                                                     # newest events read per kind (the engine throttles repeats, so a day fits)
KINDS = ("signal", "veto", "skip", "entry", "exit", "real_blocked", "order_failed", "stale")


def _why(e: Dict[str, Any]) -> str:
    return "; ".join(str(x) for x in (e.get("reasons") or e.get("why") or []))


def describe(e: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """One journal event -> {ts, kind, underlying, text, data}. `text` is one plain sentence; `data` is the rest of the event for the click-through."""
    if not e:
        return None
    k = e.get("kind")
    why = _why(e)
    who = " ".join(str(x) for x in (e.get("underlying"), e.get("strategy")) if x)
    if k == "skip":
        text = f"{who}: stopped at {e.get('stage') or 'a gate'}" + (f": {why}" if why else "")
    elif k == "veto":
        text = f"{who}: {e.get('direction') or ''} signal vetoed by the chain" + (f" ({why})" if why else "")
    elif k == "signal":
        text = f"{who}: {e.get('direction') or 'no'} bias, score {e.get('score')}" + (f" ({why})" if why else "")
    elif k == "entry":
        text = f"{e.get('label') or who}: entered {e.get('lots')} lot(s) at {e.get('fill')} ({e.get('mode') or ''})"
    elif k == "exit":
        text = f"{e.get('label') or who}: exited on {e.get('reason')}, net {e.get('net')}"
    elif k == "real_blocked":
        text = f"{who}: REAL {e.get('would') or 'order'} held back by closed locks: " + "; ".join(str(x) for x in (e.get("locks") or []))
    elif k == "order_failed":
        text = f"{who}: {e.get('side') or 'order'} failed: {e.get('message') or e.get('status') or ''}"
    elif k == "stale":
        text = f"{who}: {e.get('msg') or 'no premium for an open position'}"
    else:
        text = f"{who}: {e.get('msg') or k}"
    data = {x: y for x, y in e.items() if x not in ("id", "ts", "kind", "underlying")}
    return {"ts": e.get("ts"), "kind": k, "underlying": e.get("underlying") or "", "text": text.strip(), "data": data}


def _today(evs: List[Dict[str, Any]], day: str, day_of: Any) -> List[Dict[str, Any]]:
    out = []
    for e in evs:
        try:
            if isinstance(e, dict) and e.get("ts") and day_of(float(e["ts"])) == day:
                out.append(e)
        except (TypeError, ValueError):
            continue
    return out


def _newest(evs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return max(evs, key=lambda e: e.get("ts") or 0.0) if evs else None


def _ts(e: Optional[Dict[str, Any]]) -> float:
    return float((e or {}).get("ts") or 0.0)


def build(inp: Dict[str, Any], day_of: Any) -> List[Dict[str, Any]]:
    """inp: now, enabled, market_open, md_ok, vix_ok, expiries_ok, md_detail, last_tick, events {kind: [events]}, open_positions (int), open_real_positions (int, optional), broker (bool), reconcile (dict)."""
    now = float(inp.get("now") or 0.0)
    day = day_of(now)
    tick = inp.get("last_tick") or {}
    decisions = (tick.get("summary") or {}).get("decisions") or {}
    ev = {k: _today(list((inp.get("events") or {}).get(k) or []), day, day_of) for k in KINDS}
    tick_age = (now - float(tick["ts"])) if tick.get("ts") else None
    out: Dict[str, Dict[str, Any]] = {}

    def put(key: str, status: str, detail: str, last: Optional[Dict[str, Any]] = None, age: Optional[float] = None, count: Optional[int] = None) -> None:
        d = describe(last)
        if age is None and d and d["ts"]:
            age = now - float(d["ts"])
        out[key] = {"key": key, "label": LABELS[key], "status": status, "detail": detail, "age_s": None if age is None else round(max(0.0, age), 1), "last_decision": d, "count_today": count}

    # 1 market data: answers whether or not the engine is on
    missing = [n for n, ok in (("market-data service", inp.get("md_ok")), ("India VIX", inp.get("vix_ok")), ("expiries", inp.get("expiries_ok"))) if not ok]
    if missing:
        put("market_data", "PROBLEM", "missing: " + ", ".join(missing) + (f" ({inp['md_detail']})" if inp.get("md_detail") else ""))
    else:
        put("market_data", "OK", "market-data, India VIX and expiries answer")

    # 2 chains: the engine cycle itself
    chain_problem = False
    if not inp.get("enabled"):
        put("chains", "idle", "engine is OFF")
    elif not inp.get("market_open"):
        put("chains", "idle", "market is closed")
    elif tick.get("ts") is None:
        put("chains", "idle", "no engine cycle yet")
    elif tick.get("ok") is False:
        chain_problem = True
        put("chains", "PROBLEM", f"last cycle failed: {tick.get('error') or '?'}", age=tick_age)
    elif tick_age is not None and tick_age > STALE_CYCLE_S:
        chain_problem = True
        put("chains", "PROBLEM", f"no engine cycle for {int(tick_age)} s", age=tick_age)
    else:
        bad = [f"{u}: {', '.join(str(x) for x in (d.get('why') or []))}" for u, d in decisions.items() if d.get("action") == "wait"]
        read = [u for u, d in decisions.items() if d.get("action") != "wait"]
        if bad:
            chain_problem = True
            put("chains", "PROBLEM", "; ".join(bad), age=tick_age)
        elif read:
            put("chains", "OK", f"{len(read)} chain(s) read this cycle", age=tick_age)
        else:
            put("chains", "idle", "no index was read (not a trading day, or none selected)", age=tick_age)
    upstream_ok = out["chains"]["status"] == "OK"

    # 3 signal
    biased = {u: d for u, d in decisions.items() if d.get("direction")}
    sig_last = _newest(ev["signal"] + ev["veto"] + ev["entry"])
    n_sig = len(ev["signal"]) + len(ev["veto"])
    if upstream_ok and biased:
        put("signal", "OK", ", ".join(f"{u} {d['direction']} {d.get('score')}" for u, d in biased.items()), sig_last, age=tick_age, count=n_sig)
    else:
        put("signal", "idle", "no chain to read" if chain_problem else "waiting for a chain", sig_last, count=n_sig)

    # 4 gates: a block is the gates doing their job, so this stage is OK or idle, never PROBLEM
    gate_skips = [e for e in ev["skip"] if e.get("stage") == "gates"]
    g_last = _newest(gate_skips)
    if g_last:
        put("gates", "OK", f"{len(gate_skips)} block(s) today; latest: {_why(g_last)[:140]}", g_last, count=len(gate_skips))
    elif upstream_ok and biased:
        put("gates", "OK", "no entry blocked today", count=0)
    else:
        put("gates", "idle", "nothing reached the gates today", count=0)

    # 5 sizing / contract pick
    siz = [e for e in ev["skip"] if e.get("stage") in SIZING_STAGES]
    s_last = _newest(siz)
    last_entry = _newest(ev["entry"])
    if s_last and s_last.get("stage") == "capital" and _ts(s_last) > _ts(last_entry):
        put("sizing", "PROBLEM", "no capital to size with: " + _why(s_last), s_last, count=len(siz))
    elif s_last:
        put("sizing", "OK", f"{len(siz)} entry(ies) not sized today; latest stopped at '{s_last.get('stage')}'", s_last, count=len(siz))
    elif last_entry:
        put("sizing", "OK", "sized and entered today", last_entry, count=0)
    else:
        put("sizing", "idle", "no entry reached sizing today", count=0)

    # 6 order (the BUY side) and 7 exits (the SL / SELL side)
    entry_fails = [e for e in ev["order_failed"] if str(e.get("side") or "") not in EXIT_SIDES]
    exit_fails = [e for e in ev["order_failed"] if str(e.get("side") or "") in EXIT_SIDES]
    entry_blocked = [e for e in ev["real_blocked"] if str(e.get("would") or "BUY") not in EXIT_SIDES]
    exit_blocked = [e for e in ev["real_blocked"] if str(e.get("would") or "") in EXIT_SIDES]
    o_last = _newest(ev["entry"] + entry_fails + entry_blocked)
    if o_last and o_last.get("kind") == "order_failed":
        put("order", "PROBLEM", f"the newest order step failed: {str(o_last.get('message') or o_last.get('status') or '')[:140]}", o_last, count=len(ev["entry"]))
    elif o_last and o_last.get("kind") == "real_blocked":
        put("order", "PROBLEM", "a REAL order was held back by closed locks (nothing was sent)", o_last, count=len(ev["entry"]))
    elif o_last:
        put("order", "OK", f"{len(ev['entry'])} entry(ies) today", o_last, count=len(ev["entry"]))
    else:
        put("order", "idle", "no order today", count=0)

    open_n = int(inp.get("open_positions") or 0)
    x_last = _newest(ev["exit"])
    newest_fail = _newest(exit_fails + exit_blocked + ev["stale"])
    if open_n and newest_fail and _ts(newest_fail) > _ts(x_last):
        why_x = {"order_failed": "an exit / stop order failed", "real_blocked": "a REAL exit was held back by closed locks"}.get(str(newest_fail.get("kind")), "a position has no premium")
        put("exits", "PROBLEM", f"{open_n} open; {why_x}", newest_fail, count=len(ev["exit"]))
    elif open_n or x_last:
        put("exits", "OK", f"{open_n} open, {len(ev['exit'])} closed today", x_last, count=len(ev["exit"]))
    else:
        put("exits", "idle", "no open position", count=0)

    # 8 reconcile (REAL only)
    rec = inp.get("reconcile") or {}
    if rec.get("ts") is None or rec.get("ok") is None:
        put("reconcile", "idle", rec.get("note") or ("REAL only: not checked yet" if inp.get("broker") else "REAL only: no Dhan credentials loaded"))
    elif rec.get("ok") is False:
        put("reconcile", "PROBLEM", f"{len(rec.get('differences') or [])} difference(s) with Dhan: check the Dhan app", age=now - float(rec["ts"]))
    elif now - float(rec["ts"]) > RECONCILE_MAX_AGE_S:                   # round 306: any age used to show green here, while the readiness list already called a 20-minute-old check unknown
        mins = int((now - float(rec["ts"])) // 60)
        open_real = int(inp["open_real_positions"] if inp.get("open_real_positions") is not None else (inp.get("open_positions") or 0))   # the engine only reconciles while a REAL position is open (or mode is REAL): DEMO positions never refresh it
        if inp.get("market_open") and open_real > 0:
            put("reconcile", "PROBLEM", f"last check with Dhan is {mins} min old and {open_real} REAL position(s) are open: the engine is not comparing them with Dhan", age=now - float(rec["ts"]))
        else:
            put("reconcile", "idle", f"last check with Dhan is {mins} min old (no REAL position open to compare, or the market is closed)", age=now - float(rec["ts"]))
    else:
        put("reconcile", "OK", "our REAL positions match Dhan's", age=now - float(rec["ts"]))
    return [out[k] for k in ORDER]


def summary(stages: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"problems": [s["label"] for s in stages if s["status"] == "PROBLEM"], "ok": sum(1 for s in stages if s["status"] == "OK"), "idle": sum(1 for s in stages if s["status"] == "idle")}
