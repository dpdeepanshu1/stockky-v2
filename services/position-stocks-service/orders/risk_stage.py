"""orders/risk_stage.py - E276 round 54: reads the closed trades once (cached) and answers "which ladder stage is this setup at". See orders/risk_ladder.py.

stage_for(db, setup_id, side) -> 0..3 (round 58: a losing time slot reads 0; round 57: 3 = stage 2 sustained 60 trading days; round 55: also the daily-loss brake and short_cap_for); 0 when the switch is off, the setup is unknown or anything fails (the base heat limit). Cached RISK_LADDER_CACHE_S seconds."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import config
from orders import risk_ladder, setup_stats

logger = logging.getLogger("position-stocks-risk-stage")

_cache: Dict[str, Any] = {"at": 0.0, "val": None}


def enabled() -> bool:
    return bool(getattr(config, "RISK_LADDER_ENABLED", True))


def reset() -> None:
    _cache["at"], _cache["val"] = 0.0, None


def _load(db) -> Dict[str, Any]:
    from models import ScalpPosition
    from orders import slot_ladder
    rows = (db.query(ScalpPosition).filter(ScalpPosition.realized_pnl.isnot(None)).order_by(ScalpPosition.opened_at.desc()).limit(1500).all())
    days: Dict[str, float] = {}
    for r in rows:                                                    # net closed P&L per trading day (opened day), newest first
        d = str(getattr(r, "opened_at", "") or "")[:10]
        if d:
            days[d] = days.get(d, 0.0) + float(r.realized_pnl or 0.0)
    pool = 0.0
    try:
        from capital import ledger
        pool = float(ledger.own_heat(db)[2] or 0.0)
    except Exception:  # noqa: BLE001 - no pool = the daily-loss brake stays off
        pool = 0.0
    loss_days = risk_ladder.loss_days_brake([days[k] for k in sorted(days, reverse=True)], pool, getattr(config, "MAX_DAILY_LOSS_PCT_OF_POOL", 4.0))
    older: Dict[str, Any] = {}
    try:                                                              # stage 3: the cells as they stood before the last SUSTAIN_DAYS trading days
        ds = sorted(days, reverse=True)
        if len(ds) > risk_ladder.SUSTAIN_DAYS:
            cutoff = ds[risk_ladder.SUSTAIN_DAYS - 1]
            old_rows = [r for r in rows if str(getattr(r, "opened_at", "") or "")[:10] < cutoff]
            older = setup_stats.summarize(slot_ladder.rows_from_positions(old_rows))
    except Exception:  # noqa: BLE001
        older = {}
    return {"older": older, "cells": setup_stats.summarize(slot_ladder.rows_from_positions(rows)), "last": [r.realized_pnl for r in rows[:risk_ladder.BRAKE_TRADES]], "loss_days": loss_days}


def _data(db) -> Dict[str, Any]:
    ttl = max(0.0, float(getattr(config, "RISK_LADDER_CACHE_S", 300.0)))
    now = time.time()
    if _cache["val"] is not None and now - float(_cache["at"]) < ttl:
        return _cache["val"]
    val = _load(db)
    _cache["at"], _cache["val"] = now, val
    return val


def stage_for(db, setup_id: Optional[str], side: str = "LONG") -> int:
    try:
        if not enabled() or not setup_id:
            return 0
        d = _data(db)
        key = f"{setup_stats.cell_id(setup_id, side)}|ALL"
        stg = risk_ladder.stage((d.get("cells") or {}).get(key), d.get("last") or [], bool(d.get("loss_days")), (d.get("older") or {}).get(key))
        if stg > 0 and bool(getattr(config, "RISK_LADDER_SLOT_ENABLED", True)):     # round 58: a losing time slot of this setup stays at stage 0
            try:
                from orders import setup_stamp
                from tz_utils import ist_now
                slot_key = f"{setup_stats.cell_id(setup_id, side)}|{setup_stamp.time_slot(ist_now())}"
                if risk_ladder.slot_blocked((d.get("cells") or {}).get(slot_key)):
                    return 0
            except Exception:  # noqa: BLE001 - the slot check only ever lowers; a failure keeps the setup's stage
                logger.debug("risk_stage: ignored error (fail-open)", exc_info=True)
        return stg
    except Exception:  # noqa: BLE001
        logger.debug("risk stage: read failed (stage 0)", exc_info=True)
        return 0


def short_cap_for(db, setup_id: Optional[str] = None) -> int:
    """Live short-position cap. With a setup id: that setup's own (short cell) stage. Without one (the early guard, before the setup is known): the highest stage
    among the short setups, so the early check never refuses what the setup's own check would allow. Base cap (SHORT_MAX_POSITIONS) on any error or switch off."""
    base = int(getattr(config, "SHORT_MAX_POSITIONS", 2))
    try:
        if not enabled() or not bool(getattr(config, "RISK_LADDER_SHORT_CAP_ENABLED", True)):
            return base
        if setup_id:
            return risk_ladder.short_cap(base, stage_for(db, setup_id, "SHORT"))
        best = max((stage_for(db, sid, "SHORT") for sid in ("S2_RS_BREAKOUT", "S3_EXHAUSTION", "S4_CATALYST_DOWN")), default=0)
        return risk_ladder.short_cap(base, best)
    except Exception:  # noqa: BLE001
        return base


def risk_mult_for(db, setup_id: Optional[str], side: str = "LONG") -> float:
    """E276 round 56: per-trade risk multiple for the setup's stage (1.0 at stage 0). 1.0 on any error, an unknown setup or either switch off."""
    try:
        if not enabled() or not setup_id or not bool(getattr(config, "RISK_LADDER_RISK_ENABLED", True)):
            return 1.0
        return risk_ladder.risk_mult(stage_for(db, setup_id, side), float(getattr(config, "RISK_LADDER_RISK_STEP", 0.25)))
    except Exception:  # noqa: BLE001
        return 1.0
