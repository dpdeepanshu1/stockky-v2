"""stockky_ms.compat_board - drop-in replacements for the legacy Market Board maths (`screening/market_structure.py` find_zones / swings)
that run on the new engine. Same inputs, same output shape ({side: bull|bear, top, bottom, t, idx, status: fresh|tested, bos, fvg, valid,
impulse, atr}), so board.py can switch with one import. Pure; no I/O, no clock.

Parity choices (see tests/test_group349_compat_board.py): the legacy code uses ONE ATR (mean of the last `period` true ranges) for every
bar, so that value is passed to the engine as a constant ATR series; swings use the strict-both-sides rule (`compat` mode); zones use the
`compat` (or `full`) bounds; there is no age limit and no circuit-bar handling (the intraday board never had them). One intended
difference: the engine looks at the bar right after the displacement candle (legacy: two bars after), so it may call a zone `tested` that
legacy still calls `fresh`, and drops a zone that bar already closed through. It never adds a zone. The engine's `respected` status is
reported as `tested` (legacy has no third state). `swings()` with left or right < 1 returns [] (the engine rejects it; legacy never used it).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .ms_types import C, H, L, T
from .order_blocks import find_order_blocks
from .params import MSParams
from .swings import raw_swings

Bar = Sequence[float]
_BIG = 10 ** 9


def legacy_atr(bars: Sequence[Bar], period: int = 14, min_bars: int = 5) -> Optional[float]:
    """Mean of the last `period` true ranges (the legacy single-value ATR). None when there are too few bars."""
    if len(bars) < max(2, min_bars):
        return None
    trs = [max(bars[i][H] - bars[i][L], abs(bars[i][H] - bars[i - 1][C]), abs(bars[i][L] - bars[i - 1][C])) for i in range(1, len(bars))][-period:]
    return sum(trs) / len(trs) if trs else None


def swings(bars: Sequence[Bar], left: int = 2, right: int = 2) -> List[Dict]:
    """Fractal swings, legacy shape {idx, t, kind, price}, ordered by idx like the legacy list."""
    n = len(bars)
    if left < 1 or right < 1 or n < left + right + 1:
        return []
    raw = raw_swings(bars, [None] * n, MSParams(), mode="compat", left=left, right=right)
    raw.sort(key=lambda s: (s["idx"], s["kind"]))  # legacy emits the high before the low of the same bar
    return [{"idx": s["idx"], "t": s["ts"], "kind": s["kind"], "price": s["price"]} for s in raw]


def find_zones(bars: Sequence[Bar], atr_mult: float = 1.5, max_zones: int = 6, full_range: bool = False,
               atr_period: int = 14, swing_n: int = 2) -> List[Dict]:
    """Order-block zones in the legacy shape. Oldest first, newest `max_zones` kept (negative = keep all, 0 = none)."""
    n = len(bars)
    a = legacy_atr(bars, atr_period)
    if n < 6 or a is None or a <= 0 or swing_n < 1:
        return []
    p = MSParams(swing_left=swing_n, swing_right=swing_n, ob_atr_mult=float(atr_mult), ob_body_frac=0.6, ob_lookback=5,
                 ob_max_age=_BIG, ob_max_zones=-1, vol_mean_bars=_BIG)
    atr = [a] * n
    raw = raw_swings(bars, atr, p, mode="compat")
    off = [False] * n
    obs = find_order_blocks(bars, atr, p, raw, off, off, mode="full" if full_range else "compat")
    zones = [{"side": "bull" if z["side"] == "bullish" else "bear", "top": z["top"], "bottom": z["bottom"], "t": z["ts"], "idx": z["idx"],
              "status": "fresh" if z["status"] == "fresh" else "tested", "bos": z["bos"], "fvg": z["fvg"], "valid": bool(z["bos"] or z["fvg"]),
              "impulse": z["impulse"], "atr": z["atr"]} for z in obs]
    zones.sort(key=lambda z: z["t"])
    if max_zones is not None and max_zones >= 0:
        zones = zones[-max_zones:] if max_zones else []
    return zones
