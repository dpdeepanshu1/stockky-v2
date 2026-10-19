"""forecast_stats.py - pure numpy/pandas helpers for the Kronos worker (no torch, no network, no FastAPI), so they run and test anywhere.

  clean_history(candles, today)  -> DataFrame (open, high, low, close, volume, amount) of COMPLETED daily bars, or raises BadHistory
  summarize_paths(...)           -> the statistics the entry gate reads, from N independently sampled future paths

Kronos' own predict() averages its sampled paths, which shrinks the move and hides the spread, so the worker keeps every path and the
statistics are computed here. Paths are turned into RATIOS of the last historical close and re-based on the live price (ref_price), so a
candle series that ends yesterday can still be compared with today's stop / target. If one bar's range holds both the stop and the target,
the STOP is counted first (conservative).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, Optional

import numpy as np
import pandas as pd

MIN_BARS = 64            # fewer completed daily bars than this -> no forecast
MAX_DAILY_JUMP = 0.30    # a close-to-close move beyond +/-30% in the window looks like an unadjusted split / bad print
PRICE_COLS = ["open", "high", "low", "close"]


class BadHistory(ValueError):
    """The candle series cannot be used (too short, NaN, non-positive prices, or an unadjusted corporate action)."""


def _pick(row_keys: Iterable[str], name: str) -> Optional[str]:
    for k in row_keys:
        if str(k).lower() == name:
            return k
    return None


def clean_history(candles: Any, today: Optional[date] = None, lookback: int = 128, min_bars: int = MIN_BARS) -> pd.DataFrame:
    """market-data /history candles -> the last `lookback` COMPLETED daily bars. A bar dated `today` or later is dropped (still forming)."""
    if not isinstance(candles, list) or not candles:
        raise BadHistory("no candles")
    df = pd.DataFrame(candles)
    cols = {}
    for name in ("date", "open", "high", "low", "close", "volume"):
        c = _pick(df.columns, name)
        if c is not None:
            cols[name] = c
    for need in ("date", "open", "high", "low", "close"):
        if need not in cols:
            raise BadHistory(f"missing column {need}")
    out = pd.DataFrame({k: df[v] for k, v in cols.items()})
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"])
    try:
        if out["date"].dt.tz is not None:
            out["date"] = out["date"].dt.tz_localize(None)
    except Exception:  # noqa: BLE001
        pass
    out["date"] = out["date"].dt.normalize()
    for c in PRICE_COLS + (["volume"] if "volume" in out.columns else []):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out["volume"] = out["volume"].fillna(0.0).clip(lower=0.0)
    out = out.sort_values("date").drop_duplicates("date", keep="last")
    if today is not None:
        out = out[out["date"] < pd.Timestamp(today)]
    if len(out) < min_bars:
        raise BadHistory(f"only {len(out)} completed bars (need {min_bars})")
    out = out.tail(int(lookback)).reset_index(drop=True)
    if out[PRICE_COLS].isna().any().any():          # a hole inside the window would shift every later bar: refuse rather than patch
        raise BadHistory("missing price inside the window")
    if (out[PRICE_COLS] <= 0).any().any():
        raise BadHistory("non-positive price")
    jumps = out["close"].pct_change().abs().dropna()
    if len(jumps) and float(jumps.max()) > MAX_DAILY_JUMP:
        raise BadHistory(f"close-to-close move of {float(jumps.max()) * 100:.0f}% in the window (unadjusted split / bad print)")
    out["amount"] = out["volume"] * out[PRICE_COLS].mean(axis=1)
    return out[["date"] + PRICE_COLS + ["volume", "amount"]]


def future_dates(last_date: Any, horizon: int) -> pd.DatetimeIndex:
    """The next `horizon` business days after last_date (Kronos only reads calendar features from them; exchange holidays do not matter)."""
    start = pd.Timestamp(last_date) + pd.Timedelta(days=1)
    return pd.bdate_range(start=start, periods=int(horizon))


def _scan(highs: np.ndarray, lows: np.ndarray, stop: float, target: float) -> np.ndarray:
    """Per path: 1 = target first, -1 = stop first (also when one bar holds both), 0 = neither inside the horizon."""
    n, h = highs.shape
    res = np.zeros(n, dtype=int)
    for i in range(n):
        for j in range(h):
            if lows[i, j] <= stop:
                res[i] = -1
                break
            if highs[i, j] >= target:
                res[i] = 1
                break
    return res


def summarize_paths(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, last_close: float, ref_price: float,
                    stop_pct: float, target_pct: float) -> Dict[str, Any]:
    """closes/highs/lows: (n_paths, horizon) in price units, as forecast from a series that ended at last_close.
    ref_price: the live entry price the levels are measured from. stop_pct / target_pct: positive percentages below / above ref_price.
    Returns plain floats: p_up, q10/q50/q90_ret_pct (final close vs ref), mean_ret_pct, p_target_first, p_stop_first, p_neither, ev_pct
    (average outcome with exit at target / stop / the last close), n_paths, horizon."""
    c = np.asarray(closes, dtype=float)
    hi = np.asarray(highs, dtype=float)
    lo = np.asarray(lows, dtype=float)
    if c.ndim != 2 or c.shape != hi.shape or c.shape != lo.shape or c.shape[0] < 1 or c.shape[1] < 1:
        raise ValueError("closes/highs/lows must be the same (n_paths, horizon) shape")
    if not (np.isfinite(c).all() and np.isfinite(hi).all() and np.isfinite(lo).all()):
        raise ValueError("non-finite forecast values")
    if not (last_close > 0 and ref_price > 0 and stop_pct > 0 and target_pct > 0):
        raise ValueError("last_close, ref_price, stop_pct and target_pct must be positive")
    scale = ref_price / last_close
    c, hi, lo = c * scale, hi * scale, lo * scale
    hi = np.maximum(hi, c)           # a sampled bar can come back with high < close; the range must contain the close
    lo = np.minimum(lo, c)
    stop = ref_price * (1.0 - stop_pct / 100.0)
    target = ref_price * (1.0 + target_pct / 100.0)
    final_ret = (c[:, -1] / ref_price - 1.0) * 100.0
    out = _scan(hi, lo, stop, target)
    outcome = np.where(out == 1, float(target_pct), np.where(out == -1, -float(stop_pct), final_ret))
    n = c.shape[0]
    q = np.percentile(final_ret, [10, 50, 90])
    return {
        "n_paths": int(n), "horizon": int(c.shape[1]),
        "p_up": float((final_ret > 0).mean()),
        "q10_ret_pct": float(q[0]), "q50_ret_pct": float(q[1]), "q90_ret_pct": float(q[2]),
        "mean_ret_pct": float(final_ret.mean()),
        "p_target_first": float((out == 1).mean()), "p_stop_first": float((out == -1).mean()), "p_neither": float((out == 0).mean()),
        "ev_pct": float(outcome.mean()),
    }
