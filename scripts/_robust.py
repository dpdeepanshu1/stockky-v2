"""scripts/_robust.py - outlier-proof and market-wide-cluster-proof checks for the entry-rule / gate backtests (group 428).

WHY: the 18 Oct entry_rules report said expiry-day entries earned +3.9 % in 10 sessions and pre-holiday entries +15.8 %, with intervals running down to -51 %.
That is not an edge, it is a few huge numbers (unadjusted splits / bonuses, bad candles) and the fact that every stock shares the same calendar days, so
118,916 entries are really ~1,200 independent days. The old verdict resampled (symbol x month) clusters, which cannot see either problem.

What this adds per rule (all from the same rows, no new data):
  median_passed / median_rejected / median_edge    the typical entry, which one extreme return cannot move
  wins_edge                                        the edge after clipping every return to its own 1st-99th percentile
  day_ci                                           90 % bootstrap interval of the clipped edge, resampling whole ENTRY DAYS (market-wide clusters)
  extreme_rows                                     entries with |return| > 100 % (almost certainly data, not trading)
  verdict_robust                                   helps / hurts only when day_ci, median_edge and wins_edge all agree on the sign; else no_edge
Pure stdlib, never raises on empty input (returns None).
"""
from __future__ import annotations

import random
from statistics import mean, median
from typing import Any, Dict, List, Optional, Sequence


def _q(sorted_vals: Sequence[float], q: float) -> float:
    return sorted_vals[int(q * (len(sorted_vals) - 1))]


def robust_stats(pop_rows: Sequence[Dict[str, Any]], rej, key: str, n_boot: int = 500, seed: int = 7, day_key: str = "day") -> Optional[Dict[str, Any]]:
    vals = [r[key] for r in pop_rows]
    if not vals:
        return None
    s = sorted(vals)
    lo, hi = _q(s, 0.01), _q(s, 0.99)
    clip = lambda x: lo if x < lo else (hi if x > hi else x)  # noqa: E731
    ps = [r[key] for r in pop_rows if not rej(r)]
    rj = [r[key] for r in pop_rows if rej(r)]
    out: Dict[str, Any] = {
        "median_passed": round(median(ps), 3) if ps else None,
        "median_rejected": round(median(rj), 3) if rj else None,
        "median_edge": round(median(ps) - median(rj), 3) if ps and rj else None,
        "wins_edge": round(mean(clip(x) for x in ps) - mean(clip(x) for x in rj), 3) if ps and rj else None,
        "extreme_rows": sum(1 for v in vals if abs(v) > 100.0),
        "wins_bounds": [round(lo, 2), round(hi, 2)],
        "day_ci": None, "day_clusters": 0,
    }
    cl: Dict[str, List[float]] = {}
    for r in pop_rows:
        a = cl.setdefault(str(r.get(day_key) or r.get("month")), [0, 0.0, 0, 0.0])
        v = clip(r[key])
        if rej(r):
            a[2] += 1
            a[3] += v
        else:
            a[0] += 1
            a[1] += v
    arr = list(cl.values())
    out["day_clusters"] = len(arr)
    if len(arr) >= 20 and n_boot >= 50 and ps and rj:
        rng = random.Random(seed)
        m = len(arr)
        edges: List[float] = []
        for _ in range(n_boot):
            np_ = sp = nr = sr = 0
            for k in rng.choices(range(m), k=m):
                a = arr[k]
                np_, sp, nr, sr = np_ + a[0], sp + a[1], nr + a[2], sr + a[3]
            if np_ and nr:
                edges.append(sp / np_ - sr / nr)
        if len(edges) >= n_boot // 2:
            edges.sort()
            out["day_ci"] = [round(edges[int(0.05 * (len(edges) - 1))], 3), round(edges[int(0.95 * (len(edges) - 1))], 3)]
    return out


def robust_verdict(base_verdict: str, rs: Optional[Dict[str, Any]]) -> str:
    """insufficient_sample stays; helps / hurts need the day-cluster interval, the median edge and the clipped edge to agree; anything else no_edge."""
    if base_verdict == "insufficient_sample" or rs is None:
        return base_verdict
    ci, med, win = rs.get("day_ci"), rs.get("median_edge"), rs.get("wins_edge")
    if ci is None or med is None or win is None:
        return "no_edge"
    if ci[0] > 0 and med > 0 and win > 0:
        return "helps"
    if ci[1] < 0 and med < 0 and win < 0:
        return "hurts"
    return "no_edge"


def jump_in_window(closes: Sequence[float], start: int, stop: int, jump_pct: float) -> bool:
    """True when any one-day close-to-close move between closes[start-1] and closes[stop-1] exceeds jump_pct (a split / bonus / bad candle: NSE circuits stop
    a real stock far below 35 %). jump_pct <= 0 = never. Pure."""
    if jump_pct <= 0:
        return False
    for k in range(max(1, start), min(len(closes), stop)):
        p = closes[k - 1]
        if p > 0 and abs(closes[k] / p - 1.0) * 100.0 > jump_pct:
            return True
    return False
