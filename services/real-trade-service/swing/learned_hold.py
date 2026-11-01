"""swing/learned_hold.py - E283: how many sessions a swing position may be held, learned from the swing ledger's own closed trades (plan E282 item 5, the time-stop half).

Today the hold is the constant SWING_MAX_HOLD_DAYS (5, the replay's number). The ledger (swing_trades: SHADOW rows from the evening scan, REAL / DEMO rows from live)
records `sessions` and `net_pct` of every CLOSED trade. When at least MIN_TRADES (30) closed trades have a readable `sessions` and at least MIN_WINNERS (10) of them
won (net_pct > 0), the sessions a winner needed give the hold:

    learned = ceil(75th percentile of the winners' sessions)        (3 in 4 winners had paid by then)
    value   = ceil(w x learned + (1 - w) x SWING_MAX_HOLD_DAYS),    w = n / (n + 30), n = the closed trades with sessions

Guard rails (plan, "Guard rails for every learned value"): only ever SHORTER than SWING_MAX_HOLD_DAYS (never longer), never below SWING_LEARNED_HOLD_MIN_DAYS (3),
nothing learned under 30 closed trades or 10 winners (the constant stays), a rounded value is never shorter than computed (ceil), any read problem keeps the constant.
The SHADOW lane keeps planning with the constant (swing/engine.py), so the history the numbers are learned from is not itself shaped by them. The event extension of the
last session (SWING_EVENT_EXTEND_*) works on top of the learned hold. Switch SWING_LEARNED_HOLD_ENABLED (ships on: it does nothing until the ledger has the trades).

  learned_days(rows, base, min_days)   pure: the learned hold or None
  hold_days(db)                        the hold to use now (cached 10 minutes): learned, else SWING_MAX_HOLD_DAYS. Never raises.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger("real-trade-swing-learned-hold")

MIN_TRADES = 30
MIN_WINNERS = 10
PRIOR = 30
PCTL = 75.0
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


def learned_days(rows: Iterable[Dict[str, Any]], base: int, min_days: int = 3) -> Optional[Dict[str, Any]]:
    """{"days", "learned", "n", "winners", "w"} from closed ledger rows, or None when nothing can be learned or the result would not be shorter than `base`. Pure."""
    try:
        base = int(base)
        sess = []
        wins = []
        for r in rows:
            s, n = _num(r.get("sessions")), _num(r.get("net_pct"))
            if s is None or s < 1 or n is None:
                continue
            sess.append(s)
            if n > 0:
                wins.append(s)
        n_all = len(sess)
        if n_all < MIN_TRADES or len(wins) < MIN_WINNERS:
            return None
        learned = math.ceil(_percentile(wins, PCTL) - 1e-9)
        w = n_all / (n_all + float(PRIOR))
        val = math.ceil(w * learned + (1.0 - w) * base - 1e-9)
        val = max(int(min_days), min(base, val))
        if val >= base:
            return None
        return {"days": int(val), "learned": int(learned), "n": n_all, "winners": len(wins), "w": round(w, 3)}
    except Exception:  # noqa: BLE001
        return None


def hold_days(db: Any = None, now_ts: Optional[float] = None) -> int:
    """The sessions a swing position may be held now. SWING_MAX_HOLD_DAYS unless the ledger supports a shorter hold. Cached for 10 minutes. Never raises."""
    base = max(1, int(_cfg("SWING_MAX_HOLD_DAYS", 5)))
    try:
        if not bool(_cfg("SWING_LEARNED_HOLD_ENABLED", True)) or db is None:
            return base
        now = time.monotonic() if now_ts is None else float(now_ts)
        if _cache["at"] is not None and now - float(_cache["at"]) < CACHE_S and _cache.get("base") == base:
            v = _cache["val"]
            return int(v["days"]) if v else base
        from swing.repo import SqlRepo
        rows = SqlRepo(db).trades(status="CLOSED")
        res = learned_days(rows, base, max(1, int(_cfg("SWING_LEARNED_HOLD_MIN_DAYS", 3))))
        _cache.update({"at": now, "val": res, "base": base})
        if res:
            logger.info("swing hold learned: %s sessions (constant %s) from %s closed trades, %s winners", res["days"], base, res["n"], res["winners"])
        return int(res["days"]) if res else base
    except Exception:  # noqa: BLE001
        logger.debug("swing learned hold failed (constant kept)", exc_info=True)
        return base


def reset() -> None:
    _cache.update({"at": None, "val": None, "base": None})
