"""fno/sizing.py - how many LOTS. An option buyer's maximum loss is the premium paid, so the budget is: the stop-loss rupees must not exceed risk_per_trade_pct of capital.

    risk per lot = premium x lot_size x (sl_pct / 100)         lots = floor(budget / risk per lot)
Capped by max_lots and by what the premium itself costs (never more than `max_premium_share` of capital in one trade). Returns None when even ONE lot does not fit:
the trade is skipped, never forced (a whole-lot rounding up would exceed the risk the user set)."""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

from .params import Params


def lots_for(premium: float, lot_size: int, p: Params, max_premium_share: float = 0.35) -> Optional[int]:
    if not (premium and premium > 0) or lot_size <= 0 or p.capital <= 0 or p.sl_pct <= 0:
        return None
    budget = p.capital * p.risk_per_trade_pct / 100.0
    risk_per_lot = premium * lot_size * p.sl_pct / 100.0
    by_risk = int(math.floor(budget / risk_per_lot + 1e-9))
    by_cash = int(math.floor(p.capital * max_premium_share / (premium * lot_size) + 1e-9))
    lots = min(by_risk, by_cash, int(p.max_lots))
    return lots if lots >= 1 else None


def lots_in_room(lots: int, fill: float, lot_size: int, capital: float, used: float) -> int:
    """Round 247: lots that still fit the capital the board allots, after the premium already paid for the positions that are open (REAL only). 0 = no room.
    lots_for() sizes each order against the WHOLE allotment, so two or three open positions (FNO_MAX_OPEN_POSITIONS) could together hold more than the allotment and eat the other services' money."""
    per_lot = float(fill) * int(lot_size)
    if per_lot <= 0 or lots < 1:
        return 0
    return max(0, min(int(lots), int((float(capital) - max(0.0, float(used))) // per_lot)))


def min_capital_for_one_lot(premium: float, lot_size: int, p: Params) -> Optional[float]:
    """The capital at which ONE lot just fits the risk budget (so a skipped trade can say what it would have needed)."""
    if not (premium and premium > 0) or lot_size <= 0 or p.risk_per_trade_pct <= 0:
        return None
    return round(premium * lot_size * p.sl_pct / 100.0 / (p.risk_per_trade_pct / 100.0), 0)


def explain(premium: float, lot_size: int, p: Params) -> Dict[str, Any]:
    lots = lots_for(premium, lot_size, p)
    return {"premium": premium, "lot_size": lot_size, "lots": lots, "cost": round(premium * lot_size * (lots or 0), 2),
            "max_loss_at_stop": round(premium * lot_size * (lots or 0) * p.sl_pct / 100.0, 2),
            "budget": round(p.capital * p.risk_per_trade_pct / 100.0, 2)}
