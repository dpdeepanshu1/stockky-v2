"""orders/circuit_gate.py - exchange circuit-band check before a scalp BUY (group 329, 2026-10-12).

WHY: a stock at (or a hair under) its UPPER circuit cannot be bought at a fair price and has no room left for the scalp target
(MIN_TARGET_PCT, default 3 %); a stock close to its LOWER circuit is heading for a lock where the stop-loss or the 15:00 square-off
cannot execute. Dhan's circuit limits are in the market-data /quote row the depth gate already reads, but nothing used them: a
circuit-limit rejection was only discovered AFTER the broker bounced the order (orders/entry.py, orders/eod_squareoff.py).

RULE (config.CIRCUIT_UPPER_BUFFER_PCT default 1.0, CIRCUIT_LOWER_BUFFER_PCT default 2.0, % of price, 0 disables that side):
  ltp >= upper * (1 - upper_buffer/100)  -> skip  (CIRCUIT_UPPER)
  ltp <= lower * (1 + lower_buffer/100)  -> skip  (CIRCUIT_LOWER)

The limits are read through depth_gate._fresh_quote(): the same 5 s cached /quote body, with the same age check, so this adds no
extra network call to the entry path. FAIL OPEN: no body, a quote that is too old, a missing / zero / NaN limit, upper <= lower, or a
price more than 1 % outside the stated band (stale limits) all mean "no opinion". Never raises. Off with CIRCUIT_GATE_ENABLED=0.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Optional, Tuple

import config
from orders import depth_gate

logger = logging.getLogger("position-stocks-circuit-gate")

_BAND_SLACK = 0.01


def _pos(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def decide(price, upper, lower) -> Optional[str]:
    """Pure decision from numbers: a "CIRCUIT_UPPER:..." / "CIRCUIT_LOWER:..." reason or None. Never raises."""
    try:
        p, up, lo = _pos(price), _pos(upper), _pos(lower)
        if p is None or (up is None and lo is None):
            return None
        if up is not None and lo is not None and up <= lo:
            return None
        if up is not None and p > up * (1 + _BAND_SLACK):
            return None
        if lo is not None and p < lo * (1 - _BAND_SLACK):
            return None
        up_buf = max(0.0, float(config.CIRCUIT_UPPER_BUFFER_PCT or 0))
        lo_buf = max(0.0, float(config.CIRCUIT_LOWER_BUFFER_PCT or 0))
        if up is not None and up_buf > 0 and p >= up * (1 - up_buf / 100.0):
            return (f"CIRCUIT_UPPER:price {p:.2f} within {up_buf:.1f}% of the upper circuit {up:.2f} "
                    "(locked or no room for the target)")
        if lo is not None and lo_buf > 0 and p <= lo * (1 + lo_buf / 100.0):
            return (f"CIRCUIT_LOWER:price {p:.2f} within {lo_buf:.1f}% of the lower circuit {lo:.2f} "
                    "(may lock down; stop or square-off might not execute)")
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("circuit gate decide failed (allowing): %s", e)
        return None


def reject_reason(symbol: str, ltp) -> Optional[str]:
    """A skip reason or None when the entry may go ahead. Reads the cached /quote row. Never raises."""
    try:
        if not config.CIRCUIT_GATE_ENABLED:
            return None
        if float(config.CIRCUIT_UPPER_BUFFER_PCT or 0) <= 0 and float(config.CIRCUIT_LOWER_BUFFER_PCT or 0) <= 0:
            return None
        q = depth_gate._fresh_quote(symbol)
        if not q:
            return None
        return decide(ltp, q.get("upper_circuit"), q.get("lower_circuit"))
    except Exception as e:  # noqa: BLE001
        logger.debug("circuit gate %s failed (allowing): %s", symbol, e)
        return None


# ── E258: a SELL stop must sit INSIDE the exchange band ───────────────────────────────────────────────────────────────────────
# 29 Oct 2026, ITC: the overnight stop (entry x 0.96 = 256.15) was rejected by Dhan RMS "Rate Not Within Ckt Limit 257.80 To
# 273.70" - ITC's band was only +-3 %, so a 4 % stop lay BELOW the lower circuit - and the carry fell back to a square-off.
_BAND_RE = re.compile(r"(?:ckt limit|circuit limit|within)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*to\s*(\d+(?:\.\d+)?)", re.I)


def parse_band(message) -> Optional[Tuple[float, float]]:
    """(lower, upper) from Dhan's rejection text ('...Rate Not Within Ckt Limit 257.80 To 273.70' / 'Place order within 78.95 to 113.60'),
    else None. Never raises."""
    try:
        m = _BAND_RE.search(str(message or ""))
        if not m:
            return None
        lo, up = float(m.group(1)), float(m.group(2))
        return (lo, up) if 0 < lo < up else None
    except Exception:  # noqa: BLE001
        return None


def clamp_sell_stop(trigger, lower, tick, ltp=None) -> Optional[float]:
    """A SELL stop trigger that the exchange will take: `trigger` unchanged when it is already >= lower + one tick (or the lower
    circuit is unknown), else lower + one tick. None when even that is not below the live price `ltp` (the stock sits on its lower
    circuit: no stop can rest below the market). Pure; never raises."""
    try:
        trig = float(trigger)
        lo = _pos(lower)
        if lo is None:
            return trig
        t = _pos(tick) or 0.05
        floor = round(lo + t, 2)
        if trig >= floor:
            return trig
        px = _pos(ltp)
        if px is not None and floor >= px:
            return None
        return floor
    except Exception:  # noqa: BLE001
        return None


def band_for(symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(lower, upper, price) from the cached /quote row, each None when unknown. Never raises."""
    try:
        q = depth_gate._fresh_quote(symbol) or {}
        return _pos(q.get("lower_circuit")), _pos(q.get("upper_circuit")), _pos(q.get("price") or q.get("ltp"))
    except Exception as e:  # noqa: BLE001
        logger.debug("circuit band %s unavailable: %s", symbol, e)
        return None, None, None
