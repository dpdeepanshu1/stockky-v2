"""tsfm_features.py - turn a quantile forecast into trading PARAMETERS (group 434). Pure python, no numpy, no model, no network.

The principle of the TimesFM plan: forecast outputs are volatility / range / volume / uncertainty inputs, never a buy-or-sell direction.
A "fan" is what a backend returns for one series: {"mean": [h values], "quantiles": {"0.1": [h values], "0.5": [...], "0.9": [...], ...}}.
q10, q50 and q90 are required; any other level present is used for a finer CDF.

Derived (all relative to the last COMPLETE daily close):
  q10/q50/q90_ret_pct   final-step return quantiles
  fan_range_pct         (max over steps of q90 - min over steps of q10) / last close: the width of the forecast envelope
  forecast_vol_pct      implied daily sigma from the final-step q10-q90 spread (normal-equivalent), vs trailing_vol_pct (20-bar stdev of log returns)
  vol_ratio             forecast_vol_pct / trailing_vol_pct   (>1: the model expects a wilder week than the last month)
  p_stop_close_max      max over steps of P(close <= stop level); a LOWER BOUND for touching the stop (intraday lows are not modelled)
  p_target_close_max    max over steps of P(close >= target level), same caveat
  volume_pace           median forecast volume over the horizon / trailing 20-bar mean volume (None without a volume forecast)
Every function that takes outside data raises FeatureError on unusable input; nothing here talks to the network.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

Z90 = 1.2815515655446004          # N(0,1) 90th percentile: the q10-q90 spread is 2 * Z90 sigma for a normal
VOL_WINDOW = 20


class FeatureError(ValueError):
    pass


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def normalise_fan(fan: Any, horizon: Optional[int] = None) -> Dict[str, Any]:
    """Validate a fan, sort crossing quantiles per step (q10 <= q50 <= q90), return {"levels": [floats], "q": {level: [h]}, "mean": [h]}."""
    if not isinstance(fan, dict) or not isinstance(fan.get("quantiles"), dict):
        raise FeatureError("fan has no quantiles")
    q: Dict[float, List[float]] = {}
    for k, arr in fan["quantiles"].items():
        lv = _f(k)
        if lv is None or not 0.0 < lv < 1.0:
            raise FeatureError(f"bad quantile level {k!r}")
        vals = [_f(x) for x in (arr or [])]
        if not vals or any(x is None for x in vals):
            raise FeatureError(f"quantile {k} has missing or non-finite values")
        q[lv] = [float(x) for x in vals]
    for need in (0.1, 0.5, 0.9):
        if not any(abs(l - need) < 1e-9 for l in q):
            raise FeatureError(f"fan needs the {need} quantile")
    lens = {len(v) for v in q.values()}
    if len(lens) != 1:
        raise FeatureError("quantiles have different horizons")
    h = lens.pop()
    if horizon is not None and h < horizon:
        raise FeatureError(f"fan covers {h} steps, {horizon} wanted")
    levels = sorted(q)
    for i in range(h):                       # quantile crossing fix: sort the values at each step
        col = sorted(q[l][i] for l in levels)
        for l, x in zip(levels, col):
            q[l][i] = x
    mean = [_f(x) for x in (fan.get("mean") or [])]
    mean = [float(x) for x in mean] if mean and all(x is not None for x in mean) and len(mean) == h else list(q[min(levels, key=lambda l: abs(l - 0.5))])
    return {"levels": levels, "q": q, "mean": mean, "h": h}


def _at(nf: Dict[str, Any], level: float) -> List[float]:
    return nf["q"][min(nf["levels"], key=lambda l: abs(l - level))]


def cdf_at(price: float, step_values: Sequence[float], levels: Sequence[float]) -> float:
    """P(X <= price) from quantile points: linear between them; below the lowest point it falls linearly to 0 over one local segment width
    (above the highest it rises to 1 the same way). An approximation of the tails, which the model does not give."""
    n = len(levels)
    if n < 2:
        raise FeatureError("need two quantile points")
    xs, ps = list(step_values), list(levels)
    if price <= xs[0]:
        w = max(xs[1] - xs[0], 1e-12)
        return max(0.0, ps[0] * (1.0 - (xs[0] - price) / w))
    if price >= xs[-1]:
        w = max(xs[-1] - xs[-2], 1e-12)
        return min(1.0, ps[-1] + (1.0 - ps[-1]) * min(1.0, (price - xs[-1]) / w))
    for i in range(1, n):
        if price <= xs[i]:
            span = xs[i] - xs[i - 1]
            return ps[i - 1] if span <= 0 else ps[i - 1] + (ps[i] - ps[i - 1]) * (price - xs[i - 1]) / span
    return ps[-1]


def trailing_stats(bars: Sequence[Dict[str, Any]], window: int = VOL_WINDOW) -> Dict[str, Optional[float]]:
    """Trailing daily vol (stdev of log returns, %) and mean volume over the last `window` bars of the history given."""
    closes = [_f(b.get("close")) for b in bars]
    vols = [_f(b.get("volume")) for b in bars]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, len(closes) - window), len(closes))
            if closes[i] and closes[i - 1] and closes[i] > 0 and closes[i - 1] > 0]
    vol = None
    if len(rets) >= 5:
        m = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * 100.0
    vv = [v for v in vols[-window:] if v is not None and v >= 0]
    return {"trailing_vol_pct": vol, "trailing_volume": (sum(vv) / len(vv)) if len(vv) >= 5 else None}


def volume_pace(volume_fan: Any, trailing_volume: Optional[float], horizon: int) -> Optional[float]:
    if volume_fan is None or not trailing_volume or trailing_volume <= 0:
        return None
    nf = normalise_fan(volume_fan, horizon)
    med = _at(nf, 0.5)[:horizon]
    return round((sum(med) / len(med)) / trailing_volume, 4)


def derive(last_close: float, fan: Any, *, trailing_vol_pct: Optional[float], stop_pct: Optional[float] = None, target_pct: Optional[float] = None,
           horizon: Optional[int] = None, pace: Optional[float] = None) -> Dict[str, Any]:
    """Parameters from one close fan. stop_pct / target_pct are positive percentages below / above last_close (the touch probabilities are None without them)."""
    lc = _f(last_close)
    if lc is None or lc <= 0:
        raise FeatureError("bad last close")
    nf = normalise_fan(fan, horizon)
    h = nf["h"] if horizon is None else min(horizon, nf["h"])
    q10, q50, q90 = (_at(nf, l)[:h] for l in (0.1, 0.5, 0.9))
    out: Dict[str, Any] = {
        "horizon": h, "last_close": round(lc, 4),
        "q10_ret_pct": round((q10[-1] / lc - 1) * 100, 4), "q50_ret_pct": round((q50[-1] / lc - 1) * 100, 4), "q90_ret_pct": round((q90[-1] / lc - 1) * 100, 4),
        "fan_range_pct": round((max(q90) - min(q10)) / lc * 100, 4),
    }
    sigma_h = (q90[-1] - q10[-1]) / (2 * Z90) / lc * 100.0            # sigma of the h-day return, in %
    fvol = sigma_h / math.sqrt(h)
    out["forecast_vol_pct"] = round(fvol, 4)
    tv = _f(trailing_vol_pct)
    out["trailing_vol_pct"] = None if tv is None else round(tv, 4)
    out["vol_ratio"] = round(fvol / tv, 4) if tv and tv > 0 else None
    out["volume_pace"] = pace
    out["p_stop_close_max"] = out["p_target_close_max"] = None
    sp, tp = _f(stop_pct), _f(target_pct)
    levels = nf["levels"]
    if sp is not None and sp > 0:
        stop = lc * (1 - sp / 100.0)
        out["p_stop_close_max"] = round(max(cdf_at(stop, [nf["q"][l][i] for l in levels], levels) for i in range(h)), 4)
    if tp is not None and tp > 0:
        tgt = lc * (1 + tp / 100.0)
        out["p_target_close_max"] = round(max(1.0 - cdf_at(tgt, [nf["q"][l][i] for l in levels], levels) for i in range(h)), 4)
    return out
