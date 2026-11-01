"""E272/E273: adaptive_params - the maths that turns a fixed constant into a value learned from the stock's own history or the setup's own results.

Pure (no I/O, no config, no clock), never raises. The same file lives in position-stocks-service/orders and real-trade-service/entry_engine (IDENTICAL in scripts/check_shared_copies.py).

  * zscore(value, history)                      distance of a value from the stock's own history, in its own standard deviations (None when the history is too thin)
  * percentile_of(value, history)               0..100 percentile rank of a value inside the stock's own history (None when thin)
  * percentile_value(history, q)                the q-th percentile OF the history (linear interpolation), e.g. the 95th percentile gap
  * shrink(learned, constant, n, prior=30)      n / (n + prior) of the learned value plus the rest of the old constant; n = 0 gives exactly the constant
  * limit_weekly_change(new, previous, ...)     a learned value may move at most 25 % of the previous value per week
  * learned_value(...)                          shrink, then the weekly limit, then the hard ceiling / floor (ceilings never learn)

Guard rails (plan, 'Guard rails for every learned value'): until a cell has MIN_TRADES (30) samples the value IS the old constant; after that it moves by shrinkage;
at most 25 % a week; hard ceilings (max stop, spread ceiling, daily loss, portfolio heat, short vetoes) are passed as `ceiling` / `floor` and always win.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional

MIN_TRADES = 30
PRIOR = 30
MIN_HISTORY = 8
MAX_WEEKLY_CHANGE = 0.25


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _clean(history: Optional[Iterable[Any]]) -> List[float]:
    out: List[float] = []
    try:
        for v in history or []:
            x = _f(v)
            if x is not None:
                out.append(x)
    except Exception:  # noqa: BLE001
        return []
    return out


def zscore(value: Any, history: Optional[Iterable[Any]], min_history: int = MIN_HISTORY) -> Optional[float]:
    """(value - mean) / sample standard deviation of the stock's own history. None when the value is unreadable, the history has fewer than `min_history` numbers, or it is flat."""
    try:
        x, h = _f(value), _clean(history)
        if x is None or len(h) < max(2, int(min_history)):
            return None
        mean = sum(h) / len(h)
        sd = math.sqrt(sum((v - mean) ** 2 for v in h) / (len(h) - 1))
        if sd <= 0:
            return None
        return (x - mean) / sd
    except Exception:  # noqa: BLE001
        return None


def percentile_of(value: Any, history: Optional[Iterable[Any]], min_history: int = MIN_HISTORY) -> Optional[float]:
    """Percentile rank (0..100) of `value` inside the stock's own history: share of history at or below it, ties counted half. None when thin."""
    try:
        x, h = _f(value), _clean(history)
        if x is None or len(h) < max(1, int(min_history)):
            return None
        below = sum(1 for v in h if v < x)
        equal = sum(1 for v in h if v == x)
        return 100.0 * (below + 0.5 * equal) / len(h)
    except Exception:  # noqa: BLE001
        return None


def percentile_value(history: Optional[Iterable[Any]], q: float, min_history: int = MIN_HISTORY) -> Optional[float]:
    """The q-th percentile (0..100) of the history, linear interpolation between ranks. None when thin."""
    try:
        h = sorted(_clean(history))
        qq = _f(q)
        if qq is None or len(h) < max(1, int(min_history)):
            return None
        qq = min(100.0, max(0.0, qq))
        pos = (len(h) - 1) * qq / 100.0
        lo, hi = int(math.floor(pos)), int(math.ceil(pos))
        return h[lo] + (h[hi] - h[lo]) * (pos - lo)
    except Exception:  # noqa: BLE001
        return None


def overnight_gaps(opens: Optional[Iterable[Any]], closes: Optional[Iterable[Any]], max_n: int = 60) -> List[float]:
    """E285 round 103: the stock's own overnight gaps in %, (open[i] - close[i-1]) / close[i-1] x 100 over aligned daily bars, oldest first, at most the last `max_n`.
    A pair that is unreadable, not positive or beyond +/-60 % (a split / bad bar) is skipped. [] on any problem. Signed: a gap UP is positive."""
    try:
        o, c = list(opens or []), list(closes or [])
        n = min(len(o), len(c))
        out: List[float] = []
        for i in range(1, n):
            a, b = _f(o[i]), _f(c[i - 1])
            if a is None or b is None or a <= 0 or b <= 0:
                continue
            g = (a - b) / b * 100.0
            if abs(g) <= 60.0:
                out.append(g)
        return out[-max(1, int(max_n)):]
    except Exception:  # noqa: BLE001
        return []


def gap_pctile(gap_pct: Any, gaps: Optional[Iterable[Any]], min_history: int = 30) -> Optional[float]:
    """E285 round 103: where today's signed gap ranks among the stock's own gaps, 0..1 (1 = bigger than every one of them). None when the gap is unreadable or the history has
    fewer than `min_history` gaps (a thin history never halves a size)."""
    try:
        p = percentile_of(gap_pct, gaps, max(1, int(min_history)))
        return None if p is None else round(p / 100.0, 4)
    except Exception:  # noqa: BLE001
        return None


def shrink_weight(n: Any, prior: int = PRIOR) -> float:
    """n / (n + prior), 0 for n <= 0."""
    try:
        k = _f(n)
        p = max(0.0, float(prior))
        if k is None or k <= 0:
            return 0.0
        return k / (k + p) if (k + p) > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def shrink(learned: Any, constant: Any, n: Any, prior: int = PRIOR, min_n: int = 0) -> Optional[float]:
    """w x learned + (1 - w) x constant with w = n / (n + prior). n = 0 (or n < min_n, or an unreadable learned value) gives the constant unchanged."""
    try:
        c = _f(constant)
        l_ = _f(learned)
        k = _f(n)
        if l_ is None or k is None or k <= 0 or k < float(min_n):
            return c
        if c is None:
            return l_
        w = shrink_weight(k, prior)
        return w * l_ + (1.0 - w) * c
    except Exception:  # noqa: BLE001
        return _f(constant)


def limit_weekly_change(new: Any, previous: Any, max_change: float = MAX_WEEKLY_CHANGE, weeks: float = 1.0) -> Optional[float]:
    """Clamp `new` to previous x (1 +/- max_change x weeks) (at least one week). No previous value (or zero) means no limit. Never raises."""
    try:
        nv, pv = _f(new), _f(previous)
        if nv is None:
            return pv
        if pv is None or pv == 0:
            return nv
        step = abs(pv) * max(0.0, float(max_change)) * max(1.0, float(weeks))
        return min(pv + step, max(pv - step, nv))
    except Exception:  # noqa: BLE001
        return _f(new)


def learned_value(learned: Any, constant: Any, n: Any, *, previous: Any = None, weeks: float = 1.0, ceiling: Any = None, floor: Any = None, prior: int = PRIOR,
                  min_n: int = MIN_TRADES, max_change: float = MAX_WEEKLY_CHANGE) -> Dict[str, Any]:
    """The value to use now: shrink toward the constant (the constant itself below `min_n` samples), limit the weekly move away from `previous`, then clamp to the hard
    ceiling / floor. {"value", "source", "weight"}; source is "constant" until the cell has `min_n` samples, then "learned" (or "ceiling" / "floor" when a hard limit bound). Never raises."""
    c = _f(constant)
    try:
        k = _f(n) or 0.0
        if k < float(min_n) or _f(learned) is None:
            v, src, w = c, "constant", 0.0
        else:
            w = shrink_weight(k, prior)
            v, src = shrink(learned, constant, k, prior), "learned"
            if previous is not None:
                v = limit_weekly_change(v, previous, max_change, weeks)
        hi, lo = _f(ceiling), _f(floor)
        if v is not None and hi is not None and v > hi:
            v, src = hi, "ceiling"
        if v is not None and lo is not None and v < lo:
            v, src = lo, "floor"
        return {"value": v, "source": src, "weight": round(w, 4)}
    except Exception:  # noqa: BLE001
        return {"value": c, "source": "constant", "weight": 0.0}
