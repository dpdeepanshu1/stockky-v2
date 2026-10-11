"""orders/board_gate.py - group309: the Market Board "chase guard" for automatic entries.
An entry is "extended" when price has run more than BOARD_CHASE_ATR ATRs past the fresh order-block zone (bull side; the scalper is
long-only). BOARD_GATE_MODE: off = nothing; shadow = only LOG the verdict (scalp_board_log kind='entry'); enforce = refuse with
BOARD_CHASE:... Any error or missing data fails OPEN. Enforce is a deliberate config change, never automatic."""
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


_auto: dict = {"at": 0.0, "mode": "shadow", "verdict": None}


def _auto_verdict() -> Optional[str]:
    """group322: the shadow report's verdict for the chase guard over the last BOARD_AUTO_DAYS days, or None when it cannot be
    computed. Same data and rule as GET /board/shadow-report."""
    from datetime import datetime, timedelta, timezone
    from db import get_session_factory
    from models import ScalpBoardLog
    from screening import board, board_report
    since = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30) - timedelta(days=int(config.BOARD_AUTO_DAYS))).strftime("%Y-%m-%d")
    db = get_session_factory()()
    try:
        rows = db.query(ScalpBoardLog).filter(ScalpBoardLog.day >= since).order_by(ScalpBoardLog.id.asc()).limit(60000).all()
        dicts = [{"kind": r.kind, "day": r.day, "symbol": r.symbol, "side": r.side, "rank": r.rank, "conviction": r.conviction,
                  "note": r.note, "zone_status": r.zone_status, "ts": str(r.ts), "params_version": r.params_version,
                  "fwd15_pct": r.fwd15_pct, "fwd30_pct": r.fwd30_pct, "fwd60_pct": r.fwd60_pct,
                  "fwd_close_pct": r.fwd_close_pct} for r in rows]
    finally:
        db.close()
    out = board_report.report(dicts, config.BOARD_COST_PCT, config.BOARD_PROMOTE_MIN_OBS, config.BOARD_PROMOTE_MIN_SESSIONS,
                              board.params_version())
    return str(out["rules"]["chase_guard"]["verdict"])


def effective_mode() -> str:
    """group322: BOARD_GATE_MODE with "auto" resolved: enforce while the chase-guard verdict is "promote", else shadow. The verdict
    is cached BOARD_AUTO_REFRESH_S seconds; if it cannot be computed the last answer stays (shadow before the first one). Never raises."""
    mode = getattr(config, "BOARD_GATE_MODE", "auto")
    if mode != "auto":
        return mode
    try:
        now = time.time()
        if _auto["verdict"] is not None and now - _auto["at"] < float(config.BOARD_AUTO_REFRESH_S):
            return _auto["mode"]
        if now - _auto["at"] < 60:                      # a failed attempt is not retried every entry
            return _auto["mode"]
        _auto["at"] = now
        verdict = _auto_verdict()
        new = "enforce" if verdict == "promote" else "shadow"
        if new != _auto["mode"]:
            logger.warning("board gate auto: chase-guard verdict %r -> %s", verdict, new)
        _auto["verdict"], _auto["mode"] = verdict, new
    except Exception as e:  # noqa: BLE001
        logger.debug("board gate auto verdict failed (keeping %s): %s", _auto["mode"], e)
    return _auto["mode"]


def reset_auto() -> None:
    _auto.update(at=0.0, mode="shadow", verdict=None)


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
        _log(symbol, ltp, facts, "would_block:extended" if refuse else "pass")
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
