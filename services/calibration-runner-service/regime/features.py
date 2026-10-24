"""Features for the classifier. Every function takes bars that end BEFORE the decision time and never looks past them.
Daily features come from index (and VIX) daily bars; the opening features come from a gap number the caller supplies;
the intraday features come from index bars of the current session. A feature that cannot be computed is None, never a guess."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

Bar = Dict[str, Any]


def _pct(a: float, b: float) -> Optional[float]:
    return (a / b - 1.0) * 100.0 if b else None


def true_ranges(bars: Sequence[Bar]) -> List[float]:
    out, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"] if prev is None else max(b["h"] - b["l"], abs(b["h"] - prev), abs(b["l"] - prev))
        out.append(tr)
        prev = b["c"]
    return out


def atr_pct(bars: Sequence[Bar], n: int = 14) -> Optional[float]:
    """Mean true range of the last n bars as % of the last close (needs n+1 bars)."""
    if len(bars) < n + 1:
        return None
    tr = true_ranges(bars)[-n:]
    return sum(tr) / n / bars[-1]["c"] * 100.0


def realized_vol_pct(bars: Sequence[Bar], n: int = 10) -> Optional[float]:
    """Standard deviation of the last n daily close-to-close returns, in % (not annualised)."""
    if len(bars) < n + 1:
        return None
    rets = [(bars[i]["c"] / bars[i - 1]["c"] - 1.0) * 100.0 for i in range(len(bars) - n, len(bars))]
    m = sum(rets) / n
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (n - 1))


def sma(bars: Sequence[Bar], n: int) -> Optional[float]:
    return sum(b["c"] for b in bars[-n:]) / n if len(bars) >= n else None


def percentile_rank(values: Sequence[float], x: float) -> Optional[float]:
    """Share (0..100) of `values` that are <= x. None when there is no history."""
    vs = [v for v in values if v is not None]
    return None if not vs else 100.0 * sum(1 for v in vs if v <= x) / len(vs)


def daily_features(nifty: Sequence[Bar], vix: Optional[Sequence[Bar]] = None, vix_window: int = 250) -> Dict[str, Optional[float]]:
    """Everything the morning classification uses. `nifty` / `vix` end at the last COMPLETED session."""
    f: Dict[str, Optional[float]] = {k: None for k in (
        "ret_1d", "ret_5d", "ret_20d", "atr14_pct", "rv10_pct", "last_range_x_atr", "below_sma20_pct", "drawdown_20d_pct",
        "down_days_5", "vix", "vix_chg_1d_pct", "vix_pctile", "bars")}
    f["bars"] = float(len(nifty))
    if len(nifty) >= 2:
        f["ret_1d"] = _pct(nifty[-1]["c"], nifty[-2]["c"])
    if len(nifty) >= 6:
        f["ret_5d"] = _pct(nifty[-1]["c"], nifty[-6]["c"])
        f["down_days_5"] = float(sum(1 for i in range(len(nifty) - 5, len(nifty)) if nifty[i]["c"] < nifty[i - 1]["c"]))
    if len(nifty) >= 21:
        f["ret_20d"] = _pct(nifty[-1]["c"], nifty[-21]["c"])
    a = atr_pct(nifty, 14)
    f["atr14_pct"] = a
    f["rv10_pct"] = realized_vol_pct(nifty, 10)
    if a and len(nifty) >= 2:
        f["last_range_x_atr"] = (nifty[-1]["h"] - nifty[-1]["l"]) / nifty[-1]["c"] * 100.0 / a
    s20 = sma(nifty, 20)
    if s20:
        f["below_sma20_pct"] = max(0.0, (1.0 - nifty[-1]["c"] / s20) * 100.0)
    if len(nifty) >= 20:
        hi = max(b["h"] for b in nifty[-20:])
        f["drawdown_20d_pct"] = max(0.0, (1.0 - nifty[-1]["c"] / hi) * 100.0)
    if vix:
        f["vix"] = vix[-1]["c"]
        if len(vix) >= 2:
            f["vix_chg_1d_pct"] = _pct(vix[-1]["c"], vix[-2]["c"])
        hist = [b["c"] for b in vix[-vix_window:-1]]
        if len(hist) >= 60:
            f["vix_pctile"] = percentile_rank(hist, vix[-1]["c"])
    return f


def opening_features(prev_close: Optional[float], open_price: Optional[float], atr14: Optional[float]) -> Dict[str, Optional[float]]:
    """The gap at the open. `atr14` is the daily ATR in % (daily_features["atr14_pct"]); gap_x_atr says how big the gap is for THIS market."""
    gap = _pct(open_price, prev_close) if prev_close and open_price else None
    return {"gap_pct": gap, "gap_x_atr": (abs(gap) / atr14) if gap is not None and atr14 else None}


def intraday_features(prev_close: Optional[float], bars: Sequence[Bar], atr14: Optional[float]) -> Dict[str, Optional[float]]:
    """From the index bars of today so far: move from yesterday's close, fall from the day's high, the day's range so far in ATRs."""
    out: Dict[str, Optional[float]] = {"day_ret_pct": None, "fall_from_high_pct": None, "range_x_atr": None}
    if not bars:
        return out
    last = bars[-1]["c"]
    hi = max(b["h"] for b in bars)
    lo = min(b["l"] for b in bars)
    if prev_close:
        out["day_ret_pct"] = _pct(last, prev_close)
    out["fall_from_high_pct"] = max(0.0, (1.0 - last / hi) * 100.0)
    if atr14 and prev_close:
        out["range_x_atr"] = (hi - lo) / prev_close * 100.0 / atr14
    return out
