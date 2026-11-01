"""E285 round 106: event_stats - follow-through of each typed news event type, read from the event the entry stamped into flow_json["setup"]["event"].

The plan wanted the weight of each event type (results, order, deal, rating, ...) and how fast its edge fades to be LEARNED from trades instead of the fixed
table in the event feed (`TYPE_WEIGHT`, one-hour half-life). Nothing was recorded that could teach it, so this round records the event on every entry (setup_stamp)
and reports it here. It changes no entry, size or exit: a type reports verdict NOT_ENOUGH_DATA until it has `MIN_TRADES` (30) closed trades with a readable R, and
the age split says nothing until BOTH age groups have 30. Reuses setup_stats' R and 90 % lower bound. Pure (no I/O), never raises.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from orders import setup_stamp
from orders.setup_stats import MIN_TRADES, _f, _lower_bound, trade_r

FRESH_MAX_MIN = 30.0          # an entry within 30 minutes of the announcement is "fresh"; later is "aged"
AGE_BUCKETS = (("fresh", 0.0, FRESH_MAX_MIN), ("aged", FRESH_MAX_MIN, float("inf")))


def type_key(ev: Dict[str, Any]) -> str:
    """"<type>|UP" for a positive event (a long), "<type>|DOWN" for a negative one (a short)."""
    return f"{ev.get('type')}|{'UP' if ev.get('pol') == 1 else 'DOWN'}"


def _bucket(age: Optional[float]) -> Optional[str]:
    if age is None or age < 0:
        return None
    for name, lo, hi in AGE_BUCKETS:
        if lo <= age < hi:
            return name
    return None


def _cell(win: List[int], rs: List[float], days: List[str], strength: List[float], min_trades: int) -> Dict[str, Any]:
    n = len(win)
    lb = _lower_bound(rs, days) if len(rs) >= min_trades else None
    if n < min_trades or len(rs) < min_trades:
        verdict = "NOT_ENOUGH_DATA"
    else:
        verdict = "POSITIVE" if (lb is not None and lb > 0) else "NOT_PROVEN"
    return {"n": n, "wins": sum(win), "win_rate": round(sum(win) / n, 4) if n else None, "r_n": len(rs),
            "avg_r": round(sum(rs) / len(rs), 4) if rs else None, "lower_bound_r": None if lb is None else round(lb, 4), "verdict": verdict,
            "avg_strength": round(sum(strength) / len(strength), 3) if strength else None}


def summarize(rows: Iterable[Dict[str, Any]], min_trades: int = MIN_TRADES) -> Dict[str, Dict[str, Any]]:
    """{"<type>|UP": {n, wins, win_rate, r_n, avg_r, lower_bound_r, verdict, avg_strength, ages: {fresh: {...}, aged: {...}}, age_effect}} for every event type seen on a stamped
    entry. `age_effect` is "FADES" (fresh avg R above aged, both groups at min_trades), "NO_FADE", or "NOT_ENOUGH_DATA". Never raises."""
    acc: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"win": [], "r": [], "day": [], "st": [], "age": {"fresh": {"win": [], "r": [], "day": [], "st": []}, "aged": {"win": [], "r": [], "day": [], "st": []}}})
    try:
        for row in rows or []:
            st = setup_stamp.read(row.get("flow_json")) if isinstance(row, dict) else None
            ev = st.get("event") if st else None
            if not isinstance(ev, dict) or not ev.get("type") or ev.get("pol") not in (1, -1):
                continue
            pnl = _f(row.get("net_pnl"))
            if pnl is None:
                pnl = _f(row.get("realized_pnl"))
            if pnl is None:
                continue
            r = trade_r(row)
            day = str(row.get("opened_at") or "")[:10]
            strength = _f(ev.get("strength"))
            groups = [acc[type_key(ev)]]
            b = _bucket(_f(ev.get("age_min")))
            if b:
                groups.append(acc[type_key(ev)]["age"][b])
            for g in groups:
                g["win"].append(1 if pnl > 0 else 0)
                if strength is not None:
                    g["st"].append(strength)
                if r is not None:
                    g["r"].append(r)
                    g["day"].append(day)
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, a in sorted(acc.items()):
        cell = _cell(a["win"], a["r"], a["day"], a["st"], min_trades)
        ages = {name: _cell(a["age"][name]["win"], a["age"][name]["r"], a["age"][name]["day"], a["age"][name]["st"], min_trades) for name, _lo, _hi in AGE_BUCKETS}
        f, o = ages["fresh"], ages["aged"]
        if f["r_n"] >= min_trades and o["r_n"] >= min_trades and f["avg_r"] is not None and o["avg_r"] is not None:
            effect = "FADES" if f["avg_r"] > o["avg_r"] else "NO_FADE"
        else:
            effect = "NOT_ENOUGH_DATA"
        out[key] = {**cell, "ages": ages, "age_effect": effect}
    return out
