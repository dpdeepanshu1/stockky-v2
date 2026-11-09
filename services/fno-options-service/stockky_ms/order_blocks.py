"""stockky_ms.order_blocks - displacement candles and order-block ("demand/supply") zones with a lifecycle.

Displacement = body >= ob_atr_mult x ATR and >= ob_body_frac of the range. The zone is the last opposite-colour candle within
ob_lookback bars before it. Zone modes: compat (legacy), body, full. Zones are heuristics, never proof of intent.
status: fresh / tested / respected; broken or expired zones are not returned.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .ms_types import C, H, L, O, V, clamp
from .params import MSParams


def displacement(bar: Sequence[float], a: Optional[float], p: MSParams) -> int:
    """+1 / -1 for a bullish / bearish displacement candle, else 0."""
    if not a or a <= 0:
        return 0
    rng = bar[H] - bar[L]
    body = abs(bar[C] - bar[O])
    if rng <= 0 or body < p.ob_atr_mult * a or body < p.ob_body_frac * rng:
        return 0
    return 1 if bar[C] > bar[O] else -1


def zone_bounds(ob: Sequence[float], d: int, mode: str):
    lo, hi = min(ob[O], ob[C]), max(ob[O], ob[C])
    if mode == "full":
        return ob[L], ob[H]
    if mode == "compat":
        return (lo, ob[H]) if d > 0 else (ob[L], hi)
    return lo, hi


def _vol_ok(bars, i: int, n: int) -> Optional[bool]:
    if i < n:
        return None
    m = sum(bars[j][V] for j in range(i - n, i)) / n
    if m <= 0 or bars[i][V] <= 0:
        return None
    return bars[i][V] >= 1.2 * m


def find_order_blocks(bars: Sequence[Sequence[float]], atr: Sequence[Optional[float]], p: MSParams, raw: Sequence[Dict],
                      gap_event: Sequence[bool], circuit: Sequence[bool], mode: Optional[str] = None) -> List[Dict[str, Any]]:
    n = len(bars)
    mode = mode or p.ob_zone_mode
    zones: List[Dict[str, Any]] = []
    used = set()
    for i in range(1, n):
        if circuit[i]:
            continue
        d = displacement(bars[i], atr[i], p)
        if d == 0:
            continue
        a = atr[i]
        j = None
        for k in range(i - 1, max(-1, i - 1 - p.ob_lookback), -1):
            c = bars[k]
            if circuit[k]:
                continue
            if (d > 0 and c[C] < c[O]) or (d < 0 and c[C] > c[O]):
                j = k
                break
        if j is None or (j, d) in used:
            continue
        used.add((j, d))
        bottom, top = zone_bounds(bars[j], d, mode)
        if top <= bottom:
            continue
        prior = [s for s in raw if s["confirmed_at"] <= j]
        bos = False
        want = "high" if d > 0 else "low"
        lv = [s for s in prior if s["kind"] == want]
        if lv:
            lvl = lv[-1]["price"]
            bos = any((bars[m][C] > lvl) if d > 0 else (bars[m][C] < lvl) for m in range(i, min(n, i + 3)))
        fvg = False
        if i + 1 < n:
            fvg = (bars[i - 1][H] < bars[i + 1][L]) if d > 0 else (bars[i - 1][L] > bars[i + 1][H])
        status, entry_idx, broken = "fresh", None, False
        for m in range(i + 1, n):
            c = bars[m]
            if d > 0:
                if c[C] < bottom:
                    broken = True
                    break
                entered, outside = c[L] <= top, c[C] > top
            else:
                if c[C] > top:
                    broken = True
                    break
                entered, outside = c[H] >= bottom, c[C] < bottom
            if entry_idx is None:
                if entered:
                    entry_idx, status = m, "tested"
            elif outside:
                status = "respected"
        if broken or (n - 1 - i) > p.ob_max_age:
            continue
        zones.append({"side": "bullish" if d > 0 else "bearish", "top": top, "bottom": bottom, "idx": j, "disp_idx": i, "ts": bars[j][0],
                      "status": status, "bos": bos, "fvg": fvg, "impulse": round(abs(bars[i][C] - bars[i][O]) / a, 2),
                      "gap_event": bool(gap_event[i]), "vol_ok": _vol_ok(bars, i, p.vol_mean_bars), "age_bars": n - 1 - i, "atr": a})
    zones.sort(key=lambda z: (z["ts"], z["side"]))
    if p.ob_max_zones >= 0:
        zones = zones[-p.ob_max_zones:] if p.ob_max_zones else []
    return zones


def zone_distance_atr(z: Dict[str, Any], price: float, a: Optional[float]) -> Optional[float]:
    if not a or a <= 0:
        return None
    if z["bottom"] <= price <= z["top"]:
        return 0.0
    return (price - z["top"]) / a if price > z["top"] else (z["bottom"] - price) / a


def finalize(z: Dict[str, Any], price: float, a: Optional[float]) -> Dict[str, Any]:
    """Zone with quality in [0, 1] and distance_atr for the current price."""
    out = dict(z)
    dist = zone_distance_atr(z, price, a)
    vol = z.get("vol_ok")
    fresh = 1.0 if z["status"] == "fresh" else 0.7 if z["status"] == "respected" else 0.4
    q = (0.30 * (1.0 if z.get("bos") else 0.0) + 0.20 * min(1.0, float(z.get("impulse") or 0.0) / 3.0)
         + 0.15 * (1.0 if z.get("fvg") else 0.0) + 0.15 * fresh + 0.10 * (0.5 if vol is None else 1.0 if vol else 0.0)
         + 0.10 * (1.0 - min(1.0, (dist if dist is not None else 3.0) / 3.0)))
    if z.get("gap_event"):
        q *= 0.8
    out["quality"] = round(clamp(q), 2)
    out["distance_atr"] = round(dist, 3) if dist is not None else None
    out["distance_pct"] = round(abs(dist) * (a or 0) / price * 100.0, 2) if dist is not None and price else None
    return out
