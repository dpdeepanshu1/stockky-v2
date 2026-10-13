"""stockky_ms.liquidity - liquidity pools (raw swings, equal highs/lows) and sweeps with reclaim.

A bullish sweep: a bar pierces a confirmed, old-enough swing LOW by at least sweep_min_atr (at most sweep_max_atr; deeper is a
breakdown) and closes back above it on that bar (wick rejection >= sweep_wick_frac) or within sweep_reclaim_window bars. Bearish mirrors.
A level pierced without a valid reclaim is used up. Each swing is judged on its own, so a prefix gives what a live system saw.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .ms_types import C, H, L, clamp, r2, r3
from .params import MSParams


def _tol(price: float, a: Optional[float], p: MSParams) -> float:
    return max(price * p.equal_tol_pct / 100.0, (p.equal_tol_atr * a) if a else 0.0)


def find_sweeps(bars: Sequence[Sequence[float]], atr: Sequence[Optional[float]], raw: Sequence[Dict], p: MSParams,
                bos_events: Sequence[Dict], disp: Sequence[int]) -> Dict[str, List[Dict[str, Any]]]:
    n = len(bars)
    pools: List[Dict[str, Any]] = []
    sweeps: List[Dict[str, Any]] = []
    for s in raw:
        kind, level, i0 = s["kind"], s["price"], s["confirmed_at"]
        up = kind == "high"
        swept = False
        for j in range(i0 + 1, n):
            b = bars[j]
            if not ((b[H] > level) if up else (b[L] < level)):
                continue
            swept = True
            a = atr[j]
            if j - i0 < p.level_min_age_bars or not a or a <= 0:
                break
            depth = (b[H] - level) if up else (level - b[L])
            if depth < max(p.sweep_min_atr * a, p.tick) or depth > p.sweep_max_atr * a:
                break
            rng = b[H] - b[L]
            reclaim = None
            if ((b[C] < level) if up else (b[C] > level)):
                wick = ((b[H] - b[C]) if up else (b[C] - b[L])) / rng if rng > 0 else 0.0
                if wick >= p.sweep_wick_frac:
                    reclaim = j
            if reclaim is None:
                for m in range(j + 1, min(n, j + 1 + p.sweep_reclaim_window)):
                    if (bars[m][C] < level) if up else (bars[m][C] > level):
                        reclaim = m
                        break
                    if (bars[m][H] > b[H]) if up else (bars[m][L] < b[L]):
                        break
            if reclaim is None:
                break
            ft = any(e["dir"] == (-1 if up else 1) and reclaim < e["break_idx"] <= reclaim + 5 for e in bos_events) or \
                any((disp[m] == (-1 if up else 1)) for m in range(reclaim + 1, min(n, reclaim + 6)))
            wick_q = ((b[H] - b[C]) if up else (b[C] - b[L])) / rng if rng > 0 else 0.0
            strength = clamp(0.25 + 0.3 * wick_q + 0.2 * min(1.0, (depth / a) / 0.5) + (0.25 if ft else 0.0))
            sweeps.append({"direction": "bearish" if up else "bullish", "idx": s["idx"], "level": level, "pierce_idx": j,
                           "confirmed_idx": reclaim, "depth_atr": round(depth / a, 3), "reclaim_bars": reclaim - j,
                           "followthrough": bool(ft), "strength": round(strength, 3)})
            break
        pools.append({"kind": kind, "price": level, "idx": s["idx"], "confirmed_at": i0, "swept": swept, "source": "swing",
                      "equal": False, "touches": 1})
    for kind in ("high", "low"):
        same = [x for x in pools if x["kind"] == kind]
        for x in same:
            t = _tol(x["price"], atr[x["idx"]], p)
            mates = [y for y in same if y is not x and abs(y["price"] - x["price"]) <= t]
            if mates:
                x["equal"] = True
                x["touches"] = 1 + len(mates)
    sweeps.sort(key=lambda s: (s["confirmed_idx"], s["idx"], s["direction"]))
    uniq: List[Dict[str, Any]] = []
    for s in sweeps:
        if uniq and uniq[-1]["confirmed_idx"] == s["confirmed_idx"] and uniq[-1]["direction"] == s["direction"]:
            continue
        uniq.append(s)
    return {"pools": pools, "sweeps": uniq}


def nearest_pools(bars: Sequence[Sequence[float]], a: Optional[float], pools: Sequence[Dict[str, Any]],
                  prev: Optional[Dict[str, float]]) -> Dict[str, Optional[Dict[str, Any]]]:
    """Nearest unswept pool above / below the last close, with the distance in ATR."""
    if not bars or not a or a <= 0:
        return {"nearest_high": None, "nearest_low": None}
    price = bars[-1][C]
    hi = [(x["price"], x["equal"]) for x in pools if x["kind"] == "high" and not x["swept"] and x["price"] > price]
    lo = [(x["price"], x["equal"]) for x in pools if x["kind"] == "low" and not x["swept"] and x["price"] < price]
    if prev:
        if prev.get("high") is not None and prev["high"] > price:
            hi.append((prev["high"], False))
        if prev.get("low") is not None and prev["low"] < price:
            lo.append((prev["low"], False))
    nh = min(hi, key=lambda x: x[0]) if hi else None
    nl = max(lo, key=lambda x: x[0]) if lo else None
    return {"nearest_high": {"price": r2(nh[0]), "atr_dist": r3((nh[0] - price) / a), "equal": bool(nh[1])} if nh else None,
            "nearest_low": {"price": r2(nl[0]), "atr_dist": r3((price - nl[0]) / a), "equal": bool(nl[1])} if nl else None}
