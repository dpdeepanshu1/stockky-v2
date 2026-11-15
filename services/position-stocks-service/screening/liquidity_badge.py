"""screening/liquidity_badge.py - round 559 (E584, Tredye plan P8): the liquidity facts of one stock for the Market Board popup. DISPLAY ONLY.

Two facts, each with the time it was read, a missing one is None (never 0):
  * turnover  - the stock's traded value so far in the session (cumulative volume x price, what the board's sector shares already use) and where it stands among the board's tracked
                stocks that have a turnover: `pct_rank` = % of those with a SMALLER turnover (a rank, not a score; needs at least MIN_TRACKED of them, else None).
  * spread    - the quoted bid-ask spread the spread recorder wrote for this stock in the SAME session (last minute, the median over the session's minutes, the widest minute). Only
                a recorded value is shown: a stock with no recorded reading, or only an older session's, shows nothing (no estimate, no fill-in).
Nothing here is read by an entry, exit, gate or sizing path (a test fails if another file imports this module); the entry depth gate keeps its own spread check.
Pure helpers (`pct_rank`, `spread_stats`) take plain numbers; `build()` reads bar_store and spread_recorder and never raises.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("position-stocks-liquidity-badge")

MIN_TRACKED = 20                    # fewer stocks with a turnover than this and the rank is not a meaningful percentile
IST_OFFSET_S = 330 * 60


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def pct_rank(value: Any, universe: Sequence[Any], min_tracked: int = MIN_TRACKED) -> Optional[float]:
    """% (0-100, 1 decimal) of the stocks in `universe` whose number is SMALLER than `value`. Only positive, finite numbers count (a 0 turnover means no volume seen = unknown, not a real
    zero), and None comes back when `value` is not positive or fewer than `min_tracked` stocks qualify."""
    v = _num(value)
    if v is None or v <= 0:
        return None
    if not isinstance(universe, (list, tuple, set, frozenset)):
        return None
    pool = [x for x in (_num(u) for u in universe) if x is not None and x > 0]
    if len(pool) < max(1, int(min_tracked)):
        return None
    return round(100.0 * sum(1 for x in pool if x < v) / len(pool), 1)


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def spread_stats(rows: Any, session: str) -> Optional[Dict[str, Any]]:
    """From recorder rows [t, median_pct, max_pct, samples] (t = epoch seconds of the minute): the stats of the rows that fall on IST date `session`.
    None when there is no usable row of that day. Never an average of days."""
    if not isinstance(rows, (list, tuple)):
        return None
    use: List[List[float]] = []
    for r in rows:
        try:
            t, med, mx = _num(r[0]), _num(r[1]), _num(r[2])
        except (TypeError, IndexError, KeyError):
            continue
        if t is None or t <= 0 or med is None or med < 0 or mx is None or mx < 0:
            continue
        if (date(1970, 1, 1) + timedelta(seconds=t + IST_OFFSET_S)).isoformat() != session:
            continue
        use.append([t, med, mx])
    if not use:
        return None
    use.sort(key=lambda r: r[0])
    return {"last_pct": round(use[-1][1], 3), "last_at": use[-1][0], "median_pct": round(_median([r[1] for r in use]), 3),
            "max_pct": round(max(r[2] for r in use), 3), "minutes": len(use)}


def session_date(day_number: Any) -> Optional[str]:
    """ISO date of the board's session from bar_store.day_number() (days since 1970-01-01, IST)."""
    n = _num(day_number)
    if n is None:
        return None
    try:
        return (date(1970, 1, 1) + timedelta(days=int(n))).isoformat()
    except (OverflowError, ValueError):
        return None


def build(symbol: str) -> Dict[str, Any]:
    """{"session", "turnover": {...} | None, "spread": {...} | None}. Never raises; a failed part is None."""
    out: Dict[str, Any] = {"session": None, "turnover": None, "spread": None}
    sym = str(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    try:
        from feed import bar_store
        out["session"] = session_date(bar_store.day_number())
        bars = bar_store.get_bars(sym)
        tv = bar_store.turnover(sym)
        if bars and tv and tv > 0:
            have = [x for x in (bar_store.turnover(s) for s in bar_store.symbols()) if x and x > 0]
            out["turnover"] = {"value": round(float(tv), 0), "pct_rank": pct_rank(tv, have), "of": len(have), "as_of": float(bars[-1][0])}
    except Exception as e:  # noqa: BLE001 - display only
        logger.debug("liquidity badge: turnover for %s unavailable (%s)", sym, e)
    try:
        sess = out["session"]
        if sess:
            from feed import spread_recorder
            nxt = (date.fromisoformat(sess) + timedelta(days=1)).isoformat()
            out["spread"] = spread_stats(spread_recorder.history(sym, days=1, before_date=nxt), sess)
    except Exception as e:  # noqa: BLE001
        logger.debug("liquidity badge: spread for %s unavailable (%s)", sym, e)
    return out
