"""stockky_ms.fibonacci - Fibonacci retracement of the latest CONFIRMED impulse leg, golden zone, extensions and confluence.

Anchor = the latest leg between two consecutive structural swings (never full-history extremes) in the direction of the current
structure: bullish / neutral / transition-up -> swing low then the following swing high; bearish -> swing high then the following low.
The leg must be >= fib_min_leg_atr x ATR and >= fib_min_leg_pct %. Only structural swings (already confirmed) are used.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .ms_types import BEARISH, C, finite, r2, r3
from .params import MSParams

RETRACE_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
EXT_LEVELS = (1.272, 1.618)

FIB_KEYS = ("swing_low", "swing_high", "direction", "retracement", "in_golden_zone", "nearest_level", "distance_pct",
            "confluence_count", "leg_invalidated", "golden_low", "golden_high", "ext_1272", "ext_1618")


def empty_fib() -> Dict[str, Any]:
    return {k: None for k in FIB_KEYS}


def _anchor(struct: Sequence[Dict], state: str, last_choch_dir: int, a: float, p: MSParams) -> Optional[Dict[str, Any]]:
    bearish = state == BEARISH or (state == "transition" and last_choch_dir < 0)
    want_end = "low" if bearish else "high"
    for k in range(len(struct) - 1, 0, -1):
        end, start = struct[k], struct[k - 1]
        if end["kind"] != want_end or start["kind"] == want_end:
            continue
        leg = abs(end["price"] - start["price"])
        base = min(end["price"], start["price"])
        if leg >= p.fib_min_leg_atr * a and base > 0 and leg / base * 100.0 >= p.fib_min_leg_pct:
            return {"bearish": bearish, "start": start, "end": end, "leg": leg}
        return None  # the latest leg is too small -> no anchor (do not reach further back)
    return None


def fib_block(struct: Sequence[Dict], bars: Sequence[Sequence[float]], state: str, last_choch_dir: int, a: Optional[float],
              p: MSParams, bands: Optional[Sequence[Dict[str, float]]] = None) -> Dict[str, Any]:
    """Fibonacci block for the last close. `bands` = other price bands [{low, high}] (OB, FVG, support, prev-bar low, VWAP) used
    for the confluence count (a band counts when it overlaps the price neighbourhood +/- fib_conf_tol_pct)."""
    out = empty_fib()
    if not bars or not a or a <= 0 or len(struct) < 2:
        return out
    price = bars[-1][C]
    anc = _anchor(struct, state, last_choch_dir, a, p)
    if anc is None or not finite(price) or price <= 0:
        return out
    lo, hi, leg, bear = min(anc["start"]["price"], anc["end"]["price"]), max(anc["start"]["price"], anc["end"]["price"]), anc["leg"], anc["bearish"]

    def level_price(r: float) -> float:
        return (hi - r * leg) if not bear else (lo + r * leg)

    retr = (hi - price) / leg if not bear else (price - lo) / leg
    prices = {r: level_price(r) for r in RETRACE_LEVELS}
    g1, g2 = prices[0.5], prices[0.618]
    gl, gh = min(g1, g2), max(g1, g2)
    nearest = min(RETRACE_LEVELS, key=lambda r: (abs(prices[r] - price), r))
    invalid_close = any(b[C] < lo for b in bars[anc["end"]["idx"] + 1:]) if not bear else any(b[C] > hi for b in bars[anc["end"]["idx"] + 1:])
    tol = p.fib_conf_tol_pct / 100.0
    n_lo, n_hi = price * (1.0 - tol), price * (1.0 + tol)
    conf = 0
    items: List[Dict[str, float]] = [{"low": gl, "high": gh}] + [dict(x) for x in (bands or [])]
    for b in items:
        if finite(b.get("low")) and finite(b.get("high")) and b["low"] <= n_hi and b["high"] >= n_lo:
            conf += 1
    out.update({
        "swing_low": r2(lo), "swing_high": r2(hi), "direction": "bearish" if bear else "bullish",
        "retracement": r3(retr), "in_golden_zone": bool(0.5 <= retr <= 0.618), "nearest_level": nearest,
        "distance_pct": r2(abs(prices[nearest] - price) / price * 100.0), "confluence_count": conf,
        "leg_invalidated": bool(invalid_close), "golden_low": r2(gl), "golden_high": r2(gh),
        "ext_1272": r2(lo + 1.272 * leg) if not bear else r2(hi - 1.272 * leg),
        "ext_1618": r2(lo + 1.618 * leg) if not bear else r2(hi - 1.618 * leg),
    })
    return out
