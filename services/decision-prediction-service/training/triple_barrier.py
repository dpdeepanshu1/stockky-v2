"""triple_barrier.py - D22: a cost-aware label for "did this entry work?", instead of the raw 5-day return.

For a signal at the CLOSE of bar i (so only data up to i is used for the barriers):
    entry   = OPEN of bar i+1                      (same rule as scripts/ms_ab_report.py / ma_baseline.py)
    target  = entry + up_atr * ATR_i
    stop    = entry - dn_atr * ATR_i
    window  = bars i+1 .. i+horizon (the entry bar counts: its high / low come after its open)
First barrier touched wins; if one bar touches both, the STOP is assumed first (the conservative reading, a daily bar cannot say which came first).
Neither touched -> timeout, exit at the close of bar i+horizon.
net_return = exit / entry - 1 - (cost_pct + slip_pct) / 100        (defaults match the B tools: 0.30 % costs + 0.10 % slippage)

Columns added (NaN / None where the window does not fit inside the data, so an unfinished label is never trained on):
    tb_exit        'target' | 'stop' | 'timeout'
    tb_label       +1 target, -1 stop, 0 timeout
    tb_net_return  net of costs, as a fraction
    tb_y           1 if tb_net_return > 0 else 0   (the cost-aware classification target)
    tb_bars        bars held (1..horizon)
    tb_end_idx     positional index of the exit bar - use it to PURGE training rows whose label window overlaps a validation row
Pure pandas / numpy. No I/O, no clock.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

TB_COLUMNS = ("tb_exit", "tb_label", "tb_net_return", "tb_y", "tb_bars", "tb_end_idx")


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-style ATR (EWM alpha=1/period) from high / low / close. Uses only bars up to and including each row."""
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _empty(n: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "tb_exit": pd.Series([None] * n, dtype="object"),
            "tb_label": np.full(n, np.nan),
            "tb_net_return": np.full(n, np.nan),
            "tb_y": np.full(n, np.nan),
            "tb_bars": np.full(n, np.nan),
            "tb_end_idx": np.full(n, np.nan),
        }
    )


def triple_barrier_labels(
    df: pd.DataFrame,
    horizon: int = 5,
    up_atr: float = 2.0,
    dn_atr: float = 1.0,
    atr_period: int = 14,
    cost_pct: float = 0.30,
    slip_pct: float = 0.10,
) -> pd.DataFrame:
    """Label every row of one symbol's daily bars (sorted by time, columns open/high/low/close). Returns a frame with TB_COLUMNS, same length / order."""
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    if up_atr <= 0 or dn_atr <= 0:
        raise ValueError("up_atr and dn_atr must be > 0")
    n = len(df)
    out = _empty(n)
    if n == 0:
        return out
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    atr = atr_series(df, atr_period).to_numpy(dtype=float)
    friction = (float(cost_pct) + float(slip_pct)) / 100.0

    exit_kind = [None] * n
    label = np.full(n, np.nan)
    net = np.full(n, np.nan)
    bars = np.full(n, np.nan)
    end_idx = np.full(n, np.nan)

    # the window i+1 .. i+horizon must fit: i + horizon <= n - 1
    for i in range(max(0, n - horizon)):
        a = atr[i]
        entry = o[i + 1]
        if not (np.isfinite(a) and a > 0 and np.isfinite(entry) and entry > 0):
            continue
        tgt = entry + up_atr * a
        stp = entry - dn_atr * a
        kind, exit_px, k_end = "timeout", c[i + horizon], i + horizon
        ok = True
        for k in range(i + 1, i + horizon + 1):
            if not (np.isfinite(h[k]) and np.isfinite(l[k])):
                ok = False
                break
            hit_stop = l[k] <= stp
            hit_tgt = h[k] >= tgt
            if hit_stop:  # stop wins a same-bar tie
                kind, exit_px, k_end = "stop", stp, k
                break
            if hit_tgt:
                kind, exit_px, k_end = "target", tgt, k
                break
        if not ok or not np.isfinite(exit_px):
            continue
        r = exit_px / entry - 1.0 - friction
        exit_kind[i] = kind
        label[i] = {"target": 1, "stop": -1, "timeout": 0}[kind]
        net[i] = r
        bars[i] = k_end - i
        end_idx[i] = k_end

    out["tb_exit"] = pd.Series(exit_kind, dtype="object")
    out["tb_label"] = label
    out["tb_net_return"] = net
    out["tb_y"] = np.where(np.isfinite(net), (net > 0).astype(float), np.nan)
    out["tb_bars"] = bars
    out["tb_end_idx"] = end_idx
    return out


def purge_overlapping(train_idx, val_start: int) -> list:
    """Drop training rows whose label window (tb_end_idx given as a mapping idx -> end) reaches into the validation block.
    `train_idx` is an iterable of (row_position, tb_end_idx) pairs; keep rows that finish before `val_start`."""
    return [p for p, e in train_idx if e is not None and np.isfinite(e) and e < val_start]


def attach(df: pd.DataFrame, **kw) -> pd.DataFrame:
    """Return df with the TB columns added (index preserved)."""
    tb = triple_barrier_labels(df.reset_index(drop=True), **kw)
    tb.index = df.index
    res = df.copy()
    for col in TB_COLUMNS:
        res[col] = tb[col]
    return res
