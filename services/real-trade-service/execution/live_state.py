"""execution/live_state.py - E283: one read-only picture of Real Trade's live state for the Calibration tab (GET /live/state). Changes nothing, places nothing.

  pool         trade_accounts rows (mode, starting capital, equity, cash, the broker's raw cash): the rupee figure the heat numbers use (plan: "Real Trade's pool figure")
  heat         this service's open risk + gap allowance (REAL), the other service's, and the heat limit
  setup_stamp  how the newest ENTER decisions were stamped (flow_json["setup"]): count per setup id and time slot, and how many carry no stamp
Each part has its own try/except; a part that cannot be read is {"ok": false, "error": ...}.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

import config

logger = logging.getLogger("real-trade-live-state")


def _part(fn) -> Dict[str, Any]:
    try:
        out = fn()
        return {"ok": True, **out} if isinstance(out, dict) else {"ok": True, "value": out}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def pool_part(db: Any) -> Dict[str, Any]:
    import models
    rows = []
    for a in db.query(models.TradeAccount).all():
        rows.append({"mode": a.mode, "starting_capital": a.starting_capital, "current_equity": a.current_equity, "cash_available": a.cash_available,
                     "broker_cash_available": getattr(a, "broker_cash_available", None)})
    return {"accounts": rows}


def heat_part(db: Any) -> Dict[str, Any]:
    from execution import heat_state
    own_risk, own_gap = heat_state.own_heat(db, "REAL")
    peer_risk, peer_base = heat_state.peer_heat(db, "REAL")
    return {"enabled": bool(getattr(config, "PORTFOLIO_HEAT_SHARED_ENABLED", True)), "limit_pct": float(getattr(config, "HEAT_LIMIT_PCT", 5.0)),
            "own_open_risk": round(float(own_risk), 2), "own_gap_allowance": round(float(own_gap), 2),
            "peer_open_risk": round(float(peer_risk), 2), "peer_capital_base": round(float(peer_base), 2)}


def stamp_counts(flow_jsons) -> Dict[str, Any]:
    """Pure: {"n", "stamped", "unstamped", "by_setup": {id: n}, "by_slot": {slot: n}} over decision flow_json texts."""
    by_setup: Dict[str, int] = {}
    by_slot: Dict[str, int] = {}
    stamped = n = 0
    for fj in flow_jsons:
        n += 1
        try:
            d = json.loads(fj) if isinstance(fj, str) and fj else fj
            s = d.get("setup") if isinstance(d, dict) else None
        except Exception:  # noqa: BLE001
            s = None
        if isinstance(s, dict) and s.get("id"):
            stamped += 1
            by_setup[str(s["id"])] = by_setup.get(str(s["id"]), 0) + 1
            slot = str(s.get("slot") or "?")
            by_slot[slot] = by_slot.get(slot, 0) + 1
    return {"n": n, "stamped": stamped, "unstamped": n - stamped, "by_setup": by_setup, "by_slot": by_slot}


def stamp_part(db: Any, limit: int = 300) -> Dict[str, Any]:
    import models
    rows = (db.query(models.TradeDecision.flow_json).filter(models.TradeDecision.decision_type == "ENTRY", models.TradeDecision.action == "ENTER")
            .order_by(models.TradeDecision.id.desc()).limit(max(1, min(int(limit), 1000))).all())
    return stamp_counts([r[0] for r in rows])


def snapshot(db: Any) -> Dict[str, Any]:
    return {"pool": _part(lambda: pool_part(db)), "heat": _part(lambda: heat_part(db)), "setup_stamp": _part(lambda: stamp_part(db))}
