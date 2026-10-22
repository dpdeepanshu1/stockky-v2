"""calibration.py - D22: are the model's probabilities honest, and does picking trades with them beat a plain MA crossover?

  brier(p, y)                          mean squared error of the probabilities (lower is better)
  reliability_table(p, y, bins=10)     per probability bin: n, mean predicted, observed hit rate; plus ECE (weighted mean gap)
  IsotonicCalibrator().fit(p, y)       pool-adjacent-violators, monotone map p -> calibrated p. FIT ON A HOLD-OUT FOLD, never the rows the model trained on.
  ma_cross_signals(df, fast, slow)     True on the bar where fast SMA crosses above slow SMA (the B16 dumb baseline, decided on that bar's close)
  compare_to_baseline(model_net, base_net, ...)
                                       mean net return of the model's picks vs the MA-cross entries, 90 % bootstrap CI of the difference.
                                       Verdict follows scripts/ma_baseline.py: insufficient_sample (< MIN_N on either side, the B15 floor) /
                                       beats_dumb / worse_than_dumb / no_better. Unpaired comparison: the two groups cover different days.
Pure numpy / pandas. No I/O, no clock; the bootstrap takes a seed.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd

MIN_N = 15  # the B15 out-of-sample floor


def _clean(p, y):
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(p) & np.isfinite(y)
    return p[m], y[m]


def brier(p, y) -> float:
    p, y = _clean(p, y)
    return float("nan") if len(p) == 0 else float(np.mean((p - y) ** 2))


def reliability_table(p, y, bins: int = 10) -> Dict[str, Any]:
    p, y = _clean(p, y)
    rows = []
    ece = 0.0
    if len(p):
        edges = np.linspace(0.0, 1.0, bins + 1)
        idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
        for b in range(bins):
            m = idx == b
            n = int(m.sum())
            if n == 0:
                continue
            mp, hit = float(p[m].mean()), float(y[m].mean())
            rows.append({"bin": b, "lo": float(edges[b]), "hi": float(edges[b + 1]), "n": n, "mean_pred": mp, "hit_rate": hit})
            ece += n / len(p) * abs(mp - hit)
    return {"n": int(len(p)), "ece": float(ece) if len(p) else float("nan"), "bins": rows}


class IsotonicCalibrator:
    """Monotone non-decreasing map from raw probability to observed frequency (pool adjacent violators)."""

    def __init__(self) -> None:
        self.x_: Optional[np.ndarray] = None
        self.y_: Optional[np.ndarray] = None

    def fit(self, p, y) -> "IsotonicCalibrator":
        p, y = _clean(p, y)
        if len(p) == 0:
            raise ValueError("no finite rows to fit")
        order = np.argsort(p, kind="mergesort")
        xs, ys = p[order], y[order]
        # blocks: [value, weight, x_min, x_max]
        vals, wts, xmin, xmax = [], [], [], []
        for x, v in zip(xs, ys):
            vals.append(float(v)); wts.append(1.0); xmin.append(float(x)); xmax.append(float(x))
            while len(vals) > 1 and vals[-2] > vals[-1]:
                w = wts[-2] + wts[-1]
                v2 = (vals[-2] * wts[-2] + vals[-1] * wts[-1]) / w
                xm, xx = xmin[-2], xmax[-1]
                for lst in (vals, wts, xmin, xmax):
                    lst.pop()
                vals[-1], wts[-1], xmin[-1], xmax[-1] = v2, w, xm, xx
        # one knot per block at its midpoint
        self.x_ = np.array([(a + b) / 2.0 for a, b in zip(xmin, xmax)])
        self.y_ = np.array(vals)
        return self

    def predict(self, p) -> np.ndarray:
        if self.x_ is None:
            raise RuntimeError("fit first")
        p = np.asarray(p, dtype=float)
        out = np.interp(p, self.x_, self.y_)  # flat outside the fitted range
        out[~np.isfinite(p)] = np.nan
        return out


def ma_cross_signals(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    """True on the bar where the fast SMA closes above the slow SMA after being at or below it the bar before. Uses only data up to that bar."""
    if fast >= slow:
        raise ValueError("fast must be < slow")
    c = df["close"].astype(float)
    f = c.rolling(fast, min_periods=fast).mean()
    s = c.rolling(slow, min_periods=slow).mean()
    cross = (f.shift(1) <= s.shift(1)) & (f > s)
    return cross.fillna(False)


def _boot_diff(a: np.ndarray, b: np.ndarray, boot: int, seed: int):
    rng = np.random.default_rng(seed)
    diffs = np.empty(boot)
    for k in range(boot):
        diffs[k] = rng.choice(a, len(a)).mean() - rng.choice(b, len(b)).mean()
    return float(np.percentile(diffs, 5)), float(np.percentile(diffs, 95))


def compare_to_baseline(
    model_net: Sequence[float],
    base_net: Sequence[float],
    boot: int = 1000,
    seed: int = 7,
    min_n: int = MIN_N,
) -> Dict[str, Any]:
    """Net returns (fractions) of the entries the model picked vs the MA-cross entries, both labelled with the same triple-barrier / cost rule."""
    a = np.asarray([x for x in model_net if x is not None and np.isfinite(x)], dtype=float)
    b = np.asarray([x for x in base_net if x is not None and np.isfinite(x)], dtype=float)
    res: Dict[str, Any] = {
        "n_model": int(len(a)),
        "n_baseline": int(len(b)),
        "mean_model": float(a.mean()) if len(a) else None,
        "mean_baseline": float(b.mean()) if len(b) else None,
        "edge_pp": None,
        "ci90_pp": None,
        "verdict": "insufficient_sample",
        "note": "unpaired comparison: the two groups cover different days",
    }
    if len(a) < min_n or len(b) < min_n:
        return res
    edge = float(a.mean() - b.mean())
    lo, hi = _boot_diff(a, b, boot, seed)
    res["edge_pp"] = round(edge * 100, 3)
    res["ci90_pp"] = [round(lo * 100, 3), round(hi * 100, 3)]
    if lo > 0 and a.mean() > 0:
        res["verdict"] = "beats_dumb"
    elif hi < 0:
        res["verdict"] = "worse_than_dumb"
    else:
        res["verdict"] = "no_better"
    return res
