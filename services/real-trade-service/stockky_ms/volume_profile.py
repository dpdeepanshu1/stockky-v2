"""stockky_ms.volume_profile - volume-at-price over the last `lookback` daily bars (WS-14): POC, value area (VAL/VAH) and where price sits.

Each bar's volume is spread evenly over the price buckets its high-low range touches (a daily bar has no tick data, so this is an
approximation and is labelled `method: "range_uniform"`). Bars without finite positive volume are skipped; with no usable volume the
profile is None - never a made-up POC. Pure stdlib, deterministic.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from .ms_types import C, H, L, V, finite, r2, r3


def volume_profile(bars: Sequence[Sequence[float]], lookback: int = 120, buckets: int = 24, value_area: float = 0.70) -> Optional[Dict[str, Any]]:
    rows = [b for b in list(bars)[-max(1, int(lookback)):] if len(b) >= 6 and all(finite(b[i]) for i in (H, L, C)) and finite(b[V]) and b[V] > 0
            and b[H] >= b[L]]
    if len(rows) < 10 or buckets < 4:
        return None
    lo, hi = min(float(b[L]) for b in rows), max(float(b[H]) for b in rows)
    if hi <= lo:
        return None
    step = (hi - lo) / buckets
    vol = [0.0] * buckets
    for b in rows:
        a = min(buckets - 1, int((float(b[L]) - lo) / step))
        z = min(buckets - 1, int((float(b[H]) - lo) / step))
        share = float(b[V]) / (z - a + 1)
        for k in range(a, z + 1):
            vol[k] += share
    total = sum(vol)
    poc = max(range(buckets), key=lambda k: (vol[k], -k))      # ties -> lowest bucket (deterministic)
    inc, lo_k, hi_k = vol[poc], poc, poc
    while inc < value_area * total and (lo_k > 0 or hi_k < buckets - 1):
        down = vol[lo_k - 1] if lo_k > 0 else -1.0
        up = vol[hi_k + 1] if hi_k < buckets - 1 else -1.0
        if up >= down:
            hi_k += 1
            inc += vol[hi_k]
        else:
            lo_k -= 1
            inc += vol[lo_k]
    price = float(rows[-1][C])
    val, vah, poc_p = lo + lo_k * step, lo + (hi_k + 1) * step, lo + (poc + 0.5) * step
    pos = "above_value" if price > vah else "below_value" if price < val else "inside_value"
    return {"method": "range_uniform", "bars": len(rows), "poc": r2(poc_p), "val": r2(val), "vah": r2(vah), "position": pos,
            "price_vs_poc_pct": r3((price / poc_p - 1.0) * 100.0) if poc_p else None, "poc_share": r3(vol[poc] / total)}
