"""dhan_data/prev_close_guard.py - is the "previous close" a quote carries really the reference today's move is measured from?

Round 222 (6 Nov log): KMEW came in with open / high / low / ltp = 641.0 / 647.9 / 606.0 / 608.3 and a previous close of
3143.3, so every reader saw a "-80.6 % day" (real-trade rejected it as a volume-shock loser, but the same row would have
been a top "loser" / short candidate for anything that ranks by day move). The exchange feeds carry the RAW last close;
after a split / bonus / demerger the day's prices are on the new scale while that field is still on the old one.

This is a property of the DATA, not of one symbol, so the check is generic and needs no symbol list:

  A. The exchange's circuit band for today is set around the exchange's own (adjusted) reference price, symmetric:
         upper = base * (1 + b),  lower = base * (1 - b)   ->   base = (upper + lower) / 2
     When the band is present, plausible (lower < upper, ratio <= 3) and today's price sits inside it, the base is
     the reference. A previous close within PREV_CLOSE_GUARD_BAND_TOL_PCT of the base is confirmed and left alone;
     further away it is replaced by the base (status "corrected").
  B. When there is no usable band (feeds that carry none): a stock whose open / high / low / price ALL sit more than
     PREV_CLOSE_GUARD_GAP_PCT on the same side of the previous close never traded anywhere near it. With a 20 % band
     that is impossible for a real move, so the previous close is treated as unreliable (status "suspect": the value
     is withdrawn, never guessed). This needs the price plus at least two of open / high / low.

Never raises, never invents a number, and is idempotent (feeding a corrected row through again changes nothing).
A confirmed band (A) always wins over B. On an IPO listing day the band is set around the discovered opening price, not the issue
price, so the previous close is reported as "corrected" (the move is then measured from the opening reference); such a name has no
daily history and is not tradable by the candidate engines anyway.

SELF-CHECK (fail open): a corporate-action day touches a handful of names out of ~2,700. If more than PREV_CLOSE_GUARD_TRIP_PCT
(default 5 %) of the distinct symbols seen (at least PREV_CLOSE_GUARD_TRIP_MIN, default 100) come out corrected / suspect, the
assumption behind the check (the feed's band layout) is what is wrong, not the data: the guard then passes every row through
unchanged for PREV_CLOSE_GUARD_TRIP_S seconds (default 900), logs one ERROR and starts counting again afterwards.
PREV_CLOSE_GUARD_MODE=off turns the whole thing off.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("dhan-data.prev-close-guard")

_BAND_MAX_RATIO = 3.0       # upper / lower beyond this is a dummy / no-band range, not a usable circuit band
_BAND_PRICE_SLACK = 0.01    # price may sit this far outside the band (tick rounding, last-second band changes)


def _mode_off() -> bool:
    return (os.getenv("PREV_CLOSE_GUARD_MODE", "live") or "live").strip().lower() in ("off", "0", "false")


def _pct(name: str, default: float) -> float:
    try:
        v = float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default
    return v if math.isfinite(v) and v > 0 else default


_lock = threading.Lock()
_seen: Dict[str, bool] = {}        # distinct symbol -> was it flagged (bounded, reset after a trip)
_SEEN_MAX = 6000
_tripped_until = 0.0
_day = ""


def _note(symbol: str, flagged: bool) -> None:
    """Count one distinct symbol; trip (fail open) when the flagged share is implausibly high for corporate actions."""
    global _tripped_until, _day
    if not symbol:
        return
    with _lock:
        today = time.strftime("%Y%m%d")
        if today != _day:              # a new day starts a new count (flags of earlier days must not add up)
            _day = today
            _seen.clear()
        if symbol in _seen:
            _seen[symbol] = _seen[symbol] or flagged
        elif len(_seen) < _SEEN_MAX:
            _seen[symbol] = flagged
        n = len(_seen)
        if n >= max(1, int(_pct("PREV_CLOSE_GUARD_TRIP_MIN", 100.0))):
            bad = sum(1 for v in _seen.values() if v)
            if bad * 100.0 / n > _pct("PREV_CLOSE_GUARD_TRIP_PCT", 5.0):
                _tripped_until = time.monotonic() + _pct("PREV_CLOSE_GUARD_TRIP_S", 900.0)
                _seen.clear()
                logger.error("prev-close guard: %d of %d symbols flagged (> %.1f%%) - the feed's circuit-band / print layout does not "
                             "match the check's assumptions; passing every previous close through unchanged for %.0f s",
                             bad, n, _pct("PREV_CLOSE_GUARD_TRIP_PCT", 5.0), _pct("PREV_CLOSE_GUARD_TRIP_S", 900.0))


def tripped() -> bool:
    return time.monotonic() < _tripped_until


def reset_for_tests() -> None:
    global _tripped_until, _day
    with _lock:
        _seen.clear()
        _tripped_until = 0.0
        _day = ""


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def assess(price: Any, prev: Any, *, open_: Any = None, high: Any = None, low: Any = None,
           upper: Any = None, lower: Any = None, symbol: str = "") -> Dict[str, Any]:
    """-> {"prev": float|None, "raw": float|None, "status": ..., "reason": str|None}.

    status: "unchecked" (no price / no previous close / guard off), "ok" (confirmed, or nothing contradicts it),
    "corrected" (prev replaced by the circuit-band base), "suspect" (prev withdrawn, prev is None).
    `symbol` (optional) feeds the fail-open self-check; without it nothing is counted."""
    p, pv = _num(price), _num(prev)
    out: Dict[str, Any] = {"prev": pv, "raw": pv, "status": "unchecked", "reason": None}
    try:
        if _mode_off() or p is None or pv is None or tripped():
            return out
        res = _assess(p, pv, out, open_, high, low, upper, lower)
        _note(symbol, res["status"] in ("corrected", "suspect"))
        return res
    except Exception:  # noqa: BLE001 - a guard must never break a quote
        return {"prev": pv, "raw": pv, "status": "unchecked", "reason": None}


def _assess(p: float, pv: float, out: Dict[str, Any], open_: Any, high: Any, low: Any, upper: Any, lower: Any) -> Dict[str, Any]:
    try:
        out["status"] = "ok"
        up, lo = _num(upper), _num(lower)
        if up is not None and lo is not None and lo < up and up / lo <= _BAND_MAX_RATIO:
            base = (up + lo) / 2.0
            if lo * (1 - _BAND_PRICE_SLACK) <= p <= up * (1 + _BAND_PRICE_SLACK):
                tol = max(_pct("PREV_CLOSE_GUARD_BAND_TOL_PCT", 5.0) / 100.0, 0.10 / base)
                if abs(pv / base - 1.0) <= tol:
                    out["reason"] = "circuit_agrees"
                    return out
                out.update(prev=round(base, 2), status="corrected",
                           reason="previous close %.2f is %.0f%% away from the circuit-band base %.2f"
                                  % (pv, abs(pv / base - 1.0) * 100.0, base))
                return out
            # a band that does not even contain today's price cannot vouch for anything: fall through to B
        pts = [x for x in (_num(open_), _num(high), _num(low)) if x is not None]
        if len(pts) >= 2:
            pts.append(p)
            g = _pct("PREV_CLOSE_GUARD_GAP_PCT", 25.0) / 100.0
            if max(pts) < pv * (1.0 - g) or min(pts) > pv * (1.0 + g):
                out.update(prev=None, status="suspect",
                           reason="no print today (%.2f - %.2f) within %.0f%% of previous close %.2f"
                                  % (min(pts), max(pts), g * 100.0, pv))
        return out
    except Exception:  # noqa: BLE001 - a guard must never break a quote
        return {"prev": pv, "raw": pv, "status": "unchecked", "reason": None}


def day_change_pct(price: Any, prev: Any) -> Optional[float]:
    p, pv = _num(price), _num(prev)
    if p is None or pv is None:
        return None
    return round((p - pv) / pv * 100.0, 2)
