"""exit_engine/excursion.py - record each open position's highest / lowest price since entry (group 397).

Why: to judge a stop (was it too tight? would a tighter one have helped?) you need the peak and trough every trade actually reached.
Position-stocks has kept this since 2026-10-02 (orders/excursion.py); real-trade had no equivalent. This keeps
TradePosition.max_price_seen / min_price_seen current while a position is open, so max favourable / adverse excursion can be read
off /positions/{mode}/records.

Called once per open position per exit cycle from exit_engine.exit.evaluate_mode, before any exit check, so the price that triggers
a stop is itself recorded. Evidence only: it never changes a decision, never raises, writes only when a new extreme is reached.
Limits: sampled once per exit cycle (a spike between two cycles is missed); a tick older than EXCURSION_MAX_TICK_AGE_S (when its age
is known) is ignored; a corporate-action price jump is NOT filtered, so read a trade with an absurd gain / drawdown by hand.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger("real-trade-excursion")


def _max_tick_age_s() -> float:
    try:
        v = float((os.environ.get("EXCURSION_MAX_TICK_AGE_S") or "").strip() or 120.0)
    except ValueError:
        return 120.0
    return v if v >= 0 else 120.0


def _valid_price(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def next_extremes(entry, hi, lo, price) -> Optional[Tuple[float, float]]:
    """(new_high, new_low) when `price` widens the stored range (entry counts as a price the trade has been at), else None. Pure."""
    p, e = _valid_price(price), _valid_price(entry)
    if p is None or e is None:
        return None
    cur_hi = max(e, _valid_price(hi) or e)
    cur_lo = min(e, _valid_price(lo) or e)
    new_hi, new_lo = max(cur_hi, p), min(cur_lo, p)
    if hi is not None and lo is not None and new_hi == _valid_price(hi) and new_lo == _valid_price(lo):
        return None
    return new_hi, new_lo


def _tick_too_old(tick, now: datetime) -> bool:
    try:
        if not getattr(tick, "age_known", True):
            return False                       # age unknown: the entry engine's own gates decide; excursion accepts the price
        as_of = getattr(tick, "as_of", None)
        if as_of is None:
            return False
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        return (now - as_of).total_seconds() > _max_tick_age_s()
    except Exception:  # noqa: BLE001
        return False


def record(db, position, tick, now: Optional[datetime] = None) -> bool:
    """Widen position.max_price_seen / min_price_seen with tick.price. True when a value was saved. Never raises."""
    try:
        now = now or datetime.now(timezone.utc)
        if tick is None or _tick_too_old(tick, now):
            return False
        nxt = next_extremes(position.avg_entry_price, getattr(position, "max_price_seen", None), getattr(position, "min_price_seen", None), tick.price)
        if nxt is None:
            return False
        position.max_price_seen, position.min_price_seen = nxt
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.debug("excursion: %s skipped: %s", getattr(position, "symbol", "?"), e)
        return False


def excursion_pcts(entry, hi, lo) -> dict:
    """Max gain / max drawdown % vs entry for API output (None when unknown)."""
    e, h, l = _valid_price(entry), _valid_price(hi), _valid_price(lo)
    if e is None or h is None or l is None:
        return {"max_gain_pct": None, "max_drawdown_pct": None}
    return {"max_gain_pct": round((h - e) / e * 100.0, 3), "max_drawdown_pct": round((l - e) / e * 100.0, 3)}
