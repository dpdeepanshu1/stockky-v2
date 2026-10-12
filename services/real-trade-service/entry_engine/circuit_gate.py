"""entry_engine/circuit_gate.py - exchange circuit-band check before a BUY (group 329, 2026-10-12).

WHY: a stock sitting at (or a hair under) its UPPER circuit cannot be bought at a fair price - the order queues behind a wall of
buyers, fills late or never, and there is no upside room left. A stock sitting close to its LOWER circuit is falling toward a
lock where a SELL (stop-loss, EOD square-off) cannot be executed at all. Neither case showed up anywhere in the entry gates:
Dhan's circuit limits were already in the /quote row but nothing read them, and a circuit-limit rejection was only noticed
AFTER the broker bounced the order.

RULE (config.CIRCUIT_UPPER_BUFFER_PCT default 1.0, CIRCUIT_LOWER_BUFFER_PCT default 2.0, both % of price, 0 disables that side):
  price >= upper * (1 - upper_buffer/100)  -> reject  (CIRCUIT_UPPER)
  price <= lower * (1 + lower_buffer/100)  -> reject  (CIRCUIT_LOWER)

FAIL OPEN: no limits on the tick, a limit that is zero / negative / NaN, upper <= lower, or a price that sits outside the band
by more than 1 % (the limits are then stale or wrong, not the price) all mean "no opinion" - the entry is NOT blocked by this
module. Pure function; never raises. Off with CIRCUIT_GATE_ENABLED=false.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import config

logger = logging.getLogger("real-trade-circuit-gate")

_BAND_SLACK = 0.01     # price may sit this fraction outside the stated band before the limits are called unusable


def _pos(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def reject_reason(price, upper, lower) -> Optional[str]:
    """A "CIRCUIT_UPPER:..." / "CIRCUIT_LOWER:..." reason, or None when the entry may go ahead. Never raises."""
    try:
        if not config.CIRCUIT_GATE_ENABLED:
            return None
        p, up, lo = _pos(price), _pos(upper), _pos(lower)
        if p is None or (up is None and lo is None):
            return None
        if up is not None and lo is not None and up <= lo:
            return None                                   # nonsense band
        if up is not None and p > up * (1 + _BAND_SLACK):
            return None                                   # price far above the "upper limit": the limit is stale
        if lo is not None and p < lo * (1 - _BAND_SLACK):
            return None
        up_buf = max(0.0, float(config.CIRCUIT_UPPER_BUFFER_PCT or 0))
        lo_buf = max(0.0, float(config.CIRCUIT_LOWER_BUFFER_PCT or 0))
        if up is not None and up_buf > 0 and p >= up * (1 - up_buf / 100.0):
            return (f"CIRCUIT_UPPER: price {p:.2f} is within {up_buf:.1f}% of the upper circuit {up:.2f} "
                    "(locked or no room left; an order may not fill)")
        if lo is not None and lo_buf > 0 and p <= lo * (1 + lo_buf / 100.0):
            return (f"CIRCUIT_LOWER: price {p:.2f} is within {lo_buf:.1f}% of the lower circuit {lo:.2f} "
                    "(may lock down; a stop-loss or square-off might not execute)")
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("circuit gate failed (allowing): %s", e)
        return None


def reject_reason_tick(tick) -> Optional[str]:
    """Same check on a market_feed.Tick (reads price / upper_circuit / lower_circuit). Never raises."""
    try:
        return reject_reason(getattr(tick, "price", None), getattr(tick, "upper_circuit", None),
                             getattr(tick, "lower_circuit", None))
    except Exception:  # noqa: BLE001
        return None
