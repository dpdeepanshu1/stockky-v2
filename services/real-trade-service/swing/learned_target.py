"""swing/learned_target.py - E285 round 91: a take-profit level for a swing position, learned from the swing ledger's own closed trades.

Until now a swing trade had a ratcheting stop and a time limit, and no target (the replay tested exactly that). The ledger had nothing to learn a target from, so round 91 adds
two columns to `trade_swing_ledger`: `peak_pct` (the best high the trade reached, in % over its entry price, kept up to date every evening for SHADOW and LIVE rows) and
`target_px` (the level a LIVE entry is given when it is bought).

THE RULE. With at least MIN_TRADES (30) CLOSED ledger trades that have a readable `peak_pct` and at least MIN_WINNERS (10) of them won (net_pct > 0):

    learned % = the 50th percentile of the winners' peak_pct          (half of the winning trades went at least this far)
    target %  = max(learned %, SWING_TARGET_MIN_R x the trade's own stop %, SWING_TARGET_MIN_PCT)
    target_px = entry price x (1 + target % / 100), rounded to a paisa

The floors are the guard rails: a learned target is never nearer than 1.5 times the trade's own risk (SWING_TARGET_MIN_R) and never under 3 % (SWING_TARGET_MIN_PCT), so a
few small winners cannot make the lane sell every gain early. Nothing learned under 30 trades or 10 winners: no target at all (the position keeps its stop and its time limit).

WHERE IT ACTS. Only at the 15:00 hold decision (swing/live.eod_holds): a position that would be carried overnight but is at or above its `target_px` is sold at the close
instead (`lane.target_sale`). It can only SHORTEN a hold, never starts a trade, never moves a stop and places no resting broker order. A target touched and given back
during the session is not caught (the stop and the 15:00 decision are the only watchers). The SHADOW lane keeps planning with no target (swing/engine.py), so the history the
numbers are learned from is not shaped by them (same rule as swing/learned_hold.py).

The columns start empty, so this does nothing until 30 closed trades have been recorded with a peak: about a month of evening scans. Switch SWING_LEARNED_TARGET_ENABLED
(ships on, because it is inert until then); `target_px` already stored on a row is ignored while the switch is off.

  learned_pct(rows)                           pure: {"pct", "n", "winners"} or None
  target_pct_for(learned, stop_pct, min_r, min_pct)   pure: the target % after the floors
  learned(db)                                 the learned dict now (cached 10 minutes). Never raises.
  target_px_for(db, entry, stop_pct)          the level to store for a new LIVE entry, or None. Never raises.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger("real-trade-swing-learned-target")

MIN_TRADES = 30
MIN_WINNERS = 10
PCTL = 50.0
CACHE_S = 600.0

_cache: Dict[str, Any] = {"at": None, "val": None}


def _cfg(name: str, default: Any = None) -> Any:
    import config
    return getattr(config, name, default)


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _percentile(v, q: float) -> Optional[float]:
    s = sorted(v)
    if not s:
        return None
    k = (len(s) - 1) * q / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return float(s[lo]) if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def learned_pct(rows: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """{"pct", "n", "winners"} from closed ledger rows, or None when fewer than MIN_TRADES have a readable peak and a result, or fewer than MIN_WINNERS won. Pure."""
    try:
        peaks = []
        wins = []
        for r in rows:
            pk, n = _num(r.get("peak_pct")), _num(r.get("net_pct"))
            if pk is None or n is None:
                continue
            peaks.append(pk)
            if n > 0:
                wins.append(pk)
        if len(peaks) < MIN_TRADES or len(wins) < MIN_WINNERS:
            return None
        p = _percentile(wins, PCTL)
        if p is None or p <= 0:
            return None
        return {"pct": round(p, 3), "n": len(peaks), "winners": len(wins)}
    except Exception:  # noqa: BLE001
        return None


def target_pct_for(learned: Any, stop_pct: Any, min_r: float, min_pct: float) -> Optional[float]:
    """The target in % after the floors: max(learned, min_r x the trade's stop %, min_pct). None when there is no learned value. Pure."""
    try:
        lp = _num(learned)
        if lp is None or lp <= 0:
            return None
        sp = _num(stop_pct)
        floor = max(float(min_pct), float(min_r) * sp if sp is not None and sp > 0 else 0.0)
        return round(max(lp, floor), 2)
    except Exception:  # noqa: BLE001
        return None


def learned(db: Any = None, now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The learned dict now, or None (switch off, no database, not enough trades, any problem). Cached for 10 minutes. Never raises."""
    try:
        if not bool(_cfg("SWING_LEARNED_TARGET_ENABLED", True)) or db is None:
            return None
        now = time.monotonic() if now_ts is None else float(now_ts)
        if _cache["at"] is not None and now - float(_cache["at"]) < CACHE_S:
            return _cache["val"]
        from swing.repo import SqlRepo
        res = learned_pct(SqlRepo(db).trades(status="CLOSED"))
        _cache.update({"at": now, "val": res})
        if res:
            logger.info("swing target learned: median winner peak %.2f%% from %s closed trades with a peak, %s winners", res["pct"], res["n"], res["winners"])
        return res
    except Exception:  # noqa: BLE001
        logger.debug("swing learned target failed (no target)", exc_info=True)
        return None


def target_px_for(db: Any, entry: Any, stop_pct: Any) -> Optional[float]:
    """The take-profit level to store on a new LIVE ledger row, or None when nothing is learned yet. Never raises."""
    try:
        e = _num(entry)
        if e is None or e <= 0:
            return None
        res = learned(db)
        if not res:
            return None
        pct = target_pct_for(res["pct"], stop_pct, float(_cfg("SWING_TARGET_MIN_R", 1.5)), float(_cfg("SWING_TARGET_MIN_PCT", 3.0)))
        return None if pct is None else round(e * (1.0 + pct / 100.0), 2)
    except Exception:  # noqa: BLE001
        return None


def reset() -> None:
    _cache.update({"at": None, "val": None})
