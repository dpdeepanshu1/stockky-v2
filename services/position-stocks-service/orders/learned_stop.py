"""orders/learned_stop.py - E282 round 76: a stop learned from what the setup's own winners endured (plan E282 item 2, the stop half of ADAPTIVE_ENTRY_PARAMS).

The stop used to be a function of the stock's own noise only (ATR / 1-minute range), the same for every setup. A setup's closed winners record how far they went against the
entry before they paid (`min_price_seen` for a long, `max_price_seen` for a short; setup_stats.summarize -> `adverse_p90_pct`, the 90th percentile over WINNERS). A stop just beyond
that distance still lets 9 winners in 10 live; any wider only adds loss to the trades that were going to fail anyway. So at entry:

    learned stop % = adverse_p90_pct x BUFFER (1.10), held to +-25 % a week (learned_limit), then shrunk toward the plan's own stop with w = n / (n + 30)

Guard rails (plan, 'Guard rails for every learned value'):
  * a cell with fewer than 30 closed trades, or fewer than 10 winners with a readable excursion, gives the plan's stop unchanged (setup_stats.MIN_TRADES / MIN_EXIT_SAMPLES);
  * the stop only ever gets TIGHTER, never wider than the plan's stop; a long's stop is rounded down to the tick and a short's up, so the rounded stop is never tighter than computed;
  * never below the stock's own noise: the larger of LEARNED_STOP_MIN_PCT, OPENING_GATE_MIN_STOP_ATR_FRAC x daily ATR % and the E251 volatility floor (adaptive.stop_floor_pct).
    Without a daily ATR the noise floor is unknown and the stop is left alone;
  * only an explicit setup (S1 / S2 / S3 long, S4 / S2 / S3 short) learns: the legacy shortlist cell mixes long and short entries and is never used;
  * the target, the reward:risk floor and the SIZE are untouched. Size is worked out from the plan's stop (callers keep `budget_stop_pct`), so a tighter stop lowers the risk, it never grows the
    position. The reward:risk floor only gets easier with a nearer stop; the cost gate and the Time Machine judge the levels after this.

Switch LEARNED_STOP_ENABLED, floor LEARNED_STOP_MIN_PCT. Never raises; any problem leaves the levels as they were.

  cell_adverse(setup, now, db, side)       {"adverse_pct", "n", "cell"} for the setup's cell, else None
  learned_stop_pct(plan_pct, adverse, ...) the shrunk, weekly-limited value (pure apart from the learned_limit anchor)
  adjust(levels, live, ...)                edits `levels` (stop_price / stop_pct) in place and returns a note dict ({} when nothing moved)
  stamp(flow_json, note)                   the note under flow_json["learned_stop"] (record only)
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, Optional

import config

logger = logging.getLogger("position-stocks-learned-stop")

BUFFER = 1.10
LONG_SETUPS = ("S1_CATALYST_UP", "S2_RS_BREAKOUT", "S3_EXHAUSTION")
SHORT_SETUPS = ("S4_CATALYST_DOWN", "S2_RS_BREAKOUT", "S3_EXHAUSTION")
MIN_STEP_PCT = 0.02                    # a tightening smaller than this (percentage points) is not worth a note


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def enabled() -> bool:
    return bool(getattr(config, "LEARNED_STOP_ENABLED", True)) and bool(getattr(config, "ADAPTIVE_ENTRY_PARAMS", True))     # E285 round 110: ADAPTIVE_ENTRY_PARAMS is the master switch


def _min_pct() -> float:
    v = _num(getattr(config, "LEARNED_STOP_MIN_PCT", 0.5))
    return v if v is not None and v > 0 else 0.5


def cell_adverse(setup: Any, now: Any, db: Any, side: str = "LONG") -> Optional[Dict[str, Any]]:
    """{"adverse_pct", "n", "cell"} when the setup's cell (its slot, else ALL) has 30+ closed trades and 10+ winners with a readable excursion; else None. Never raises."""
    try:
        side = "SHORT" if str(side or "LONG").upper() == "SHORT" else "LONG"
        if not isinstance(setup, dict) or db is None:
            return None
        sid = setup.get("id")
        if sid not in (SHORT_SETUPS if side == "SHORT" else LONG_SETUPS):
            return None
        from orders import setup_engine, setup_stats, slot_ladder
        cells = slot_ladder.cells(db) or {}
        slot = setup.get("slot") or setup_engine.slot(now)
        cid = setup_stats.cell_id(sid, side)
        for key in (f"{cid}|{slot}", f"{cid}|ALL"):
            c = cells.get(key)
            if not isinstance(c, dict) or int(c.get("n") or 0) < setup_stats.MIN_TRADES:
                continue
            mae_n, p90 = int(c.get("mae_n") or 0), _num(c.get("adverse_p90_pct"))
            if p90 is not None and p90 > 0 and mae_n >= setup_stats.MIN_EXIT_SAMPLES:
                return {"adverse_pct": p90, "n": mae_n, "cell": key}
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("learned stop: cell read failed: %s", e)
        return None


def learned_stop_pct(plan_pct: float, adverse: Dict[str, Any], now_ts: Optional[float] = None) -> Optional[float]:
    """The stop % the setup's winners support: adverse_p90 x BUFFER, held to the weekly limit, then n / (n + 30) of it plus the rest of the plan's own stop. None on bad input."""
    try:
        plan, p90, n = _num(plan_pct), _num((adverse or {}).get("adverse_pct")), int((adverse or {}).get("n") or 0)
        if plan is None or plan <= 0 or p90 is None or p90 <= 0 or n <= 0:
            return None
        learned = p90 * BUFFER
        try:
            from orders import learned_limit
            lim, _hit = learned_limit.limit(f"{(adverse or {}).get('cell', '?')}|stop|adverse_pct", learned, None, now_ts)    # at most 25 % a week
            learned = float(lim) if lim is not None else learned
        except Exception:  # noqa: BLE001
            logger.debug("learned_stop: ignored error (fail-open)", exc_info=True)
        from orders.adaptive_params import shrink_weight
        w = shrink_weight(n)
        return w * learned + (1.0 - w) * plan
    except Exception:  # noqa: BLE001
        return None


def floor_pct(symbol: str, atr_pct: Optional[float] = None, m1_pct: Optional[float] = None) -> Optional[float]:
    """The smallest stop this stock's own noise allows, as % (None when its daily ATR is unknown). Arguments override the live reads (tests)."""
    try:
        if atr_pct is None:
            from screening import prev_day
            pdv = prev_day.peek(symbol)
            atr_pct = getattr(pdv, "atr_pct", None) if pdv else None
        atr = _num(atr_pct)
        if atr is None or atr <= 0:
            return None
        if m1_pct is None:
            try:
                from orders import entry_timing
                m1_pct = entry_timing.live_avg_1m_range_pct(symbol)
            except Exception:  # noqa: BLE001
                m1_pct = None
        parts = [_min_pct()]
        frac = _num(getattr(config, "OPENING_GATE_MIN_STOP_ATR_FRAC", 0.3)) or 0.0
        if frac > 0:
            parts.append(frac * atr)
        if bool(getattr(config, "ADAPTIVE_STOP_FLOOR_ENABLED", True)):         # the E251 noise floor: if it is on and cannot be read, the stop is left alone (outer except -> None)
            from orders import adaptive
            vol_floor, _why = adaptive.stop_floor_pct(atr, _num(m1_pct))
            if vol_floor and vol_floor > 0:
                parts.append(float(vol_floor))
        return max(parts)
    except Exception:  # noqa: BLE001
        return None


def _tick(live: float, tick: Optional[float]) -> float:
    if tick is not None and tick > 0:
        return float(tick)
    try:
        from execution import dhan_client
        t = float(dhan_client.tick_size_for_price(live))
        if t > 0:
            return t
    except Exception:  # noqa: BLE001
        logger.debug("learned_stop: ignored error (fail-open)", exc_info=True)
    return 0.05


def adjust(levels: Any, live: Any, *, symbol: str, setup: Any = None, now: Any = None, db: Any = None, side: str = "LONG",
           atr_pct: Optional[float] = None, m1_pct: Optional[float] = None, tick: Optional[float] = None, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Tighten `levels.stop_price` / `levels.stop_pct` in place to the learned stop; {} (levels untouched) when anything is missing or the result would not be tighter."""
    try:
        if not enabled():
            return {}
        short = str(side or "LONG").upper() == "SHORT"
        lv, cur_px, cur_pct = _num(live), _num(getattr(levels, "stop_price", None)), _num(getattr(levels, "stop_pct", None))
        if not lv or lv <= 0 or not cur_px or not cur_pct or cur_pct <= 0:
            return {}
        if (short and cur_px <= lv) or (not short and cur_px >= lv):
            return {}                                                   # a stop on the wrong side of price is not ours to fix
        adverse = cell_adverse(setup, now, db, "SHORT" if short else "LONG")
        if not adverse:
            return {}
        val = learned_stop_pct(cur_pct, adverse, now_ts)
        floor = floor_pct(symbol, atr_pct, m1_pct)
        if val is None or floor is None:
            return {}
        new_pct = max(val, floor)
        if new_pct >= cur_pct - MIN_STEP_PCT:
            return {}                                                   # only ever tighter
        t = _tick(lv, tick)
        raw = lv * (1.0 + new_pct / 100.0) if short else lv * (1.0 - new_pct / 100.0)
        steps = raw / t
        px = (math.ceil(round(steps, 6)) if short else math.floor(round(steps, 6))) * t         # never tighter than computed
        px = round(px, 4)
        if (short and not (lv < px < cur_px)) or (not short and not (cur_px < px < lv)):
            return {}
        pct = abs(px - lv) / lv * 100.0
        if pct < floor - 1e-9:
            return {}
        levels.stop_price = px
        levels.stop_pct = round(pct, 4)
        return {"was_stop_pct": round(cur_pct, 4), "stop_pct": round(pct, 4), "adverse_p90_pct": round(float(adverse["adverse_pct"]), 3), "n": int(adverse["n"]),
                "cell": adverse["cell"], "floor_pct": round(floor, 4), "side": "SHORT" if short else "LONG"}
    except Exception as e:  # noqa: BLE001
        logger.debug("learned stop adjust failed for %s: %s", symbol, e)
        return {}


def stamp(flow_json: Any, note: Any) -> Any:
    """flow_json text with the note under "learned_stop"; unchanged when there is no note or on any problem."""
    try:
        if not isinstance(note, dict) or not note:
            return flow_json
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else {})
        if not isinstance(d, dict):
            return flow_json
        d["learned_stop"] = note
        return json.dumps(d, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json
