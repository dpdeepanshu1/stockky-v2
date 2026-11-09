"""stockky_ms.atr - ATR as a rolling mean of the true range, with the corporate-action clamp the technical service already uses."""
from __future__ import annotations

from typing import List, Optional, Sequence

from .ms_types import C, H, L


def true_ranges(bars: Sequence[Sequence[float]], jump_pct: float) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b[H] - b[L])
            continue
        pc = bars[i - 1][C]
        tr = max(b[H] - b[L], abs(b[H] - pc), abs(b[L] - pc))
        ret = abs(b[C] / pc - 1.0) * 100.0 if pc else 0.0
        out.append(None if ret > jump_pct else tr)
    return out


def atr_series(bars: Sequence[Sequence[float]], period: int = 14, min_bars: int = 5, jump_pct: float = 30.0) -> List[Optional[float]]:
    """ATR at every index (None until `min_bars` valid true ranges are inside the `period` window)."""
    trs = true_ranges(bars, jump_pct)
    out: List[Optional[float]] = []
    for i in range(len(bars)):
        win = [x for x in trs[max(0, i - period + 1): i + 1] if x is not None]
        out.append(sum(win) / len(win) if len(win) >= min_bars else None)
    return out
