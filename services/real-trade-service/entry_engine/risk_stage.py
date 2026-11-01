"""entry_engine/risk_stage.py - E276 round 59: the Real Trade risk ladder. Reads the closed REAL positions once (cached) and answers "which ladder stage is this setup at".

A closed position's setup comes from the entry decision's flow_json["setup"], found through the "[decision=<id>]" tag that portfolio.py now writes into the OPENED event (no database column).
Positions opened before the tag, imported holdings and positions whose decision has no setup stamp are not counted. R = net P&L / (initial stop distance x entry quantity).
stage_for(db, setup_id) -> 0..2 (stage 3 needs 60 sustained trading days; not read here). 0 when the switch is off, the setup is unknown or anything fails (the base heat limit).
heat_limit_pct(db, setup_id) = DEFAULT_MAX_PORTFOLIO_RISK_PCT + stage x RISK_LADDER_HEAT_STEP_PCT is applied in entry_engine/entry.py. Stage rules: entry_engine/risk_ladder.py (identical copy of position-stocks)."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Optional

import config
from entry_engine import ladder_stats, risk_ladder

logger = logging.getLogger("real-trade-risk-stage")

_cache: Dict[str, Any] = {"at": 0.0, "val": None}
_OPENED_RE = re.compile(r"^\s*(\d+)\s*@.*?\[decision=(\d+)\]")


def enabled() -> bool:
    return bool(getattr(config, "RISK_LADDER_ENABLED", True))


def reset() -> None:
    _cache["at"], _cache["val"] = 0.0, None


def decision_tag(decision_id: Any) -> str:
    """The text portfolio.py appends to an OPENED event so the position can be tied back to its entry decision."""
    try:
        return f" [decision={int(decision_id)}]" if decision_id else ""
    except (TypeError, ValueError):
        return ""


def parse_opened(detail: Any):
    """(quantity, decision id) from an OPENED event detail written with decision_tag, else None."""
    try:
        m = _OPENED_RE.match(str(detail or ""))
        return (int(m.group(1)), int(m.group(2))) if m else None
    except Exception:  # noqa: BLE001
        return None


def build_rows(positions, opened: Dict[int, Any], decisions: Dict[int, Any]) -> list:
    """The dict rows ladder_stats reads, from closed positions, {position id: OPENED detail} and {decision id: flow_json}. Pure; never raises."""
    out = []
    for p in positions:
        try:
            po = parse_opened(opened.get(p.id))
            if not po:
                continue
            qty, did = po
            fj = decisions.get(did)
            pnl = p.net_realized_pnl if p.net_realized_pnl is not None else p.realized_pnl
            e = float(p.avg_entry_price)
            d = float(p.initial_stop_distance or 0.0)
            out.append({"flow_json": fj, "net_pnl": pnl, "entry_price": e, "stop_price": e - d, "quantity": qty, "opened_at": str(p.opened_at or ""),
                        "opened_ts": p.opened_at.timestamp() if getattr(p, "opened_at", None) else None, "max_price_seen": getattr(p, "max_price_seen", None),
                        "min_price_seen": getattr(p, "min_price_seen", None), "side": "LONG"})
        except Exception:  # noqa: BLE001
            continue
    return out


def _load(db) -> Dict[str, Any]:
    import models
    pos = (db.query(models.TradePosition).filter(models.TradePosition.mode == "REAL", models.TradePosition.status == "CLOSED",
                                                 models.TradePosition.broker_imported == False)  # noqa: E712  (E283: Oracle-safe `= 0` form)
           .order_by(models.TradePosition.opened_at.desc()).limit(1000).all())
    ids = [p.id for p in pos]
    opened: Dict[int, Any] = {}
    for i in range(0, len(ids), 400):
        for ev in db.query(models.TradePositionEvent).filter(models.TradePositionEvent.position_id.in_(ids[i:i + 400]),
                                                              models.TradePositionEvent.event_type == "OPENED").order_by(models.TradePositionEvent.id).all():
            opened.setdefault(ev.position_id, ev.detail)
    dids = sorted({po[1] for po in (parse_opened(v) for v in opened.values()) if po})
    decisions: Dict[int, Any] = {}
    for i in range(0, len(dids), 400):
        for d in db.query(models.TradeDecision).filter(models.TradeDecision.id.in_(dids[i:i + 400])).all():
            decisions[d.id] = d.flow_json
    rows = build_rows(pos, opened, decisions)
    pnls = [(p.net_realized_pnl if p.net_realized_pnl is not None else p.realized_pnl) for p in pos[:risk_ladder.BRAKE_TRADES]]
    return {"cells": ladder_stats.summarize(rows), "last": pnls}


def _data(db) -> Dict[str, Any]:
    ttl = max(0.0, float(getattr(config, "RISK_LADDER_CACHE_S", 300.0)))
    now = time.time()
    if _cache["val"] is not None and now - float(_cache["at"]) < ttl:
        return _cache["val"]
    val = _load(db)
    _cache["at"], _cache["val"] = now, val
    return val


def stage_for(db, setup_id: Optional[str]) -> int:
    try:
        if not enabled() or not setup_id:
            return 0
        d = _data(db)
        return min(2, risk_ladder.stage((d.get("cells") or {}).get(f"{ladder_stats.cell_id(setup_id, 'LONG')}|ALL"), d.get("last") or []))
    except Exception:  # noqa: BLE001
        logger.debug("risk stage: read failed (stage 0)", exc_info=True)
        return 0


def heat_limit_pct(db, setup_id: Optional[str], base_pct: float) -> float:
    """The portfolio heat limit % for this setup: the base, or the base raised by the setup's stage. The base on any error."""
    try:
        return risk_ladder.heat_limit_pct(base_pct, stage_for(db, setup_id), float(getattr(config, "RISK_LADDER_HEAT_STEP_PCT", 1.0)))
    except Exception:  # noqa: BLE001
        return base_pct
