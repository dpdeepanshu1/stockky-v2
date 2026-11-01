"""orders/long_target.py - E283: a target learned from the setup's own winners, for a LONG (plan E282 item 2, the target half of ADAPTIVE_ENTRY_PARAMS).

The long target used to come from the stock's own volatility only. A setup cell's closed WINNERS record how far they went in favour (`max_price_seen`;
setup_stats.summarize -> `typical_gain_pct`, the median best gain of the winners, n >= 10). When the cell has 30+ closed trades, the same rules as the short side
(orders/short_target.py) and the learned stop (orders/learned_stop.py) apply:

  * only an explicit long setup learns (S1 / S2 / S3); the legacy shortlist cell mixes long and short entries and is never used;
  * value = n / (n + 30) of the cell's typical gain + the rest of the plan's own target (setup_stats.exit_numbers), held to +-25 % a week (learned_limit);
  * the target only ever gets NEARER (never farther than the plan's), rounded UP to the tick so the rounded target is never nearer than computed, and never below
    LONG_TARGET_MIN_PCT of the price;
  * the reward:risk floor stays: when the nearer target would fall under ENTRY_MIN_REWARD_RISK x the stop, the plan's target is KEPT (the entry is not turned into a
    refusal by a learned value; the floor, the Time Machine and the cost gate then judge the plan's target as before);
  * stop, size and budget are untouched.

Switch LONG_TARGET_LEARNED_ENABLED (ships on: it does nothing until a cell has 30 trades), floor LONG_TARGET_MIN_PCT. Never raises; any problem leaves the levels as they were.

  learned_gain(setup, now, db, plan_target_pct)   {"gain_pct", "source", "cell"} or None
  adjust(levels, live, ...)                       edits `levels` (target_price / target_pct) in place; returns a note dict ({} when nothing moved)
  stamp(flow_json, note)                          the note under flow_json["long_target"] (record only)
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, Optional

import config

logger = logging.getLogger("position-stocks-long-target")

LONG_SETUPS = ("S1_CATALYST_UP", "S2_RS_BREAKOUT", "S3_EXHAUSTION")
MIN_STEP_PCT = 0.02                    # a pull-in smaller than this (percentage points) is not worth a note


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def enabled() -> bool:
    return bool(getattr(config, "LONG_TARGET_LEARNED_ENABLED", True))


def _min_pct() -> float:
    v = _num(getattr(config, "LONG_TARGET_MIN_PCT", 0.8))
    return v if v is not None and v > 0 else 0.8


def _min_rr() -> float:
    v = _num(getattr(config, "ENTRY_MIN_REWARD_RISK", 2.0))
    return v if v is not None and v > 0 else 0.0


def learned_gain(setup: Any, now: Any, db: Any, plan_target_pct: float) -> Optional[Dict[str, Any]]:
    """The setup cell's learned typical gain (shrunk toward the plan's target, weekly-limited), or None. Reads the slot-ladder cells (cached); never raises."""
    try:
        if not isinstance(setup, dict) or setup.get("id") not in LONG_SETUPS or db is None:
            return None
        from orders import learned_limit, setup_engine, setup_stats, slot_ladder
        cells = slot_ladder.cells(db) or {}
        slot = setup.get("slot") or setup_engine.slot(now)
        cid = setup_stats.cell_id(setup["id"], "LONG")
        for key in (f"{cid}|{slot}", f"{cid}|ALL"):
            num = setup_stats.exit_numbers(cells.get(key), ttp_fallback_min=0.0, target_pct=plan_target_pct)
            g = _num(num.get("typical_gain_pct"))
            if str(num.get("gain_source", "")).startswith("learned") and g and g > 0:
                try:
                    lim, _hit = learned_limit.limit(f"{key}|long_target|gain_pct", g, None, None)    # at most 25 % a week
                    g = float(lim) if lim is not None else g
                except Exception:  # noqa: BLE001
                    pass
                return {"gain_pct": g, "source": num["gain_source"], "cell": key}
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("long target: learned gain failed: %s", e)
        return None


def adjust(levels: Any, live: Any, *, setup: Any = None, now: Any = None, db: Any = None, tick: Optional[float] = None) -> Dict[str, Any]:
    try:
        if not enabled():
            return {}
        lv, tp, tpct = _num(live), _num(getattr(levels, "target_price", None)), _num(getattr(levels, "target_pct", None))
        sp, spct = _num(getattr(levels, "stop_price", None)), _num(getattr(levels, "stop_pct", None))
        if not lv or lv <= 0 or not tp or tp <= lv or not tpct or tpct <= 0:
            return {}
        lg = learned_gain(setup, now, db, tpct)
        if not lg or lg["gain_pct"] < _min_pct():
            return {}
        if tick is None:
            try:
                from execution import dhan_client
                tick = dhan_client.tick_size_for_price(lv)
            except Exception:  # noqa: BLE001
                tick = 0.05
        t = float(tick) if tick and float(tick) > 0 else 0.05
        raw = lv * (1.0 + lg["gain_pct"] / 100.0)
        px = round(math.ceil(round(raw / t, 6)) * t, 4)               # never nearer than computed
        if not (lv < px < tp):                                          # only ever NEARER than the plan's target, still above the price
            return {}
        new_pct = (px - lv) / lv * 100.0
        if new_pct < _min_pct() or tpct - new_pct < MIN_STEP_PCT:
            return {}
        rr = _min_rr()
        if rr > 0 and sp and spct and spct > 0 and sp < lv and new_pct < rr * spct - 1e-9:
            return {"kept_plan": "RR_FLOOR", "would_be_pct": round(new_pct, 4), "stop_pct": round(spct, 4), "min_rr": rr, "cell": lg["cell"]}
        levels.target_price = px
        levels.target_pct = round(new_pct, 4)
        return {"was_target_pct": round(tpct, 4), "target_pct": round(new_pct, 4), "gain_pct": round(lg["gain_pct"], 3), "source": lg["source"], "cell": lg["cell"]}
    except Exception as e:  # noqa: BLE001
        logger.debug("long target adjust failed: %s", e)
        return {}


def stamp(flow_json: Any, note: Any) -> Any:
    """flow_json text with the note under "long_target"; unchanged when there is no note or on any problem."""
    try:
        if not isinstance(note, dict) or not note:
            return flow_json
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else {})
        if not isinstance(d, dict):
            return flow_json
        d["long_target"] = note
        return json.dumps(d, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json
