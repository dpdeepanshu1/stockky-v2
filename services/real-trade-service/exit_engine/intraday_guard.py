"""intraday_guard.py - two pure exit rules for a LONG bought today (E286 round 214; real-trade only). Stdlib only, no I/O, no config: the caller passes every number.

1 failed_entry   the trade never worked and is now losing a fraction of its stop distance -> get out early instead of waiting for a 4-5 % stop.
                 "Never worked" = the best price since entry gained no more than `follow_frac` x R (R = initial stop distance); "losing" = the price is at
                 least `loss_frac` x R below entry; and the trade is at least `min_age_min` minutes old (so the opening noise after the buy is not an exit).
                 (COFORGE / CARERATING 4 Nov 2026: bought, never went above entry, then sat toward the 4-5 % stop.)
2 profit_lock    a winner's stop trails the BEST price by max(`min_gap_pct`, `range_mult` x the stock's own average 1-minute range %), capped at `max_gap_pct`,
                 once the best gain is at least `min_gain_pct`. Never below entry + `min_lock_pct`, never loosened, at least two ticks better than the old stop,
                 never at / above the live price. (MRPL 4 Nov 2026: +3.3 % but the ATR trail sat about 7 % below the price, so the stop stayed at breakeven.)

Missing or bad data = no action (the existing stop, ATR trail and time rules are untouched). Never raises.
"""
from __future__ import annotations

import math
from typing import Optional


def _num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _floor(x: float, tick: float) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 4)


def failed_entry(entry, stop, ltp, best, age_min, initial_dist=None, *, min_age_min: float = 20.0, loss_frac: float = 0.35,
                 follow_frac: float = 0.15) -> dict:
    """{"action": "EXIT" | "HOLD", "reason": str}. EXIT only when the trade never worked, is old enough and has lost `loss_frac` of R."""
    try:
        e, s, p, b = _num(entry), _num(stop), _num(ltp), _num(best)
        if e is None or p is None or b is None:
            return {"action": "HOLD", "reason": "no price data"}
        r = _num(initial_dist)
        if r is None and s is not None and s < e:
            r = e - s
        if r is None:
            return {"action": "HOLD", "reason": "no stop distance"}
        try:
            age = float(age_min)
        except (TypeError, ValueError):
            return {"action": "HOLD", "reason": "no age"}
        if not math.isfinite(age) or age < float(min_age_min):
            return {"action": "HOLD", "reason": "too young"}
        if b - e > float(follow_frac) * r:
            return {"action": "HOLD", "reason": "it did follow through"}
        loss = e - p
        if loss < float(loss_frac) * r:
            return {"action": "HOLD", "reason": "loss below the early-exit fraction"}
        return {"action": "EXIT", "reason": (f"failed entry: best ₹{b:.2f} never beat entry ₹{e:.2f} by more than {follow_frac:g}R (R=₹{r:.2f}), "
                                            f"now ₹{p:.2f} = -{loss / r:.2f}R after {age:.0f} min (limit -{loss_frac:g}R)")}
    except Exception:  # noqa: BLE001 - advisory; never raise
        return {"action": "HOLD", "reason": "error"}


def profit_lock(entry, best, ltp, current_stop, avg_range_pct, tick, *, range_mult: float = 3.0, min_gap_pct: float = 0.35,
                max_gap_pct: float = 1.5, fallback_gap_pct: float = 0.6, min_gain_pct: float = 0.8, min_lock_pct: float = 0.1) -> Optional[dict]:
    """{"new_stop", "gap_pct", "reason"} when the stop can be raised to lock gain, else None."""
    try:
        e, b, p, t = _num(entry), _num(best), _num(ltp), _num(tick)
        if e is None or b is None or p is None or t is None:
            return None
        b = max(b, p)
        gain_pct = (b / e - 1.0) * 100.0
        if gain_pct < float(min_gain_pct):
            return None
        rng = _num(avg_range_pct)
        gap = float(fallback_gap_pct) if rng is None else max(float(min_gap_pct), float(range_mult) * rng)
        gap = min(gap, float(max_gap_pct))
        cand = _floor(b * (1.0 - gap / 100.0), t)
        cand = min(cand, _floor(p - t, t))                     # never at / above the live price
        if cand < e * (1.0 + float(min_lock_pct) / 100.0):
            return None
        cs = _num(current_stop)
        if cs is not None and cand < cs + 2 * t - 1e-9:
            return None                                        # never loosen; not worth a move under two ticks
        return {"new_stop": cand, "gap_pct": round(gap, 3),
                "reason": (f"profit lock: best ₹{b:.2f} (+{gain_pct:.2f}%), trail gap {gap:.2f}% "
                           f"({'1-min range ' + format(rng, '.3f') + '% x ' + format(float(range_mult), 'g') if rng is not None else 'no range data, fallback'}) "
                           f"-> stop ₹{cand:.2f} (was ₹{cs if cs is not None else 'none'})")}
    except Exception:  # noqa: BLE001
        return None
