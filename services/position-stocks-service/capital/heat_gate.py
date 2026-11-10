"""capital/heat_gate.py - E276 round 51: the shared portfolio-heat limit, enforced at a Position Stocks entry.

Heat = rupees lost if every open stop is hit (this pool + real-trade-service's published figure, plus the gap allowance on carried positions),
as a % of the combined pool. A new entry is refused when it would push that past HEAT_LIMIT_PCT (same 5 % real-trade-service uses).
FAIL-OPEN: switch off, limit 0, any DB / read error, a peer row that is missing or stale = no refusal. Never raises.
"""
from __future__ import annotations

import logging
from typing import Optional

import config
from capital import portfolio_heat, shared_heat

logger = logging.getLogger("position-stocks-heat-gate")


def _sector_extra(db, symbol, side, new_risk):
    """E276 round 60: (surcharge in rupees, sector) for a new entry that lands in a sector already holding an open position on the same side, counted across
    this pool and the peer's published split. (0.0, None) when the rule is off, the symbol is unknown / unlisted, or on any error (fail-open)."""
    try:
        if not symbol or not bool(getattr(config, "SECTOR_HEAT_ENABLED", True)):
            return 0.0, None
        from feed import sector_map
        from capital import ledger
        sec = sector_map.sector_of(symbol)
        if sec == "Other":
            return 0.0, None
        key = portfolio_heat.sector_key(sec, side)
        both = portfolio_heat.merge_sector_risk(ledger.own_sector_risk(db), shared_heat.get_peer_sectors(db, float(getattr(config, "HEAT_PEER_MAX_AGE_S", 900.0))))
        return portfolio_heat.sector_surcharge(both.get(key, 0.0), new_risk, float(getattr(config, "SECTOR_HEAT_MULT", 1.5))), sec
    except Exception as e:  # noqa: BLE001
        logger.warning("sector heat failed (no surcharge): %s", e)
        return 0.0, None


def reject_reason(db, price: float, stop_price: float, quantity: int, side: str = "LONG", setup_id: Optional[str] = None, symbol: Optional[str] = None) -> Optional[str]:
    """None = allowed. Otherwise a HEAT_LIMIT:... reason for the candidate log. E276 round 54: a setup on ladder stage 1 / 2 has the limit raised (orders/risk_ladder.py)."""
    try:
        if not bool(getattr(config, "HEAT_GATE_ENABLED", True)):
            return None
        limit = float(getattr(config, "HEAT_LIMIT_PCT", 5.0) or 0.0)
        if limit <= 0:
            return None
        stg = 0
        if setup_id:
            try:
                from orders import risk_ladder, risk_stage
                stg = risk_stage.stage_for(db, setup_id, side)
                limit = risk_ladder.heat_limit_pct(limit, stg, float(getattr(config, "RISK_LADDER_HEAT_STEP_PCT", 1.0)))
            except Exception:  # noqa: BLE001 - the ladder only ever loosens; a failure leaves the base limit
                stg = 0
        from capital import ledger
        own_risk, own_gap, own_pool = ledger.own_heat(db)
        peer_risk, peer_pool, _age = shared_heat.get_peer(db, float(getattr(config, "HEAT_PEER_MAX_AGE_S", 900.0)))
        new_risk = portfolio_heat.stop_risk(price, stop_price, quantity, side, max_stop_pct=float(getattr(config, "MAX_STOP_PCT", 5.0)))
        sec_extra, sec = _sector_extra(db, symbol, side, new_risk)
        ok, after, cap = portfolio_heat.check(own_risk, own_gap, peer_risk, new_risk, own_pool, peer_pool, limit, sec_extra)
        if ok:
            return None
        _s = f":sector={sec}:sector_extra=Rs{sec_extra:.0f}" if sec_extra > 0 else ""
        return (f"HEAT_LIMIT:after={after:.2f}%>limit={limit:.2f}%:stage={stg}:cap=Rs{cap:.0f}:own={own_risk + own_gap:.0f}:peer={peer_risk:.0f}:new={new_risk:.0f}{_s}")
    except Exception as e:  # noqa: BLE001 - a risk check must never block a trade on its own failure
        logger.warning("heat gate failed (entry allowed): %s", e)
        return None
