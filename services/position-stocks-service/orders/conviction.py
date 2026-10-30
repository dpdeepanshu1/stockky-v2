"""orders/conviction.py - E266 (2026-10-30): the 0-100 conviction score of ONE entry and the size multiple it asks for. Pure reads; places nothing, reserves nothing,
never raises (any problem = None = the old fixed size).

The maths is capital/conviction_sizing.py (identical copy in real-trade-service). This file only gathers what the entry already knows:

  LONG   board     Market Board bullish conviction for this name (the latest shortlist pass)       0.30   conviction 40 -> 0, 90 -> 100
         rr        reward:risk of the FINAL levels, UPSIDE-ONLY: the 1:2 floor is neutral because  0.20   1:2 -> unknown, 1:3.5 -> 100
                   every entry clears it; only a wider adaptive target raises the score
         volume    volume pace of the signal window (rvol)                                         0.15   1.0x -> 0, 3.0x -> 100
         technical technical score from the quality gate                                           0.15   40 -> 0, 90 -> 100
         zone      Market Board zone score                                                         0.10   as is
         catalyst  positive catalyst 100, bulk deal 75, otherwise unknown                          0.10
  SHORT  board bearish conviction 0.35, rr 0.20, weakness (100 - technical score) 0.20, volume 0.15, zone 0.10

An unknown input counts as neutral (50) and can never lift a score; fewer than two known inputs = no opinion.
CONVICTION_SIZING_ENABLED=false (or a missing config) = None for everything.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import config
from capital import conviction_sizing as cs
from orders import rr_floor

logger = logging.getLogger("position-stocks-conviction")

W_LONG = {"board": 0.30, "rr": 0.20, "volume": 0.15, "technical": 0.15, "zone": 0.10, "catalyst": 0.10}
W_SHORT = {"board": 0.35, "rr": 0.20, "weakness": 0.20, "volume": 0.15, "zone": 0.10}
_RR_NEUTRAL_EPS = 0.05


def _sym(candidate: Any) -> str:
    try:
        return str(getattr(candidate, "symbol", "?"))
    except Exception:  # noqa: BLE001 - only used inside an except handler: it must not raise itself
        return "?"


def enabled() -> bool:
    return bool(getattr(config, "CONVICTION_SIZING_ENABLED", False))


def _rr_part(levels: Any) -> Optional[float]:
    """UPSIDE-ONLY: a target at the 1:2 floor is unknown (neutral); 1:2.75 is 50; 1:3.5 is full marks."""
    rr = rr_floor.reward_risk(levels)
    floor = max(1.0, float(getattr(config, "ENTRY_MIN_REWARD_RISK", 2.0) or 2.0))
    if rr is None or rr <= floor + _RR_NEUTRAL_EPS:
        return None
    return cs.scale(rr, floor, floor + 1.5)


def _multiplier(score: float, side: str) -> float:
    max_mult = getattr(config, "CONVICTION_SHORT_MAX_MULT", 2.0) if side == "SHORT" else getattr(config, "CONVICTION_MAX_MULT", 3.0)
    return cs.multiplier(score, low=config.CONVICTION_SCORE_LOW, mid=config.CONVICTION_SCORE_MID, high=config.CONVICTION_SCORE_HIGH,
                         min_mult=config.CONVICTION_MIN_MULT, max_mult=max_mult)


def _decide(parts, side: str) -> Optional[Dict[str, Any]]:
    b = cs.blend(parts, min_known=2)
    if b is None:
        return None
    score, detail = b
    return {"score": score, "mult": round(_multiplier(score, side), 3), "detail": detail, "side": side}


def long_decision(candidate: Any, quality: Any, levels: Any) -> Optional[Dict[str, Any]]:
    """{"score", "mult", "detail", "side"} for a long entry, or None (feature off / too little known). Never raises."""
    try:
        if not enabled():
            return None
        from screening import shortlist
        seen = shortlist.recent(getattr(candidate, "symbol", ""), "LONG") or {}
        tech = getattr(quality, "technical_score", None)
        if getattr(quality, "has_positive_catalyst", None):
            catalyst: Optional[float] = 100.0
        elif getattr(quality, "bulk_deal_flag", False):
            catalyst = 75.0
        else:
            catalyst = None
        parts = [
            ("board", cs.scale(seen.get("board_conviction"), 40, 90), W_LONG["board"]),
            ("rr", _rr_part(levels), W_LONG["rr"]),
            ("volume", cs.scale(getattr(candidate, "rvol", None), 1.0, 3.0), W_LONG["volume"]),
            ("technical", cs.scale(tech, 40, 90), W_LONG["technical"]),
            ("zone", cs.scale(seen.get("zone_score"), 0, 100), W_LONG["zone"]),
            ("catalyst", catalyst, W_LONG["catalyst"]),
        ]
        return _decide(parts, "LONG")
    except Exception as e:  # noqa: BLE001
        logger.debug("conviction (long) failed for %s: %s", _sym(candidate), e)
        return None


def short_decision(candidate: Any, quality: Any, board_row: Optional[dict], levels: Any) -> Optional[Dict[str, Any]]:
    """Same for a short. `board_row` is the Market Board BEARISH row the short entry already holds (conviction, zone). Never raises."""
    try:
        if not enabled():
            return None
        brow = board_row if isinstance(board_row, dict) else {}
        tech = cs._num(getattr(quality, "technical_score", None))
        zone = brow.get("zone")
        zone_score = zone.get("score") if isinstance(zone, dict) else brow.get("zone_score")
        parts = [
            ("board", cs.scale(brow.get("conviction"), 40, 90), W_SHORT["board"]),
            ("rr", _rr_part(levels), W_SHORT["rr"]),
            ("weakness", cs.scale(None if tech is None else 100.0 - tech, 40, 90), W_SHORT["weakness"]),
            ("volume", cs.scale(getattr(candidate, "rvol", None), 1.0, 3.0), W_SHORT["volume"]),
            ("zone", cs.scale(zone_score, 0, 100), W_SHORT["zone"]),
        ]
        return _decide(parts, "SHORT")
    except Exception as e:  # noqa: BLE001
        logger.debug("conviction (short) failed for %s: %s", _sym(candidate), e)
        return None


def note(symbol: str, side: str, decision: Optional[Dict[str, Any]]) -> str:
    """One short line for the log / Telegram: 'conviction 72 -> x2.0 size'. Empty when there is no decision."""
    if not decision:
        return ""
    return f"conviction {decision['score']:.0f} -> x{decision['mult']:.2f} size"