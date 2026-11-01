"""orders/short_target.py - E274 round 43: a reachable target for a SHORT (plan: "Target from the long-style adaptive levels").

The long-style adaptive target was reached by 3.3 % of replayed shorts (33 % needed to break even): down-moves are faster and shorter. At entry this module makes a short's
target NEARER in two ways (never farther, never touching the stop, the size or the stop-based budget):

  1. learned   an S4_CATALYST_DOWN short (a short-only setup; E274 round 44: also a short S2 / S3, whose cell is its own "..._SHORT" one) whose setup cell has 30+ closed trades and 10+ winners with a recorded best price (setup_stats.exit_numbers) takes
               the cell's typical favourable move (median best gain of its winners, shrunk n / (n + 30) toward the plan's own target) when that is nearer than the plan's target.
               A LEGACY_SHORTLIST short is never retargeted from its cell: that cell also holds the long entries.
  2. wall      one tick ABOVE the next high-volume node below the price (`hvn_below` of the RELIABLE order-flow tape), when that is nearer, still below the price and still pays
               SHORT_TARGET_MIN_PCT. The same rule orders/tape_exit.py applies to an open trade.

The reward:risk floor, the time-machine gate and the cost gate run AFTER this on the new target: a target that is reachable but does not pay is a refusal, not a loosened gate.
The time stop already comes from the setup's own time-to-peak (exit_adaptive.learned_time_stop, E275 round 38). Switches: SHORT_TARGET_LEARNED_ENABLED, SHORT_TARGET_HVN_ENABLED.

  adjust(levels, live, ...)  edits `levels` (target_price / target_pct) in place and returns a note dict ({} when nothing moved). Never raises.
  stamp(flow_json, note)     the note under flow_json["short_target"] (record only).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import config

logger = logging.getLogger("position-stocks-short-target")

S4 = "S4_CATALYST_DOWN"


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _min_pct() -> float:
    v = _num(getattr(config, "SHORT_TARGET_MIN_PCT", 0.5))
    return v if v is not None and v > 0 else 0.5


def _set_target(levels: Any, live: float, price: float) -> None:
    levels.target_price = price
    levels.target_pct = round((live - price) / live * 100.0, 4)


def learned_gain(setup: Any, now: Any, db: Any, plan_target_pct: float) -> Optional[Dict[str, Any]]:
    """{"gain_pct", "source", "cell"} when the S4 cell has enough winners, else None. Reads the slot-ladder cells (cached); never raises."""
    try:
        if not isinstance(setup, dict) or setup.get("id") not in (S4, "S2_RS_BREAKOUT", "S3_EXHAUSTION") or db is None:
            return None
        from orders import setup_engine, setup_stats, slot_ladder
        cells = slot_ladder.cells(db) or {}
        slot = setup.get("slot") or setup_engine.slot(now)
        cid = setup_stats.cell_id(setup["id"], "SHORT")             # E274 round 44: a short S2 / S3 has its own cell, so it may learn too
        for key in (f"{cid}|{slot}", f"{cid}|ALL"):
            num = setup_stats.exit_numbers(cells.get(key), ttp_fallback_min=0.0, target_pct=plan_target_pct)
            if str(num.get("gain_source", "")).startswith("learned") and _num(num.get("typical_gain_pct")):
                return {"gain_pct": float(num["typical_gain_pct"]), "source": num["gain_source"], "cell": key}
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("short target: learned gain failed: %s", e)
        return None


def wall_price(symbol: str, verdict: Any = None) -> Optional[float]:
    """The next HVN price below, from the RELIABLE order-flow tape. None when absent / unreliable. With no verdict handed in it reads flow_gate's cached SHORT verdict, and
    makes no new market-data call when the order-flow gate is off."""
    try:
        v = verdict
        if v is None:
            from orders import flow_gate
            if flow_gate.mode() == "off":
                return None
            v = flow_gate.fetch_verdict(symbol, "SHORT")
        if not isinstance(v, dict) or v.get("reliable") is not True:
            return None
        w = _num(v.get("hvn_below"))
        return w if w and w > 0 else None
    except Exception:  # noqa: BLE001
        return None


def adjust(levels: Any, live: Any, *, symbol: str, setup: Any = None, now: Any = None, db: Any = None, verdict: Any = None, tick: Optional[float] = None) -> Dict[str, Any]:
    note: Dict[str, Any] = {}
    try:
        lv = _num(live)
        tp = _num(getattr(levels, "target_price", None))
        tpct = _num(getattr(levels, "target_pct", None))
        if not lv or lv <= 0 or not tp or tp >= lv or tpct is None:
            return note
        if tick is None:
            try:
                from execution import dhan_client
                tick = dhan_client.tick_size_for_price(lv)
            except Exception:  # noqa: BLE001
                tick = 0.05
        from orders.adaptive import round_to_tick
        floor = _min_pct()
        if bool(getattr(config, "SHORT_TARGET_LEARNED_ENABLED", True)):
            lg = learned_gain(setup, now, db, tpct)
            if lg and lg["gain_pct"] >= floor:
                px = round_to_tick(lv * (1.0 - lg["gain_pct"] / 100.0))
                if tp < px < lv:                                     # nearer than the plan's target, still below the price
                    note["learned"] = {"gain_pct": round(lg["gain_pct"], 3), "source": lg["source"], "cell": lg["cell"], "was_target": round(tp, 2)}
                    _set_target(levels, lv, px)
                    tp = px
        if bool(getattr(config, "SHORT_TARGET_HVN_ENABLED", True)):
            w = wall_price(symbol, verdict)
            if w is not None and w < lv:
                cand = round(w + float(tick), 4)
                if tp < cand < lv and (lv - cand) / lv * 100.0 >= floor:
                    note["wall"] = {"hvn_below": round(w, 2), "target": round(cand, 2), "was_target": round(tp, 2)}
                    _set_target(levels, lv, cand)
        return note
    except Exception as e:  # noqa: BLE001
        logger.debug("short target adjust failed for %s: %s", symbol, e)
        return {}


def stamp(flow_json: Any, note: Any) -> Any:
    """flow_json text with the note under "short_target"; unchanged when there is no note or on any problem."""
    try:
        if not isinstance(note, dict) or not note:
            return flow_json
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else {})
        if not isinstance(d, dict):
            return flow_json
        d["short_target"] = note
        return json.dumps(d, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json
