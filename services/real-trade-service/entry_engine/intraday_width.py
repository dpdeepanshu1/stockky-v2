"""intraday_width.py - cap the stop / target width of an old-rule shortlist entry that will be sold TODAY (E286 round 214; real-trade only). Pure, no I/O, never raises.

The ATR stop is 1.5 x the DAILY ATR % (2 to 6 %) and the target twice that, so COFORGE, CARERATING and MRPL (4 Nov 2026, "Shortlist (old rule)", horizon mid) got 4-5 % stops and
10-12 % targets - a multi-day swing width on a trade the end-of-day square-off closes the same afternoon. A stock does not usually travel that far in one session, so the
target was never reachable and the stop only ever cost the full distance.

cap_for_intraday(stop_pct, target_pct, atr_pct, atr_frac, floor_pct):
  cap = max(floor_pct, atr_frac x the daily ATR %)
  stop <= cap -> unchanged. Otherwise the stop becomes the cap and the target keeps the SAME reward:risk ratio (so every R:R floor downstream still passes).
No usable ATR % = no cap (the flat fallback is left alone).
"""
from __future__ import annotations

import math
from typing import Optional, Tuple


def cap_for_intraday(stop_pct, target_pct, atr_pct, atr_frac: float = 0.6, floor_pct: float = 1.2) -> Tuple[float, float, Optional[str]]:
    """(stop_pct, target_pct, note). note is None when nothing changed."""
    try:
        s, t, a = float(stop_pct), float(target_pct), float(atr_pct)
        if not all(math.isfinite(x) and x > 0 for x in (s, t, a)):
            return stop_pct, target_pct, None
        cap = max(float(floor_pct), float(atr_frac) * a)
        if s <= cap + 1e-9:
            return stop_pct, target_pct, None
        ratio = t / s
        ns = round(cap, 2)
        nt = round(ns * ratio, 2)
        return ns, nt, f"intraday width cap: stop {s:.2f}% -> {ns:.2f}%, target {t:.2f}% -> {nt:.2f}% (cap = max({floor_pct:g}%, {atr_frac:g} x daily ATR {a:.2f}%))"
    except Exception:  # noqa: BLE001
        return stop_pct, target_pct, None
