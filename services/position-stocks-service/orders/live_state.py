"""orders/live_state.py - E283: one read-only picture of the live safety state for the Calibration tab (GET /live/state).

Four parts, each in its own try/except (a part that cannot be read is {"ok": false, "error": ...}; the others still answer). It changes nothing and places nothing:
  order_rate   execution/order_rate.snapshot()   Dhan order calls counted this process, waits, the per-second budget (E282 round 77)
  momentum     orders/momentum_hook.stats()      momentum-exit mode and counters since the last restart
  shared_heat  capital/shared_heat.get_peer*     the OTHER service's open risk, capital base, age and sector risk, plus this service's heat limit
  ladder       orders/risk_stage.stage_for       the risk-ladder stage (0..) of every setup / side, and whether the ladder is on
"""
from __future__ import annotations

import logging
from typing import Any, Dict

import config

logger = logging.getLogger("position-stocks-live-state")

LONG_SETUPS = ("S1_CATALYST_UP", "S2_RS_BREAKOUT", "S3_EXHAUSTION")
SHORT_SETUPS = ("S2_RS_BREAKOUT", "S3_EXHAUSTION", "S4_CATALYST_DOWN")


def _part(fn) -> Dict[str, Any]:
    try:
        out = fn()
        return {"ok": True, **out} if isinstance(out, dict) else {"ok": True, "value": out}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def order_rate_part() -> Dict[str, Any]:
    from execution import order_rate
    out = order_rate.snapshot()
    if out.get("shared_enabled"):
        from execution import shared_rate_window
        out["shared"] = shared_rate_window.snapshot()          # E285 round 87: the account-wide window both services append to (calls in the last second per service)
    return out


def momentum_part() -> Dict[str, Any]:
    from orders import momentum_hook
    return momentum_hook.stats()


def heat_part(db: Any) -> Dict[str, Any]:
    from capital import shared_heat
    age_limit = float(getattr(config, "HEAT_PEER_MAX_AGE_S", 900.0))
    risk, base, age = shared_heat.get_peer(db, age_limit)
    return {"enabled": bool(getattr(config, "PORTFOLIO_HEAT_SHARED_ENABLED", True)), "limit_pct": float(getattr(config, "HEAT_LIMIT_PCT", 5.0)),
            "peer_open_risk": round(float(risk), 2), "peer_capital_base": round(float(base), 2), "peer_age_s": round(float(age), 1),
            "peer_max_age_s": age_limit, "peer_sector_risk": shared_heat.get_peer_sectors(db, age_limit) or {}}


def ladder_part(db: Any) -> Dict[str, Any]:
    from orders import risk_stage
    stages: Dict[str, int] = {}
    for sid in LONG_SETUPS:
        stages[f"{sid}|LONG"] = int(risk_stage.stage_for(db, sid, "LONG"))
    for sid in SHORT_SETUPS:
        stages[f"{sid}|SHORT"] = int(risk_stage.stage_for(db, sid, "SHORT"))
    return {"enabled": bool(risk_stage.enabled()), "stages": stages, "short_cap": int(risk_stage.short_cap_for(db))}


def snapshot(db: Any) -> Dict[str, Any]:
    return {"order_rate": _part(order_rate_part), "momentum": _part(momentum_part), "shared_heat": _part(lambda: heat_part(db)), "ladder": _part(lambda: ladder_part(db))}
