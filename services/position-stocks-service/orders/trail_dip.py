"""orders/trail_dip.py - E285 round 107: the trail distance learned from what the setup's own winners dipped through (plan: "trail uses the setup winners' giveback").

A trail that sits closer to the peak than a winner of the same setup normally dips before it makes its next high shakes that winner out. excursion.py records, for every open trade, the
deepest pullback from an in-profit peak that price then RECOVERED from (flow_json["peak"]["dip_pct"]); setup_stats.summarize turns the WINNERS' values into `dip_p75_pct` per setup cell.
(Only recovered dips are used on purpose: the pullback a trail exits on is never recovered, and counting it would only echo the trail's own distance.)

    distance = plan distance + w x max(0, learned - plan distance),   learned = dip_p75 held to +-25 % a week (learned_limit),  w = n / (n + 30)

Guard rails: the distance only ever gets WIDER by this (a tighter learned value changes nothing); a cell with fewer than 30 closed trades or fewer than 10 winners with a recorded dip gives the
plan distance; only an explicit setup learns (never the legacy shortlist cell); exit_adaptive keeps the result no wider than the stop. Switch TRAIL_LEARNED_DIP_ENABLED. Never raises.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import config

logger = logging.getLogger("position-stocks-trail-dip")


def enabled() -> bool:
    return bool(getattr(config, "TRAIL_LEARNED_DIP_ENABLED", True))


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def cell_dip(pos: Any, db: Any) -> Optional[Dict[str, Any]]:
    """{"dip_pct", "n", "cell"} for the setup the position was taken on (its slot's cell, else ALL), or None. Never raises."""
    try:
        if not enabled() or db is None:
            return None
        from orders import learned_stop, setup_stamp, setup_stats, slot_ladder
        st = setup_stamp.read(getattr(pos, "flow_json", None))
        if not st:
            return None
        side = "SHORT" if str(st.get("side") or "LONG").upper() == "SHORT" else "LONG"
        sid = st.get("id")
        if sid not in (learned_stop.SHORT_SETUPS if side == "SHORT" else learned_stop.LONG_SETUPS):
            return None
        cells = slot_ladder.cells(db) or {}
        cid = setup_stats.cell_id(sid, side)
        for key in (f"{cid}|{st.get('slot') or 'later'}", f"{cid}|ALL"):
            c = cells.get(key)
            if not isinstance(c, dict) or int(c.get("n") or 0) < setup_stats.MIN_TRADES:
                continue
            n, v = int(c.get("dip_n") or 0), _num(c.get("dip_p75_pct"))
            if v is not None and v > 0 and n >= setup_stats.MIN_EXIT_SAMPLES:
                return {"dip_pct": v, "n": n, "cell": key}
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("trail dip: cell read failed: %s", e)
        return None


def widen(dist_pct: float, dip: Optional[Dict[str, Any]], now_ts: Optional[float] = None) -> float:
    """`dist_pct` widened toward the setup's learned dip; unchanged without a usable dip or when the learned value is not wider. Never raises."""
    try:
        if not enabled() or not isinstance(dip, dict):
            return dist_pct
        base, v, n = _num(dist_pct), _num(dip.get("dip_pct")), int(dip.get("n") or 0)
        if base is None or base <= 0 or v is None or v <= 0 or n <= 0:
            return dist_pct
        learned = v
        try:
            from orders import learned_limit
            lim, _hit = learned_limit.limit(f"{dip.get('cell', '?')}|trail|dip_pct", learned, None, now_ts)
            learned = float(lim) if lim is not None else learned
        except Exception:  # noqa: BLE001
            logger.debug("trail_dip: weekly limit skipped", exc_info=True)
        from orders.adaptive_params import shrink_weight
        w = shrink_weight(n)
        return base + w * max(0.0, learned - base)
    except Exception:  # noqa: BLE001
        return dist_pct
