"""E272: setup_stats - win rate, average R and a 90 % lower bound per setup and time slot, read from the setup stamps of closed trades.

Input rows are dicts with `flow_json` (text or dict), `net_pnl` (or `realized_pnl`), `stop_price`, `entry_price`, `quantity` and optionally `opened_at`
(a date string used to cluster the standard error by session day). R = net P&L / rupees risked at the stop; when the risk cannot be read the row is
counted for win rate but not for R. A cell with fewer than MIN_TRADES closed trades reports verdict NOT_ENOUGH_DATA and never a lower bound that could
promote a setup. Pure (no I/O), never raises.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from orders import setup_stamp

MIN_TRADES = 30
Z90 = 1.645


SHORT_CELL_SETUPS = ("S2_RS_BREAKOUT", "S3_EXHAUSTION")


def cell_id(setup_id: Any, side: Any = "LONG") -> str:
    """E274 round 44: the id a setup's cell is keyed by. S2 and S3 fire on both sides, so a SHORT S2 / S3 trade has its OWN cell ("S2_RS_BREAKOUT_SHORT") and never mixes with the long
    one (promotion, demotion and the learned exits would be read from the wrong trades). S1 is long only and S4 short only: their id is the cell id. Never raises."""
    try:
        sid = str(setup_id or "")
        return f"{sid}_SHORT" if str(side or "LONG").upper() == "SHORT" and sid in SHORT_CELL_SETUPS else sid
    except Exception:  # noqa: BLE001
        return str(setup_id or "")


def cell_key(stamp: Any, slot: Any = None) -> str:
    """"<cell id>|<slot>" of a setup stamp (flow_json["setup"]); the stamp's own side picks the long or the short cell. Never raises."""
    try:
        st = stamp if isinstance(stamp, dict) else {}
        return f"{cell_id(st.get('id'), st.get('side'))}|{slot or st.get('slot') or 'later'}"
    except Exception:  # noqa: BLE001
        return "|later"


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def trade_r(row: Dict[str, Any]) -> Optional[float]:
    pnl = _f(row.get("net_pnl"))
    if pnl is None:
        pnl = _f(row.get("realized_pnl"))
    e, s, q = _f(row.get("entry_price")), _f(row.get("stop_price")), _f(row.get("quantity"))
    if pnl is None or e is None or s is None or q is None or q <= 0:
        return None
    risk = abs(e - s) * q
    return pnl / risk if risk > 0 else None


MIN_EXIT_SAMPLES = 10


def _median(v: List[float]) -> Optional[float]:
    if not v:
        return None
    s = sorted(v)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _gain_pct(row: Dict[str, Any], short: bool) -> Optional[float]:
    """Best price the trade reached, as % of entry in its own direction (a long's highest price, a short's lowest); None when unreadable or not above entry."""
    e = _f(row.get("entry_price"))
    best = _f(row.get("min_price_seen") if short else row.get("max_price_seen"))
    if e is None or e <= 0 or best is None or best <= 0:
        return None
    g = ((e - best) if short else (best - e)) / e * 100.0
    return g if g > 0 else None


def _ttp_min(row: Dict[str, Any], short: bool) -> Optional[float]:
    """Minutes from the entry to the trade's best price, from flow_json["peak"] (written by excursion.py) and `opened_ts`; None when missing or absurd."""
    try:
        fj = row.get("flow_json")
        d = json.loads(fj) if isinstance(fj, str) and fj else fj
        pk = d.get("peak") if isinstance(d, dict) else None
        at = _f(pk.get("lo_at" if short else "hi_at")) if isinstance(pk, dict) else None
        t0 = _f(row.get("opened_ts"))
        if at is None or t0 is None:
            return None
        m = (at - t0) / 60.0
        return m if 0.0 <= m <= 600.0 else None
    except Exception:  # noqa: BLE001
        return None


def exit_numbers(cell: Optional[Dict[str, Any]], *, ttp_fallback_min: float, target_pct: Optional[float] = None, prior: int = MIN_TRADES) -> Dict[str, Any]:
    """E275 round 37: the exit numbers one setup cell's own winners support, shrunk toward the old constants. A cell with fewer than MIN_TRADES closed trades, or a metric
    with fewer than MIN_EXIT_SAMPLES samples, gives the constant (source "fallback"). Otherwise value = w x learned + (1 - w) x constant with w = n / (n + prior).
    {"ttp_min", "typical_gain_pct", "ttp_source", "gain_source"}; typical_gain_pct is None when the trade's own target is unknown and nothing is learned. Never raises."""
    out: Dict[str, Any] = {"ttp_min": float(ttp_fallback_min), "typical_gain_pct": target_pct, "ttp_source": "fallback", "gain_source": "fallback"}
    try:
        if not isinstance(cell, dict) or int(cell.get("n") or 0) < MIN_TRADES:
            return out
        n_t, v_t = int(cell.get("ttp_n") or 0), _f(cell.get("ttp_min"))
        if v_t is not None and n_t >= MIN_EXIT_SAMPLES:
            w = n_t / (n_t + float(prior))
            out["ttp_min"] = round(w * v_t + (1.0 - w) * float(ttp_fallback_min), 2)
            out["ttp_source"] = f"learned n={n_t} w={w:.2f}"
        n_g, v_g = int(cell.get("gain_n") or 0), _f(cell.get("typical_gain_pct"))
        if v_g is not None and n_g >= MIN_EXIT_SAMPLES:
            w = n_g / (n_g + float(prior))
            base = _f(target_pct)
            out["typical_gain_pct"] = round(w * v_g + (1.0 - w) * base, 3) if base else round(v_g, 3)
            out["gain_source"] = f"learned n={n_g} w={w:.2f}"
        return out
    except Exception:  # noqa: BLE001
        return {"ttp_min": float(ttp_fallback_min), "typical_gain_pct": target_pct, "ttp_source": "fallback", "gain_source": "fallback"}


def _lower_bound(rs: List[float], days: List[str]) -> Optional[float]:
    """90 % lower bound of the mean R. With session days known the standard error is taken across day means (trades of one day share the market)."""
    n = len(rs)
    if n < 2:
        return None
    mean = sum(rs) / n
    by_day: Dict[str, List[float]] = defaultdict(list)
    for r, d in zip(rs, days):
        by_day[d or "?"].append(r)
    if len(by_day) >= 5:
        dm = [sum(v) / len(v) for v in by_day.values()]
        k = len(dm)
        mu = sum(dm) / k
        var = sum((x - mu) ** 2 for x in dm) / (k - 1)
        se = math.sqrt(var / k)
    else:
        var = sum((x - mean) ** 2 for x in rs) / (n - 1)
        se = math.sqrt(var / n)
    return mean - Z90 * se


def summarize(rows: Iterable[Dict[str, Any]], min_trades: int = MIN_TRADES) -> Dict[str, Dict[str, Any]]:
    """{"<setup id>|<slot>": {n, wins, win_rate, days, avg_r, r_n, lower_bound_r, verdict}} for every cell that has a stamp, plus "<setup id>|ALL"."""
    cells: Dict[str, Dict[str, list]] = defaultdict(lambda: {"win": [], "r": [], "rday": [], "day": [], "gain": [], "ttp": [], "alldays": []})
    try:
        for row in rows or []:
            st = setup_stamp.read(row.get("flow_json")) if isinstance(row, dict) else None
            if not st:
                continue
            pnl = _f(row.get("net_pnl"))
            if pnl is None:
                pnl = _f(row.get("realized_pnl"))
            if pnl is None:
                continue
            r = trade_r(row)
            day = str(row.get("opened_at") or "")[:10]
            short = str(row.get("side") or st.get("side") or "LONG").upper() == "SHORT"
            gain = _gain_pct(row, short) if pnl > 0 else None          # E275 round 37: winners only - the typical maximum gain a WINNER reached
            ttp = _ttp_min(row, short) if pnl > 0 else None
            cid = cell_id(st["id"], "SHORT" if short else "LONG")                  # E274 round 44: a short S2 / S3 has its own cell
            for key in (f"{cid}|{st.get('slot') or 'later'}", f"{cid}|ALL"):
                c = cells[key]
                c["win"].append(1 if pnl > 0 else 0)
                if day:
                    c["alldays"].append(day)          # E276 round 72: sessions a cell has trades in (NO_SETUP_MODE readiness)
                if gain is not None:
                    c["gain"].append(gain)
                if ttp is not None:
                    c["ttp"].append(ttp)
                if r is not None:
                    c["r"].append(r)
                    c["rday"].append(day)
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, c in sorted(cells.items()):
        n = len(c["win"])
        rs = c["r"]
        lb = _lower_bound(rs, c["rday"]) if len(rs) >= min_trades else None
        if n < min_trades or len(rs) < min_trades:
            verdict = "NOT_ENOUGH_DATA"
        else:
            verdict = "POSITIVE" if (lb is not None and lb > 0) else "NOT_PROVEN"
        out[key] = {"n": n, "wins": sum(c["win"]), "win_rate": round(sum(c["win"]) / n, 4) if n else None, "r_n": len(rs), "days": len(set(c["alldays"])),
                    "avg_r": round(sum(rs) / len(rs), 4) if rs else None, "lower_bound_r": None if lb is None else round(lb, 4), "verdict": verdict,
                    "gain_n": len(c["gain"]), "typical_gain_pct": None if len(c["gain"]) < MIN_EXIT_SAMPLES else round(_median(c["gain"]), 3),
                    "ttp_n": len(c["ttp"]), "ttp_min": None if len(c["ttp"]) < MIN_EXIT_SAMPLES else round(_median(c["ttp"]), 2)}
    return out
