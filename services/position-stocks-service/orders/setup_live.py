"""orders/setup_live.py - feed setup_engine from the live entry path (E273 round 2, 31 Oct 2026). Position Stocks, LONG side, S1_CATALYST_UP only.

`facts_long` turns what attempt_entry already holds (the typed event on the quality signal, the ORB record in flow_json["orb"], the tape in flow_json["tape"],
the cached /quote row, the pre-open gap) into the `facts` dict of setup_engine.classify. A fact that is not there is simply missing = not true. S2, S3 and S4
need facts that do not exist yet (relative-strength rank, z-scores, short path) and arrive in later rounds; so does the NO_SETUP refusal: until every setup
can fire, an entry that matches no setup keeps the legacy shortlist stamp and full size. A matched S1 is stamped as S1 and sized by the engine
(0.25 in the 09:20 slot, 0.5 later; never below SETUP_SIZE_FLOOR). Pure apart from the optional quote argument; never raises.
"""
from __future__ import annotations

import logging

import json
from datetime import datetime
from typing import Any, Dict, Optional

from orders import setup_engine

logger = logging.getLogger("position-stocks-setup-live")


def _num(v) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _load(flow_json) -> Dict[str, Any]:
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (flow_json if isinstance(flow_json, dict) else {})
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def tape_sign(tape: Any) -> int:
    """+1 buying / -1 selling / 0 unknown from flow_json["tape"]. Only a RELIABLE tape speaks."""
    try:
        if not isinstance(tape, dict) or tape.get("reliable") is not True:
            return 0
        buy = _num(tape.get("buy_pct_10m"))
        if buy is not None and buy >= 55.0 and tape.get("delta_agrees") is not False:
            return 1
        if buy is not None and buy <= 45.0:
            return -1
        return 0
    except Exception:  # noqa: BLE001
        return 0


INPUT_KEYS = frozenset({"sector_third", "event_priced_in", "rs_pct", "gap_pct", "structure_ok", "or_break", "pullback_hold", "vol_vs_norm", "candle", "z_move", "kind",
                        "liquid", "failure_candle", "tape_exhaustion", "vwap_stretch", "climax_volume", "wall_close", "promoted", "gap_pctile"})


def allowed_set(*, s1: bool = True, s2: bool = False, s3: bool = False) -> set:
    """The setup ids this entry may match (the Calibration switches SETUP_S1/S2/S3_ENABLED)."""
    return ({setup_engine.S1} if s1 else set()) | ({setup_engine.S2} if s2 else set()) | ({setup_engine.S3} if s3 else set())


def no_setup_refusal(res: Optional[Dict[str, Any]], *, mode: str, exempt: bool, all_enabled: bool) -> Optional[str]:
    """The "no setup, no trade" refusal text, or None. Only in mode "live", only when S1, S2 and S3 can all fire (a switched-off setup would otherwise
    block entries it was meant to take), never for a pullback re-arm or reclaim entry (they carry their own trigger), and only for a real NO_SETUP: an engine
    error is not a verdict, so it lets the entry go on."""
    try:
        if str(mode or "off").strip().lower() != "live" or exempt or not all_enabled:
            return None
        if isinstance(res, dict) and res.get("id"):
            return None
        if not isinstance(res, dict) or res.get("why") == "error":
            return None
        return "NO_SETUP: no S1 / S2 / S3 trigger with two confirmations"
    except Exception:  # noqa: BLE001
        return None


def facts_long(*, symbol: str, price: Optional[float], now: Optional[datetime], event_polarity: Optional[int], flow_json: Any, quote: Optional[dict],
               gap_pct: Optional[float], tape: Any = None, penalties: Optional[Dict[str, float]] = None,
               allowed: Optional[set] = None, inputs: Optional[Dict[str, Any]] = None, penalty_skip: Optional[float] = None) -> Dict[str, Any]:
    f: Dict[str, Any] = {"side": "LONG", "service": "PS", "now": now, "allowed": set(allowed) if allowed else {setup_engine.S1}, "penalties": dict(penalties or {})}
    if penalty_skip is not None:
        f["penalty_skip"] = penalty_skip                       # E285 round 129: the skip total (soft_gates.skip_limit()); absent = the engine's own 4.0
    try:
        f["price"] = _num(price)
        f["event_polarity"] = int(event_polarity) if event_polarity in (1, -1) else 0
        fj = _load(flow_json)
        orb = fj.get("orb") if isinstance(fj.get("orb"), dict) else {}
        f["or_high"] = _num(orb.get("range_high"))
        if orb.get("close_trigger") is True:
            f["bar_close"] = _num(orb.get("trigger_close"))
            f["bar_closed_after_range"] = True               # bar_fields only counts bars that start after the range end
        q = quote if isinstance(quote, dict) else {}
        pc, g = _num(q.get("previous_close") if q.get("previous_close") is not None else q.get("prev_close")), _num(gap_pct)
        if pc and pc > 0 and g is not None:
            f["preopen_price"] = pc * (1.0 + g / 100.0)       # the final pre-open read as a price
        vw = _num(q.get("avg_price"))
        if vw and vw > 0 and f["price"]:
            f["above_vwap"] = f["price"] >= vw
        f["tape"] = tape_sign(tape if tape is not None else fj.get("tape"))   # the entry passes the live (cached) verdict: flow_json["tape"] is stamped later
        if isinstance(inputs, dict):                                          # E273 round 5: the S2 / S3 facts and S1's sector third / priced_in (orders/setup_inputs.py)
            f.update({k: v for k, v in inputs.items() if k in INPUT_KEYS})
    except Exception:  # noqa: BLE001
        logger.debug("setup_live: ignored error (fail-open)", exc_info=True)
    return f


def classify_long(**kw) -> Dict[str, Any]:
    """setup_engine.classify over facts_long; {"id": None, ...} when nothing fires. Never raises."""
    try:
        return setup_engine.classify(facts_long(**kw))
    except Exception:  # noqa: BLE001
        return {"id": None, "skip": "NO_SETUP", "why": "error"}


def extra_factor(setup_mult: Optional[float], already_applied: float) -> float:
    """Further shrink needed on top of a multiplier already applied (the opening-window size): the LOWER of the two wins, they do not stack. 1.0 = nothing more."""
    try:
        sm, ap = float(setup_mult), float(already_applied)
        if sm <= 0 or ap <= 0:
            return 1.0
        return min(1.0, sm / ap)
    except (TypeError, ValueError):
        return 1.0


def penalty_size(penalties: Optional[Dict[str, float]]) -> Optional[float]:
    """Size multiplier for an entry that matched no setup but carries soft penalties: 1 / (1 + total), never below the engine's floor. None = no penalty.
    (A matched setup already has the penalties inside its own size_mult.)"""
    try:
        tot = sum(float(v) for v in (penalties or {}).values())
        return None if tot <= 0 else round(max(setup_engine.SIZE_FLOOR, 1.0 / (1.0 + tot)), 4)
    except (TypeError, ValueError):
        return None
