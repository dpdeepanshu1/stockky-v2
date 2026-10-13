"""stockky_ms.swings - fractal swings (v1 | compat) with confirmation index.

v1: pivot high at i when high[i] > max(left bars) (strict) AND high[i] >= max(right bars) (non-strict), so a plateau of equal highs
yields exactly one swing. compat: strict on both sides (legacy board). A swing is emitted with confirmed_at = idx + right.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .ms_types import H, L, T, clamp
from .params import MSParams


def raw_swings(bars: Sequence[Sequence[float]], atr: Sequence[Optional[float]], p: MSParams, mode: str = "v1",
               circuit: Optional[Sequence[bool]] = None, left: Optional[int] = None, right: Optional[int] = None) -> List[Dict]:
    lf = p.swing_left if left is None else left
    rt = p.swing_right if right is None else right
    n = len(bars)
    out: List[Dict] = []
    if lf < 1 or rt < 1:
        return out
    strict_right = mode == "compat"
    for i in range(lf, n - rt):
        if circuit is not None and circuit[i]:
            continue
        h, l = bars[i][H], bars[i][L]
        lh = max(bars[j][H] for j in range(i - lf, i))
        rh = max(bars[j][H] for j in range(i + 1, i + rt + 1))
        if h > lh and (h > rh if strict_right else h >= rh):
            out.append(_mk(bars, atr, i, "high", h, rt))
        ll = min(bars[j][L] for j in range(i - lf, i))
        rl = min(bars[j][L] for j in range(i + 1, i + rt + 1))
        if l < ll and (l < rl if strict_right else l <= rl):
            out.append(_mk(bars, atr, i, "low", l, rt))
    out.sort(key=lambda s: (s["confirmed_at"], s["idx"], s["kind"]))
    return out


def _mk(bars, atr, i, kind, price, rt) -> Dict:
    a = atr[i] if i < len(atr) else None
    lo = max(0, i - 8)
    hi = min(len(bars), i + rt + 1)
    if kind == "high":
        leg = price - min(bars[j][L] for j in range(lo, hi))
    else:
        leg = max(bars[j][H] for j in range(lo, hi)) - price
    strength = clamp(leg / (3.0 * a)) if a and a > 0 else 0.0
    return {"idx": i, "ts": bars[i][T], "kind": kind, "price": price, "confirmed_at": i + rt, "strength": round(strength, 3)}
