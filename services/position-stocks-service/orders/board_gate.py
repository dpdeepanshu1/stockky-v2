"""orders/board_gate.py - group309: the Market Board "chase guard" for automatic entries.
An entry is "extended" when price has run more than BOARD_CHASE_ATR ATRs past the fresh order-block zone (bull side; the scalper is
long-only). BOARD_GATE_MODE: enforce (always live, default) = refuse with BOARD_CHASE:...; off = kill switch. Any error or missing data fails OPEN."""
from __future__ import annotations

import logging
import time
from typing import Optional

import config

logger = logging.getLogger("position-stocks-board-gate")


def evaluate(symbol: str, ltp: float) -> Optional[dict]:
    try:
        from screening import board
        v = board.zone_view(symbol, "bull", ltp)
        if not v:
            return None
        z = v["zone"]
        return {"chase_atr": v["chase_atr"], "zone_status": z["status"] if z else None, "rr": v["rr"]["rr"] if v["rr"] else None,
                "extended": bool(v["chase_atr"] is not None and v["chase_atr"] > config.BOARD_CHASE_ATR)}
    except Exception as e:  # noqa: BLE001
        logger.debug("board gate evaluate(%s) failed: %s", symbol, e)
        return None


def effective_mode() -> str:
    """group370: enforce (always live) or off (kill switch). The record-only "shadow" and the "auto" promotion were removed; any value
    other than "off" - including a stale "shadow" / "auto" in an old .env - is enforce. Never raises."""
    return "off" if str(getattr(config, "BOARD_GATE_MODE", "enforce") or "enforce").lower() == "off" else "enforce"


def check(symbol: str, ltp: float) -> Optional[str]:
    """None = let the entry go on; a string = refuse it (enforce mode only). Never raises."""
    mode = effective_mode()
    if mode == "off" or not getattr(config, "BOARD_ENABLED", True):
        return None
    try:
        facts = evaluate(symbol, ltp)
        if facts is None:
            _log(symbol, ltp, None, "no_zone_data")
            return None
        refuse = facts["extended"]
        if mode == "enforce" and refuse:
            _log(symbol, ltp, facts, "enforced:extended")
            return f"BOARD_CHASE:{facts['chase_atr']:.2f} ATR past the zone > {config.BOARD_CHASE_ATR:.2f}"
        _log(symbol, ltp, facts, "pass")
        return None
    except Exception as e:  # noqa: BLE001 - fail open
        logger.debug("board gate check(%s) failed open: %s", symbol, e)
        return None


def _log(symbol: str, ltp: float, facts: Optional[dict], note: str) -> None:
    try:
        from datetime import datetime, timezone
        from screening import board, board_log
        from feed import ws_client
        now = time.time()
        ds = ws_client.get_day_stats(symbol) or (None, None, None, None)
        board_log.enqueue({
            "ts": datetime.fromtimestamp(now, tz=timezone.utc), "day": board_log._day_str(now), "kind": "entry",
            "symbol": symbol, "side": "bull", "rank": None, "strength": None, "conviction": None, "ltp": ltp,
            "day_open": ds[0], "vwap": ws_client.get_exchange_vwap(symbol),
            "chase_atr": (facts or {}).get("chase_atr"), "rr": (facts or {}).get("rr"),
            "zone_status": (facts or {}).get("zone_status"), "in_position": False, "note": note,
            "params_version": board.params_version(),
        })
    except Exception as e:  # noqa: BLE001
        logger.debug("board gate log failed: %s", e)
