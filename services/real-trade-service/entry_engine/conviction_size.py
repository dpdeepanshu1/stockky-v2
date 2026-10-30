"""entry_engine/conviction_size.py - E266 (2026-10-30): the 0-100 conviction score of ONE real-trade entry and the size multiple it asks for. Pure; places nothing,
reserves nothing, no DB, never raises (any problem = None = the old fixed caps).

The maths is entry_engine/conviction_sizing.py (identical copy in position-stocks-service). This file only gathers what the entry cycle already knows:

  conviction  the candidate's own conviction score (the floor to be a candidate is 55)                  0.35   55 -> 0, 95 -> 100
  board       Market Board bullish conviction for the name (position-stocks /board, cached)             0.20   40 -> 0, 90 -> 100
  rr          reward:risk of the FINAL levels, UPSIDE-ONLY: the entry floor is neutral because every     0.15   floor -> unknown, floor+1.5 -> 100
              entry clears it; only a wider adaptive target (1:2.5, 1:3 ...) raises the score
  market      today's regime score minus its own adaptive gate (0 in a regime override)                 0.15   +0 -> 0, +25 -> 100
  fresh       entry freshness: how little the price ran from the signal (drift vs its ATR limit)        0.15   safety 20 % -> 0, 100 % -> 100

An unknown input counts as neutral (50) and can never lift a score, so a trade with nothing known gets exactly the old size; fewer than two known inputs = no opinion.

The multiple moves the two rupee caps of risk_engine (single-position concentration and max trade value). It is HELD at 1.0 (an upsize only; a downsize always applies) when
  * today's realized P&L is negative (CONVICTION_NO_UPSIZE_ON_LOSS_DAY),
  * the entry is a regime override (it already goes in at a reduced risk),
  * a VIX / calendar / opening-window size-down is in force: those scale the SHARE COUNT before the rupee caps, so on an upsized cap they would stop biting.
CONVICTION_SIZING_ENABLED=false (or a missing config) = None for everything.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import config
from entry_engine import conviction_sizing as cs

logger = logging.getLogger("real-trade-conviction")

WEIGHTS = {"conviction": 0.35, "board": 0.20, "rr": 0.15, "market": 0.15, "fresh": 0.15}
_RR_NEUTRAL_EPS = 0.05


def _sym(cand: Any) -> str:
    try:
        return str(getattr(cand, "symbol", "?"))
    except Exception:  # noqa: BLE001 - only used inside an except handler: it must not raise itself
        return "?"


def enabled() -> bool:
    return bool(getattr(config, "CONVICTION_SIZING_ENABLED", False))


def _rr_part(rr: Any, floor: Any) -> Optional[float]:
    """UPSIDE-ONLY: a target at the floor (default 1:2) is unknown (neutral); floor+0.75 is 50; floor+1.5 (1:3.5) is full marks."""
    r, f = cs._num(rr), cs._num(floor)
    f = max(1.0, f if f is not None else 2.0)
    if r is None or r <= f + _RR_NEUTRAL_EPS:
        return None
    return cs.scale(r, f, f + 1.5)


def _market_part(market_score: Any, threshold: Any, regime_override: bool) -> Optional[float]:
    if regime_override:
        return 0.0                                   # a name let through against a still-weak market read gets no credit for the market
    m, t = cs._num(market_score), cs._num(threshold)
    if m is None or t is None:
        return None
    return cs.scale(m - t, 0.0, 25.0)


def _fresh_part(drift_pct: Any, max_drift_pct: Any) -> Optional[float]:
    d, mx = cs._num(drift_pct), cs._num(max_drift_pct)
    if d is None or mx is None or mx <= 0:
        return None
    safety = (1.0 - abs(d) / mx) * 100.0
    return cs.scale(safety, 20.0, 100.0)


def _board_part(board_row: Any) -> Optional[float]:
    if not isinstance(board_row, dict) or board_row.get("side") not in (None, "bull"):
        return None
    return cs.scale(board_row.get("conviction"), 40.0, 90.0)


def hold_reasons(*, realized_pnl_today: Any = 0.0, regime_override: bool = False, vix_mult: float = 1.0, calendar_mult: float = 1.0, opening_mult: float = 1.0) -> List[str]:
    """Why an UPSIZE must stay at 1.0 right now (empty list = no reason). Pure; never raises."""
    out: List[str] = []
    try:
        pnl = cs._num(realized_pnl_today)
        if getattr(config, "CONVICTION_NO_UPSIZE_ON_LOSS_DAY", True) and pnl is not None and pnl < 0:
            out.append("losing day")
        if regime_override:
            out.append("regime override")
        for label, v in (("VIX size-down", vix_mult), ("calendar size-down", calendar_mult), ("opening-window size-down", opening_mult)):
            f = cs._num(v)
            if f is not None and f < 1.0:
                out.append(label)
    except Exception:  # noqa: BLE001
        pass
    return out


def long_decision(cand: Any, *, rr: Any, rr_floor: Any = 2.0, market_score: Any = None, threshold: Any = None, regime_override: bool = False,
                  drift_pct: Any = None, max_drift_pct: Any = None, board_row: Optional[dict] = None,
                  hold: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """{"score", "mult", "raw_mult", "held", "detail"} or None (feature off / too little known). `mult` is the multiple to use (already held at 1.0 when `hold` has a reason
    and the raw multiple is an upsize). Never raises."""
    try:
        if not enabled():
            return None
        parts = [
            ("conviction", cs.scale(getattr(cand, "conviction_score", None), 55.0, 95.0), WEIGHTS["conviction"]),
            ("board", _board_part(board_row), WEIGHTS["board"]),
            ("rr", _rr_part(rr, rr_floor), WEIGHTS["rr"]),
            ("market", _market_part(market_score, threshold, bool(regime_override)), WEIGHTS["market"]),
            ("fresh", _fresh_part(drift_pct, max_drift_pct), WEIGHTS["fresh"]),
        ]
        b = cs.blend(parts, min_known=2)
        if b is None:
            return None
        score, detail = b
        raw = cs.multiplier(score, low=config.CONVICTION_SCORE_LOW, mid=config.CONVICTION_SCORE_MID, high=config.CONVICTION_SCORE_HIGH,
                            min_mult=config.CONVICTION_MIN_MULT, max_mult=config.CONVICTION_MAX_MULT)
        held = list(hold or [])
        mult = cs.hold_back(raw, no_upsize=bool(held))
        return {"score": score, "mult": round(mult, 3), "raw_mult": round(raw, 3), "held": held if (raw > 1.0 and held) else [], "detail": detail}
    except Exception as e:  # noqa: BLE001
        logger.debug("conviction (real) failed for %s: %s", _sym(cand), e)
        return None


def ceiling_pct() -> float:
    """Most of this service's equity one upsized position may take, in %. 0 / unreadable = no upsize (the old caps stand)."""
    try:
        v = float(getattr(config, "CONVICTION_MAX_POSITION_PCT", 0.0))
        return v if v == v and v > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def note(decision: Optional[Dict[str, Any]]) -> str:
    """One short line for the log / Telegram / order event: 'conviction 72 -> x2.00 size'. Empty when there is no decision."""
    if not decision:
        return ""
    s = f"conviction {decision['score']:.0f} -> x{decision['mult']:.2f} size"
    if decision.get("held"):
        s += f" (upsize x{decision['raw_mult']:.2f} held: {', '.join(decision['held'])})"
    return s
