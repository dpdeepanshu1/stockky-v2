"""swing/stats.py - E132: the verdict arithmetic for the swing ledger (and scripts/swing_shadow_report.py). Stdlib only, pure, never raises.

Same words and the same kind of test as the replays: the net mean per trade, a 90 % interval got by resampling WHOLE entry days (the picks of one day move
together), USEFUL only when the interval's 5th percentile is above zero. NOT_ENOUGH_DATA is the normal answer for a long while: a few weeks of shadow days
are a handful of independent days. This measures the SIGNAL after costs; it does not prove the broker, the stop fill or the eDIS step.
"""
from __future__ import annotations

import random
from statistics import mean, median
from typing import Any, Dict, List, Optional, Sequence


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def day_bootstrap_ci(rows: Sequence[Dict[str, Any]], key: str, day_key: str = "entry_day", n_boot: int = 500, seed: int = 7,
                     min_days: int = 20) -> Optional[List[float]]:
    """90 % interval of the mean of `key`, resampling whole days. None under `min_days` days or n_boot < 50."""
    cl: Dict[str, List[float]] = {}
    for r in rows:
        v = _num(r.get(key))
        if v is None:
            continue
        a = cl.setdefault(str(r.get(day_key)), [0.0, 0.0])
        a[0] += 1
        a[1] += v
    arr = list(cl.values())
    if len(arr) < min_days or n_boot < 50:
        return None
    rng = random.Random(seed)
    m = len(arr)
    means: List[float] = []
    for _ in range(n_boot):
        n = s = 0.0
        for k in rng.choices(range(m), k=m):
            n += arr[k][0]
            s += arr[k][1]
        if n:
            means.append(s / n)
    if len(means) < n_boot // 2:
        return None
    means.sort()
    return [round(means[int(0.05 * (len(means) - 1))], 3), round(means[int(0.95 * (len(means) - 1))], 3)]


def summarize(rows: Sequence[Dict[str, Any]], key: str = "net_pct", n_boot: int = 500, min_days: int = 20) -> Dict[str, Any]:
    vals = [v for v in (_num(r.get(key)) for r in rows) if v is not None]
    days = len({str(r.get("entry_day")) for r in rows if _num(r.get(key)) is not None})
    if not vals:
        return {"n": 0, "days": 0, "mean": None, "median": None, "win_pct": None, "ci90": None}
    return {"n": len(vals), "days": days, "mean": round(mean(vals), 3), "median": round(median(vals), 3),
            "win_pct": round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1),
            "ci90": day_bootstrap_ci(rows, key, "entry_day", n_boot, min_days=min_days)}


def verdict(rows: Sequence[Dict[str, Any]], min_n: int = 30, min_days: int = 20, n_boot: int = 500) -> Dict[str, Any]:
    """rows: CLOSED trades with entry_day, net_pct, gross_pct, exit_reason. NOT_ENOUGH_DATA under min_n trades or min_days entry days; USEFUL when the 5th
    percentile of the net mean is above zero; NO_EDGE otherwise (a positive gross mean eaten by costs is named)."""
    closed = [r for r in rows if _num(r.get("net_pct")) is not None]
    net = summarize(closed, "net_pct", n_boot, min_days)
    gross = summarize(closed, "gross_pct", 0, min_days)
    out: Dict[str, Any] = {"n": net["n"], "days": net["days"], "net": net, "gross": gross, "reasons": []}
    stopped = sum(1 for r in closed if r.get("exit_reason") == "stop")
    out["stopped_pct"] = round(100.0 * stopped / len(closed), 1) if closed else None
    out["gap_through"] = sum(1 for r in closed if r.get("gap_through"))
    if net["n"] < min_n or net["days"] < min_days:
        out["verdict"] = "NOT_ENOUGH_DATA"
        out["reasons"].append(f"{net['n']} closed trades on {net['days']} entry days; needs {min_n} trades on {min_days} days")
        return out
    ci = net["ci90"]
    if ci and ci[0] > 0:
        out["verdict"] = "USEFUL"
        return out
    out["verdict"] = "NO_EDGE"
    out["reasons"].append("net mean per trade: the 90% interval does not stay above zero" if ci else "too few entry days for an interval")
    if gross["mean"] is not None and gross["mean"] > 0 and (net["mean"] is None or net["mean"] <= 0):
        out["reasons"].append(f"gross mean {gross['mean']}% is positive but costs eat it")
    return out
