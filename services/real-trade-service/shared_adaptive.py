"""
shared/adaptive_thresholds.py  §5 of the master implementation prompt.

percentile_rank, adaptive_gate, hybrid_gate — used by candidate_engine,
surprise_scanner, technical scoring, and risk_engine.

RULES (per the PDF):
  - hybrid_gate  = relative AND absolute — every pass/fail trading decision.
  - adaptive_gate = relative only — display/ranking only, never standalone gate.
  - abs_floor on 10-day return = 3.0 minimum (smaller lets noise through).
  - min_sample = 8 — thin-sample fires routinely; budget for it.
  - Returns native Python bool — never numpy.bool_ (would break `is True` checks).
"""
from __future__ import annotations
from typing import Optional


def percentile_rank(value: float, window: list) -> float:
    """Percentile of value within window (0–100). Display/ranking only."""
    if not window:
        return 50.0
    return round(100.0 * sum(1 for x in window if x <= value) / len(window), 1)


def adaptive_gate(
    value: float,
    window: list,
    base_pctl: float,
    guardrail_min: float,
    guardrail_max: float,
    min_sample: int = 8,
) -> tuple:
    """
    (passes: bool, threshold: float, note: str)
    Thin sample → guardrail_min as threshold.
    Returns native Python bool — not numpy.bool_.
    """
    if len(window) < min_sample:
        return bool(value >= guardrail_min), guardrail_min, "fallback: thin sample"
    pctl = percentile_rank(value, window)
    threshold = max(guardrail_min, min(base_pctl, guardrail_max))
    return bool(pctl >= threshold), threshold, f"adaptive pctl={pctl:.1f}"


def hybrid_gate(
    value: float,
    window: list,
    base_pctl: float,
    guardrail_min: float,
    guardrail_max: float,
    abs_floor: float,
    min_sample: int = 8,
) -> bool:
    """
    Pass/fail gate: relative percentile AND absolute floor BOTH must hold.
    Thin sample (<min_sample) → refuse (False), never guess.
    This is the ONLY gate for pass/fail trading decisions.
    """
    if len(window) < min_sample:
        return False  # thin sample → refuse rather than guess
    passes_pctl, _thr, _note = adaptive_gate(
        value, window, base_pctl, guardrail_min, guardrail_max, min_sample
    )
    return bool(passes_pctl and (value >= abs_floor))


MIN_SECTOR_SAMPLE = 8


async def relative_strength_vs_sector(
    symbol: str,
    sector: str,
    get_return_fn,
    get_peers_fn,
    window_days: int = 10,
) -> dict:
    """
    10-day relative strength vs sector peers via hybrid_gate.
    NEVER gate on 1-day move (mean-reversion risk). abs_floor=3.0.
    """
    stock_ret: Optional[float] = await get_return_fn(symbol, window_days)
    if stock_ret is None:
        return {"stock_return_10d": None, "sector_percentile": None,
                "passes": False, "note": "no data"}
    peers = await get_peers_fn(sector)
    peer_rets = []
    for p in peers:
        if p == symbol:
            continue
        r = await get_return_fn(p, window_days)
        if r is not None:
            peer_rets.append(r)
    pctl = percentile_rank(stock_ret, peer_rets) if peer_rets else 50.0
    passes = hybrid_gate(stock_ret, peer_rets, 70, 55, 85, abs_floor=3.0,
                         min_sample=MIN_SECTOR_SAMPLE)
    return {
        "stock_return_10d": round(stock_ret, 2),
        "sector_percentile": pctl,
        "peers_in_window": len(peer_rets),
        "passes": passes,
        "note": (
            "thin sample — guardrail" if len(peer_rets) < MIN_SECTOR_SAMPLE
            else f"p{pctl:.0f} vs {len(peer_rets)} peers"
        ),
    }
