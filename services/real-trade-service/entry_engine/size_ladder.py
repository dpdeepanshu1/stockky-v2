"""entry_engine/size_ladder.py - evidence-gated step-up of the per-trade value cap (group 481, 2026-10-21).

WHY: the Rs 3,000 MAX_TRADE_VALUE cap is small against the account, but a bigger position scales profit and loss alike and Dhan brokerage is a
percentage below about Rs 66,700 per order, so size lowers no fee percentage. With no proven edge a larger size only loses faster. This lets the
cap grow BY ITSELF, but only after the stored daily net P&L (trade_pnl_daily: gross minus charges) has earned it, and it falls back the moment the
evidence goes.

RULES (stateless, recomputed from the last captured trading days, newest first; a day counts only when it has orders and a stored net):
  level L (1..SIZE_LADDER_MAX_LEVEL) needs the newest SIZE_LADDER_MIN_DAYS x L days, all captured, with total net > 0 and at least
  SIZE_LADDER_MIN_POSITIVE_DAYS_PCT % of them net-positive. BRAKE: when the newest SIZE_LADDER_BRAKE_DAYS days sum below 0 the level is 0.
  cap = base x (1 + SIZE_LADDER_STEP_PCT / 100) ** level. Never below base. REAL only (DEMO and manual orders keep the plain cap).
  The existing 25 % concentration cap, capital-share cap, cash check and every other risk gate still apply on top.
Any error returns the base cap. Off with SIZE_LADDER_ENABLED=false.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

import config

logger = logging.getLogger("real-trade-size-ladder")

_CACHE: dict = {}
_TTL_S = 300.0


def reset() -> None:
    _CACHE.clear()


def level_for(nets_newest_first: List[float]) -> Tuple[int, str]:
    """(level, reason) from the net P&L of the captured trading days, newest first. Pure."""
    max_level = max(0, int(config.SIZE_LADDER_MAX_LEVEL))
    per = max(1, int(config.SIZE_LADDER_MIN_DAYS))
    brake_n = max(0, int(config.SIZE_LADDER_BRAKE_DAYS))
    if max_level == 0:
        return 0, "max level 0"
    if brake_n and len(nets_newest_first) >= brake_n and sum(nets_newest_first[:brake_n]) < 0:
        return 0, f"brake: last {brake_n} days net {sum(nets_newest_first[:brake_n]):.2f} < 0"
    best, why = 0, f"need {per} captured days, have {len(nets_newest_first)}"
    for lvl in range(1, max_level + 1):
        need = per * lvl
        if len(nets_newest_first) < need:
            why = f"level {lvl} needs {need} captured days, have {len(nets_newest_first)}"
            break
        win = nets_newest_first[:need]
        pos = 100.0 * sum(1 for v in win if v > 0) / need
        if sum(win) > 0 and pos >= float(config.SIZE_LADDER_MIN_POSITIVE_DAYS_PCT):
            best, why = lvl, f"level {lvl}: {need} days net {sum(win):.2f}, {pos:.0f}% positive days"
        else:
            why = f"level {lvl} not earned: {need} days net {sum(win):.2f}, {pos:.0f}% positive days"
            break
    return best, why


def _captured_nets(db, mode: str) -> List[float]:
    import models
    limit = max(1, int(config.SIZE_LADDER_MIN_DAYS) * max(1, int(config.SIZE_LADDER_MAX_LEVEL)) + int(config.SIZE_LADDER_BRAKE_DAYS) + 5)
    rows = (db.query(models.TradePnlDaily).filter(models.TradePnlDaily.mode == mode.upper())
            .order_by(models.TradePnlDaily.day.desc()).limit(limit).all())
    return [float(r.net_realized) for r in rows if r.net_realized is not None and (r.orders or 0) > 0]


def status(db, mode: str, base_cap: Optional[float]) -> dict:
    """Level, reason and effective cap (cached for 5 minutes per mode). Never raises."""
    try:
        if not config.SIZE_LADDER_ENABLED or (mode or "").upper() != "REAL" or not base_cap or base_cap <= 0:
            return {"level": 0, "reason": "off or not REAL", "base_cap": base_cap, "cap": base_cap}
        now = time.monotonic()
        hit = _CACHE.get(mode.upper())
        if hit and now - hit[0] < _TTL_S and hit[1] == base_cap:
            return hit[2]
        lvl, why = level_for(_captured_nets(db, mode))
        cap = round(float(base_cap) * (1.0 + float(config.SIZE_LADDER_STEP_PCT) / 100.0) ** lvl, 2)
        out = {"level": lvl, "reason": why, "base_cap": base_cap, "cap": max(cap, float(base_cap))}
        _CACHE[mode.upper()] = (now, base_cap, out)
        return out
    except Exception as e:  # noqa: BLE001 - sizing must never break an entry cycle
        logger.warning("size ladder unavailable, using the base cap: %s", e)
        return {"level": 0, "reason": f"error: {str(e)[:80]}", "base_cap": base_cap, "cap": base_cap}


def effective_cap(db, mode: str, base_cap: Optional[float]) -> Optional[float]:
    return status(db, mode, base_cap)["cap"]
