"""risk_engine/effective_risk.py - which risk-per-trade % the engine really uses (E47, 2026-10-23).

The stored row (`trade_risk_config.risk_per_trade_pct`) used to be the only source. The Real Money Calibration page edits
`config.DEFAULT_RISK_PER_TRADE_PCT`, which only seeds a NEW row, so the page switch did nothing once the row existed.

Rule now:
  * an admin saved the row (PUT /risk-config sets `updated_by` to that admin / "demo-user")  -> the row wins, exactly as before;
  * the row is still system-owned (`updated_by` is one of our own tags: the seed tag or a migration tag)  -> the live config value
    is used, read at call time, so the calibration page works at once. An old row with NO tag is treated as stored (the E46 migration
    tags it on the next boot when it is still at the old 5.0 default).
Fails safe: a missing / odd row falls back to the stored number, and a non-positive config value is ignored.
"""
from __future__ import annotations

from typing import Any

import config

SYSTEM_TAGS = ("system-seed", "system-migration-session79", "system-migration-e46")


def is_system_owned(risk_row: Any) -> bool:
    return str(getattr(risk_row, "updated_by", None) or "") in SYSTEM_TAGS


def market_state_risk_mult() -> float:
    """Market Weather multiplier on the per-trade risk % (config.MARKET_STATE_RISK_MULT, read at call time). 1.0 = no change; the value is clamped to
    0.1 .. 1.0 so it can only ever REDUCE risk; anything unreadable counts as 1.0."""
    try:
        v = float(getattr(config, "MARKET_STATE_RISK_MULT", 1.0))
    except Exception:  # noqa: BLE001
        return 1.0
    if v != v:
        return 1.0
    return min(1.0, max(0.1, v))


def effective_risk_pct_in_force(risk_row: Any):
    """effective_risk_pct() x today's market-state multiplier: the number the engine really caps one trade at. DISPLAY ONLY (/risk-config, the
    portfolio summary). The engine feeds effective_risk_pct() (NOT this) into AccountState and applies the multiplier itself, once, in
    risk_engine/engine.py, so the multiplier is never applied twice."""
    base = effective_risk_pct(risk_row)
    if base is None:
        return None
    try:
        return float(base) * market_state_risk_mult()
    except (TypeError, ValueError):
        return base


def effective_risk_pct(risk_row: Any):
    stored = getattr(risk_row, "risk_per_trade_pct", None)
    if not is_system_owned(risk_row):
        return stored
    try:
        live = float(config.DEFAULT_RISK_PER_TRADE_PCT)
    except Exception:  # noqa: BLE001
        return stored
    return live if live > 0 else stored
