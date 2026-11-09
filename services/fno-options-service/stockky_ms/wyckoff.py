"""stockky_ms.wyckoff - a deterministic Wyckoff-style PHASE LABEL (WS-14). Context only: it never changes a decision, and `confidence` is the
share of the listed checks that held, not a probability. Wyckoff reading is subjective; these are fixed, documented rules over the last 60 bars
(range width and net move in ATR units, position in the range, volume on up vs down bars, a spring / upthrust). Anything unclear is "unknown".

  markup        net rise >= markup_atr, price in the top third of the window range
  markdown      mirror
  accumulation  tight range after a decline (prior 60 bars fell >= prior_atr), net move small, with evidence a low was swept and reclaimed
                and/or more volume on up bars than down bars (3 of 4 checks must hold)
  distribution  mirror (prior advance, tight range, upthrust and/or more volume on down bars)
Pure stdlib, no I/O, no clock, no env.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .atr import atr_series
from .ms_types import C, H, L, O, V, finite, r2, r3
from .params import MSParams
from .prep import prep

WINDOW = 60
MARKUP_ATR = 4.0
PRIOR_ATR = 3.0
RANGE_MAX_ATR = 8.0
NET_MAX_ATR = 2.0


def _unknown(reason: str) -> Dict[str, Any]:
    return {"phase": "unknown", "confidence": None, "evidence": [], "reason": reason, "context_only": True}


def wyckoff_context(raw_bars: Sequence[Sequence[float]], params: Optional[MSParams] = None) -> Dict[str, Any]:
    p = params or MSParams()
    pr = prep(raw_bars, p)
    bars = pr["bars"]
    if pr["quality"] == "insufficient" or len(bars) < 2 * WINDOW:
        return _unknown("insufficient_history")
    atr = atr_series(bars, p.atr_period, p.atr_min_bars, p.corp_action_jump_pct)[-1]
    if not atr or atr <= 0:
        return _unknown("no_atr")
    win, prior = bars[-WINDOW:], bars[-2 * WINDOW:-WINDOW]
    hh, ll = max(b[H] for b in win), min(b[L] for b in win)
    rng = (hh - ll) / atr
    net = (win[-1][C] - win[0][O]) / atr
    pnet = (prior[-1][C] - prior[0][O]) / atr
    pos = (win[-1][C] - ll) / (hh - ll) if hh > ll else 0.5
    up_v = sum(b[V] for b in win if b[C] >= b[O] and finite(b[V]))
    dn_v = sum(b[V] for b in win if b[C] < b[O] and finite(b[V]))
    vol_known = (up_v + dn_v) > 0
    m = {"net_atr": r2(net), "range_atr": r2(rng), "prior_net_atr": r2(pnet), "pos_in_range": r3(pos),
         "up_down_volume": r3(up_v / dn_v) if dn_v > 0 else None}
    if net >= MARKUP_ATR and pos >= 2 / 3:
        return {"phase": "markup", "confidence": r3(0.5 + 0.5 * min(1.0, net / (2 * MARKUP_ATR))), "evidence": ["net_rise", "top_third"], "metrics": m,
                "context_only": True}
    if net <= -MARKUP_ATR and pos <= 1 / 3:
        return {"phase": "markdown", "confidence": r3(0.5 + 0.5 * min(1.0, -net / (2 * MARKUP_ATR))), "evidence": ["net_fall", "bottom_third"], "metrics": m,
                "context_only": True}
    tight = rng <= RANGE_MAX_ATR and abs(net) <= NET_MAX_ATR
    if tight and pnet <= -PRIOR_ATR:
        checks: List[str] = ["prior_decline", "tight_range"]
        first_low = min(b[L] for b in win[: WINDOW // 2])
        if any(b[L] < first_low and b[C] > first_low for b in win[WINDOW // 2:]):
            checks.append("spring_reclaim")
        if vol_known and up_v > dn_v:
            checks.append("up_volume_dominates")
        if len(checks) >= 3:
            return {"phase": "accumulation", "confidence": r3(len(checks) / 4.0), "evidence": checks, "metrics": m, "context_only": True}
    if tight and pnet >= PRIOR_ATR:
        checks = ["prior_advance", "tight_range"]
        first_high = max(b[H] for b in win[: WINDOW // 2])
        if any(b[H] > first_high and b[C] < first_high for b in win[WINDOW // 2:]):
            checks.append("upthrust_rejection")
        if vol_known and dn_v > up_v:
            checks.append("down_volume_dominates")
        if len(checks) >= 3:
            return {"phase": "distribution", "confidence": r3(len(checks) / 4.0), "evidence": checks, "metrics": m, "context_only": True}
    out = _unknown("no_clear_phase")
    out["metrics"] = m
    return out
