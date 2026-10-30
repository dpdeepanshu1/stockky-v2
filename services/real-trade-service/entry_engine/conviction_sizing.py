"""conviction_sizing.py - E266 (2026-10-30): the pure maths of conviction-based trade size. No config, no DB, no I/O, never raises.

WHY: every trade used to get about the same rupees whatever the setup (position-stocks: risk % / slots / stop, slot-capped at 30 % of the pool; real-trade:
25 % concentration cap and a Rs 3,000 flat cap), so a well-confirmed setup that hit its 2.5 % target paid ~Rs 20 while Rs 14,000 sat idle. Now each entry gets an
internal 0-100 conviction score and a size MULTIPLE: a doubtful setup is smaller than the old fixed size, a very sure one is bigger, always inside hard ceilings.

ONE FILE, TWO COPIES: services/position-stocks-service/capital/conviction_sizing.py and services/real-trade-service/entry_engine/conviction_sizing.py are byte-for-byte
identical (scripts/check_shared_copies.py fails if they differ). The callers (orders/conviction.py, entry_engine/conviction_size.py) collect the inputs and read config.

RULES THAT KEEP IT SAFE
  * an unknown input is NEUTRAL (50 at full weight), it can never lift a score; fewer than two known inputs = no opinion = None = the old fixed size;
  * an UPSIZE needs a known ceiling; without one the old size stands;
  * the result of an upsize is never below the old size (a ceiling lower than the old size does not cut the old size) and never above the ceiling / risk cap / cash;
  * a multiple below 1 only ever makes a trade smaller.
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def scale(x, lo, hi) -> Optional[float]:
    """Linear map of x from [lo, hi] onto 0..100, clamped. None when x / lo / hi is unknown or lo >= hi."""
    xv, a, b = _num(x), _num(lo), _num(hi)
    if xv is None or a is None or b is None or b <= a:
        return None
    return max(0.0, min(100.0, (xv - a) / (b - a) * 100.0))


def blend(parts: Sequence[Tuple[str, Optional[float], float]], *, min_known: int = 2, neutral: float = 50.0
          ) -> Optional[Tuple[float, Dict[str, Optional[float]]]]:
    """parts: [(name, value 0..100 or None, weight)]. Returns (score 0..100, {name: value or None}) or None when fewer than `min_known` parts are known.
    A part that is None counts as `neutral` at its FULL weight, so missing data can never raise a score above what the known parts earn."""
    known = 0
    total_w = 0.0
    acc = 0.0
    detail: Dict[str, Optional[float]] = {}
    for name, value, weight in parts:
        w = _num(weight)
        if w is None or w <= 0:
            continue
        v = _num(value)
        if v is None:
            detail[name] = None
            acc += neutral * w
        else:
            v = max(0.0, min(100.0, v))
            detail[name] = round(v, 1)
            acc += v * w
            known += 1
        total_w += w
    if known < max(1, int(min_known)) or total_w <= 0:
        return None
    return round(acc / total_w, 1), detail


def multiplier(score, *, low: float, mid: float, high: float, min_mult: float, max_mult: float) -> float:
    """Score -> size multiple. <= low gives min_mult, mid gives 1.0, >= high gives max_mult, straight lines in between.
    Unknown score or an inconsistent setting (low < mid < high needed) gives 1.0 (the old fixed size)."""
    s, lo, md, hi = _num(score), _num(low), _num(mid), _num(high)
    mn, mx = _num(min_mult), _num(max_mult)
    if s is None or lo is None or md is None or hi is None or mn is None or mx is None or not (lo < md < hi):
        return 1.0
    mn = min(1.0, max(0.0, mn))
    mx = max(1.0, mx)
    if s <= lo:
        return mn
    if s >= hi:
        return mx
    if s <= md:
        return mn + (1.0 - mn) * (s - lo) / (md - lo)
    return 1.0 + (mx - 1.0) * (s - md) / (hi - md)


def hold_back(mult, *, no_upsize: bool) -> float:
    """An upsize (mult > 1) is held at 1.0 when no_upsize is set (a losing day). A downsize is never held back. Unknown = 1.0."""
    m = _num(mult)
    if m is None or m <= 0:
        return 1.0
    if m > 1.0 and no_upsize:
        return 1.0
    return m


def upsized_value(base, mult, *, ceiling=None, risk_cap=None, available=None) -> float:
    """`base` rupees (the old fixed size) x `mult`.
      mult <= 1  -> base x mult (smaller; no ceiling needed).
      mult  > 1  -> base x mult, bounded by `ceiling`, `risk_cap` and `available` (each ignored when None / <= 0 except `ceiling`, which is REQUIRED: without
                    one the old size stands), and never below `base`.
    Never raises; any unreadable input returns `base`."""
    b, m = _num(base), _num(mult)
    if b is None or b <= 0 or m is None or m <= 0:
        return b if b is not None else 0.0
    if m <= 1.0:
        return b * m
    c = _num(ceiling)
    if c is None or c <= 0:
        return b
    v = min(b * m, c)
    rc = _num(risk_cap)
    if rc is not None and rc > 0:
        v = min(v, rc)
    av = _num(available)
    if av is not None and av > 0:
        v = min(v, av)
    return max(b, v)


def scaled_cap(base_cap, mult, ceiling_rs) -> float:
    """A rupee cap moved by the multiple: smaller for mult < 1, larger (up to `ceiling_rs`, never below base_cap) for mult > 1."""
    return upsized_value(base_cap, mult, ceiling=ceiling_rs)