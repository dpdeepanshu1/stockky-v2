"""stockky_ms.fvg - three-bar fair-value gaps ("imbalance zones") with a lifecycle.

Bullish: high[i-1] < low[i+1] with a bullish middle bar; visible only once bar i+1 has closed. Status precedence broken > filled >
expired > tested/partial > fresh. Active = fresh / partial / tested.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .ms_types import C, H, L, O, clamp
from .params import MSParams

ACTIVE = ("fresh", "partial", "tested")


def find_fvgs(bars: Sequence[Sequence[float]], atr: Sequence[Optional[float]], p: MSParams, gap_event: Sequence[bool],
              bos_events: Sequence[Dict], circuit: Sequence[bool]) -> List[Dict[str, Any]]:
    n = len(bars)
    out: List[Dict[str, Any]] = []
    for i in range(1, n - 1):
        a = atr[i]
        if not a or a <= 0 or circuit[i]:
            continue
        mid = bars[i]
        body = abs(mid[C] - mid[O])
        if body < p.fvg_min_body_atr * a:
            continue
        if mid[C] > mid[O] and bars[i - 1][H] < bars[i + 1][L]:
            side, bottom, top = "bullish", bars[i - 1][H], bars[i + 1][L]
        elif mid[C] < mid[O] and bars[i - 1][L] > bars[i + 1][H]:
            side, bottom, top = "bearish", bars[i + 1][H], bars[i - 1][L]
        else:
            continue
        size = top - bottom
        if size < max(p.fvg_min_gap_atr * a, p.tick):
            continue
        bull = side == "bullish"
        entered = filled = broken = outside_after = False
        deepest = None
        for m in range(i + 2, n):
            c = bars[m]
            if (c[C] < bottom) if bull else (c[C] > top):
                broken = True
                break
            reach = c[L] if bull else c[H]
            if (reach < top) if bull else (reach > bottom):
                entered = True
                deepest = reach if deepest is None else (min(deepest, reach) if bull else max(deepest, reach))
                if (reach <= bottom) if bull else (reach >= top):
                    filled = True
                if (c[C] >= top) if bull else (c[C] <= bottom):
                    outside_after = True
            elif entered and ((c[C] >= top) if bull else (c[C] <= bottom)):
                outside_after = True
        age = n - 1 - (i + 1)
        if broken:
            status = "broken"
        elif filled:
            status = "filled"
        elif age > p.fvg_max_age:
            status = "expired"
        elif entered:
            status = "tested" if outside_after else "partial"
        else:
            status = "fresh"
        pct = 0.0 if deepest is None else clamp(((top - deepest) if bull else (deepest - bottom)) / size) * 100.0
        bos = any(e["dir"] == (1 if bull else -1) and i <= e["break_idx"] <= i + 3 for e in bos_events)
        ge = bool(gap_event[i] or gap_event[i + 1])
        q = (0.3 * min(1.0, size / (1.5 * a)) + 0.3 * (1.0 if status == "fresh" else 0.6 if status in ("partial", "tested") else 0.0)
             + 0.2 * (1.0 if bos else 0.0) + 0.2 * min(1.0, body / (2.0 * a)))
        if ge:
            q *= 0.7
        out.append({"side": side, "bottom": bottom, "top": top, "idx": i, "ts": bars[i][0], "status": status, "age_bars": age,
                    "filled_pct": round(pct, 1), "quality": round(clamp(q), 2), "gap_event": ge, "bos": bos})
    return out


def nearest_active(fvgs: Sequence[Dict[str, Any]], side: str, price: float, a: Optional[float]) -> Optional[Dict[str, Any]]:
    pool = []
    for z in fvgs:
        if z["side"] != side or z["status"] not in ACTIVE:
            continue
        d = 0.0 if z["bottom"] <= price <= z["top"] else (price - z["top"]) if price > z["top"] else (z["bottom"] - price)
        pool.append((d, -z["idx"], z))
    if not pool:
        return None
    d, _, z = min(pool, key=lambda x: (x[0], x[1]))
    out = dict(z)
    out["distance_pct"] = round(d / price * 100.0, 2) if price else None
    out["distance_atr"] = round(d / a, 3) if a and a > 0 else None
    return out
