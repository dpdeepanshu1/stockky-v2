"""stockky_ms.replay - run analyze() over a history exactly as it would have run on each past day (no lookahead).

    replay(bars, params=None, warmup=None, step=1, ctx_fn=default_ctx, keep_block=False) -> list[dict]

For every index i (from warmup-1, every `step` bars) the engine sees ONLY bars[:i+1]. Each result is
    {i, ts, price, quality, summary (flat ms_* dict), block (only when keep_block)}.
This is the one primitive the backtest, the observation evaluator and the training builder share, so "what the engine said that day" is
always computed the same way. Pure and deterministic: no I/O, no clock, no env. Cost is O(n^2) in the history length; use `step` to thin it.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from .engine import analyze
from .ms_types import C, finite
from .params import MSParams
from .summary import flatten_summary

EMA_PERIOD = 20


def ema_last(closes: Sequence[float], period: int = EMA_PERIOD) -> Optional[float]:
    """Last value of the exponential moving average (seeded with the first `period` closes' mean). None when there are too few closes."""
    vals = [float(c) for c in closes if finite(c)]
    if len(vals) < period:
        return None
    k = 2.0 / (period + 1.0)
    e = sum(vals[:period]) / period
    for v in vals[period:]:
        e = v * k + e * (1.0 - k)
    return e


def default_ctx(bars_so_far: Sequence[Sequence[float]]) -> Dict[str, Any]:
    """The context the live path can always supply from daily bars alone: ema20 (never vwap - that needs intraday data)."""
    e = ema_last([b[C] for b in bars_so_far])
    return {"ema20": e} if e is not None else {}


def replay(bars: Sequence[Sequence[float]], params: Optional[MSParams] = None, warmup: Optional[int] = None, step: int = 1,
           ctx_fn: Optional[Callable[[Sequence[Sequence[float]]], Dict[str, Any]]] = default_ctx, keep_block: bool = False) -> List[Dict[str, Any]]:
    p = params or MSParams()
    n = len(bars)
    w = max(5, int(warmup if warmup is not None else p.min_bars))
    st = max(1, int(step))
    out: List[Dict[str, Any]] = []
    i = w - 1
    while i < n:
        view = bars[: i + 1]
        ctx = ctx_fn(view) if ctx_fn else {}
        blk = analyze(view, params=p, ctx=ctx)
        row: Dict[str, Any] = {"i": i, "ts": bars[i][0], "price": float(bars[i][C]), "quality": blk.get("quality"),
                               "summary": flatten_summary(blk)}
        if keep_block:
            row["block"] = blk
        out.append(row)
        i += st
    return out
