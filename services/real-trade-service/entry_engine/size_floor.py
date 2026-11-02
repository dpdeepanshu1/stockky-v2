"""entry_engine/size_floor.py - E285 round 132: a floor under the STACK of size penalties on one Real Trade long entry. Pure; no DB; never raises.

Found (2 Nov): the entry's risk percentage is multiplied one factor after another - the regime-override scale, India VIX, calendar day, the weak-market regime rules, the
opening-gate soft tests, the soft gates (each floored at SOFT_GATE_FLOOR on its own), the overseas lead, the matched setup - and then the opening window halves the share
count. Each factor is reasonable alone; together, on an ordinary opening morning, they leave 1-3 shares (the "tiny orders"): too small to matter when it wins, and the same
fixed charges when it loses. A trade that passed every GATE is a trade worth taking at a sensible size, so the stack as a whole may not take the size below
`SIZE_STACK_FLOOR` (default 0.35) of the risk the conviction rule asked for. A deliberate regime-override reduction is part of that base, not of the stack. The floor only
lifts a size that stacking pushed too low; it never goes above the base risk, and a refusal (a WAIT) is never turned into an entry by it. 0 = off.
"""
from __future__ import annotations

from typing import Tuple

import config


def floor() -> float:
    try:
        v = float(getattr(config, "SIZE_STACK_FLOOR", 0.35))
        return v if v == v and 0.0 < v <= 1.0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def floored_risk_pct(base_pct, adj_pct) -> Tuple[float, bool]:
    """(risk %, lifted). The base is the risk before the penalty stack; the result is never below base x floor and never above `adj_pct` unless that was lifted."""
    try:
        b, a, f = float(base_pct), float(adj_pct), floor()
        if f <= 0.0 or not (b == b and a == a) or b <= 0.0 or a >= b * f:
            return a, False
        return b * f, True
    except (TypeError, ValueError):
        return adj_pct, False


def open_window_mult(base_pct, adj_pct, open_mult) -> float:
    """The opening-window share multiplier, eased so (risk stack x window) is not below the floor either. Never below `open_mult`, never above 1.0."""
    try:
        b, a, m, f = float(base_pct), float(adj_pct), float(open_mult), floor()
        if f <= 0.0 or m >= 1.0 or b <= 0.0 or a <= 0.0:
            return m
        need = (b * f) / a                                   # window multiple at which the whole stack equals the floor
        return max(m, min(1.0, need))
    except (TypeError, ValueError, ZeroDivisionError):
        return open_mult
