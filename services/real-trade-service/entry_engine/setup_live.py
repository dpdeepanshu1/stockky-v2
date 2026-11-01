"""entry_engine/setup_live.py - feed setup_engine from the live Real Trade entry path (E273 round 19, 31 Oct 2026). LONG side: S1 always, S2 and S3 from E276 round 68.

E276 round 68: `inputs` (entry_engine/setup_inputs.gather_long: relative-strength rank, structure, z-score spike, candle, volume, liquidity, sector third, priced-in)
is forwarded to the engine, and `allowed` may be widened to S2 / S3 (the SETUP_S2_ENABLED / SETUP_S3_ENABLED switches); with no `inputs` only S1 can fire, as before.
The S1 description below is unchanged: a fresh typed POSITIVE event (watchlist_engine.intraday_news.positive_event), the
price holding at or above the pre-open price (previous close x (1 + the pre-open gap)), and a closed 1-minute bar above the opening-range high (orb_bars.range_trigger),
plus two confirmations from what Real Trade has: `above_vwap` (price over the bars' VWAP) and `tape_buying`, which here is the 5-level book imbalance above +0.15
(a resting-order snapshot, not a trade tape: the name is the engine's, the evidence is weaker). A fact that is missing is simply not true. No match = the legacy
shortlist stamp and full size. Pure; never raises.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from entry_engine import setup_engine

BOOK_BUY_IMBALANCE = 0.15

# the facts entry_engine/setup_inputs.gather_long may add; anything else in `inputs` is ignored
INPUT_KEYS = frozenset({"sector_third", "event_priced_in", "rs_pct", "gap_pct", "structure_ok", "or_break", "pullback_hold", "vol_vs_norm", "candle", "z_move", "kind",
                        "liquid", "failure_candle", "tape_exhaustion", "vwap_stretch", "climax_volume", "wall_close", "promoted", "gap_pctile"})


def allowed_set(*, s1: bool = True, s2: bool = False, s3: bool = False) -> set:
    """The setup ids this entry may match (the SETUP_S1 / S2 / S3_ENABLED switches)."""
    return ({setup_engine.S1} if s1 else set()) | ({setup_engine.S2} if s2 else set()) | ({setup_engine.S3} if s3 else set())


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def facts_long(*, price: Any, prev_close: Any, gap_pct: Any, event: Optional[Dict[str, Any]], bars_facts: Optional[Dict[str, Any]], imbalance_5: Any,
               now: Optional[datetime], penalties: Optional[Dict[str, float]] = None, allowed: Optional[set] = None,
               inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The `facts` dict of setup_engine.classify for one Real Trade long. Never raises."""
    f: Dict[str, Any] = {"side": "LONG", "service": "RT", "now": now, "allowed": set(allowed) if allowed else {setup_engine.S1}, "penalties": dict(penalties or {})}
    try:
        p, pc, g = _num(price), _num(prev_close), _num(gap_pct)
        f["price"] = p
        f["event_polarity"] = 1 if isinstance(event, dict) and event.get("polarity") == 1 else 0
        if pc and g is not None:
            f["preopen_price"] = pc * (1.0 + g / 100.0)
        bf = bars_facts if isinstance(bars_facts, dict) else {}
        f["or_high"], f["bar_close"], f["bar_closed_after_range"] = bf.get("or_high"), bf.get("bar_close"), bool(bf.get("bar_closed_after_range"))
        vw = _num(bf.get("vwap"))
        f["above_vwap"] = (p > vw) if (p is not None and vw is not None) else None
        imb = _num(imbalance_5)
        f["tape"] = 1 if (imb is not None and imb > BOOK_BUY_IMBALANCE) else 0
        if isinstance(inputs, dict):
            f.update({k: v for k, v in inputs.items() if k in INPUT_KEYS})
    except Exception:  # noqa: BLE001
        pass
    return f


def no_setup_refusal(res: Optional[Dict[str, Any]], *, mode: str, exempt: bool, all_ran: bool) -> Optional[str]:
    """E276 round 70: the "no setup, no trade" refusal text, or None. Only in mode "live"; only when the walk really ran with S1, S2 and S3 all able to fire (`all_ran`:
    all three switches on AND the clock at or past the S3 start, so a 09:30 candidate is never refused for a setup that cannot fire yet); never for a pullback re-arm
    entry (`exempt`, it carries its own trigger); and only for a real NO_SETUP verdict: no result (the walk did not run) or an engine error is not a verdict."""
    try:
        if str(mode or "off").strip().lower() != "live" or exempt or not all_ran:
            return None
        if not isinstance(res, dict) or res.get("id") or res.get("why") == "error":
            return None
        if res.get("skip") != "NO_SETUP":
            return None                                       # PENALTY_TOO_HIGH has its own meaning; it is not this refusal
        return "NO_SETUP: no S1 / S2 / S3 trigger with two confirmations"
    except Exception:  # noqa: BLE001
        return None


def classify_long(**kw) -> Dict[str, Any]:
    """setup_engine.classify over facts_long(**kw). {"id": None, "skip": "NO_SETUP", "why": "error"} on any problem."""
    try:
        return setup_engine.classify(facts_long(**kw))
    except Exception:  # noqa: BLE001
        return {"id": None, "skip": "NO_SETUP", "why": "error"}
